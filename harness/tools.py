"""Tool implementations: shell, files, python REPL, web, desktop, system."""
import ctypes
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .config import DATA_DIR
from . import browser, netio, search as search_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_OUT = 9000
SKIP_DIRS = {".git", "node_modules", "__pycache__", "$RECYCLE.BIN", "AppData", ".venv", "venv", "target", ".gradle"}


def _askpass_path():
    """Пустой askpass-хелпер: ssh берёт из него пустой пароль и сразу падает, вместо того
    чтобы ждать ввода с консоли и висеть до бесконечности."""
    path = os.path.join(DATA_DIR, "askpass.cmd")
    try:
        if not os.path.isfile(path):
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="ascii", newline="") as fh:
                fh.write("@echo off\r\nexit /b 1\r\n")
    except Exception:
        return None
    return path


def child_env():
    """Окружение для дочерних процессов: ssh никогда не спрашивает пароль интерактивно."""
    env = dict(os.environ)
    askpass = _askpass_path()
    if askpass:
        env["SSH_ASKPASS"] = askpass
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
    return env


def kill_tree(pid):
    """Убить процесс и его потомков: bash -lc 'ssh ...' оставляет сироту, который живёт после таймаута."""
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


def run_capture(argv, timeout, cwd=None, flags=0, env=None):
    """Запуск без интерактива: stdin закрыт, поэтому ssh/curl не ждут пароль, а падают сразу.
    По таймауту убивается всё дерево процессов."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, cwd=cwd, creationflags=flags, shell=False,
                            env=env or child_env())
    try:
        out, err = proc.communicate(timeout=float(timeout))
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = b"", b""
        return proc.returncode, out or b"", err or b"", True

WORKER = r'''
import sys, json, io, contextlib, traceback
ns = {"__name__": "__main__"}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        job = json.loads(line)
    except Exception:
        continue
    buf = io.StringIO()
    res = None
    err = None
    code = job.get("code", "")
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                res = eval(code, ns)
            except SyntaxError:
                exec(compile(code, "<lh>", "exec"), ns)
        if res is not None:
            buf.write("\n=> " + repr(res)[:4000])
    except BaseException as exc:
        err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    sys.stdout.write(json.dumps({"id": job.get("id"), "ok": err is None,
                                 "out": buf.getvalue()[:200000], "error": err}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''


def decode_bytes(raw):
    if not raw:
        return ""
    for enc in ("utf-8", "cp866", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def truncate(text, limit=MAX_OUT, head=None):
    text = text or ""
    if len(text) <= limit:
        return text, 0
    keep = head or int(limit * 0.7)
    return text[:keep] + "\n…[пропущено %d знаков]…\n" % (len(text) - keep - 600) + text[-600:], len(text) - limit


NO_PYTHON = ("нет интерпретатора Python для инструмента python: в собранном .exe sys.executable — сам харнес, "
             "задай LH_PYTHON или поставь Python в PATH")
_PYTHON_CANDIDATES = ("python.exe", "python3.exe", "py.exe")
_interp_cache = {}


def python_interp():
    """The REPL worker needs a real interpreter: inside a frozen build sys.executable is LocalHarness.exe."""
    if not getattr(sys, "frozen", False) and os.path.exists(sys.executable):
        return sys.executable
    if "path" in _interp_cache:
        return _interp_cache["path"]
    found = None
    for candidate in (os.environ.get("LH_PYTHON"), shutil.which("python"), shutil.which("python3"), shutil.which("py")):
        if candidate and os.path.exists(candidate):
            found = candidate
            break
    if not found:
        roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")]
        for root in roots:
            if not root:
                continue
            for name in _PYTHON_CANDIDATES:
                hits = sorted(glob.glob(os.path.join(root, "Programs", "Python", "*", name))
                              + glob.glob(os.path.join(root, "Python*", name)))
                if hits:
                    found = hits[-1]
                    break
            if found:
                break
    _interp_cache["path"] = found
    return found


class Repl:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _start(self):
        interp = python_interp()
        if not interp:
            return False
        env = dict(child_env(), PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [interp, "-c", WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            env=env, creationflags=NO_WINDOW, cwd=DATA_DIR, bufsize=1,
        )
        return True

    def run(self, code, timeout=30.0):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if not self._start():
                    return {"ok": False, "out": "", "error": NO_PYTHON}
            job_id = uuid.uuid4().hex[:8]
            try:
                self.proc.stdin.write(json.dumps({"id": job_id, "code": code}) + "\n")
                self.proc.stdin.flush()
            except Exception:
                if not self._start():
                    return {"ok": False, "out": "", "error": NO_PYTHON}
                self.proc.stdin.write(json.dumps({"id": job_id, "code": code}) + "\n")
                self.proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == job_id:
                    return msg
            self.kill()
            return {"ok": False, "out": "", "error": "python REPL завис на %s с — убит, переменные сброшены" % timeout}

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc = None


def resolve_path(path, base=None):
    if not path:
        return os.path.abspath(base or DATA_DIR)
    path = os.path.expanduser(path.strip().strip('"').strip("'"))
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base or DATA_DIR, path))


class Toolkit:
    def __init__(self, cfg):
        self.cfg = cfg
        self.workspace = cfg.get("workspace") or DATA_DIR
        self.repl = Repl()
        self.jobs = {}
        self.tunnels = {}
        os.makedirs(self.workspace, exist_ok=True)
        self.shot_dir = os.path.join(DATA_DIR, "shots")
        os.makedirs(self.shot_dir, exist_ok=True)

    # ---------- shell / remote shell ----------
    def powershell(self, cmd=None, cwd=None, timeout=60, background=False, **kw):
        """Explicit PowerShell entry point for models that need Windows automation."""
        return self.shell(cmd=cmd, cwd=cwd, timeout=timeout, background=background, mode="powershell")

    @staticmethod
    def _available_keys():
        """Реальные приватные ключи этой машины."""
        ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
        rows = []
        if os.path.isdir(ssh_dir):
            for name in sorted(os.listdir(ssh_dir)):
                if name.endswith(".pub") or name == "known_hosts" or name.endswith(".config"):
                    continue
                path = os.path.join(ssh_dir, name)
                if os.path.isfile(path):
                    rows.append(path)
        return rows

    @staticmethod
    def _available_pubkeys():
        """Реальные публичные ключи этой машины (.pub)."""
        ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
        rows = {}
        if os.path.isdir(ssh_dir):
            for name in sorted(os.listdir(ssh_dir)):
                if name.endswith(".pub"):
                    path = os.path.join(ssh_dir, name)
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            content = fh.read().strip()
                        if content and ("ssh-" in content or "ecdsa-" in content):
                            rows[name] = content
                    except Exception:
                        pass
        return rows

    @staticmethod
    def _parse_ssh_config():
        """Читает хосты из системного ~/.ssh/config."""
        ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
        cfg_file = os.path.join(ssh_dir, "config")
        if not os.path.isfile(cfg_file):
            return {}
        hosts = {}
        current_hosts = []
        try:
            with open(cfg_file, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) < 2:
                        continue
                    key, val = parts[0].lower(), parts[1].strip()
                    if key == "host":
                        current_hosts = [h for h in val.split() if "*" not in h and "?" not in h]
                        for h in current_hosts:
                            if h not in hosts:
                                hosts[h] = {}
                    elif current_hosts:
                        for h in current_hosts:
                            if key in ("hostname", "host"):
                                hosts[h]["host"] = val
                            elif key == "user":
                                hosts[h]["user"] = val
                            elif key == "port" and val.isdigit():
                                hosts[h]["port"] = int(val)
                            elif key == "identityfile":
                                resolved_id = os.path.expanduser(val.strip('"').strip("'"))
                                hosts[h]["key"] = resolved_id
        except Exception:
            pass
        return hosts

    def _resolve_key(self, key):
        """Модель часто придумывает linux-путь к ключу (/home/.../.ssh/id_rsa). Проверяем файл
        и, если его нет, подставляем реальный ключ этой машины — иначе SSH всегда откажет."""
        if key:
            path = os.path.expanduser(str(key))
            if os.path.isfile(path):
                return path, ""
            note = "ключ %s на этой машине не найден. " % path
        else:
            note = ""
        available = self._available_keys()
        if available:
            return available[0], note + "использован ключ по умолчанию: %s (доступные: %s)" \
                % (available[0], ", ".join(available))
        return key, note + ("доступных ключей в %s нет" % os.path.join(os.path.expanduser("~"), ".ssh"))

    def _ssh_params(self, profile=None, host=None, user=None, port=None, key=None):
        profiles = dict(self.cfg.get("ssh_profiles") or {})
        sys_hosts = self._parse_ssh_config()
        for h_name, h_info in sys_hosts.items():
            if h_name not in profiles:
                profiles[h_name] = h_info

        prof_name = profile or self.cfg.get("ssh_profile") or (host if host in profiles else None)
        selected = profiles.get(prof_name) or {}
        host = host or selected.get("host")
        user = user or selected.get("user")
        port = int(port if port is not None else (selected.get("port") or 22))
        key = key or selected.get("key")
        key, note = self._resolve_key(key)
        return host, user, port, key, note

    @staticmethod
    def _ssh_opts(timeout):
        # BatchMode: вместо зависшего password-промпта сразу ошибка; accept-new: первый коннект не спрашивает yes/no
        return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=%d" % max(1, min(int(timeout), 300)),
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

    def ssh_connect(self, profile=None, host=None, user=None, port=None, key=None, sysinfo=True, **kw):
        diag_cmd = (
            "printf 'LOCALHARNESS_SSH_OK\n'; "
            "uname -sm 2>/dev/null || ver; "
            "echo '---LH_SYS---'; "
            "cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|NAME)=' | head -1 | cut -d= -f2 | tr -d '\"'; "
            "echo '---LH_UP---'; uptime -p 2>/dev/null || uptime 2>/dev/null; "
            "echo '---LH_MEM---'; free -h 2>/dev/null | awk '/Mem:/ {print $2 \" total, \" $7 \" avail\"}' || free -m 2>/dev/null | awk '/Mem:/ {print $2 \"MB total, \" $7 \"MB avail\"}'; "
            "echo '---LH_DISK---'; df -h / 2>/dev/null | awk 'NR==2 {print $2 \" total, \" $4 \" free (\" $5 \" used)\"}'; "
            "echo '---LH_DOCKER---'; command -v docker >/dev/null 2>&1 && docker --version 2>/dev/null || echo 'none'"
        ) if sysinfo else "printf LOCALHARNESS_SSH_OK; uname -sm 2>/dev/null || ver"
        result = self.ssh(command=diag_cmd, profile=profile, host=host, user=user, port=port, key=key, timeout=25)
        if result.get("ok"):
            result["connected"] = True
            stdout = result.get("stdout") or ""
            if "LOCALHARNESS_SSH_OK" in stdout:
                rest = stdout.split("LOCALHARNESS_SSH_OK", 1)[1].strip()
                lines = rest.splitlines()
                first_line = lines[0].strip() if lines else ""
                result["remote_os"] = first_line
                if "---LH_SYS---" in rest:
                    sys_info = {}
                    parts = rest.split("---LH_")
                    for p in parts[1:]:
                        marker, _, val = p.partition("---")
                        val = val.strip()
                        if marker == "SYS":
                            sys_info["distro"] = val
                        elif marker == "UP":
                            sys_info["uptime"] = val
                        elif marker == "MEM":
                            sys_info["memory"] = val
                        elif marker == "DISK":
                            sys_info["disk"] = val
                        elif marker == "DOCKER":
                            sys_info["docker"] = val if val != "none" else None
                    result["system"] = sys_info
            else:
                result["remote_os"] = stdout.strip()[:100]
        return result

    def ssh_profile(self, action="list", name=None, host=None, user=None, port=None, key=None, **kw):
        """List saved VPS profiles, save/delete, or show public keys for server setup."""
        from .config import save
        profiles = dict(self.cfg.get("ssh_profiles") or {})
        sys_hosts = self._parse_ssh_config()
        pubkeys = self._available_pubkeys()
        if action == "save":
            if not name or not host:
                return {"ok": False, "error": "для save нужны name и host"}
            entry = {"host": host}
            if user:
                entry["user"] = user
            if port:
                entry["port"] = int(port)
            if key:
                entry["key"] = os.path.expanduser(str(key))
            profiles[str(name)] = entry
            self.cfg["ssh_profiles"] = profiles
            save(self.cfg)
            result = {"ok": True, "saved": str(name), "profile": entry}
            if key and not os.path.isfile(os.path.expanduser(str(key))):
                result["note"] = ("путь к ключу %s на этой машине не найден — SSH его не увидит. "
                                  "Реальные ключи: %s" % (entry["key"], ", ".join(self._available_keys()) or "нет"))
            return result
        if action == "delete":
            if not name:
                return {"ok": False, "error": "для delete нужно указать name профиля"}
            if str(name) in profiles:
                deleted = profiles.pop(str(name))
                self.cfg["ssh_profiles"] = profiles
                save(self.cfg)
                return {"ok": True, "deleted": str(name), "profile": deleted}
            return {"ok": False, "error": "профиль %s не найден в config.json" % name}
        if action == "pubkey":
            if not pubkeys:
                return {"ok": False, "error": "публичные ключи (.pub) в ~/.ssh не найдены"}
            chosen_key = None
            if name and str(name) in profiles:
                k_path = profiles[str(name)].get("key")
                if k_path and os.path.isfile(k_path + ".pub"):
                    try:
                        chosen_key = open(k_path + ".pub", "r", encoding="utf-8").read().strip()
                    except Exception:
                        pass
            if not chosen_key:
                ed = [v for k, v in pubkeys.items() if "ed25519" in k]
                chosen_key = ed[0] if ed else next(iter(pubkeys.values()))
            cmd_instruction = "echo '%s' >> ~/.ssh/authorized_keys" % chosen_key
            return {"ok": True, "pubkey": chosen_key, "pubkeys": pubkeys,
                    "install_command": cmd_instruction,
                    "hint": "Выполните эту команду на удаленном сервере для настройки беспарольного доступа"}
        return {"ok": True, "profiles": profiles, "default": self.cfg.get("ssh_profile") or "",
                "available_keys": self._available_keys(), "system_hosts": sys_hosts,
                "public_keys": list(pubkeys.keys())}

    def _scp(self, argv_tail, host, user, port, key, timeout):
        scp = shutil.which("scp") or shutil.which("scp.exe")
        if not scp:
            return {"ok": False, "error": "OpenSSH scp не найден в PATH"}
        target = "%s@%s" % (user, host) if user else str(host)
        argv = [scp, "-P", str(port)] + self._ssh_opts(timeout)
        if key:
            argv += ["-i", os.path.expanduser(str(key))]
        argv += argv_tail
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=float(timeout), creationflags=NO_WINDOW,
                                  env=child_env())
            return {"ok": proc.returncode == 0, "exit": proc.returncode, "host": target,
                    "stdout": truncate(decode_bytes(proc.stdout), 3000)[0],
                    "stderr": truncate(decode_bytes(proc.stderr), 3000)[0]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "SCP таймаут %s с" % timeout, "host": target}
        except OSError as exc:
            return {"ok": False, "error": "SCP не запустился: %s" % exc}

    def ssh_upload(self, local_path=None, remote_path=None, profile=None, host=None, user=None,
                   port=None, key=None, timeout=120, recursive=False, **kw):
        host, user, port, key, key_note = self._ssh_params(profile, host, user, port, key)
        if not host:
            return {"ok": False, "error": "не указан host"}
        if not local_path:
            return {"ok": False, "error": "не указан local_path"}
        local = resolve_path(local_path, self.workspace)
        if not os.path.exists(local):
            return {"ok": False, "error": "нет локального пути: %s" % local}
        target = "%s@%s" % (user, host) if user else str(host)
        is_dir = os.path.isdir(local)
        argv_tail = []
        if recursive or is_dir:
            argv_tail.append("-r")
        argv_tail += [local, "%s:%s" % (target, remote_path)]
        result = self._scp(argv_tail, host, user, port, key, timeout)
        return dict(result, local_path=local, remote_path=remote_path, is_dir=is_dir)

    def ssh_download(self, remote_path=None, local_path=None, profile=None, host=None, user=None,
                     port=None, key=None, timeout=120, recursive=False, **kw):
        """Скачать файл с VPS через scp (remote -> local)."""
        host, user, port, key, key_note = self._ssh_params(profile, host, user, port, key)
        if not host:
            return {"ok": False, "error": "не указан host"}
        if not remote_path:
            return {"ok": False, "error": "нужен remote_path"}
        local = resolve_path(local_path or os.path.join(self.workspace, os.path.basename(str(remote_path))),
                             self.workspace)
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        target = "%s@%s" % (user, host) if user else str(host)
        argv_tail = []
        if recursive:
            argv_tail.append("-r")
        argv_tail += ["%s:%s" % (target, remote_path), local]
        result = self._scp(argv_tail, host, user, port, key, timeout)
        if result.get("ok"):
            if os.path.isfile(local):
                result["bytes"] = os.path.getsize(local)
            elif os.path.isdir(local):
                total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(local) for f in fs)
                result["bytes"] = total
                result["is_dir"] = True
        return dict(result, local_path=local, remote_path=remote_path)

    def ssh(self, command=None, profile=None, host=None, user=None, port=None, key=None,
            timeout=60, background=False, mode="auto", **kw):
        """Run a command on a VPS through the installed OpenSSH client.

        Authentication is intentionally delegated to OpenSSH: use an ssh key,
        ssh-agent, or a configured profile. Passwords are not accepted in JSON
        config and are not logged by LocalHarness.
        """
        host, user, port, key, key_note = self._ssh_params(profile, host, user, port, key)
        if not host:
            return {"ok": False, "error": "не указан host: передай host или настрой ssh_profiles в config.json"}
        if not command or not str(command).strip():
            return {"ok": False, "error": "пустая команда для VPS"}
        ssh_bin = shutil.which("ssh") or shutil.which("ssh.exe")
        if not ssh_bin:
            return {"ok": False, "error": "OpenSSH client не найден в PATH"}
        target = "%s@%s" % (user, host) if user else str(host)
        argv = [ssh_bin, "-p", str(port)] + self._ssh_opts(timeout)
        if key:
            argv += ["-i", os.path.expanduser(str(key))]

        cmd_str = str(command)
        use_stdin = (mode == "script" or (mode == "auto" and "\n" in cmd_str)) and not background
        if use_stdin:
            argv += [target, "bash -s 2>&1 || sh -s 2>&1"]
        else:
            argv += [target, cmd_str]

        try:
            if background:
                log_path = os.path.join(DATA_DIR, "jobs", "%s.log" % uuid.uuid4().hex[:6])
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                handle = open(log_path, "w", encoding="utf-8", errors="replace")
                proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                                        creationflags=NO_WINDOW)
                self.jobs[proc.pid] = {"pid": proc.pid, "log": log_path,
                                       "cmd": "ssh %s" % target, "started": time.time(), "handle": handle}
                return {"ok": True, "background": True, "pid": proc.pid, "log": log_path,
                        "host": target}
            proc = subprocess.run(argv, input=cmd_str.encode("utf-8") if use_stdin else None,
                                  capture_output=True, timeout=float(timeout), creationflags=NO_WINDOW,
                                  env=child_env())
            out = truncate(decode_bytes(proc.stdout), MAX_OUT)[0]
            err = truncate(decode_bytes(proc.stderr), 3000)[0]
            result = {"ok": proc.returncode == 0, "exit": proc.returncode,
                      "stdout": out, "stderr": err, "host": target, "port": port}
            if key_note:
                result["note"] = key_note
            if not result["ok"] and "Permission denied" in (err or ""):
                result["hint"] = ("сервер не принял ключ. Проверь, что нужный публичный ключ добавлен на VPS "
                                  "(~/.ssh/authorized_keys), или используй ssh_profile(action='pubkey'). "
                                  "Пароль харнес не спрашивает и не подставляет.")
            return result
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "SSH таймаут %s с для %s" % (timeout, target), "host": target}
        except OSError as exc:
            return {"ok": False, "error": "SSH не запустился: %s" % exc}

    def ssh_tunnel(self, action="open", local_port=None, remote_port=None, remote_host="127.0.0.1",
                   profile=None, host=None, user=None, port=None, key=None, pid=None, **kw):
        """SSH port forwarding tunnel:
        open — пробросить local_port к remote_host:remote_port на VPS в фоне.
        close — остановить туннель по local_port или pid.
        list — показать активные туннели.
        """
        if not hasattr(self, "tunnels"):
            self.tunnels = {}

        if action == "list":
            rows = []
            for lp, meta in list(self.tunnels.items()):
                proc = meta.get("proc")
                alive = (proc.poll() is None) if proc else psutil_alive(meta.get("pid"))
                if not alive:
                    self.tunnels.pop(lp, None)
                    continue
                rows.append({"local_port": lp, "remote": meta["remote"],
                             "target": meta["target"], "pid": meta["pid"],
                             "uptime_sec": int(time.time() - meta["started"])})
            return {"ok": True, "tunnels": rows}

        if action == "close":
            target_port = int(local_port) if local_port is not None else None
            meta = None
            if target_port and target_port in self.tunnels:
                meta = self.tunnels.pop(target_port)
            elif pid is not None:
                p_id = int(pid)
                for lp, m in list(self.tunnels.items()):
                    if m.get("pid") == p_id:
                        meta = self.tunnels.pop(lp)
                        target_port = lp
                        break
            if not meta:
                return {"ok": False, "error": "туннель для %s не найден" % (local_port or pid)}
            proc = meta.get("proc")
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return {"ok": True, "closed": target_port, "pid": meta.get("pid")}

        if action == "open":
            if not local_port or not remote_port:
                return {"ok": False, "error": "для open нужны local_port и remote_port"}
            l_port = int(local_port)
            r_port = int(remote_port)
            r_host = str(remote_host or "127.0.0.1").strip()

            if l_port in self.tunnels:
                meta = self.tunnels[l_port]
                if (meta.get("proc") and meta["proc"].poll() is None) or psutil_alive(meta.get("pid")):
                    return {"ok": True, "already_open": True, "local_port": l_port,
                            "remote": meta["remote"], "pid": meta["pid"]}

            host, user, port, key, key_note = self._ssh_params(profile, host, user, port, key)
            if not host:
                return {"ok": False, "error": "не указан host"}
            ssh_bin = shutil.which("ssh") or shutil.which("ssh.exe")
            if not ssh_bin:
                return {"ok": False, "error": "OpenSSH client не найден в PATH"}
            target = "%s@%s" % (user, host) if user else str(host)

            argv = [ssh_bin, "-N", "-L", "%d:%s:%d" % (l_port, r_host, r_port),
                    "-p", str(port)] + self._ssh_opts(60)
            if key:
                argv += ["-i", os.path.expanduser(str(key))]
            argv += [target]

            log_path = os.path.join(DATA_DIR, "jobs", "tunnel_%d.log" % l_port)
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            handle = open(log_path, "w", encoding="utf-8", errors="replace")
            try:
                proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                                        creationflags=NO_WINDOW)
            except Exception as exc:
                return {"ok": False, "error": "не удалось запустить ssh tunnel: %s" % exc}

            time.sleep(1.0)
            if proc.poll() is not None:
                handle.close()
                err = ""
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                        err = fh.read().strip()
                except Exception:
                    pass
                return {"ok": False, "error": "SSH tunnel завершился сразу (код %s): %s" % (proc.returncode, err or "ошибка подключения")}

            meta = {"pid": proc.pid, "proc": proc, "local_port": l_port,
                    "remote": "%s:%d" % (r_host, r_port), "target": target,
                    "log": log_path, "started": time.time()}
            self.tunnels[l_port] = meta
            self.jobs[proc.pid] = {"pid": proc.pid, "log": log_path,
                                   "cmd": "ssh -L %d:%s:%d %s" % (l_port, r_host, r_port, target),
                                   "started": time.time(), "handle": handle}
            return {"ok": True, "local_port": l_port, "remote": "%s:%d" % (r_host, r_port),
                    "target": target, "pid": proc.pid}

        return {"ok": False, "error": "действие %s неизвестно (open|close|list)" % action}

    def model_info(self, **kw):
        try:
            from .llm import LLM
            from . import sizing as sizing_mod
            client = LLM(self.cfg)
            hw = sizing_mod.get_hardware_profile()
            return {"ok": True, "provider": client.label, "models": client.models(), "hardware": hw}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def model_estimate(self, model=None, context_length=8192, **kw):
        """Оценивает требования модели к памяти (RAM/VRAM), скорость tok/s и риск лагов UI."""
        from . import sizing as sizing_mod
        from . import lms as lms_mod
        cfg = self.cfg
        ctx = int(context_length or cfg.get("lms_context_length") or 8192)
        disk, _ = lms_mod.disk_models(cfg)
        loaded, _ = lms_mod.loaded_models(cfg)
        target = None
        m_name = str(model or cfg.get("model") or "").strip()
        for item in (loaded or []) + (disk or []):
            if m_name and m_name in (item.get("modelKey"), item.get("path"), item.get("identifier"), item.get("displayName")):
                target = item
                break
        if not target and (disk or loaded):
            target = (loaded or disk)[0]
        if not target:
            target = {"modelKey": m_name or "default-8b", "paramsString": "8B", "sizeBytes": 5000000000}
        hw = sizing_mod.get_hardware_profile()
        estimate = sizing_mod.estimate_model_fit(target, context_length=ctx, hw=hw)
        return {"ok": True, "model": target.get("displayName") or target.get("modelKey") or m_name,
                "estimate": estimate}

    def dataset_append(self, path=None, instruction=None, input="", output=None, **kw):
        target = resolve_path(path, self.workspace)
        if not instruction or output is None:
            return {"ok": False, "error": "нужны instruction и output"}
        os.makedirs(os.path.dirname(target), exist_ok=True)
        row = {"instruction": str(instruction), "input": str(input or ""), "output": str(output)}
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {"ok": True, "path": target, "record": row}

    def dataset_validate(self, path=None, **kw):
        target = resolve_path(path, self.workspace)
        if not os.path.isfile(target):
            return {"ok": False, "error": "датасет не найден: %s" % target}
        valid = invalid = 0
        errors = []
        with open(target, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not row.get("instruction") or "output" not in row:
                        raise ValueError("нужны instruction и output")
                    valid += 1
                except Exception as exc:
                    invalid += 1
                    if len(errors) < 10:
                        errors.append({"line": line_no, "error": str(exc)})
        return {"ok": invalid == 0 and valid > 0, "path": target, "valid": valid, "invalid": invalid, "errors": errors}

    def train_model(self, command=None, cwd=None, timeout=3600, background=True, **kw):
        if not command:
            return {"ok": False, "error": "нужна команда тренера"}
        return self.shell(cmd=command, cwd=cwd or self.workspace, timeout=timeout, background=background, mode="powershell")

    def shell(self, cmd=None, cwd=None, timeout=60, background=False, mode=None, command=None, **kw):
        cmd = cmd or command or ""
        if not cmd.strip():
            return {"ok": False, "error": "пустая команда"}
        cwd = resolve_path(cwd, self.workspace)
        mode = mode or ("bash" if os.environ.get("BASH") else "powershell")
        if mode == "bash":
            argv = ["bash", "-lc", cmd]
        elif mode == "raw":
            argv = cmd
        else:
            # PS 5.1 пишет в OEM-кодировке — кириллица билась; форсируем UTF-8 на вывод
            cmd = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                   "$OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd)
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]
        flags = NO_WINDOW
        try:
            if background:
                log_path = os.path.join(DATA_DIR, "jobs", "%s.log" % uuid.uuid4().hex[:6])
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                handle = open(log_path, "w", encoding="utf-8", errors="replace")
                proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, cwd=cwd,
                                        creationflags=flags, shell=False, env=child_env())
                self.jobs[proc.pid] = {"pid": proc.pid, "log": log_path, "cmd": cmd, "started": time.time(), "handle": handle}
                return {"ok": True, "background": True, "pid": proc.pid, "log": log_path,
                        "note": "запущено в фоне; забери вывод через jobs(action='log', pid=%d)" % proc.pid}
            try:
                code, raw_out, raw_err, timed_out = run_capture(argv, timeout, cwd=cwd, flags=flags)
            except FileNotFoundError as exc:
                return {"ok": False, "error": "не найден интерпретатор: %s" % exc}
            out, dropped = truncate(decode_bytes(raw_out) if isinstance(raw_out, bytes) else (raw_out or ""))
            err, dropped2 = truncate(decode_bytes(raw_err) if isinstance(raw_err, bytes) else (raw_err or ""), 3000)
            if timed_out:
                return {"ok": False, "error": "таймаут %s с — команда убита вместе с дочерними процессами" % timeout,
                        "stdout_so_far": out}
            return {"ok": code == 0, "exit": code, "stdout": out,
                    "stderr": err, "dropped_chars": dropped + dropped2, "cwd": cwd}
        except FileNotFoundError as exc:
            return {"ok": False, "error": "не найден интерпретатор: %s" % exc}

    def jobs_tool(self, action="list", pid=None, lines=60, **kw):
        if action == "list":
            rows = []
            for meta in self.jobs.values():
                rows.append({"pid": meta["pid"], "alive": psutil_alive(meta["pid"]), "cmd": meta["cmd"][:120], "log": meta["log"]})
            return {"ok": True, "jobs": rows}
        if pid is None:
            return {"ok": False, "error": "нужен pid"}
        meta = self.jobs.get(int(pid)) if str(pid).isdigit() else None
        if not meta:
            for root, _dirs, _files in os.walk(os.path.join(DATA_DIR, "jobs")):
                if os.path.exists(os.path.join(root, "%s.log" % pid)):
                    meta = {"log": os.path.join(root, "%s.log" % pid)}
                    break
        if not meta:
            return {"ok": False, "error": "нет задачи с pid=%s" % pid}
        if action == "kill":
            try:
                import psutil
                psutil.Process(int(pid)).terminate()
                return {"ok": True, "killed": int(pid)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        try:
            with open(meta["log"], "r", encoding="utf-8", errors="replace") as fh:
                tail = fh.read()
            return {"ok": True, "alive": psutil_alive(int(pid)), "log": truncate(tail, 4000)[0]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- files ----------
    def read_file(self, path=None, offset=1, limit=400, **kw):
        target = resolve_path(path, self.workspace)
        if not os.path.exists(target):
            return {"ok": False, "error": "нет файла: %s" % target}
        size = os.path.getsize(target)
        if size > 6_000_000 and not os.environ.get("LH_FORCE"):
            return {"ok": False, "error": "файл %d МБ — читай частями через offset/limit" % (size // 1048576)}
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except IsADirectoryError:
            return self.list_dir(path=target)
        start = max(1, int(offset or 1))
        lim = max(1, int(limit or 400))
        chunk = lines[start - 1:start - 1 + lim]
        body = "".join("%5d | %s" % (i + start, ln.rstrip("\n")) for i, ln in enumerate(chunk))
        text, dropped = truncate(body, MAX_OUT)
        return {"ok": True, "path": target, "lines_total": len(lines), "shown": "%d-%d" % (start, start + len(chunk) - 1),
                "content": text, "truncated_chars": dropped}

    def write_file(self, path=None, content="", mode="overwrite", **kw):
        target = resolve_path(path, self.workspace)
        if mode == "append":
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            tmp = target + ".lh.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, target)
        return {"ok": True, "path": target, "bytes": os.path.getsize(target)}

    def edit_file(self, path=None, find=None, replace=None, all=False, **kw):
        target = resolve_path(path, self.workspace)
        if find is None or replace is None:
            return {"ok": False, "error": "нужны find и replace"}
        if not os.path.exists(target):
            return {"ok": False, "error": "нет файла: %s" % target}
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        count = src.count(find)
        if count == 0 and ("\r\n" in src or "\r\n" in find):
            # Пробуем нормализацию переносов строк CRLF <-> LF
            if "\r\n" in src and "\r\n" not in find:
                find_alt = find.replace("\n", "\r\n")
                if src.count(find_alt) > 0:
                    find = find_alt
                    replace = replace.replace("\r\n", "\n").replace("\n", "\r\n")
                    count = src.count(find)
            elif "\r\n" not in src and "\r\n" in find:
                find_alt = find.replace("\r\n", "\n")
                if src.count(find_alt) > 0:
                    find = find_alt
                    replace = replace.replace("\r\n", "\n")
                    count = src.count(find)
        if count == 0:
            sample = "\n".join(src.splitlines()[:12])
            return {"ok": False, "error": "фрагмент не найден буквально. Начало файла:\n%s" % sample}
        if count > 1 and not all:
            return {"ok": False, "error": "совпадений %d — уточни find или поставь all=true" % count}
        new = src.replace(find, replace) if all else src.replace(find, replace, 1)
        tmp = target + ".lh.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, target)
        return {"ok": True, "path": target, "replacements": count if all else 1, "bytes": os.path.getsize(target)}

    def list_dir(self, path=None, limit=200, **kw):
        target = resolve_path(path, self.workspace)
        if not os.path.isdir(target):
            return {"ok": False, "error": "не папка: %s" % target}
        lim = max(1, int(limit or 200))
        rows = []
        for name in sorted(os.listdir(target))[:lim]:
            full = os.path.join(target, name)
            try:
                stat = os.stat(full)
                rows.append({"name": name, "dir": os.path.isdir(full), "bytes": stat.st_size,
                             "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))})
            except Exception:
                rows.append({"name": name, "dir": None, "bytes": None})
        return {"ok": True, "path": target, "entries": rows[:lim], "total": len(os.listdir(target))}

    def glob(self, pattern=None, path=None, limit=200, **kw):
        target = resolve_path(path, self.workspace)
        hits = []
        lim = max(1, int(limit or 200))
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if fnmatch.fnmatch(name, pattern or "*") or fnmatch.fnmatch(os.path.join(root, name).replace("\\", "/"), pattern or "*"):
                    hits.append(os.path.join(root, name))
                    if len(hits) >= lim:
                        return {"ok": True, "matches": hits, "capped": True}
        return {"ok": True, "matches": hits, "count": len(hits)}

    def grep(self, pattern=None, path=None, glob=None, ignore_case=True, context=0, limit=60, **kw):
        target = resolve_path(path, self.workspace)
        flags = re.I if ignore_case else 0
        try:
            rx = re.compile(pattern or "", flags)
        except re.error as exc:
            return {"ok": False, "error": "кривой regex: %s" % exc}
        files = []
        if os.path.isfile(target):
            files = [target]
        else:
            for root, dirs, fnames in os.walk(target):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for name in fnames:
                    if not glob or any(fnmatch.fnmatch(name, g.strip()) for g in str(glob).split(",")):
                        files.append(os.path.join(root, name))
        hits = []
        lim = max(1, int(limit or 60))
        ctx = max(0, int(context or 0))
        for fpath in files:
            if len(hits) >= lim:
                break
            try:
                if os.path.getsize(fpath) > 3_000_000:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except Exception:
                continue
            per_file = 0
            for num, line in enumerate(lines):
                if not rx.search(line):
                    continue
                entry = {"file": fpath, "line": num + 1, "text": line.rstrip()[:300]}
                if ctx:
                    lo = max(0, num - ctx)
                    hi = min(len(lines), num + ctx + 1)
                    entry["around"] = "".join(lines[lo:hi])[:1200]
                hits.append(entry)
                per_file += 1
                if per_file >= 5 or len(hits) >= lim:
                    break
        return {"ok": True, "pattern": pattern, "scanned_files": len(files), "matches": hits}

    # ---------- git ----------
    GIT_ACTIONS = ("status", "diff", "log", "show", "add", "commit", "branch")

    @staticmethod
    def _git_bin():
        found = shutil.which("git")
        if found:
            return found
        for cand in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
            if os.path.isfile(cand):
                return cand
        return None

    def _git_run(self, argv, cwd, timeout):
        exe = self._git_bin()
        if not exe:
            return {"ok": False, "error": "git не найден — поставь Git for Windows и добавь его в PATH"}
        env = child_env()
        # без TTY: никаких редакторов/пейджеров и висящих запросов учётных данных
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true"})
        full = [exe, "-c", "core.quotepath=false", "-c", "color.ui=false", "-c", "core.pager=cat"] + argv
        try:
            code, raw_out, raw_err, timed_out = run_capture(full, timeout, cwd=cwd, flags=NO_WINDOW, env=env)
        except OSError as exc:
            return {"ok": False, "error": "git не запустился: %s" % exc}
        out = decode_bytes(raw_out)
        err = decode_bytes(raw_err)
        if timed_out:
            return {"ok": False, "error": "git таймаут %s с" % timeout, "stdout": truncate(out, 4000)[0]}
        return {"ok": code == 0, "exit": code, "stdout": truncate(out, 12000)[0], "stderr": truncate(err, 3000)[0]}

    @staticmethod
    def _git_friendly(res, root):
        if res.get("ok"):
            return res
        raw = ((res.get("stderr") or "") + " " + (res.get("stdout") or "") + " "
               + str(res.get("error") or "")).lower()
        if "not a git repository" in raw:
            res["error"] = "не git-репозиторий: %s" % root
            res["hint"] = "перейди в папку с .git или создай репозиторий через shell: git init"
        elif "nothing to commit" in raw:
            res["error"] = "нечего коммитить — индекс пуст"
            res["hint"] = "сначала git action=add (path=... или all=true), затем commit"
        elif "please tell me who you are" in raw or "unable to auto-detect email" in raw:
            res["error"] = "в git не настроены имя и почта автора"
            res["hint"] = 'настрой один раз: git config --global user.name "Имя"; git config --global user.email "mail@example.com"'
        elif not res.get("error"):
            res["error"] = (res.get("stderr") or res.get("stdout") or "git завершился с ошибкой")[:1500].strip()
        return res

    def git(self, action="status", cwd=None, path=None, message=None, all=False,
            staged=False, stat=False, rev=None, limit=15, name=None, timeout=30, **kw):
        """Локальные git-операции: осмотр (status/diff/log/show), индекс (add), коммит, ветки.
        Сеть (push/pull/fetch) сюда не входит: такие действия — только по явной просьбе через shell."""
        action = str(action or "status").lower().strip()
        if action not in self.GIT_ACTIONS:
            return {"ok": False, "error": "action: %s" % "|".join(self.GIT_ACTIONS)}
        root = resolve_path(cwd, self.workspace)
        if os.path.isfile(root):
            root = os.path.dirname(root)
        if not os.path.isdir(root):
            return {"ok": False, "error": "нет папки: %s" % root}
        rel = None
        if path:
            rel = os.path.relpath(resolve_path(path, self.workspace), root)
            if rel.startswith(".."):
                return {"ok": False, "error": "путь вне репозитория: %s" % path}
        timeout = max(5.0, min(float(timeout or 30), 120.0))

        if action == "status":
            res = self._git_run(["status", "--short", "--branch"], root, timeout)
            if res.get("ok"):
                lines = (res.get("stdout") or "").splitlines()
                head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
                if head.startswith("No commits yet on "):
                    branch = head[len("No commits yet on "):].strip()
                else:
                    branch = head.split("...")[0].split(" ")[0] if head else ""
                res["branch"] = branch
                res["clean"] = not any(l.strip() and not l.startswith("## ") for l in lines)
                top = self._git_run(["rev-parse", "--show-toplevel"], root, timeout)
                if top.get("ok"):
                    res["repo"] = (top.get("stdout") or "").strip()
        elif action == "diff":
            argv = ["diff"]
            if staged:
                argv.append("--cached")
            if stat:
                argv.append("--stat")
            if rev:
                argv.append(str(rev))
            if rel:
                argv += ["--", rel]
            res = self._git_run(argv, root, timeout)
        elif action == "log":
            argv = ["log", "--oneline", "--decorate", "-n", str(max(1, min(int(limit or 15), 200)))]
            if rev:
                argv.append(str(rev))
            if rel:
                argv += ["--", rel]
            res = self._git_run(argv, root, timeout)
            if res.get("ok"):
                res["commits"] = len([l for l in (res.get("stdout") or "").splitlines() if l.strip()])
        elif action == "show":
            argv = ["show", "--stat", "--decorate", str(rev or "HEAD")]
            if rel:
                argv += ["--", rel]
            res = self._git_run(argv, root, timeout)
        elif action == "add":
            if all:
                # git add -A из подпапки цепляет весь репозиторий: разрешаем только из его корня
                top = self._git_run(["rev-parse", "--show-toplevel"], root, timeout)
                top_path = (top.get("stdout") or "").strip()
                same = False
                if top_path:
                    try:
                        same = os.path.samefile(top_path, root)
                    except OSError:
                        same = (os.path.normcase(os.path.normpath(top_path))
                                == os.path.normcase(os.path.normpath(root)))
                if top_path and not same:
                    return {"ok": False,
                            "error": "all=true только из корня репозитория: %s" % top_path,
                            "hint": "укажи path=... или cwd=<корень репозитория>"}
                argv = ["add", "-A"]
            elif rel:
                argv = ["add", "--", rel]
            else:
                return {"ok": False, "error": "укажи path или all=true"}
            res = self._git_run(argv, root, timeout)
            if res.get("ok"):
                names = self._git_run(["diff", "--cached", "--name-only"], root, timeout)
                staged_list = [l for l in (names.get("stdout") or "").splitlines() if l.strip()]
                res["staged"] = staged_list[:50]
                res["staged_count"] = len(staged_list)
        elif action == "commit":
            if not (message or "").strip():
                return {"ok": False, "error": "нужен message"}
            res = self._git_run(["commit", "-m", str(message)], root, timeout)
            if res.get("ok"):
                head = self._git_run(["show", "-s", "--oneline", "HEAD"], root, timeout)
                res["commit"] = (head.get("stdout") or "").strip()
        else:  # branch
            if name:
                name = str(name).strip()
                exists = self._git_run(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % name], root, timeout)
                if exists.get("exit") == 0:
                    res = self._git_run(["switch", name], root, timeout)
                else:
                    res = self._git_run(["switch", "-c", name], root, timeout)
            else:
                res = self._git_run(["branch", "--list", "--no-color"], root, timeout)
        res = self._git_friendly(res, root)
        res["root"] = root
        return res

    # ---------- interpreter ----------
    def python(self, code=None, timeout=30, **kw):
        if not code:
            return {"ok": False, "error": "пустой код"}
        return self.repl.run(code, timeout=float(timeout))

    # ---------- web ----------
    def web_search(self, query=None, count=8, engine=None, **kw):
        """Ищет сразу по нескольким источникам: поисковики (DuckDuckGo, Bing, свой SearXNG
        если включён в настройках) и базы (Wikipedia, StackOverflow, GitHub, arXiv, npm,
        PyPI, Hacker News)."""
        try:
            searxng_url = None
            if self.cfg.get("search_searxng"):
                searxng_url = (self.cfg.get("search_searxng_url") or "").strip() or "http://127.0.0.1:8080"
            res = search_mod.search(query or "", count=int(count or 8),
                                    engine=engine or self.cfg.get("search_engine", "auto"),
                                    searxng_url=searxng_url)
            if not res["results"]:
                res["note"] = ("пусто; попробуй другой запрос, другой engine или web_fetch по прямому URL. "
                               "Ошибки источников: %s" % (res.get("errors") or "нет"))
            return {"ok": True, **res}
        except Exception as exc:
            return {"ok": False, "error": "поиск не удался: %s (%s). getaddrinfo сломан, но обход через IP должен работать" % (type(exc).__name__, exc)}

    def web_fetch(self, url=None, limit=12000, **kw):
        try:
            return {"ok": True, **netio.web_fetch(url or "", limit=int(limit))}
        except Exception as exc:
            return {"ok": False, "error": "загрузка не удалась: %s (%s)" % (type(exc).__name__, exc)}

    def download(self, url=None, path=None, **kw):
        try:
            res = netio.request("GET", url or "", timeout=120.0, max_bytes=200_000_000)
            if res["status"] >= 400:
                return {"ok": False, "error": "HTTP %s" % res["status"]}
            name = os.path.basename((url or "").split("?")[0]) or "download.bin"
            target = resolve_path(path or os.path.join(self.workspace, name), self.workspace)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(res["body"])
            return {"ok": True, "path": target, "bytes": os.path.getsize(target), "status": res["status"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser(self, action="tabs", url=None, selector=None, value=None, tab_id=None, **kw):
        cdp = self.cfg.get("chrome_cdp")
        try:
            if action == "tabs":
                return {"ok": True, "tabs": browser.tabs(cdp)}
            if action == "open":
                import requests
                requests.get(cdp.rstrip("/") + "/json/new?" + url, timeout=6)
                return {"ok": True, "opened": url}
            if action == "read":
                page = browser.read_page(url=url, tab_id=tab_id, cdp=cdp)
                page["text"] = truncate(page.get("text", ""), MAX_OUT)[0]
                return {"ok": True, **page}
            if action == "click":
                return {"ok": True, "result": browser.click(selector, tab_id=tab_id, cdp=cdp)}
            if action == "fill":
                return {"ok": True, "result": browser.fill(selector, value or "", tab_id=tab_id, cdp=cdp)}
        except Exception as exc:
            message = str(exc)
            if "9222" in (cdp or "") or "Connection refused" in message or "NewConnectionError" in message:
                return {"ok": False, "error": (
                    "Chrome недоступен: не запущен браузер с отладочным портом %s (или порт закрыт). "
                    "Перезагружать компьютер и сбрасывать сеть НЕЛЬЗЯ — это уронит машину пользователя. "
                    "Дальше: возьми ту же страницу через web_fetch (обычный HTTP), или web_search по её "
                    "теме, или попроси пользователя открыть Chrome с ключом --remote-debugging-port=%s."
                    % ((cdp or "").rsplit(":", 1)[-1], (cdp or "").rsplit(":", 1)[-1]))}
            return {"ok": False, "error": "Chrome/CDP: %s" % message}
        return {"ok": False, "error": "неизвестное действие %s" % action}

    # ---------- desktop ----------
    def screenshot(self, region="screen", title=None, hwnd=None, x=None, y=None, w=None, h=None,
                   delay=0, focus=True, **kw):
        """Снимок: screen — весь виртуальный экран, window — окно по title/hwnd,
        active — переднее окно, rect — область x,y,w,h. delay — пауза перед снимком."""
        if delay:
            time.sleep(max(0.0, min(float(delay), 10.0)))
        region = str(region or "screen").lower()
        rect = None
        if region in ("window", "active"):
            if os.name != "nt":
                return {"ok": False, "error": "снимок окна доступен только на Windows"}
            u = _user32()
            handle = u.GetForegroundWindow() if region == "active" else (int(hwnd) if hwnd else None)
            if handle is None and title:
                for item in _win(self, "list").get("windows", []):
                    if str(title).lower() in item["title"].lower():
                        handle = item["hwnd"]
                        break
            if not handle:
                return {"ok": False, "error": "окно не найдено — дай title или hwnd из windows(action='list')"}
            if focus:
                u.ShowWindow(handle, 9)
                u.SetForegroundWindow(handle)
                time.sleep(0.5)
            r = RECT()
            try:
                # DWM-границы без невидимой рамки изменения размера (Win10+)
                fr = RECT()
                if ctypes.windll.dwmapi.DwmGetWindowAttribute(handle, 9, ctypes.byref(fr), ctypes.sizeof(fr)) == 0:
                    r = fr
            except Exception:
                pass
            if not r.right and not u.GetWindowRect(handle, ctypes.byref(r)):
                return {"ok": False, "error": "не удалось получить границы окна"}
            if r.left <= -30000 or r.top <= -30000:
                return {"ok": False, "error": "окно свёрнуто — разверни его (windows action='focus') и повтори"}
            rect = (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
            if rect[2] <= 0 or rect[3] <= 0:
                return {"ok": False, "error": "нулевой размер окна"}
        elif region == "rect":
            if None in (x, y, w, h):
                return {"ok": False, "error": "для region='rect' нужны x, y, w, h"}
            rect = (int(x), int(y), int(w), int(h))
            if rect[2] <= 0 or rect[3] <= 0:
                return {"ok": False, "error": "w и h должны быть положительными"}
        elif region != "screen":
            return {"ok": False, "error": "region: screen|window|active|rect, не %s" % region}
        path = os.path.join(self.shot_dir, "%s_%s.png" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4]))
        if rect:
            rx, ry, rw, rh = rect
            code = (
                "Add-Type -AssemblyName System.Drawing;"
                "$bmp=New-Object System.Drawing.Bitmap(%d,%d);"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen(%d,%d,0,0,$bmp.Size);"
                "$bmp.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png);"
                "Write-Output ('%dx%d')" % (rw, rh, rx, ry, path.replace("'", ""), rw, rh)
            )
        else:
            code = (
                "Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
                "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
                "$bmp.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png);"
                "Write-Output ('{0}x{1}' -f $b.Width, $b.Height)" % (path.replace("'", ""),)
            )
        res = self.shell(cmd=code, timeout=45)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("stderr") or res.get("error")}
        return {"ok": True, "path": path, "size": res.get("stdout", "").strip(), "region": region, "rect": rect,
                "note": "скриншот сохранён; vision-модель получает его автоматически, текстовой опиши словами, что проверять"}

    def windows(self, action="list", title=None, hwnd=None, combo=None, text=None, x=None, y=None, **kw):
        return _win(self, action=action, title=title, hwnd=hwnd, combo=combo, text=text, x=x, y=y)

    def clipboard(self, action="get", text=None, **kw):
        if action == "set":
            if text is None:
                return {"ok": False, "error": "нужен text"}
            esc = str(text).replace("'", "''")
            return self.shell(cmd="Set-Clipboard -Value '%s'" % esc, timeout=20)
        res = self.shell(cmd="Get-Clipboard | Out-String", timeout=20)
        return {"ok": res.get("ok"), "text": truncate(res.get("stdout", ""), MAX_OUT)[0]}

    def sysinfo(self, action="overview", **kw):
        try:
            import psutil
        except ImportError:
            return {"ok": False, "error": "psutil не установлен"}
        if action == "processes":
            rows = []
            for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
                try:
                    rows.append({"pid": p.info["pid"], "name": p.info["name"],
                                 "mem": round(p.info["memory_percent"] or 0, 1), "cpu": round(p.info["cpu_percent"] or 0, 1)})
                except Exception:
                    continue
            rows.sort(key=lambda r: (r["mem"] or 0), reverse=True)
            return {"ok": True, "count": len(rows), "top_by_memory": rows[:25]}
        if action == "ports":
            listeners = {}
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.status == "LISTEN":
                    listeners["%s:%d" % (c.laddr.ip, c.laddr.port)] = c.pid
            return {"ok": True, "listeners": [{"addr": k, "pid": v} for k, v in sorted(listeners.items())]}
        if action == "disks":
            return {"ok": True, "disks": [{"mount": part.mountpoint,
                                           "used_pct": psutil.disk_usage(part.mountpoint).percent}
                                          for part in psutil.disk_partitions()]}
        if action == "env":
            return {"ok": True, "computer": os.environ.get("COMPUTERNAME"), "user": os.environ.get("USERNAME"),
                    "python": sys.version.split()[0], "path_sep": os.sep}
        if action == "gpu":
            res = self.shell(cmd="nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader", timeout=15)
            return {"ok": res.get("ok"), "nvidia_smi": res.get("stdout") or res.get("stderr")}
        vm = psutil.virtual_memory()
        return {"ok": True, "cpu_pct": psutil.cpu_percent(), "cores": psutil.cpu_count(),
                "ram_used_pct": vm.percent, "ram_total_gb": round(vm.total / 1073741824, 1),
                "ram_free_gb": round(vm.available / 1073741824, 1),
                "uptime_min": round((time.time() - psutil.boot_time()) / 60)}

    def kill_process(self, pid=None, name=None, **kw):
        try:
            import psutil
        except ImportError:
            return {"ok": False, "error": "нет psutil"}
        killed = []
        if pid:
            try:
                psutil.Process(int(pid)).terminate()
                killed.append(int(pid))
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        elif name:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if (proc.info["name"] or "").lower() == name.lower():
                        proc.terminate()
                        killed.append(proc.info["pid"])
                except Exception:
                    continue
            if not killed:
                return {"ok": False, "error": "процесс %s не найден" % name}
        else:
            return {"ok": False, "error": "нужен pid или name"}
        return {"ok": True, "terminated": killed}

    def lmstudio(self, action="status", model=None, **kw):
        prov = self.cfg["providers"][self.cfg["provider"]]
        lms = prov.get("lms_cli")
        from . import llm as llm_mod
        if action in ("status", "models"):
            try:
                client = llm_mod.LLM(self.cfg)
                return {"ok": True, "base": client.base, "models": client.models(), "alive": client.alive()}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        if not lms or not os.path.exists(lms):
            return {"ok": False, "error": "lms.exe не найден по %s" % lms}
        cmd = {
            "start": '"%s" server start' % lms,
            "stop": '"%s" server stop' % lms,
            "load": '"%s" load "%s"' % (lms, model or ""),
            "unload": '"%s" unload --all' % lms,
            "ps": '"%s" ps' % lms,
        }.get(action)
        if not cmd:
            return {"ok": False, "error": "действие %s неизвестно" % action}
        return self.shell(cmd=cmd, timeout=120, mode="raw")

    def remember(self, text=None, **kw):
        notes = os.path.join(DATA_DIR, "notes.md")
        with open(notes, "a", encoding="utf-8") as fh:
            fh.write("\n- %s — %s" % (time.strftime("%Y-%m-%d %H:%M"), (text or "").strip()))
        return {"ok": True, "saved": True, "file": notes}

    def call(self, name, args):
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name in ("call", "cfg", "jobs"):
            from . import plugins
            external = plugins.call(name, self.cfg, args)
            if external is not None:
                return external
            return {"ok": False, "error": "нет такого инструмента: %s" % name}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "error": "кривые аргументы: %s" % exc}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def psutil_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return None


# ---------------- Windows desktop primitives (ctypes, no PowerShell startup) ----------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", ctypes.c_byte * 32), ("ki", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint), ("ii", INPUT_UNION)]


VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
      "right": 0x27, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22, "win": 0x5B,
      "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "f1": 0x70, "f2": 0x71, "f3": 0x72,
      "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79}


def _user32():
    return ctypes.windll.user32


def _win(toolkit, action="list", title=None, hwnd=None, combo=None, text=None, x=None, y=None):
    if os.name != "nt":
        return {"ok": False, "error": "оконные действия доступны только на Windows"}
    u = _user32()
    if action == "list":
        found = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _param):
            if not u.IsWindowVisible(hwnd):
                return True
            length = u.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            proc_id = ctypes.c_uint()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            name = ""
            try:
                import psutil
                name = psutil.Process(proc_id.value).name()
            except Exception:
                pass
            found.append({"hwnd": hwnd, "title": buf.value, "pid": proc_id.value, "process": name})
            return True

        u.EnumWindows(EnumProc(cb), 0)
        return {"ok": True, "windows": found[:60], "count": len(found)}
    if action == "focus":
        handle = int(hwnd) if hwnd else None
        if handle is None and title:
            for item in _win(toolkit, "list")["windows"]:
                if title.lower() in item["title"].lower():
                    handle = item["hwnd"]
                    break
        if not handle:
            return {"ok": False, "error": "окно не найдено — дай title или hwnd из windows(action='list')"}
        u.ShowWindow(handle, 9)
        u.SetForegroundWindow(handle)
        return {"ok": True, "hwnd": handle}
    if action == "keys":
        if not combo:
            return {"ok": False, "error": "нужен combo, например ctrl+s или enter"}
        parts = [p.strip().lower() for p in str(combo).split("+")]
        codes = [VK.get(p, ord(p.upper())) for p in parts]
        mods, key = codes[:-1], codes[-1]
        for code in mods:
            u.keybd_event(ctypes.c_ubyte(code), 0, 0, 0)
        u.keybd_event(ctypes.c_ubyte(key), 0, 0, 0)
        u.keybd_event(ctypes.c_ubyte(key), 0, 2, 0)
        for code in reversed(mods):
            u.keybd_event(ctypes.c_ubyte(code), 0, 2, 0)
        return {"ok": True, "pressed": combo}
    if action == "type":
        if text is None:
            return {"ok": False, "error": "нужен text"}
        esc = str(text).replace("'", "''")
        res = toolkit.shell(cmd="Set-Clipboard -Value '%s'" % esc, timeout=20)
        if not res.get("ok"):
            return res
        u.keybd_event(0x11, 0, 0, 0)
        u.keybd_event(0x56, 0, 0, 0)
        u.keybd_event(0x56, 0, 2, 0)
        u.keybd_event(0x11, 0, 2, 0)
        return {"ok": True, "typed_len": len(str(text)), "note": "ввод через буфер обмена + Ctrl+V; буфер перезаписан"}
    if action in ("click", "move"):
        if x is None or y is None:
            return {"ok": False, "error": "нужны координаты x,y"}
        u.SetCursorPos(int(x), int(y))
        if action == "click":
            u.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.04)
            u.mouse_event(0x0004, 0, 0, 0, 0)
        return {"ok": True, "at": [int(x), int(y)]}
    return {"ok": False, "error": "действие %s неизвестно" % action}
