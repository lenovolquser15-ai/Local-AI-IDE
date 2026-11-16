"""Удалённый доступ: временный код, подписанные cookie и WebSocket-реле.

ПК сам открывает исходящее соединение с Worker, поэтому белый IP и проброс
портов не нужны. Приложение выдаёт код, сайт меняет его на токен, а сервер
пускает запросы из интернета только с валидной подписанной cookie.
"""
import atexit
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from .config import LOCK as CONFIG_LOCK, load, save

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: без I, L, O, U
CODE_LEN = 10
CODE_RE = re.compile(r"^[%s]{%d}$" % (ALPHABET, CODE_LEN))
COOKIE = "lh_remote"

STATE = {"phase": "idle", "url": "", "error": "", "worker_error": "", "started": 0.0,
         "code": "", "exp": 0, "room": False, "connected": False, "mode": "",
         "relay": None, "port": 0, "lines": [], "rev": 0}
LOCK = threading.RLock()


def _set(**kw):
    with LOCK:
        STATE.update(kw)
        STATE["rev"] = int(STATE.get("rev") or 0) + 1


def _log(line):
    with LOCK:
        STATE["lines"].append(str(line)[:300])
        del STATE["lines"][:-40]


def remote_cfg():
    return load().get("remote") or {}


def save_cfg(**kw):
    with CONFIG_LOCK:
        cfg = load()
        current = dict(cfg.get("remote") or {})
        current.update(kw)
        cfg["remote"] = current
        save(cfg)
        return current


def secret():
    cfg = remote_cfg()
    value = str(cfg.get("secret") or "")
    if value:
        return value
    value = secrets.token_hex(32)
    save_cfg(secret=value)
    return value


def epoch():
    try:
        return int(remote_cfg().get("epoch") or 1)
    except (TypeError, ValueError):
        return 1


