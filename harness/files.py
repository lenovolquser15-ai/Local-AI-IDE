"""Root-scoped mobile file editing with a separate LAN pairing cookie."""
import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
import threading
import time

from . import config as config_mod

COOKIE = "lh_files"
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_TEXT_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 1000
_LOCK = threading.RLock()
_PAIR = {"hash": "", "exp": 0.0}
_ATTEMPTS = {}


class FilesError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_local(addr):
    value = str(addr or "")
    if value.startswith("::ffff:"):
        value = value[7:]
    return value in ("127.0.0.1", "::1", "localhost")


def _settings(cfg):
    value = cfg.get("mobile_files")
    return value if isinstance(value, dict) else {}


def _root(cfg):
    configured = str(_settings(cfg).get("root") or "").strip()
    root = os.path.realpath(configured or cfg.get("workspace") or os.path.join(config_mod.ROOT, "work"))
    if not os.path.isdir(root):
        raise FilesError("Папка редактора не найдена: %s" % root, 409)
    protected = [os.path.realpath(config_mod.config_path()), os.path.realpath(config_mod.data_dir())]
    for path in protected:
        if os.path.commonpath([root, path]) == path:
            raise FilesError("Нельзя использовать config.json или папку data как корень редактора", 409)
    return root


def _clean_rel(value):
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise FilesError("Нужен путь относительно папки редактора")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FilesError("Недопустимый путь: %s" % raw)
    return "/".join(parts)


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _protected_path(path):
    protected = [os.path.realpath(config_mod.config_path()), os.path.realpath(config_mod.data_dir())]
    return any(_inside(path, item) for item in protected)


def _safe_path(cfg, value, expect="any"):
    root = _root(cfg)
    rel = _clean_rel(value)
    current = root
    parts = rel.split("/") if rel else []
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise FilesError("Символические ссылки в редакторе запрещены: %s" % rel)
        if index < len(parts) - 1 and not os.path.isdir(current):
            raise FilesError("Папка не найдена: %s" % "/".join(parts[:index + 1]), 404)
    if not _inside(os.path.realpath(current), root):
        raise FilesError("Путь выходит за папку редактора")
    if _protected_path(os.path.realpath(current)):
        raise FilesError("Системные файлы LocalHarness защищены от редактирования")
    if expect == "dir" and not os.path.isdir(current):
        raise FilesError("Папка не найдена: %s" % rel, 404)
    if expect == "file" and not os.path.isfile(current):
        raise FilesError("Файл не найден: %s" % rel, 404)
    return root, rel, current


def _secret(cfg):
    value = str(_settings(cfg).get("secret") or "")
    if value:
        return value
    value = secrets.token_hex(32)
    with config_mod.LOCK:
        current = config_mod.load()
        settings = dict(current.get("mobile_files") or {})
        settings["secret"] = value
        current["mobile_files"] = settings
        config_mod.save(current)
    return value


def _epoch(cfg):
    try:
        return int(_settings(cfg).get("epoch") or 1)
    except (TypeError, ValueError):
        return 1


def _sign(secret, message):
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _mint(cfg, hours=None):
    settings = _settings(cfg)
    ttl = settings.get("ttl_hours", 24)
    try:
        ttl = max(1, min(168, int(ttl)))
    except (TypeError, ValueError):
        ttl = 24
    if hours is not None:
        ttl = hours
    exp = int(time.time()) + ttl * 3600
    sid = secrets.token_hex(8)
    epoch = _epoch(cfg)
    message = "v1|%d|%s|%d" % (exp, sid, epoch)
    return "v1.%d.%s.%d.%s" % (exp, sid, epoch, _sign(_secret(cfg), message)), exp


def _cookie_token(headers):
    for chunk in str(headers.get("Cookie") or "").split(";"):
        key, _, value = chunk.strip().partition("=")
        if key == COOKIE:
            return value.strip()
    return None


def _verify(cfg, token):
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "v1":
        return None
    try:
        exp = int(parts[1])
        epoch = int(parts[3])
    except ValueError:
        return None
    if exp <= time.time() or epoch != _epoch(cfg):
        return None
    message = "v1|%d|%s|%d" % (exp, parts[2], epoch)
    return hmac.compare_digest(parts[4], _sign(_secret(cfg), message))


def cookie_header(token, secure=False):
    value = "%s=%s; Path=/api/files; HttpOnly; SameSite=Strict; Max-Age=%d" % (
        COOKIE, token, max(0, int(token.split(".")[1]) - int(time.time())))
    if secure:
        value += "; Secure"
    return value


def authorized(addr, headers, cfg, write=False):
    if write and not bool(_settings(cfg).get("write", True)):
        return False
    if not write and not bool(_settings(cfg).get("read", True)):
        return False
    if is_local(addr):
        return True
    return bool(cfg.get("lan_access") and _verify(cfg, _cookie_token(headers)))


def _new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def pair_status(cfg, addr, headers=None, lan_url=""):
    local = is_local(addr)
    allowed = authorized(addr, headers or {}, cfg)
    result = {"ok": True, "local": local, "authorized": allowed,
              "lan_access": bool(cfg.get("lan_access")), "pair_required": bool(cfg.get("lan_access") and not local)}
    if allowed:
        result.update({"root": _root(cfg), "read": bool(_settings(cfg).get("read", True)),
                       "write": bool(_settings(cfg).get("write", True))})
    if local and cfg.get("lan_access"):
        with _LOCK:
            if not _PAIR.get("code") or _PAIR.get("exp", 0) <= time.time():
                code = _new_code()
                _PAIR.update(code=code, hash=hashlib.sha256(code.encode("ascii")).hexdigest(), exp=time.time() + 300)
            code = _PAIR["code"]
            expires = _PAIR["exp"]
        result.update({"code": code, "display": code[:5] + "-" + code[5:],
                       "pair_url": (lan_url or "").rstrip("/") + "/files.html#pair=" + code,
                       "pair_expires_in": max(0, int(expires - time.time()))})
    return result


