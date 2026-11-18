"""Codex Security plugin based on https://github.com/openai/codex-security

Wraps the official CLI (`cs` / `codex-security`, or `npx @openai/codex-security`):
status check, repository scans, saved-scan lookup and raw CLI passthrough.

Requires Node.js 22.13.x, 24.x or 26.x and Python 3.10+.
Sign in once with `cs login`, or set OPENAI_API_KEY / CODEX_API_KEY for CI.
"""
import os
import re
import shlex
import shutil
import subprocess

NAME = "codex_security"
PACKAGE = "@openai/codex-security"
MAX_TIMEOUT = 3600


def _which_cli():
    for name in ("cs", "codex-security"):
        found = shutil.which(name)
        if found:
            return [found], name
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", PACKAGE], "npx"
    return None, ""


def _wrap(cmd):
    first = cmd[0].lower()
    if os.name == "nt" and first.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c"] + cmd
    return cmd


def _safe_text(value):
    text = str(value or "")
    return not any(ch in text for ch in "&|<>^%\n\r")


def _as_int(value, default, lo, hi):
    try:
        return max(lo, min(int(float(value)), hi))
    except (TypeError, ValueError):
        return default


def _run(cfg, extra, timeout):
    base, launcher = _which_cli()
    if not base:
        return {"ok": False, "package": PACKAGE,
                "error": ("CLI Codex Security не найден. Нужен Node.js 22.13+ (или 24.x/26.x), "
                          "затем `npm i -g @openai/codex-security` и `cs login`.")}
    cmd = _wrap(base + list(extra))
    workdir = str((cfg or {}).get("workspace") or "") if isinstance(cfg, dict) else ""
    cwd = workdir if workdir and os.path.isdir(workdir) else os.getcwd()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=env, shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "команда не уложилась в %d с и была остановлена" % timeout,
                "launcher": launcher, "command": cmd, "cwd": cwd}
    except OSError as exc:
        return {"ok": False, "error": "не удалось запустить CLI: %s" % exc,
                "launcher": launcher, "command": cmd}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "launcher": launcher,
            "command": cmd, "cwd": cwd,
            "stdout": (proc.stdout or "")[-12000:], "stderr": (proc.stderr or "")[-4000:]}


def codex_security_status(cfg, args):
    node = shutil.which("node")
    node_ver = ""
    if node:
        try:
            probe = subprocess.run(_wrap([node, "--version"]), capture_output=True, text=True,
                                   timeout=10, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            node_ver = ((probe.stdout or "") + (probe.stderr or "")).strip()
        except Exception:
            node_ver = ""
    base, launcher = _which_cli()
    auth = ""
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        if os.environ.get(key):
            auth = key
            break
    if not node:
        hint = "Установи Node.js 22.13.x, 24.x или 26.x с nodejs.org, потом повтори."
    elif not base:
        hint = "npm i -g @openai/codex-security, затем cs login"
    elif not auth:
        hint = "Авторизуйся: cs login (или задай OPENAI_API_KEY / CODEX_API_KEY)"
    else:
        hint = "Можно сканировать: codex_security_scan"
    return {"ok": True, "installed": bool(base), "launcher": launcher or "нет",
            "node": node_ver or ("нет" if not node else "есть"), "auth": auth or "не задан",
            "package": PACKAGE, "hint": hint}


def codex_security_scan(cfg, args):
    target = str(args.get("path") or ".").strip() or "."
    extra = ["scan", target]

    paths = args.get("paths") or ""
    if isinstance(paths, list):
        paths = ",".join(str(p) for p in paths)
    for part in str(paths).split(","):
        part = part.strip()
        if not part:
            continue
        if not _safe_text(part):
            return {"ok": False, "error": "в paths запрещены символы & | < > ^ %"}
        extra += ["--path", part]

    for part in str(args.get("exclude") or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not _safe_text(part):
            return {"ok": False, "error": "в exclude запрещены символы & | < > ^ %"}
        extra += ["--exclude-path", part]

    diff = str(args.get("diff") or "").strip()
    if diff and diff.lower() not in ("false", "0", "no", "none"):
        if not _safe_text(diff):
            return {"ok": False, "error": "в diff запрещены символы & | < > ^ %"}
        extra += ["--diff", diff]

    mode = str(args.get("mode") or "").strip().lower()
    if mode in ("deep", "standard"):
        extra += ["--mode", mode]

    max_time = _as_int(args.get("max_time"), 0, 0, 240)
    if max_time:
        extra += ["--max-time", str(max_time)]

    if not _safe_text(target):
        return {"ok": False, "error": "в path запрещены символы & | < > ^ %"}

    timeout = _as_int(args.get("timeout"), 900, 30, MAX_TIMEOUT)
    res = _run(cfg, extra, timeout)
    text = res.get("stdout") or ""
    match = re.search(r"(?im)^\s*report\b[:\s]+(.+)$", text)
    if match:
        res["report"] = match.group(1).strip()
    res["max_time_min"] = max_time or None
    return res


def codex_security_scans(cfg, args):
    timeout = _as_int(args.get("timeout"), 120, 10, 600)
    return _run(cfg, ["scans", "show"], timeout)


def codex_security_cli(cfg, args):
    raw = args.get("args") or ""
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    raw = str(raw).strip()
    if not raw:
        return {"ok": False, "error": "укажи args, например '--help' или 'scans show'"}
    if not _safe_text(raw):
        return {"ok": False, "error": "в args запрещены символы & | < > ^ %"}
    try:
        extra = shlex.split(raw, posix=True)
    except ValueError as exc:
        return {"ok": False, "error": "не удалось разобрать args: %s" % exc}
    timeout = _as_int(args.get("timeout"), 300, 10, MAX_TIMEOUT)
    return _run(cfg, extra, timeout)


TOOLS = [
    {
        "name": "codex_security_status",
        "desc": "Codex Security: Node, CLI, авторизация и подсказка, что установить.",
        "params": {},
        "required": [],
        "fn": codex_security_status,
    },
    {
        "name": "codex_security_scan",
        "desc": "Скан репозитория Codex Security; deep — повторный глубокий проход.",
        "params": {
            "path": ("string", "что сканировать, по умолчанию ."),
            "paths": ("string", "только эти пути через запятую"),
            "exclude": ("string", "исключить пути через запятую"),
            "diff": ("string", "базовая ревизия для diff, напр. origin/main"),
            "mode": ("string", "standard|deep"),
            "max_time": ("number", "лимит минут на исследование"),
            "timeout": ("number", "жёсткий таймаут процесса, секунды"),
        },
        "required": [],
        "fn": codex_security_scan,
    },
    {
        "name": "codex_security_scans",
        "desc": "Показать последний сохранённый скан и его отчёт (cs scans show).",
        "params": {"timeout": ("number", "секунды")},
        "required": [],
        "fn": codex_security_scans,
    },
    {
        "name": "codex_security_cli",
        "desc": "Прямой вызов cs: --help, policy ., export --scan ID --artifact threat-model.",
        "params": {
            "args": ("string", "аргументы CLI"),
            "timeout": ("number", "секунды"),
        },
        "required": ["args"],
        "fn": codex_security_cli,
    },
]