def sign(message):
    return hmac.new(secret().encode("utf-8"), str(message).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def worker_url():
    return str(remote_cfg().get("worker_url") or "").rstrip("/")


def mint_token(exp=None, epoch_value=None, sid=None):
    cfg = remote_cfg()
    now = int(time.time())
    if exp is None:
        exp = now + max(600, int(cfg.get("code_ttl") or 28800))
    if epoch_value is None:
        epoch_value = epoch()
    sid = sid or secrets.token_hex(8)
    message = "v1|%d|%s|%d" % (int(exp), sid, int(epoch_value))
    return "v1.%d.%s.%d.%s" % (int(exp), sid, int(epoch_value), sign(message))


def verify_token(token, now=None):
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "v1":
        return None
    try:
        exp = int(parts[1])
        ep = int(parts[3])
    except ValueError:
        return None
    if exp <= (time.time() if now is None else now):
        return None
    if ep != epoch():
        return None
    message = "v1|%d|%s|%d" % (exp, parts[2], ep)
    if not hmac.compare_digest(parts[4], sign(message)):
        return None
    return {"exp": exp, "sid": parts[2], "epoch": ep}


def cookie_token(raw):
    for chunk in str(raw or "").split(";"):
        key, _, value = chunk.strip().partition("=")
        if key == COOKIE:
            return value.strip()
    return None


def authorized(headers):
    return verify_token(cookie_token(headers.get("Cookie"))) is not None


def is_tunnel(headers):
    return bool(headers.get("CF-Ray") or headers.get("CF-Connecting-IP") or headers.get("X-LH-Relay"))


def is_https(headers):
    return bool(headers.get("CF-Ray")) or str(headers.get("X-Forwarded-Proto") or "").lower() == "https"


def make_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def format_code(code):
    code = str(code or "")
    return code[:5] + "-" + code[5:] if len(code) == CODE_LEN else code


def normalize_code(raw):
    code = re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()
    code = code.replace("O", "0").replace("I", "1").replace("L", "1").replace("U", "V")
    return code if CODE_RE.match(code) else ""


def _http(method, url, body=None, headers=None, timeout=20):
    """Идём в реле через dnsfix: системный getaddrinfo на этой машине не работает."""
    from urllib.parse import urlsplit
    from . import dnsfix
    parts = urlsplit(url)
    path = parts.path + (("?" + parts.query) if parts.query else "")
    conn = dnsfix.connection_for(url, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def register(code, url, exp, relay=False):
    worker = worker_url()
    if not worker:
        _set(room=False, worker_error="не задан адрес реле")
        return False
    body = json.dumps({"code": code, "url": url or worker, "exp": int(exp),
                       "epoch": epoch(), "relay": bool(relay)}, separators=(",", ":"))
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-LH-Ts": ts,
               "X-LH-Sign": sign(ts + "." + body)}
    error = ""
    ok = False
    try:
        status, raw = _http("POST", worker + "/api/rooms", body.encode("utf-8"),
                            headers=headers, timeout=20)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        ok = bool(status < 400 and payload.get("ok"))
        if not ok:
            error = str(payload.get("error") or "реле ответило %s" % status)[:200]
    except Exception as exc:
        error = str(exc)[:200]
    _set(room=ok, worker_error="" if ok else error)
    return ok


def unregister(code):
    worker = worker_url()
    if not worker or not code:
        return
    ts = str(int(time.time()))
    try:
        _http("DELETE", worker + "/api/rooms/" + code, timeout=10,
              headers={"X-LH-Ts": ts, "X-LH-Sign": sign(ts + "." + code)})
    except Exception:
        pass


def ensure_code(force=False):
    cfg = remote_cfg()
    ttl = max(600, int(cfg.get("code_ttl") or 28800))
    with LOCK:
        code, exp = STATE.get("code"), int(STATE.get("exp") or 0)
        mode = STATE.get("mode") or ""
    if force or not code or exp - time.time() < 300:
        code, exp = make_code(), int(time.time()) + ttl
        _set(code=code, exp=exp, room=False, worker_error="")
    url = STATE.get("url")
    if url:
        register(code, url, exp, relay=(mode == "relay"))
    return code, exp


def keep_alive():
    """Продлеваем код, пока реле на связи, иначе комната и cookie умрут по TTL."""
    cfg = remote_cfg()
    ttl = max(600, min(int(cfg.get("code_ttl") or 28800), 6 * 86400))
    with LOCK:
        code, exp = STATE.get("code"), int(STATE.get("exp") or 0)
        url, mode = STATE.get("url") or "", STATE.get("mode") or ""
    if not code or not url:
        return False
    now = int(time.time())
    margin = min(1800, max(300, ttl // 4))
    if exp - now > margin:
        return False
    fresh_exp = now + ttl
    if not register(code, url, fresh_exp, relay=(mode == "relay")):
        return False
    _set(exp=fresh_exp)
    return True


def start(port=None):
    from . import relay as relay_mod
    with LOCK:
        if STATE.get("phase") in ("starting", "running"):
            return status()
        old = STATE.get("relay")
    if old is not None:
        old.close()
    with LOCK:
        STATE.update(phase="starting", url="", error="", worker_error="", started=time.time(),
                     lines=[], code="", exp=0, room=False, connected=False, mode="relay")
    port = int(port or load().get("port", 7788))
    fresh = relay_mod.Relay(port)
    with LOCK:
        STATE["relay"] = fresh
        STATE["port"] = port
    fresh.start()
    return status()


def stop():
    with LOCK:
        relay, code = STATE.get("relay"), STATE.get("code")
    if relay is not None:
        relay.close()
        if relay.thread is not None:
            relay.thread.join(timeout=3)
    if code:
        unregister(code)
    _set(phase="idle", url="", code="", exp=0, room=False, error="", worker_error="",
         started=0.0, connected=False, mode="", relay=None)
    return status()


def rotate():
    from . import relay as relay_mod
    with LOCK:
        relay, port, old = STATE.get("relay"), STATE.get("port"), STATE.get("code")
    if relay is not None:
        relay.close()
        if relay.thread is not None:
            relay.thread.join(timeout=3)
    if old:
        unregister(old)
    save_cfg(epoch=epoch() + 1)
    with LOCK:
        STATE["code"] = ""
        STATE["exp"] = 0
        STATE["room"] = False
    ensure_code(force=True)
    if relay is not None:
        fresh = relay_mod.Relay(port or load().get("port", 7788))
        with LOCK:
            STATE["relay"] = fresh
        fresh.start()
    return status()


def status():
    cfg = remote_cfg()
    site = str(cfg.get("site_url") or "").rstrip("/")
    worker = str(cfg.get("worker_url") or "").rstrip("/")
    with LOCK:
        snapshot = {k: STATE.get(k) for k in
                    ("phase", "url", "error", "worker_error", "started", "code", "exp",
                     "room", "connected", "mode", "lines")}
    exp = int(snapshot.get("exp") or 0)
    code = snapshot.get("code") or ""
    now = int(time.time())
    direct = ""
    if worker and exp > now and snapshot.get("connected"):
        direct = "%s/api/remote/claim?rt=%s" % (worker, mint_token(exp))
    qr_url = ""
    if (site or worker) and code:
        qr_url = "%s/#c=%s" % (site or worker, code)
    elif direct:
        qr_url = direct
    return {
        "phase": snapshot["phase"],
        "running": snapshot["phase"] in ("starting", "running"),
        "mode": snapshot.get("mode") or "",
        "connected": bool(snapshot.get("connected")),
        "url": snapshot.get("url") or "",
        "code": code,
        "display": format_code(code),
        "expires": exp,
        "expires_in": max(0, exp - now),
        "site_url": site,
        "worker_url": worker,
        "room": bool(snapshot.get("room")),
        "error": snapshot.get("error") or "",
        "worker_error": snapshot.get("worker_error") or "",
        "download": 0,
        "cloudflared": "",
        "direct_url": direct,
        "qr_url": qr_url,
        "auto_start": bool(cfg.get("auto_start")),
        "lines": list(snapshot.get("lines") or [])[-5:],
    }


def autostart():
    if remote_cfg().get("auto_start"):
        start()


def _kill():
    with LOCK:
        relay = STATE.get("relay")
    if relay is not None:
        relay.close()


atexit.register(_kill)