def desktop_pair_status(cfg, lan_url=""):
    return pair_status(cfg, "127.0.0.1", lan_url=lan_url)


def rotate_pair(cfg):
    with config_mod.LOCK:
        current = config_mod.load()
        settings = dict(current.get("mobile_files") or {})
        settings["epoch"] = _epoch(current) + 1
        current["mobile_files"] = settings
        config_mod.save(current)
    with _LOCK:
        _PAIR.clear()
    return desktop_pair_status(config_mod.load())


def pair(cfg, addr, raw_code):
    if not cfg.get("lan_access"):
        raise FilesError("Сначала включите доступ по Wi-Fi", 409)
    code = re.sub(r"[^0-9A-Za-z]", "", str(raw_code or "")).upper()
    now = time.time()
    with _LOCK:
        recent = [stamp for stamp in _ATTEMPTS.get(addr, []) if now - stamp < 60]
        if len(recent) >= 5:
            raise FilesError("Слишком много попыток. Подождите минуту", 429)
        _ATTEMPTS[addr] = recent
        valid = bool(code and _PAIR.get("hash") and _PAIR.get("exp", 0) > now)
        if valid:
            valid = hmac.compare_digest(hashlib.sha256(code.encode("ascii")).hexdigest(), _PAIR["hash"])
        if not valid:
            recent.append(now)
            raise FilesError("Неверный или просроченный код", 401)
        _PAIR.clear()
    token, exp = _mint(cfg)
    return {"ok": True, "expires": exp, "cookie": cookie_header(token)}


def set_config(cfg, values):
    with config_mod.LOCK:
        current = config_mod.load()
        settings = dict(current.get("mobile_files") or {})
        if "root" in values:
            raw = str(values.get("root") or "").strip()
            if raw and not os.path.isdir(raw):
                raise FilesError("Папка не найдена: %s" % raw)
            settings["root"] = os.path.realpath(raw) if raw else ""
        for key in ("read", "write"):
            if key in values:
                if not isinstance(values[key], bool):
                    raise FilesError("%s должен быть true или false" % key)
                settings[key] = values[key]
        if "ttl_hours" in values:
            try:
                settings["ttl_hours"] = max(1, min(168, int(values["ttl_hours"])))
            except (TypeError, ValueError):
                raise FilesError("ttl_hours должен быть числом")
        current["mobile_files"] = settings
        config_mod.save(current)
    return desktop_pair_status(config_mod.load())


def list_dir(cfg, value=""):
    root, rel, target = _safe_path(cfg, value, expect="dir")
    rows = []
    truncated = False
    with os.scandir(target) as entries:
        for entry in entries:
            path = os.path.realpath(entry.path)
            if _protected_path(path):
                continue
            try:
                if entry.is_symlink():
                    kind = "link"
                    size = 0
                elif entry.is_dir(follow_symlinks=False):
                    kind = "dir"
                    size = 0
                elif entry.is_file(follow_symlinks=False):
                    kind = "file"
                    size = entry.stat(follow_symlinks=False).st_size
                else:
                    continue
                rows.append({"name": entry.name, "path": (rel + "/" if rel else "") + entry.name,
                             "type": kind, "size": size, "mtime": entry.stat(follow_symlinks=False).st_mtime})
            except OSError:
                continue
    rows.sort(key=lambda row: (row["type"] != "dir", row["name"].lower()))
    if len(rows) > MAX_LIST_ENTRIES:
        truncated = True
        rows = rows[:MAX_LIST_ENTRIES]
    return {"ok": True, "root": root, "path": rel, "entries": rows, "truncated": truncated}


def _read_bytes(path):
    if os.path.getsize(path) > MAX_TEXT_BYTES:
        raise FilesError("Файл больше 1 МБ — мобильный редактор открывает только текстовые файлы", 413)
    with open(path, "rb") as fh:
        data = fh.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise FilesError("Файл больше 1 МБ", 413)
    if b"\x00" in data:
        raise FilesError("Это бинарный файл — он не открывается в текстовом редакторе")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        raise FilesError("Файл не в кодировке UTF-8 — редактор не будет портить кодировку")


def read_file(cfg, value):
    root, rel, target = _safe_path(cfg, value, expect="file")
    data, text = _read_bytes(target)
    return {"ok": True, "root": root, "path": rel, "text": text, "size": len(data),
            "revision": hashlib.sha256(data).hexdigest(), "mtime": os.path.getmtime(target)}


def save_file(cfg, value, text, revision):
    if not isinstance(text, str):
        raise FilesError("text должен быть строкой")
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise FilesError("Файл больше 1 МБ — сохрани его через проводник на ПК", 413)
    with _LOCK:
        root, rel, target = _safe_path(cfg, value, expect="any")
        exists = os.path.exists(target)
        if exists:
            if not os.path.isfile(target):
                raise FilesError("Путь занят папкой")
            current, _ = _read_bytes(target)
            current_revision = hashlib.sha256(current).hexdigest()
            if not revision or revision != current_revision:
                raise FilesError("Файл изменился на ПК после открытия. Переоткрой его и перенеси свои правки.", 409)
        elif revision is not None:
            raise FilesError("Файл больше не существует — переоткри список", 409)
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            raise FilesError("Родительская папка не найдена", 404)
        fd, tmp = tempfile.mkstemp(prefix=".lh-files-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if exists:
                os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return read_file(cfg, rel)
