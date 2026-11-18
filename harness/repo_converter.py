"""GitHub Repository to LocalHarness Plugin Converter.

Turns any GitHub repository (AI agent, MCP server, CLI utility, library) into a
native LocalHarness plugin that keeps working after the conversion:

- info: live repository metadata (stars, language, license, topics)
- files: file tree via the GitHub API
- read: raw file content (path traversal guarded, size capped)
- cli: runs the repository's own CLI when it declares one (npm bin,
  console_scripts, cargo/go manifests); falls back to `npx -y <package>`

Python functions are still discovered with AST and reported as metadata, but the
generated plugin never fakes an execution result.
"""
import ast
import json
import os
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from . import dnsfix

    _orig_gai = socket.getaddrinfo

    def _safe_gai(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _orig_gai(host, port, family, type, proto, flags)
        except Exception:
            try:
                ip = dnsfix.resolve(host)
                return _orig_gai(ip, port, family, type, proto, flags)
            except Exception:
                raise

    socket.getaddrinfo = _safe_gai
except Exception:
    pass

from . import virustotal


def parse_repo_target(target: str) -> Tuple[str, str]:
    """Extracts (owner, repo) from a GitHub URL or 'owner/repo' string."""
    target = (target or "").strip()
    match = re.search(r"(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", target)
    if not match:
        raise ValueError(f"Некорректная ссылка на репозиторий: '{target}'. Нужен формат 'owner/repo' или 'https://github.com/owner/repo'")
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def fetch_github_json(path: str, token: Optional[str] = None) -> Any:
    """Fetches a JSON endpoint from GitHub REST API."""
    url = f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "LocalHarness-RepoConverter/2.0",
    }
    if not token:
        from .config import load as load_cfg
        token = load_cfg().get("github_token") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token.strip()}"

    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code == 404:
        raise ValueError(f"Репозиторий или ресурс не найден: {path}")
    if resp.status_code == 403:
        raise ValueError(f"Превышен лимит запросов GitHub API (403): {resp.text[:120]}")
    resp.raise_for_status()
    return resp.json()


def fetch_raw_file(owner: str, repo: str, branch: str, file_path: str) -> str:
    """Fetches the raw text of a file from GitHub."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path.lstrip('/')}"
    headers = {"User-Agent": "LocalHarness-RepoConverter/2.0"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def analyze_python_ast(code: str) -> List[Dict[str, Any]]:
    """Inspects Python code with AST and extracts candidates for plugin tools."""
    tools = []
    try:
        tree = ast.parse(code)
    except Exception:
        return []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if name.startswith("_"):
                continue

            doc = ast.get_docstring(node) or f"Инструмент {name}"
            params: Dict[str, Any] = {}
            required: List[str] = []

            args = node.args.args
            defaults = node.args.defaults
            num_defaults = len(defaults)
            num_args = len(args)

            for idx, arg in enumerate(args):
                arg_name = arg.arg
                if arg_name in ("self", "cls"):
                    continue

                is_required = (idx < (num_args - num_defaults))
                if is_required:
                    required.append(arg_name)

                param_type = "string"
                if arg.annotation:
                    annot_str = ast.unparse(arg.annotation).lower() if hasattr(ast, "unparse") else ""
                    if "int" in annot_str or "float" in annot_str or "number" in annot_str:
                        param_type = "number"
                    elif "bool" in annot_str:
                        param_type = "bool"
                    elif "dict" in annot_str or "list" in annot_str:
                        param_type = "object"

                params[arg_name] = (param_type, f"параметр {arg_name}")

            tools.append({
                "name": name,
                "desc": doc.strip().split("\n")[0][:150],
                "params": params,
                "required": required,
                "raw_func": name,
            })

    return tools


# ---------------------------------------------------------------- manifests

_NPM_HINT = "нужен Node.js 22.13+ и npm i -g {pkg}"


def _readme_summary(owner: str, repo: str, branch: str, paths: List[str]) -> str:
    names = [p for p in paths if re.match(r"(?i)^readme(\.(md|rst|txt|markdown))?$", p)]
    if not names:
        return ""
    try:
        text = fetch_raw_file(owner, repo, branch, names[0])
    except Exception:
        return ""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "!", "[!", "<", "---", "===", "```", "|")):
            continue
        lines.append(s)
        if sum(len(x) for x in lines) > 500:
            break
    summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(lines))
    return re.sub(r"\s+", " ", summary)[:600].strip()


_NOISE_DIRS = ("node_modules/", "fixtures/", "examples/", "evals/", "tests/", "test/")


def _manifest_candidates(paths: List[str], filename: str, max_depth: int = 4) -> List[str]:
    """Shallow-first manifest paths, skipping vendored and fixture trees."""
    found = []
    for path in paths:
        if os.path.basename(path).lower() != filename:
            continue
        low = path.lower()
        if any(part in low for part in _NOISE_DIRS):
            continue
        depth = path.count("/")
        if depth > max_depth:
            continue
        found.append((depth, path))
    found.sort()
    return [p for _, p in found]


def _detect_manifests(owner: str, repo: str, branch: str, paths: List[str]) -> Dict[str, Any]:
    """Reads package.json / pyproject / setup.py / Cargo.toml / go.mod.

    Monorepos often keep a private root package.json with no name or bin, so
    nested manifests are scanned shallow-first and the first one declaring a
    package name (preferably with a bin/console script) wins.
    """
    info: Dict[str, Any] = {
        "kind": "", "package": "", "description": "", "bins": [],
        "install_hint": "",
    }

    def _bins_from(value) -> List[str]:
        if isinstance(value, str):
            return [os.path.basename(value).replace(".js", "")]
        if isinstance(value, dict):
            return [str(k) for k in value.keys()]
        if isinstance(value, list):
            return [os.path.basename(str(v)) for v in value]
        return []

    best_bin = None
    plain = None
    for path in _manifest_candidates(paths, "package.json")[:6]:
        try:
            data = json.loads(fetch_raw_file(owner, repo, branch, path))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("private") and not data.get("name"):
            continue
        name = str(data.get("name") or "")
        record = (name, str(data.get("description") or ""), _bins_from(data.get("bin")))
        if record[2] and best_bin is None:
            best_bin = record
        elif name and plain is None:
            plain = record
        if best_bin:
            break
    chosen = best_bin or plain
    if chosen:
        info["kind"] = "npm"
        info["package"], info["description"], info["bins"] = chosen

    if not info["kind"]:
        for candidate in ("pyproject.toml", "setup.py", "setup.cfg"):
            for path in _manifest_candidates(paths, candidate, max_depth=3)[:3]:
                try:
                    src = fetch_raw_file(owner, repo, branch, path)
                except Exception:
                    continue
                if candidate == "pyproject.toml":
                    m = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', src) or \
                        re.search(r"(?m)^\s*name\s*=\s*'([^']+)'", src)
                    pkg = m.group(1) if m else ""
                    scripts = re.search(r"(?ms)^\[(?:project|tool\.poetry)\.scripts\]\s*(.*?)(?:^\[|\Z)", src)
                    bins = re.findall(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=", scripts.group(1)) if scripts else []
                elif candidate == "setup.py":
                    m = re.search(r"""name\s*=\s*['"]([^'"]+)['"]""", src)
                    pkg = m.group(1) if m else ""
                    bins = (re.findall(r"""['"]([A-Za-z0-9_.-]+)\s*=\s*[A-Za-z0-9_.:]+['"]""", src)
                            if "console_scripts" in src else [])
                else:
                    m = re.search(r"(?m)^name\s*=\s*(\S+)", src)
                    pkg = m.group(1) if m else ""
                    bins = (re.findall(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=\s*[A-Za-z0-9_.:]+", src)
                            if "console_scripts" in src else [])
                if pkg or bins:
                    info["kind"] = "python"
                    info["package"], info["bins"] = pkg, bins
                    break
            if info["kind"]:
                break

    if not info["kind"]:
        cargo = _manifest_candidates(paths, "cargo.toml", max_depth=3)
        if cargo:
            info["kind"] = "cargo"
            try:
                src = fetch_raw_file(owner, repo, branch, cargo[0])
                m = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', src)
                if m:
                    info["package"] = m.group(1)
                info["bins"] = re.findall(r'(?m)^\s*name\s*=\s*"([^"]+)"', src)[:4]
            except Exception:
                pass

    if not info["kind"]:
        gomod = _manifest_candidates(paths, "go.mod", max_depth=2)
        if gomod:
            info["kind"] = "go"
            try:
                m = re.search(r"(?m)^module\s+(\S+)", fetch_raw_file(owner, repo, branch, gomod[0]))
                if m:
                    info["package"] = m.group(1)
            except Exception:
                pass

    bins: List[str] = []
    for name in info["bins"]:
        name = str(name).strip()
        if name and name not in bins:
            bins.append(name)
    if info["package"]:
        short = info["package"].split("/")[-1]
        if short and short not in bins:
            bins.append(short)
    info["bins"] = bins

    kind = info["kind"]
    pkg = info["package"]
    if kind == "npm" and pkg:
        info["install_hint"] = _NPM_HINT.format(pkg=pkg)
    elif kind == "python" and pkg:
        info["install_hint"] = f"pip install {pkg}"
    elif kind == "cargo":
        info["install_hint"] = "cargo install --path ."
    elif kind == "go":
        info["install_hint"] = "go install " + (pkg or "./...")
    return info


def _repo_language(meta: Dict[str, Any], paths: List[str]) -> str:
    lang = meta.get("language")
    if lang:
        return str(lang)
    ext = {"py": "Python", "js": "JavaScript", "ts": "TypeScript", "go": "Go",
           "rs": "Rust", "java": "Java", "rb": "Ruby", "php": "PHP", "cs": "C#"}
    counts: Dict[str, int] = {}
    for path in paths:
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        name = ext.get(suffix)
        if name:
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else "unknown"


# ---------------------------------------------------------------- template

_PLUGIN_TEMPLATE = '''"""LocalHarness plugin converted from https://github.com/{{REPO}}

{{DESCRIPTION}}
Mode: {{MODE}} | package: {{PACKAGE}} | CLI: {{BINS}}
Generated by the built-in repo converter; talks to GitHub at call time.
"""
import json
import os
import shlex
import shutil
import subprocess
import urllib.request

NAME = "{{NAME}}"
REPO = "{{REPO}}"
BRANCH = "{{BRANCH}}"
DESCRIPTION = {{DESC_REPR}}
PACKAGE = "{{PACKAGE}}"
BIN = {{BINS_REPR}}
INSTALL_HINT = {{HINT_REPR}}
MAX_CHARS = 200000
USER_AGENT = "LocalHarness-RepoPlugin/2.0"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "token " + token.strip())
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def _api(path):
    return json.loads(_get("https://api.github.com/" + path.lstrip("/")).decode("utf-8", "replace"))


def _repo_info(cfg, args):
    try:
        meta = _api("repos/" + REPO)
    except Exception as exc:
        return {"ok": False, "error": "GitHub недоступен: %s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "repo": REPO, "description": meta.get("description") or DESCRIPTION,
            "stars": meta.get("stargazers_count"), "language": meta.get("language"),
            "topics": meta.get("topics") or [], "license": (meta.get("license") or {}).get("spdx_id"),
            "homepage": meta.get("homepage"), "updated": meta.get("updated_at"),
            "branch": BRANCH, "package": PACKAGE, "cli": BIN, "install": INSTALL_HINT}


def _repo_files(cfg, args):
    try:
        limit = max(1, min(int(float(args.get("limit") or 200)), 2000))
    except (TypeError, ValueError):
        limit = 200
    try:
        data = _api("repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH))
    except Exception as exc:
        return {"ok": False, "error": "не удалось получить список файлов: %s: %s" % (type(exc).__name__, exc)}
    items = [i for i in (data.get("tree") or []) if i.get("path")]
    shown = items[:limit]
    return {"ok": True, "repo": REPO, "branch": BRANCH, "total": len(items), "shown": len(shown),
            "truncated": len(items) > len(shown),
            "entries": [{"path": i["path"], "type": i.get("type"), "size": i.get("size")} for i in shown]}


def _safe_path(path):
    p = str(path or "").strip().replace("\\\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x]
    if not parts or ".." in parts:
        raise ValueError("недопустимый путь: %r" % path)
    return "/".join(parts)


def _repo_read(cfg, args):
    try:
        path = _safe_path(args.get("path"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        limit = max(500, min(int(float(args.get("max_chars") or 20000)), MAX_CHARS))
    except (TypeError, ValueError):
        limit = 20000
    try:
        raw = _get("https://raw.githubusercontent.com/%s/%s/%s" % (REPO, BRANCH, path))
    except Exception as exc:
        return {"ok": False, "error": "не удалось прочитать %s: %s: %s" % (path, type(exc).__name__, exc)}
    return {"ok": True, "path": path, "size": len(raw), "truncated": len(raw) > limit,
            "content": raw[:limit].decode("utf-8", "replace")}


def _cli_base():
    for name in BIN:
        found = shutil.which(name)
        if found:
            return [found]
    if PACKAGE and shutil.which("npx"):
        return [shutil.which("npx"), "-y", PACKAGE]
    return None


def _wrap(cmd):
    first = cmd[0].lower()
    if os.name == "nt" and first.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c"] + cmd
    return cmd


def _cli(cfg, args):
    raw_args = args.get("args") or ""
    if isinstance(raw_args, list):
        raw_args = " ".join(str(x) for x in raw_args)
    raw_args = str(raw_args)
    if any(ch in raw_args for ch in "&|<>^%\\n\\r"):
        return {"ok": False, "error": "в args запрещены символы & | < > ^ %"}
    base = _cli_base()
    if not base:
        hint = INSTALL_HINT or ("Установи CLI " + (PACKAGE or REPO) + " и повтори.")
        return {"ok": False, "repo": REPO, "package": PACKAGE,
                "error": "CLI не найден (%s). %s" % (", ".join(BIN) or "команда", hint)}
    try:
        extra = shlex.split(raw_args, posix=True)
    except ValueError as exc:
        return {"ok": False, "error": "не удалось разобрать args: %s" % exc}
    cmd = _wrap(base + extra)
    try:
        timeout = max(5, min(int(float(args.get("timeout") or 60)), 900))
    except (TypeError, ValueError):
        timeout = 60
    cwd = str(args.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        cwd = os.getcwd()
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
                "command": cmd, "cwd": cwd}
    except OSError as exc:
        return {"ok": False, "error": "не удалось запустить CLI: %s" % exc, "command": cmd}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "command": cmd, "cwd": cwd,
            "stdout": (proc.stdout or "")[-8000:], "stderr": (proc.stderr or "")[-4000:]}


TOOLS = [
{{TOOLS_BLOCK}}]
'''


def _tools_block(name: str, mode: str) -> str:
    tools = [
        {"suffix": "info", "desc": "Справка о репозитории: звёзды, язык, лицензия, пакет, CLI.",
         "params": {}, "required": [], "fn": "_repo_info"},
        {"suffix": "files", "desc": "Список файлов репозитория через GitHub API.",
         "params": {"limit": ("number", "сколько записей")}, "required": [], "fn": "_repo_files"},
        {"suffix": "read", "desc": "Прочитать файл из репозитория.",
         "params": {"path": ("string", "путь в репозитории"),
                    "max_chars": ("number", "лимит знаков")},
         "required": ["path"], "fn": "_repo_read"},
        {"suffix": "cli", "desc": "Запустить CLI репозитория (если установлен).",
         "params": {"args": ("string", "аргументы CLI"),
                    "cwd": ("string", "рабочая папка"),
                    "timeout": ("number", "секунды")},
         "required": [], "fn": "_cli"},
    ]
    lines = []
    for t in tools:
        if t["suffix"] == "cli" and mode == "read":
            continue
        params = ", ".join('%r: (%r, %r)' % (k, v[0], v[1]) for k, v in t["params"].items())
        lines.append(
            '    {"name": %r, "desc": %r, "params": {%s}, "required": %r, "fn": %s},\n'
            % (f"{name}_{t['suffix']}", t["desc"], params, t["required"], t["fn"]))
    return "".join(lines)


def _plugin_code(owner: str, repo: str, name: str, branch: str, description: str,
                 summary: str, meta: Dict[str, Any], mode: str) -> str:
    bins = list(meta.get("bins") or [])
    tokens = {
        "NAME": name,
        "REPO": f"{owner}/{repo}",
        "BRANCH": branch,
        "DESC_REPR": repr(description),
        "PACKAGE": str(meta.get("package") or ""),
        "BINS_REPR": repr(bins),
        "HINT_REPR": repr(meta.get("install_hint") or ""),
        "MODE": mode,
        "BINS": ", ".join(bins) or "—",
        "TOOLS_BLOCK": _tools_block(name, mode),
    }
    header_desc = description if not summary else f"{description}\n\n{summary}"
    tokens["DESCRIPTION"] = header_desc.replace("\\", "\\\\").replace('"""', "'''")
    code = _PLUGIN_TEMPLATE
    for key, value in tokens.items():
        code = code.replace("{{%s}}" % key, str(value))
    return code


def convert_repo(repo_url: str, custom_name: Optional[str] = None, save: bool = True) -> Dict[str, Any]:
    """Analyzes any GitHub repo and builds a working LocalHarness plugin."""
    owner, repo = parse_repo_target(repo_url)
    clean_name = re.sub(r"[^A-Za-z0-9_]", "_", (custom_name or repo).lower()).strip("_")
    if not clean_name:
        clean_name = "repo_plugin"
    if clean_name[0].isdigit():
        clean_name = "repo_" + clean_name

    meta = fetch_github_json(f"repos/{owner}/{repo}")
    description = str(meta.get("description") or f"Плагин на основе {owner}/{repo}")
    branch = meta.get("default_branch") or "main"
    stars = meta.get("stargazers_count", 0)

    try:
        tree_data = fetch_github_json(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
        items = [i for i in (tree_data.get("tree") or []) if i.get("path")]
    except Exception:
        items = []
    paths = [i["path"] for i in items]

    manifest = _detect_manifests(owner, repo, branch, paths)
    language = _repo_language(meta, paths)
    summary = _readme_summary(owner, repo, branch, paths)
    if not description and summary:
        description = summary[:200]
    if manifest.get("description") and len(description) < 20:
        description = manifest["description"]

    py_files = [p for p in paths if p.endswith(".py") and not p.startswith("test")]
    py_files.sort(key=lambda p: (
        0 if re.search(r"(tools?|mcp|agent)", p, re.I) else 1,
        0 if re.search(r"(main|__init__)\.py$", p) else 1,
        p.count("/"),
    ))
    python_functions: List[str] = []
    for pf in py_files[:5]:
        try:
            found = analyze_python_ast(fetch_raw_file(owner, repo, branch, pf))
        except Exception:
            continue
        python_functions.extend(t["name"] for t in found)
        if len(python_functions) >= 40:
            break

    has_cli = bool(manifest.get("bins") or manifest.get("package"))
    mode = "cli" if has_cli else "read"

    generated_code = _plugin_code(owner, repo, clean_name, branch, description,
                                  summary, manifest, mode)
    try:
        compile(generated_code, f"{clean_name}.py", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"сгенерированный плагин не компилируется: {exc}",
                "repo": f"{owner}/{repo}", "code": generated_code}

    vt_report = virustotal.scan_plugin(generated_code)

    file_name = f"{clean_name}.py"
    install_result = None
    if save and vt_report.get("clean"):
        from . import plugins
        install_result = plugins.install_code(generated_code, file_name)

    names = [f"{clean_name}_{suffix}" for suffix in ("info", "files", "read")]
    if mode == "cli":
        names.append(f"{clean_name}_cli")
    return {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "name": clean_name,
        "file": file_name,
        "description": description,
        "summary": summary,
        "stars": stars,
        "branch": branch,
        "language": language,
        "mode": mode,
        "package": manifest.get("package") or "",
        "cli": manifest.get("bins") or [],
        "install_hint": manifest.get("install_hint") or "",
        "files_count": len(items),
        "tools_count": len(names),
        "tools": names,
        "python_functions": python_functions,
        "code": generated_code,
        "security": vt_report,
        "installed": bool(install_result and install_result.get("ok")),
        "install_error": (install_result or {}).get("error") if install_result and not install_result.get("ok") else "",
    }
