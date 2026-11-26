"""Local HTTP server: SSE event stream for the web UI, plus provider autostart."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dnsfix
from . import LOCALHARNESS
from . import llm as llm_mod
from . import lms as lms_mod
from . import memory as mem_mod
from . import plugins
from . import remote
from . import schemas
from . import session as session_mod
from . import sizing as sizing_mod
from . import thermal as thermal_mod
from . import vision as vision_mod
from . import dashboard as dashboard_mod
from . import cluster as cluster_mod
from . import obscura_browser as obscura_mod
from . import backup as backup_mod
from . import scheduler as scheduler_mod
from . import updater as updater_mod
from . import notify as notify_mod
from . import rag as rag_mod
from . import mcp_client as mcp_mod
from . import voice as voice_mod
from . import unsloth_trainer as train_mod
from . import files as files_mod


def _obscura_status_safe(cfg):
    try:
        return obscura_mod.status(cfg)
    except Exception:
        return {"available": False, "installed": False, "running": False, "exe": "", "cdp_url": "http://127.0.0.1:9223"}
from .agent import Agent, Bridge
from .config import (DATA_DIR, ROOT, LOCK as CONFIG_LOCK, load, save, _merge,
                     as_int, normalize_context, MAX_TOKENS_MIN, MAX_TOKENS_MAX)

RUNS = {}
RUNS_LOCK = threading.Lock()
STARTED = time.time()

# Смена lan_access меняет адрес привязки (127.0.0.1 ↔ 0.0.0.0), а живой сокет
# перепривязать нельзя: сервер закрывается и открывается заново на том же порту.
_RESTART = threading.Event()
_RESTARTER = {"fn": None}
_RESTARTER_LOCK = threading.Lock()

# события, которые живут только в прямом эфире и не засоряют журнал сессии
TRANSIENT = {"delta", "reason", "speed", "tool_start"}

# provider probe cache: /api/state is polled, and a cold /models with 15s timeout hurts the UI
_PROBE = {"at": 0.0, "value": None, "lock": threading.Lock()}
_PROBE_TTL = 3.0

# LM Studio long jobs (download / load / import): one at a time, log tail for the UI
LMS_JOB = {"id": None, "kind": "", "status": "idle", "lines": [], "started": 0.0, "ended": None,
           "running": False, "rc": None}
LMS_LOCK = threading.Lock()


class Run:
    """Один прогон агента: лента событий с индексами и условием для SSE-подписчиков."""

    def __init__(self, sid):
        self.sid = sid
        self.events = []
        self.cond = threading.Condition()
        self.finished = False
        self.thread = None
        self.bridge = None

    def broadcast(self, event):
        with self.cond:
            event = dict(event)
            event["_i"] = len(self.events)
            self.events.append(event)
            if event.get("type") in ("done", "stopped"):
                self.finished = True
            self.cond.notify_all()


class SseBridge(Bridge):
    """Мост агента в веб: durable-события пишутся в журнал сессии, всё уходит в эфир."""

    def __init__(self, run):
        Bridge.__init__(self)
        self.run = run

    def emit(self, event):
        if isinstance(event, dict) and event.get("type") not in TRANSIENT:
            try:
                session_mod.event(self.run.sid, event)
            except Exception:
                pass
        self.run.broadcast(event)


_DNS_PROBE = {"value": None, "at": 0.0, "busy": False, "lock": threading.Lock()}
_DNS_PROBE_TTL = 60.0


def cached_dns_probe():
    now = time.time()
    if _DNS_PROBE["value"] is not None and now - _DNS_PROBE["at"] < _DNS_PROBE_TTL:
        return _DNS_PROBE["value"]

    def _refresh():
        try:
            val = {"ok": True, "ip": dnsfix.resolve("html.duckduckgo.com")}
        except Exception as exc:
            val = {"ok": False, "error": str(exc)}
        with _DNS_PROBE["lock"]:
            _DNS_PROBE["value"] = val
            _DNS_PROBE["at"] = time.time()
            _DNS_PROBE["busy"] = False

    with _DNS_PROBE["lock"]:
        if _DNS_PROBE["value"] is not None and now - _DNS_PROBE["at"] < _DNS_PROBE_TTL:
            return _DNS_PROBE["value"]
        if not _DNS_PROBE["busy"]:
            _DNS_PROBE["busy"] = True
            t = threading.Thread(target=_refresh, daemon=True)
            t.start()
        else:
            t = None
    if _DNS_PROBE["value"] is None and t is not None:
        t.join(timeout=1.5)
    return _DNS_PROBE["value"] or {"ok": True, "ip": "cached"}


def cached_provider_status(cfg):
    now = time.time()
    if _PROBE["value"] is not None and now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["value"]
    with _PROBE["lock"]:
        if _PROBE["value"] is not None and time.time() - _PROBE["at"] < _PROBE_TTL:
            return _PROBE["value"]
        value = provider_status(cfg)
        _PROBE["value"] = value
        _PROBE["at"] = time.time()
        return value


def _lms_snapshot():
    with LMS_LOCK:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in LMS_JOB.items()}


def _lms_line(job, text):
    for chunk in str(text).replace("\r", "\n").split("\n"):
        chunk = chunk.strip()
        if chunk:
            job["lines"].append(chunk[:400])
    del job["lines"][:-80]


_LAN_IP = {"value": "", "at": 0.0, "lock": threading.Lock()}
_LAN_IP_TTL = 15.0

# виртуальные адаптеры: их адрес не откроется с телефона в той же Wi-Fi
_VIRTUAL_IFACES = ("vethernet", "wsl", "hyper-v", "virtualbox", "vmware", "loopback", "bluetooth",
                   "throne", "zero", "zerotier", "radmin", "tailscale", "hamachi", "vpn", "tun", "tap",
                   "docker", "br-", "utun", "wg", "wireguard", "npcap", "teredo", "isatap")


def _lan_candidates():
    """Приватные IPv4 с метрикой «насколько похоже на домашнюю сеть»."""
    import ipaddress
    import socket
    try:
        import psutil
    except Exception:
        return []
    stats = {}
    try:
        stats = psutil.net_if_stats()
    except Exception:
        pass
    found = []
    for name, addrs in psutil.net_if_addrs().items():
        low = name.lower()
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        if any(bad in low for bad in _VIRTUAL_IFACES):
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.address:
                continue
            try:
                ip = ipaddress.ip_address(a.address)
            except ValueError:
                continue
            if not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
            text = str(ip)
            score = 100
            if text.startswith("192.168."):
                score = 0
            elif text.startswith("10."):
                score = 10
            elif text.startswith("172."):
                score = 20
            if any(word in low for word in ("wi-fi", "wifi", "wlan", "ethernet", "eth")):
                score -= 5
            found.append((score, text))
    found.sort()
    return [ip for _, ip in found]


def get_lan_ip():
    with _LAN_IP["lock"]:
        if _LAN_IP["value"] and time.time() - _LAN_IP["at"] < _LAN_IP_TTL:
            return _LAN_IP["value"]
        candidates = _lan_candidates()
        ip = candidates[0] if candidates else ""
        if not ip:
            # UDP-трюк знает только маршрут наружу: VPN-адаптер может оказаться первым
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
            except Exception:
                ip = "127.0.0.1"
        _LAN_IP["value"] = ip
        _LAN_IP["at"] = time.time()
        return ip


def is_allowed_client(addr, cfg):
    """Loopback всегда; при lan_access — только приватные адреса (LAN/VPN), не интернет."""
    addr = str(addr or "")
    if addr.startswith("::ffff:"):
        addr = addr[7:]
    if addr in ("127.0.0.1", "::1", "localhost"):
        return True
    if not cfg.get("lan_access", False):
        return False
    try:
        import ipaddress
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    # LAN, VPN и link-local: всё, что не является публичным адресом интернета
    return not ip.is_global


def make_qr_svg(url):
    try:
        import io
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(url, image_factory=factory, box_size=6)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


def lms_cli(cfg):
    return lms_mod.cli_path(cfg)


def lms_state(cfg):
    """Две плашки LM Studio: что в памяти (ps) и что на диске (ls --json) + оценка совместимости."""
    base, alive, err = "", False, ""
    try:
        client = llm_mod.LLM(cfg)
        base = client.base
        alive = client.alive()
    except Exception as exc:
        err = str(exc)[:200]
    disk, disk_err = lms_mod.disk_models(cfg)
    loaded, ps_err = lms_mod.loaded_models(cfg)
    hw = sizing_mod.get_hardware_profile()
    ctx_len = normalize_context(cfg.get("lms_context_length") or cfg.get("context_tokens"), 8192)
    enriched_disk = [sizing_mod.enrich_model_item(m, context_length=ctx_len, hw=hw) for m in (disk or [])]
    enriched_loaded = [sizing_mod.enrich_model_item(m, context_length=m.get("contextLength") or ctx_len, hw=hw)
                       for m in (loaded or [])]
    return {"base": base, "alive": alive, "disk": enriched_disk, "loaded": enriched_loaded,
            "hardware": hw, "error": err or disk_err or ps_err or "", "job": _lms_snapshot()}


def start_lms_job(kind, args, timeout=3600):
    cli = lms_cli(load())
    if not cli:
        return {"ok": False, "error": "lms.exe не найден (config providers.lmstudio.lms_cli)"}
    with LMS_LOCK:
        if LMS_JOB.get("running"):
            return {"ok": False, "error": "уже выполняется: %s" % LMS_JOB.get("kind"), "job": _lms_snapshot()}
        LMS_JOB.update({"id": "%s-%d" % (kind, int(time.time())), "kind": kind, "status": "running",
                        "lines": [], "started": time.time(), "ended": None, "running": True, "rc": None})
    flags = 0x08000000 if os.name == "nt" else 0

    def runner():
        try:
            proc = subprocess.Popen([cli] + list(args), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=flags)
            deadline = time.time() + timeout
            for raw in proc.stdout:
                _lms_line(LMS_JOB, raw.decode("utf-8", "replace"))
                if time.time() > deadline:
                    proc.kill()
                    _lms_line(LMS_JOB, "таймаут %s с — убил процесс" % timeout)
                    break
            rc = proc.wait(timeout=30)
            with LMS_LOCK:
                LMS_JOB["rc"] = rc
                LMS_JOB["status"] = "done" if rc == 0 else "failed (код %s)" % rc
                LMS_JOB["ended"] = time.time()
                LMS_JOB["running"] = False
        except Exception as exc:
            with LMS_LOCK:
                LMS_JOB["status"] = "упал: %s" % exc
                LMS_JOB["ended"] = time.time()
                LMS_JOB["running"] = False
        finally:
            lms_mod.invalidate()
            with _PROBE["lock"]:
                _PROBE["at"] = 0.0

    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "job": _lms_snapshot()}


def provider_status(cfg):
    """Модели для выпадающего списка: API + скачанные на диск (JIT загрузит их по запросу)."""
    client = llm_mod.LLM(cfg)
    base = client.base
    api_models, alive, err = [], False, ""
    try:
        api_models = client.models()
        alive = True
    except Exception as exc:
        err = str(exc)[:300]
    chat = [m for m in api_models if m and "embedding" not in m.lower()]
    disk = []
    if cfg.get("provider") == "lmstudio":
        try:
            disk, _disk_err = lms_mod.disk_models(cfg, timeout=8)
        except Exception:
            disk = []
    for item in disk:
        key = lms_mod.model_key(item)
        if not key or item.get("type") == "embedding" or "embedding" in key.lower():
            continue
        if key not in chat:
            chat.append(key)
    current = cfg.get("model")
    if current and current not in chat and "embedding" not in current.lower():
        chat.insert(0, current)
    vision_models = []
    for m in chat:
        low = m.lower()
        flagged = False
        for item in disk:
            if not item.get("vision"):
                continue
            mk = lms_mod.model_key(item).lower()
            if mk and (mk == low or mk in low or low in mk):
                flagged = True
                break
        if flagged or any(h in low for h in vision_mod.VISION_HINTS):
            vision_models.append(m)
    ok = alive or bool(chat)
    return {"ok": ok, "label": client.label, "base": base, "alive": alive,
            "models": chat, "current": current, "vision_models": vision_models,
            "error": "" if ok else err}


def ensure_provider(cfg, wait=45.0):
    """Разбудить провайдера: GUI через explorer (иначе процесс умирает без рабочего стола),
    затем при необходимости lms server start."""
    status = provider_status(cfg)
    if status["alive"]:
        return status
    prov = cfg["providers"][cfg["provider"]]
    if not prov.get("autostart"):
        return status
    exe = prov.get("exe")
    if exe and os.path.exists(exe):
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", exe])
            else:
                subprocess.Popen([exe])
        except Exception:
            pass
    cli_tried = False
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.5)
        status = provider_status(cfg)
        if status["alive"]:
            return status
        if not cli_tried and cfg.get("provider") == "lmstudio" and time.time() > deadline - wait / 2:
            cli_tried = True
            threading.Thread(target=lms_mod.server_start, args=(cfg, 20), daemon=True).start()
    return provider_status(cfg)


def bind_address(cfg=None):
    cfg = load() if cfg is None else cfg
    host = "0.0.0.0" if cfg.get("lan_access", False) else cfg.get("host", "127.0.0.1")
    return host, int(cfg.get("port", 7788))


def make_server(cfg=None, handler=None):
    host, port = bind_address(cfg)
    srv = ThreadingHTTPServer((host, port), handler or Handler)
    srv.daemon_threads = True
    return srv


def register_restarter(fn):
    with _RESTARTER_LOCK:
        _RESTARTER["fn"] = fn


def request_restart():
    """Перезапустить приём соединений с новым адресом привязки.

    Обработчик уже отправил ответ; остановка старого сервера идёт в отдельном
    потоке с небольшой задержкой, чтобы ответ успел уйти клиенту.
    """
    _RESTART.set()
    with _RESTARTER_LOCK:
        fn = _RESTARTER["fn"]
    if fn is None:
        return False
    threading.Thread(target=fn, name="LH-restart", daemon=True).start()
    return True


def _json_response(handler, data, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


_REMOTE_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LocalHarness</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f17;
color:#e8eefc;font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:24px}
.box{max-width:420px;background:#131a26;border:1px solid #24314a;border-radius:18px;padding:26px}
h1{font-size:18px;margin:0 0 10px}p{margin:0;color:#9fb0cc}
code{color:#8fd0ff}</style>
<div class="box"><h1>%s</h1><p>%s</p></div></html>"""


def _remote_denied(handler, detail):
    body = _REMOTE_PAGE % ("Нужен код доступа", detail)
    payload = body.encode("utf-8")
    handler.send_response(401)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Robots-Tag", "noindex")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not is_allowed_client(self.client_address[0], load()):
            _json_response(self, {"ok": False, "error": "доступ с этого адреса запрещён"}, 403)
            return

        tunneled = remote.is_tunnel(self.headers)
        if tunneled and path != "/api/remote/claim" and not remote.authorized(self.headers):
            _remote_denied(self, "Открой сайт подключения и введи временный код из раздела "
                                 "«Удалённый доступ» — без него интернет-запросы закрыты.")
            return

        if path == "/api/remote/claim":
            self.serve_remote_claim(query.get("rt", [""])[0])
            return

        if path == "/api/remote/status":
            _json_response(self, remote.status())
            return

        if path == "/api/state":
            cfg = load()
            ps = cached_provider_status(cfg)
            dns_probe = cached_dns_probe()
            pl = plugins.status()
            tools_list = [spec["name"] for spec in schemas.entries(cfg.get("tool_profile", "core"), cfg)]
            loaded_items, _ = lms_mod.loaded_models(cfg) if cfg.get("provider") == "lmstudio" else ([], "")
            lms_loaded = [lms_mod.model_key(m) for m in (loaded_items or []) if lms_mod.model_key(m)]
            lan_ip = get_lan_ip()
            port = int(cfg.get("port", 7788))
            _json_response(self, {
                "version": LOCALHARNESS,
                "uptime": int(time.time() - STARTED),
                "provider": cfg["provider"],
                "providers": {k: v["label"] for k, v in cfg["providers"].items()},
                "model": cfg.get("model"),
                "tool_profile": cfg.get("tool_profile"),
                "tool_protocol": cfg.get("tool_protocol"),
                "permissions": cfg.get("permissions", {"mode": "auto"}),
                "max_steps": cfg.get("max_steps"),
                "goal_rounds": cfg.get("goal_rounds"),
                "context_tokens": cfg.get("context_tokens"),
                "lms_context_length": cfg.get("lms_context_length"),
                "lms_ttl": cfg.get("lms_ttl"),
                "workspace": cfg.get("workspace"),
                "system_prompt": cfg.get("system_prompt", ""),
                "search_searxng": cfg.get("search_searxng", False),
                "search_searxng_url": cfg.get("search_searxng_url", "http://127.0.0.1:8080"),
                "search_obscura": cfg.get("search_obscura", False),
                "obscura_path": cfg.get("obscura_path", ""),
                "obscura_cdp": cfg.get("obscura_cdp", "http://127.0.0.1:9223"),
                "obscura_status": _obscura_status_safe(cfg),
                "memory_enabled": cfg.get("memory_enabled", False),
                "memory_facts_count": len(mem_mod.list_facts()),
                "image_gen_enabled": cfg.get("image_gen_enabled", False),
                "comfyui_url": cfg.get("comfyui_url", "http://127.0.0.1:8188"),
                "lan_access": cfg.get("lan_access", False),
                "lan_ip": lan_ip,
                "lan_url": "http://%s:%d" % (lan_ip, port),
                "game_failsafe": cfg.get("game_failsafe", True),
                "virustotal_enabled": bool(cfg.get("virustotal_api_key")),
                "sessions": session_mod.list_sessions(),
                "tools": tools_list,
                "plugins": pl,
                "provider_status": ps,
                "lms_loaded": lms_loaded,
                "model_loaded": bool(lms_loaded),
                "dns_probe": dns_probe,
                "gpu_thermal": thermal_mod.get_thermal_status(cfg),
                "theme": cfg.get("theme", "dark"),
                "persona": cfg.get("persona", "default"),
                "personas": schemas.PERSONAS,
                "toast_notifications": cfg.get("toast_notifications", True),
                "rag": rag_mod.status(),
                "mcp_servers": mcp_mod.list_servers(cfg).get("servers", []),
                "voice": {
                    "engine": cfg.get("voice_engine", "auto"),
                    "xtts_url": cfg.get("voice_xtts_url", "http://127.0.0.1:8020"),
                    "edge_available": voice_mod.edge_tts_available(),
                    "voice_preset": cfg.get("voice_preset", "ru_female_warm"),
                    "voice_language": cfg.get("voice_language", "ru"),
                    "voice_speed": cfg.get("voice_speed", 1.0),
                    "voice_pitch": cfg.get("voice_pitch", 0),
                    "voice_auto_speak": cfg.get("voice_auto_speak", False),
                    "presets": voice_mod.list_presets(cfg),
                },
            })
            return

        if path == "/api/model/readiness":
            _json_response(self, lms_mod.readiness(load()))
            return

        if path == "/api/thermal/status":
            force = query.get("force", ["1"])[0] not in ("0", "false", "no")
            _json_response(self, thermal_mod.get_thermal_status(load(), force=force))
            return

        if path == "/api/dashboard/history":
            try:
                mins = float(query.get("minutes", ["10"])[0])
            except (TypeError, ValueError):
                mins = 10.0
            mins = max(1.0, min(60.0, mins))
            _json_response(self, {
                "ok": True,
                "history": dashboard_mod.get_history(minutes=mins),
                "summary": dashboard_mod.get_summary(),
            })
            return

        if path == "/api/dashboard/latest":
            _json_response(self, dashboard_mod.get_latest())
            return

        if path == "/api/dashboard/processes":
            _json_response(self, dashboard_mod.get_top_processes())
            return

        if path == "/api/dashboard/export.csv":
            try:
                mins = float(query.get("minutes", ["60"])[0])
            except (TypeError, ValueError):
                mins = 60.0
            csv_bytes = dashboard_mod.export_csv(minutes=max(1.0, min(60.0, mins))).encode("utf-8-sig")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="localharness-telemetry-{stamp}.csv"')
            self.send_header("Content-Length", str(len(csv_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(csv_bytes)
            return

        if path == "/api/cluster/status":
            cfg = load()
            _json_response(self, cluster_mod.status(cfg, lan_ip=get_lan_ip(), port=int(cfg.get("port", 7788))))
            return

        if path == "/api/cluster/poll":
            wid = query.get("worker_id", [""])[0]
            try:
                wait = float(query.get("wait", ["2.0"])[0])
            except (TypeError, ValueError):
                wait = 2.0
            _json_response(self, cluster_mod.poll_task(wid, wait=wait))
            return

        if path == "/api/backup/export":
            inc_sess = query.get("sessions", ["1"])[0] != "0"
            inc_plug = query.get("plugins", ["1"])[0] != "0"
            inc_mem = query.get("memory", ["1"])[0] != "0"
            payload = backup_mod.export_profile(
                include_sessions=inc_sess, include_plugins=inc_plug, include_memory=inc_mem
            )
            stamp = time.strftime("%Y%m%d-%H%M")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="localharness-backup-%s.zip"' % stamp)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/scheduler/list":
            _json_response(self, scheduler_mod.list_tasks())
            return

        if path == "/api/update/check":
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            _json_response(self, updater_mod.check_update(force=force))
            return

        if path == "/api/update/status":
            _json_response(self, updater_mod.download_status())
            return

        if path == "/api/notify/recent":
            _json_response(self, {"ok": True, "notifications": notify_mod.recent()})
            return

        if path == "/api/rag/status":
            _json_response(self, rag_mod.status())
            return

        if path == "/api/mcp/list":
            _json_response(self, mcp_mod.list_servers(load()))
            return

        if path == "/api/voice/status":
            _json_response(self, voice_mod.status(load()))
            return

        if path == "/api/voice/presets":
            _json_response(self, {"ok": True, "presets": voice_mod.list_presets(load())})
            return

        if path == "/api/train/status":
            _json_response(self, {
                **train_mod.job_status(),
                "datasets": train_mod.list_datasets().get("datasets", []),
                "local_models": train_mod.list_local_models().get("models", []),
                "presets": train_mod.PRESET_MODELS,
                "hf_datasets": train_mod.PRESET_HF_DATASETS,
                "chat_templates": train_mod.CHAT_TEMPLATES,
                "quant_types": train_mod.QUANT_TYPES,
                "llama_quantize_available": bool(train_mod.find_llama_tool("llama-quantize")),
            })
            return

        if path == "/api/train/local_models":
            _json_response(self, train_mod.list_local_models())
            return

        if path == "/api/train/dataset/preview":
            name = query.get("name", [""])[0]
            _json_response(self, train_mod.preview_dataset(name))
            return

        if path == "/api/train/env":
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            _json_response(self, train_mod.env_status(force=force))
            return

        if path == "/api/train/datasets":
            _json_response(self, train_mod.list_datasets())
            return

        if path == "/api/desktop":
            from . import desktop
            _json_response(self, desktop.status())
            return

        if path == "/api/session/export":
            sid = query.get("session", [""])[0]
            text = session_mod.export_markdown(sid)
            if text is None:
                _json_response(self, {"error": "no such session"}, 404)
                return
            payload = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="%s.md"' % sid)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/history":
            sid = query.get("session", [""])[0]
            data = session_mod.load(sid)
            if not data:
                _json_response(self, {"error": "no such session"}, 404)
            else:
                with RUNS_LOCK:
                    run = RUNS.get(sid)
                data["running"] = bool(run and not run.finished)
                _json_response(self, data)
            return

        if path == "/api/events":
            sid = query.get("session", [""])[0]
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            self.stream_events(sid, since)
            return

        if path == "/api/new":
            _json_response(self, {"session": session_mod.create("")})
            return

        if path == "/api/plugins":
            _json_response(self, plugins.status())
            return

        if path == "/api/plugins/catalog":
            _json_response(self, plugins.catalog())
            return

        if path == "/api/plugins/workshop":
            _json_response(self, plugins.workshop_catalog())
            return

        if path == "/api/plugins/workshop/code":
            plugin_id = query.get("id", [""])[0]
            _json_response(self, plugins.get_workshop_code(plugin_id))
            return

        if path == "/api/plugins/code":
            fname = os.path.basename(query.get("file", [""])[0])
            _json_response(self, plugins.get_plugin_code(fname))
            return

        if path == "/api/plugins/settings":
            fname = query.get("file", [""])[0]
            _json_response(self, plugins.get_plugin_settings(fname))
            return

        if path == "/api/plugins/download":
            fname = os.path.basename(query.get("file", [""])[0])
            code_path = os.path.join(plugins.PLUGIN_DIR, fname)
            if not os.path.isfile(code_path):
                code_path = os.path.join(plugins.WORKSHOP_DIR, fname)
            if not os.path.isfile(code_path):
                self.send_error(404, "File not found")
                return
            with open(code_path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/memory":
            _json_response(self, {"ok": True, "facts": mem_mod.list_facts()})
            return

        if path == "/api/comfyui/status":
            from . import comfyui
            _json_response(self, comfyui.status(load()))
            return

        if path == "/api/qrcode":
            cfg = load()
            lan_ip = get_lan_ip()
            port = int(cfg.get("port", 7788))
            url_to_encode = query.get("url", [""])[0] or ("http://%s:%d" % (lan_ip, port))
            svg_data = make_qr_svg(url_to_encode)
            if not svg_data:
                self.send_response(500)
                self.end_headers()
                return
            payload = svg_data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/lms/state":
            _json_response(self, lms_state(load()))
            return

        if path == "/api/lms/job":
            _json_response(self, _lms_snapshot())
            return

        if path == "/api/provider/start":
            try:
                wait = float(query.get("wait", ["45"])[0])
            except ValueError:
                wait = 45.0
            _json_response(self, ensure_provider(load(), wait))
            return

        if path == "/shot":
            self.serve_shot(query.get("path", [""])[0])
            return

        if path == "/api/files/status":
            cfg = load()
            lan_url = "http://%s:%d" % (get_lan_ip(), int(cfg.get("port", 7788)))
            try:
                _json_response(self, files_mod.pair_status(cfg, self.client_address[0], self.headers, lan_url=lan_url))
            except files_mod.FilesError as exc:
                _json_response(self, {"ok": False, "error": str(exc)}, exc.status)
            return

        if path == "/api/files/list":
            cfg = load()
            if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=False):
                _json_response(self, {"ok": False, "error": "требуется код сопряжения редактора файлов"}, 401)
                return
            try:
                _json_response(self, files_mod.list_dir(cfg, query.get("path", [""])[0]))
            except files_mod.FilesError as exc:
                _json_response(self, {"ok": False, "error": str(exc)}, exc.status)
            return

        if path == "/api/files/read":
            cfg = load()
            if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=False):
                _json_response(self, {"ok": False, "error": "требуется код сопряжения редактора файлов"}, 401)
                return
            try:
                _json_response(self, files_mod.read_file(cfg, query.get("path", [""])[0]))
            except files_mod.FilesError as exc:
                _json_response(self, {"ok": False, "error": str(exc)}, exc.status)
            return

        if path == "/api/files/search":
            cfg = load()
            if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=False):
                _json_response(self, {"ok": False, "error": "требуется код сопряжения редактора файлов"}, 401)
                return
            try:
                _json_response(self, files_mod.search_files(cfg, query.get("q", [""])[0]))
            except files_mod.FilesError as exc:
                _json_response(self, {"ok": False, "error": str(exc)}, exc.status)
            return

        if path in ("/", "/index.html"):
            self.serve_file("webui/index.html", "text/html")
            return
        if path in ("/worker", "/worker.html"):
            self.serve_file("webui/worker.html", "text/html")
            return
        if path in ("/files", "/files.html"):
            self.serve_file("webui/files.html", "text/html")
            return
        if path == "/app.js":
            self.serve_file("webui/app.js", "application/javascript")
            return
        if path == "/style.css":
            self.serve_file("webui/style.css", "text/css")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not is_allowed_client(self.client_address[0], load()):
            _json_response(self, {"ok": False, "error": "доступ с этого адреса запрещён"}, 403)
            return
        if remote.is_tunnel(self.headers):
            if not remote.authorized(self.headers):
                _json_response(self, {"ok": False, "error": "нужен временный код доступа"}, 401)
                return
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
                _json_response(self, {"ok": False, "error": "запрос с чужого источника"}, 403)
                return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except ValueError:
            _json_response(self, {"ok": False, "error": "битый JSON в теле запроса"}, 400)
            return
        if not isinstance(body, dict):
            body = {}

        routes = {
            "/api/chat": self.handle_chat,
            "/api/respond": self.handle_respond,
            "/api/stop": self.handle_stop,
            "/api/session/delete": self.handle_session_delete,
            "/api/session/rename": self.handle_session_rename,
            "/api/desktop/autostart": self.handle_autostart,
            "/api/config": self.handle_config,
            "/api/plugins/toggle": self.handle_plugin_toggle,
            "/api/plugins/remove": self.handle_plugin_remove,
            "/api/plugins/install": self.handle_plugin_install,
            "/api/plugins/create": self.handle_plugin_create,
            "/api/plugins/publish": self.handle_plugin_publish,
            "/api/plugins/unpublish": self.handle_plugin_unpublish,
            "/api/plugins/test_tool": self.handle_plugin_test_tool,
            "/api/plugins/generate_ai": self.handle_plugin_generate_ai,
            "/api/plugins/workshop/install": self.handle_plugin_workshop_install,
            "/api/plugins/convert_repo": self.handle_plugin_convert_repo,
            "/api/plugins/virustotal": self.handle_plugin_virustotal,
            "/api/plugins/save_code": self.handle_plugin_save_code,
            "/api/plugins/settings": self.handle_plugin_save_settings,
            "/api/dashboard/reset": self.handle_dashboard_reset,
            "/api/lms/load": self.handle_lms_load,
            "/api/lms/unload": self.handle_lms_unload,
            "/api/lms/download": self.handle_lms_download,
            "/api/lms/import": self.handle_lms_import,
            "/api/lms/estimate": self.handle_lms_estimate,
            "/api/remote/start": self.handle_remote_start,
            "/api/remote/stop": self.handle_remote_stop,
            "/api/remote/rotate": self.handle_remote_rotate,
            "/api/thermal/status": self.handle_thermal_status,
            "/api/thermal/cooldown/start": self.handle_thermal_cooldown_start,
            "/api/thermal/cooldown/cancel": self.handle_thermal_cooldown_cancel,
            "/api/thermal/unload": self.handle_thermal_unload,
            "/api/thermal/simulate": self.handle_thermal_simulate,
            "/api/cluster/register": self.handle_cluster_register,
            "/api/cluster/heartbeat": self.handle_cluster_heartbeat,
            "/api/cluster/unregister": self.handle_cluster_unregister,
            "/api/cluster/result": self.handle_cluster_result,
            "/api/cluster/benchmark": self.handle_cluster_benchmark,
            "/api/cluster/map": self.handle_cluster_map,
            "/api/cluster/scan": self.handle_cluster_scan,
            "/api/cluster/node/add": self.handle_cluster_node_add,
            "/api/cluster/node/toggle": self.handle_cluster_node_toggle,
            "/api/cluster/node/remove": self.handle_cluster_node_remove,
            "/api/cluster/llm_query": self.handle_cluster_llm_query,
            "/api/cluster/rpc/start": self.handle_cluster_rpc_start,
            "/api/cluster/rpc/stop": self.handle_cluster_rpc_stop,
            "/api/obscura/test": self.handle_obscura_test,
            "/api/backup/import": self.handle_backup_import,
            "/api/scheduler/add": self.handle_scheduler_add,
            "/api/scheduler/toggle": self.handle_scheduler_toggle,
            "/api/scheduler/delete": self.handle_scheduler_delete,
            "/api/scheduler/run": self.handle_scheduler_run,
            "/api/update/download": self.handle_update_download,
            "/api/notify/test": self.handle_notify_test,
            "/api/rag/index": self.handle_rag_index,
            "/api/rag/search": self.handle_rag_search,
            "/api/rag/remove": self.handle_rag_remove,
            "/api/rag/clear": self.handle_rag_clear,
            "/api/mcp/add": self.handle_mcp_add,
            "/api/mcp/remove": self.handle_mcp_remove,
            "/api/mcp/toggle": self.handle_mcp_toggle,
            "/api/mcp/connect": self.handle_mcp_connect,
            "/api/voice/speak": self.handle_voice_speak,
            "/api/voice/sample/upload": self.handle_voice_sample_upload,
            "/api/voice/sample/delete": self.handle_voice_sample_delete,
            "/api/session/truncate": self.handle_session_truncate,
            "/api/chat/edit": self.handle_chat_edit,
            "/api/chat/regenerate": self.handle_chat_regenerate,
            "/api/train/install": self.handle_train_install,
            "/api/train/start": self.handle_train_start,
            "/api/train/stop": self.handle_train_stop,
            "/api/train/script": self.handle_train_script,
            "/api/train/dataset/from_sessions": self.handle_train_ds_sessions,
            "/api/train/dataset/append": self.handle_train_ds_append,
            "/api/train/dataset/delete": self.handle_train_ds_delete,
            "/api/train/dataset/import": self.handle_train_ds_import,
            "/api/train/dataset/synthesize": self.handle_train_ds_synthesize,
            "/api/train/autotune": self.handle_train_autotune,
            "/api/train/estimate": self.handle_train_estimate,
            "/api/train/eval": self.handle_train_eval,
            "/api/train/layers": self.handle_train_layers,
            "/api/train/hf/search": self.handle_train_hf_search,
            "/api/train/hf/download": self.handle_train_hf_download,
            "/api/train/quantize": self.handle_train_quantize,
            "/api/train/dataset/clean": self.handle_train_ds_clean,
            "/api/train/benchmark_gguf": self.handle_train_benchmark_gguf,
            "/api/files/pair": self.handle_files_pair,
            "/api/files/rotate": self.handle_files_rotate,
            "/api/files/config": self.handle_files_config,
            "/api/files/save": self.handle_files_save,
            "/api/files/mkdir": self.handle_files_mkdir,
            "/api/files/delete": self.handle_files_delete,
        }
        if path == "/api/new":
            _json_response(self, {"session": session_mod.create(str(body.get("title") or ""))})
            return
        if path == "/api/memory/add":
            _json_response(self, mem_mod.add_fact(body.get("text")))
            return
        if path == "/api/memory/delete":
            _json_response(self, mem_mod.delete_fact(body.get("id")))
            return
        if path == "/api/memory/clear":
            _json_response(self, mem_mod.clear_facts())
            return
        if path == "/api/plugins/reload":
            plugins.scan(force=True)
            _json_response(self, plugins.status())
            return
        if path == "/api/provider/start":
            try:
                wait = float(body.get("wait", 45))
            except (TypeError, ValueError):
                wait = 45.0
            _json_response(self, ensure_provider(load(), wait))
            return
        handler = routes.get(path)
        if handler:
            handler(body)
            return
        self.send_response(404)
        self.end_headers()

    def serve_file(self, rel_path, content_type):
        name = os.path.basename(rel_path)
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            bases = [os.path.join(exe_dir, "webui"), os.path.join(exe_dir, "harness", "webui")]
        else:
            bases = [os.path.join(ROOT, "harness", "webui"), os.path.join(ROOT, "webui")]
        for base in bases:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(path, "rb") as fh:
                    self.wfile.write(fh.read())
                return
        self.send_response(404)
        self.end_headers()

    def serve_shot(self, raw):
        shots = os.path.realpath(os.path.join(DATA_DIR, "shots"))
        target = os.path.realpath(raw) if raw else ""
        if not target or not target.startswith(shots + os.sep) or not os.path.isfile(target):
            self.send_response(404)
            self.end_headers()
            return
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".gif": "image/gif"}.get(os.path.splitext(target)[1].lower(),
                                                                 "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(target, "rb") as fh:
            self.wfile.write(fh.read())

    def handle_chat(self, body):
        sid = body.get("session") or session_mod.create("")
        text = str(body.get("text") or "")
        image = body.get("image")
        images = [i for i in (body.get("images") or []) if i]
        if not text.strip() and not image and not images:
            _json_response(self, {"ok": False, "error": "пустой запрос"}, 400)
            return
        cfg = load()
        history = []
        if body.get("session"):
            data = session_mod.load(sid)
            if data is None:
                _json_response(self, {"ok": False, "error": "no such session"}, 404)
                return
            history = data.get("messages") or []
        run = Run(sid)
        bridge = SseBridge(run)
        run.bridge = bridge
        agent = Agent(cfg, bridge, sid)
        is_goal = body.get("mode") == "goal"
        criteria = str(body.get("criteria") or "")
        goal_objective = text
        auto_goal_triggered = False
        if not is_goal:
            from .agent import detect_goal_intent
            detected = detect_goal_intent(text)
            if detected["is_goal"]:
                is_goal = True
                auto_goal_triggered = True
                goal_objective = detected["objective"] or text
                if not criteria and detected.get("criteria"):
                    criteria = detected["criteria"]

        def run_agent():
            if auto_goal_triggered:
                bridge.emit({"type": "notice", "text": "🎯 Режим цели активирован автоматически по вашему сообщению."})
            goal = {"objective": goal_objective, "criteria": criteria} if is_goal else None
            dashboard_mod.record_session()
            try:
                agent.run(text, history=history, goal=goal, image=image, images=images)
            except Exception as exc:
                bridge.emit({"type": "error", "text": str(exc)})
                bridge.emit({"type": "done", "text": "Error: %s" % exc, "steps": 0, "protocol": "json",
                             "speed": {}, "rounds": 0})
            finally:
                dashboard_mod.record_session_end()

        run.thread = threading.Thread(target=run_agent, daemon=True)
        with RUNS_LOCK:
            if session_mod.load(sid) is None:
                _json_response(self, {"ok": False, "error": "no such session"}, 404)
                return
            existing = RUNS.get(sid)
            if existing and not existing.finished:
                _json_response(self, {"ok": False, "error": "прогон уже идёт — дождись ответа или нажми «стоп»"}, 409)
                return
            RUNS[sid] = run
            if len(RUNS) > 60:
                finished = [k for k, r in RUNS.items() if r.finished]
                for key in finished[:-40]:
                    RUNS.pop(key, None)
        run.thread.start()
        _json_response(self, {"ok": True, "session": sid})

    def handle_respond(self, body):
        sid = body.get("session")
        key = body.get("key")
        value = body.get("value")
        with RUNS_LOCK:
            run = RUNS.get(sid)
        if run and run.bridge.respond(key, value):
            _json_response(self, {"ok": True})
        else:
            _json_response(self, {"ok": False, "error": "no such pending request"}, 404)

    def handle_stop(self, body):
        sid = body.get("session")
        with RUNS_LOCK:
            run = RUNS.get(sid)
        if not run:
            _json_response(self, {"ok": False, "error": "no such run"}, 404)
            return
        run.bridge.request_stop()
        _json_response(self, {"ok": True})

    def handle_session_delete(self, body):
        sid = str(body.get("session") or "")
        with RUNS_LOCK:
            run = RUNS.get(sid)
            if run and (not run.finished or (run.thread and run.thread.is_alive())):
                _json_response(self, {"ok": False,
                                      "error": "сессия ещё выполняется — сначала остановите прогон"}, 409)
                return
            if not session_mod.delete(sid):
                _json_response(self, {"ok": False, "error": "no such session"}, 404)
                return
            RUNS.pop(sid, None)
        _json_response(self, {"ok": True})

    def handle_session_rename(self, body):
        sid = body.get("session")
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120 or any(ord(c) < 32 for c in title):
            _json_response(self, {"ok": False, "error": "название: от 1 до 120 символов, без переносов строк"}, 400)
            return
        if not session_mod.set_title(sid, title.strip()):
            _json_response(self, {"ok": False, "error": "no such session"}, 404)
            return
        _json_response(self, {"ok": True, "title": title.strip()})

    def serve_remote_claim(self, token):
        info = remote.verify_token(token)
        insecure = not remote.is_https(self.headers)
        if not info:
            body = _REMOTE_PAGE % ("Код недействителен",
                                   "Срок действия истёк или ссылка повреждена. "
                                   "Запроси новый код в приложении LocalHarness.")
            payload = body.encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        max_age = max(60, int(info["exp"] - time.time()))
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s" % (
            remote.COOKIE, token, max_age, "" if insecure else "; Secure")
        self.send_response(302)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()

    def handle_remote_start(self, body):
        remote.save_cfg(auto_start=True)
        _json_response(self, remote.start())

    def handle_remote_stop(self, body):
        remote.save_cfg(auto_start=False)
        _json_response(self, remote.stop())

    def handle_remote_rotate(self, body):
        _json_response(self, remote.rotate())

    def handle_autostart(self, body):
        from . import desktop
        origin = self.headers.get("Origin")
        if (origin and origin != "http://" + self.headers.get("Host", "")) or self.headers.get_content_type() != "application/json":
            _json_response(self, {"ok": False, "error": "требуется запрос из интерфейса LocalHarness"}, 403)
            return
        if not isinstance(body.get("enabled"), bool):
            _json_response(self, {"ok": False, "error": "enabled должен быть true или false"}, 400)
            return
        try:
            desktop.set_autostart(body["enabled"])
            _json_response(self, {"ok": True, **desktop.status()})
        except (OSError, RuntimeError) as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, 400)

    def handle_config(self, body):
        incoming = body.get("config") if isinstance(body.get("config"), dict) else {}
        warnings = []
        with CONFIG_LOCK:
            old = load()
            cfg = _merge(old, incoming)
            for key in ("context_tokens", "lms_context_length"):
                if key in incoming:
                    raw = cfg.get(key)
                    fixed = normalize_context(raw, old.get(key) or 8192)
                    if str(raw) != str(fixed):
                        warnings.append("Контекст: %s → %d (допустимо 512–262144)" % (raw, fixed))
                    cfg[key] = fixed
            if "max_tokens" in incoming:
                raw = cfg.get("max_tokens")
                fixed = as_int(raw, old.get("max_tokens") or 1500, MAX_TOKENS_MIN, MAX_TOKENS_MAX)
                if str(raw) != str(fixed):
                    warnings.append("max_tokens: %s → %d (допустимо 64–32768)" % (raw, fixed))
                cfg["max_tokens"] = fixed
            save(cfg)
        lms_mod.invalidate()
        with _PROBE["lock"]:
            _PROBE["at"] = 0.0
        payload = {"ok": True, "config": cfg}
        if warnings:
            payload["warnings"] = warnings
        if bind_address(old) != bind_address(cfg):
            payload["restart"] = request_restart()
        _json_response(self, payload)

    def handle_plugin_toggle(self, body):
        _json_response(self, plugins.set_enabled(body.get("file"), body.get("enabled", True)))

    def handle_plugin_remove(self, body):
        _json_response(self, plugins.remove_file(body.get("file")))

    def handle_plugin_install(self, body):
        _json_response(self, plugins.install_catalog(body.get("id", ""), body.get("url", "")))

    def handle_plugin_create(self, body):
        _json_response(self, plugins.create_plugin(
            name=body.get("name", ""),
            code=body.get("code", ""),
            desc=body.get("desc", ""),
            author=body.get("author", ""),
            publish=bool(body.get("publish", False)),
            tags=body.get("tags"),
        ))

    def handle_plugin_publish(self, body):
        _json_response(self, plugins.publish_to_workshop(
            fname_or_name=body.get("file") or body.get("name", ""),
            author=body.get("author", "Пользователь LocalHarness"),
            desc=body.get("desc", ""),
            tags=body.get("tags"),
        ))

    def handle_plugin_unpublish(self, body):
        plugin_id = body.get("id") or body.get("name") or body.get("file", "")
        _json_response(self, plugins.unpublish_from_workshop(plugin_id))

    def handle_plugin_test_tool(self, body):
        tool_name = body.get("tool", "")
        args = body.get("args") or {}
        _json_response(self, plugins.test_plugin_tool(tool_name, args, load()))

    def handle_plugin_generate_ai(self, body):
        prompt = body.get("prompt", "")
        _json_response(self, plugins.generate_plugin_ai(prompt, load()))

    def handle_plugin_workshop_install(self, body):
        _json_response(self, plugins.install_workshop(body.get("id", "")))

    def handle_plugin_convert_repo(self, body):
        _json_response(self, plugins.convert_and_install_repo(body.get("url", ""), body.get("name")))

    def handle_plugin_virustotal(self, body):
        _json_response(self, plugins.scan_virustotal(body.get("file") or body.get("code", "")))

    def handle_plugin_save_code(self, body):
        res = plugins.update_plugin_code(body.get("file", ""), body.get("code", ""))
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_plugin_save_settings(self, body):
        res = plugins.save_plugin_settings(body.get("file", ""), body.get("values") or {})
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_dashboard_reset(self, body):
        _json_response(self, dashboard_mod.reset_counters())

    def handle_lms_load(self, body):
        cfg = load()
        model = body.get("model")
        if not model:
            _json_response(self, {"ok": False, "error": "не указана модель"}, 400)
            return
        context_length = normalize_context(
            body.get("context_length"), cfg.get("lms_context_length") or cfg.get("context_tokens") or 8192)
        ttl = body.get("ttl", cfg.get("lms_ttl", 0))
        args = ["load", model, "--context-length", str(context_length), "-y"]
        try:
            if int(ttl) >= 1:
                args += ["--ttl", str(int(ttl))]
        except (TypeError, ValueError):
            pass
        loaded, _ = lms_mod.loaded_models(cfg, force=True)
        for m in loaded:
            if any(lms_mod._match_alias(m.get(k), model) for k in ("identifier", "modelKey", "path", "id", "displayName", "indexedModelIdentifier")):
                lms_mod.run_cli(["unload", str(m.get("identifier") or model)], cfg, timeout=30)
                lms_mod.invalidate()
                break
        result = start_lms_job("load", args)
        if result.get("ok"):
            dashboard_mod.record_model_load()
        _json_response(self, result)

    def handle_lms_unload(self, body):
        cfg = load()
        model = str(body.get("model") or "").strip()
        if model and model != "all":
            loaded, _ = lms_mod.loaded_models(cfg, force=True)
            target_id = model
            for m in (loaded or []):
                if any(lms_mod._match_alias(m.get(k), model) for k in ("identifier", "modelKey", "path", "id", "displayName", "indexedModelIdentifier")):
                    target_id = str(m.get("identifier") or m.get("modelKey") or model)
                    break
            args = ["unload", target_id]
        else:
            args = ["unload", "--all"]
        out, err = lms_mod.run_cli(args, cfg, timeout=30)
        lms_mod.invalidate()
        with _PROBE["lock"]:
            _PROBE["at"] = 0.0
        if err:
            _json_response(self, {"ok": False, "error": err}, 400)
            return
        _json_response(self, {"ok": True, "unloaded": model or "all", "output": (out or "").strip()})

    def handle_lms_download(self, body):
        query = str(body.get("query") or "").strip()
        if not query:
            _json_response(self, {"ok": False, "error": "пустой запрос поиска"}, 400)
            return
        _json_response(self, start_lms_job("download", ["get", query, "--gguf", "-y"], timeout=7200))

    def handle_lms_import(self, body):
        cfg = load()
        result = lms_mod.import_model(body.get("path"), body.get("mode", "copy"), cfg)
        if result.get("ok") and result.get("job"):
            result = start_lms_job("import", result["args"])
        else:
            lms_mod.invalidate()
        _json_response(self, result)

    def handle_lms_estimate(self, body):
        cfg = load()
        model_name = body.get("model")
        ctx = normalize_context(body.get("context_length"), cfg.get("lms_context_length") or 8192)
        disk, _ = lms_mod.disk_models(cfg)
        loaded, _ = lms_mod.loaded_models(cfg)
        target = None
        for item in (loaded or []) + (disk or []):
            if model_name in (item.get("modelKey"), item.get("path"), item.get("identifier"), item.get("displayName")):
                target = item
                break
        if not target and isinstance(body.get("model_data"), dict):
            target = body["model_data"]
        if not target:
            target = {"modelKey": model_name or "unknown", "paramsString": body.get("params", "8B"),
                      "sizeBytes": body.get("size_bytes", 0)}
        hw = sizing_mod.get_hardware_profile()
        estimate = sizing_mod.estimate_model_fit(target, context_length=ctx, hw=hw)
        _json_response(self, {"ok": True, "model": model_name, "estimate": estimate})

    def handle_thermal_status(self, body):
        _json_response(self, thermal_mod.get_thermal_status(load()))

    def handle_thermal_cooldown_start(self, body):
        duration = body.get("duration")
        target_temp = body.get("target_temp")
        unload = body.get("unload", True)
        status = thermal_mod.start_cooldown(duration=duration, target_temp=target_temp, unload=unload, cfg=load())
        _json_response(self, status)

    def handle_thermal_cooldown_cancel(self, body):
        _json_response(self, thermal_mod.cancel_cooldown())

    def handle_thermal_unload(self, body):
        _json_response(self, thermal_mod.unload_model(load()))

    def handle_thermal_simulate(self, body):
        temp = body.get("temp")
        thermal_mod.set_simulated_temp(temp)
        _json_response(self, thermal_mod.get_thermal_status(load(), force=True))

    def handle_cluster_register(self, body):
        w = cluster_mod.register_worker(body, client_ip=self.client_address[0])
        _json_response(self, {"ok": True, "worker": w})

    def handle_cluster_heartbeat(self, body):
        wid = str(body.get("id") or body.get("worker_id") or "")
        w = cluster_mod.heartbeat_worker(wid, body)
        if not w:
            _json_response(self, {"ok": False, "error": "worker not found"}, 404)
            return
        _json_response(self, {"ok": True, "worker": w})

    def handle_cluster_unregister(self, body):
        wid = str(body.get("id") or body.get("worker_id") or "")
        _json_response(self, cluster_mod.unregister_worker(wid))

    def handle_cluster_result(self, body):
        _json_response(self, cluster_mod.submit_result(
            task_id=body.get("task_id"),
            worker_id=body.get("worker_id"),
            ok=body.get("ok", True),
            result=body.get("result"),
            error=body.get("error"),
            duration_ms=body.get("duration_ms"),
        ))

    def handle_cluster_benchmark(self, body):
        chunks = body.get("chunks", 12)
        iterations = body.get("iterations", 45000)
        _json_response(self, cluster_mod.run_cluster_benchmark(chunks=chunks, iterations=iterations))

    def handle_cluster_map(self, body):
        kind = body.get("kind", "compute_bench")
        items = body.get("items") or []
        _json_response(self, cluster_mod.map_tasks(kind, items))

    def handle_cluster_scan(self, body):
        res = cluster_mod.scan_lan_llm_nodes(get_lan_ip())
        if res.get("found"):
            with CONFIG_LOCK:
                cfg = load()
                cfg["cluster_llm_nodes"] = cluster_mod.export_nodes_for_config()
                save(cfg)
        _json_response(self, res)

    def handle_cluster_node_add(self, body):
        res = cluster_mod.add_llm_node(body.get("url", ""), body.get("label", ""))
        if res.get("ok"):
            with CONFIG_LOCK:
                cfg = load()
                cfg["cluster_llm_nodes"] = cluster_mod.export_nodes_for_config()
                save(cfg)
        _json_response(self, res)

    def handle_cluster_node_toggle(self, body):
        res = cluster_mod.toggle_llm_node(body.get("url", ""), body.get("enabled", True))
        if res.get("ok"):
            with CONFIG_LOCK:
                cfg = load()
                cfg["cluster_llm_nodes"] = cluster_mod.export_nodes_for_config()
                save(cfg)
        _json_response(self, res)

    def handle_cluster_node_remove(self, body):
        res = cluster_mod.remove_llm_node(body.get("url", ""))
        with CONFIG_LOCK:
            cfg = load()
            cfg["cluster_llm_nodes"] = cluster_mod.export_nodes_for_config()
            save(cfg)
        _json_response(self, res)

    def handle_cluster_llm_query(self, body):
        _json_response(self, cluster_mod.query_remote_llm(
            body.get("prompt", ""),
            node_url=body.get("node_url"),
            model=body.get("model"),
        ))

    def handle_cluster_rpc_start(self, body):
        res = cluster_mod.start_rpc_server(
            host=body.get("host", "0.0.0.0"),
            port=int(body.get("port") or 50052),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_cluster_rpc_stop(self, body):
        _json_response(self, cluster_mod.stop_rpc_server())

    def handle_obscura_test(self, body):
        cfg = load()
        if body.get("obscura_path") is not None:
            cfg["obscura_path"] = str(body.get("obscura_path") or "").strip()
        if body.get("obscura_cdp") is not None:
            cfg["obscura_cdp"] = str(body.get("obscura_cdp") or "").strip() or "http://127.0.0.1:9223"
        st = obscura_mod.status(cfg)
        if st.get("running"):
            _json_response(self, {"ok": True, "status": st, "message": "Obscura CDP отвечает на %s" % st["cdp_url"]})
            return
        if st.get("installed"):
            try:
                cdp = obscura_mod.get_session().ensure_started(cfg)
                st = obscura_mod.status(cfg)
                _json_response(self, {"ok": True, "status": st, "message": "Obscura запущен на %s (--stealth)" % cdp})
                return
            except Exception as exc:
                _json_response(self, {"ok": False, "status": st, "error": str(exc)})
                return
        _json_response(self, {
            "ok": False,
            "status": st,
            "error": "Бинарник obscura не найден. Скачайте с github.com/h4ckf0r0day/obscura и укажите путь или добавьте в PATH.",
        })

    def handle_backup_import(self, body):
        res = backup_mod.import_profile_b64(
            body.get("data_b64", ""),
            restore_config=body.get("restore_config", True),
            restore_memory=body.get("restore_memory", True),
            restore_sessions=body.get("restore_sessions", True),
            restore_plugins=body.get("restore_plugins", True),
        )
        _json_response(self, res)

    def handle_scheduler_add(self, body):
        res = scheduler_mod.add_task(
            name=body.get("name"),
            payload=body.get("payload"),
            kind=body.get("kind", "prompt"),
            schedule_type=body.get("schedule_type", "interval"),
            interval_min=body.get("interval_min", 60),
            daily_time=body.get("daily_time", "09:00"),
            enabled=body.get("enabled", True),
        )
        _json_response(self, res)

    def handle_scheduler_toggle(self, body):
        _json_response(self, scheduler_mod.toggle_task(body.get("id"), body.get("enabled", True)))

    def handle_scheduler_delete(self, body):
        _json_response(self, scheduler_mod.delete_task(body.get("id")))

    def handle_scheduler_run(self, body):
        _json_response(self, scheduler_mod.run_now(body.get("id"), sync=bool(body.get("sync", False))))

    def handle_update_download(self, body):
        _json_response(self, updater_mod.download_and_launch(
            installer_url=body.get("installer_url"),
            installer_name=body.get("installer_name"),
            launch=bool(body.get("launch", True)),
        ))

    def handle_notify_test(self, body):
        title = str(body.get("title") or "LocalHarness")
        message = str(body.get("message") or "Тестовое Windows Toast уведомление работает!")
        ok = notify_mod.send(title, message, cfg=load())
        _json_response(self, {"ok": bool(ok), "title": title, "message": message})

    def handle_rag_index(self, body):
        _json_response(self, rag_mod.index_path(
            body.get("path", ""),
            cfg=load(),
            use_embeddings=bool(body.get("use_embeddings", False)),
        ))

    def handle_rag_search(self, body):
        _json_response(self, rag_mod.search(
            body.get("query", ""),
            top_k=body.get("top_k", 5),
            cfg=load(),
        ))

    def handle_rag_remove(self, body):
        _json_response(self, rag_mod.remove_folder(body.get("path", "")))

    def handle_rag_clear(self, body):
        _json_response(self, rag_mod.clear())

    def handle_mcp_add(self, body):
        _json_response(self, mcp_mod.add_server(
            name=body.get("name", ""),
            command=body.get("command", ""),
            args=body.get("args"),
            env=body.get("env"),
            connect=bool(body.get("connect", True)),
        ))

    def handle_mcp_remove(self, body):
        _json_response(self, mcp_mod.remove_server(body.get("name", "")))

    def handle_mcp_toggle(self, body):
        _json_response(self, mcp_mod.toggle_server(body.get("name", ""), body.get("enabled", True)))

    def handle_mcp_connect(self, body):
        _json_response(self, mcp_mod.connect_server(body.get("name", ""), cfg=load()))

    def handle_voice_speak(self, body):
        _json_response(self, voice_mod.synthesize(
            text=body.get("text", ""),
            cfg=load(),
            voice=body.get("voice"),
            language=body.get("language"),
            speed=body.get("speed"),
            pitch=body.get("pitch"),
            engine=body.get("engine"),
            save_file=bool(body.get("save_file", False)),
        ))

    def handle_voice_sample_upload(self, body):
        _json_response(self, voice_mod.save_sample(body.get("name", ""), body.get("data_b64", "")))

    def handle_voice_sample_delete(self, body):
        _json_response(self, voice_mod.delete_sample(body.get("id") or body.get("file", "")))

    def handle_session_truncate(self, body):
        sid = str(body.get("session") or "")
        with RUNS_LOCK:
            run = RUNS.get(sid)
            if run and not run.finished:
                _json_response(self, {"ok": False, "error": "сессия выполняется — сначала остановите прогон"}, 409)
                return
        res = session_mod.truncate_from_user_turn(sid, body.get("turn_index", -1))
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_chat_edit(self, body):
        sid = str(body.get("session") or "")
        new_text = str(body.get("text") or "").strip()
        if not sid or not new_text:
            _json_response(self, {"ok": False, "error": "нужны session и text"}, 400)
            return
        with RUNS_LOCK:
            run = RUNS.get(sid)
            if run and not run.finished:
                _json_response(self, {"ok": False, "error": "сессия выполняется — сначала остановите прогон"}, 409)
                return
        trunc = session_mod.truncate_from_user_turn(sid, body.get("turn_index", -1))
        if not trunc.get("ok"):
            _json_response(self, trunc, 400)
            return
        self.handle_chat({"session": sid, "text": new_text, "mode": body.get("mode", "chat")})

    def handle_chat_regenerate(self, body):
        sid = str(body.get("session") or "")
        if not sid:
            _json_response(self, {"ok": False, "error": "нужна session"}, 400)
            return
        with RUNS_LOCK:
            run = RUNS.get(sid)
            if run and not run.finished:
                _json_response(self, {"ok": False, "error": "сессия выполняется — сначала остановите прогон"}, 409)
                return
        trunc = session_mod.truncate_from_user_turn(sid, body.get("turn_index", -1))
        if not trunc.get("ok"):
            _json_response(self, trunc, 400)
            return
        prompt_text = str(body.get("text") or trunc.get("removed_text") or "").strip()
        if not prompt_text:
            _json_response(self, {"ok": False, "error": "не удалось определить исходный текст запроса"}, 400)
            return
        self.handle_chat({"session": sid, "text": prompt_text, "mode": body.get("mode", "chat")})

    def handle_train_install(self, body):
        _json_response(self, train_mod.install_unsloth_env())

    def handle_train_start(self, body):
        res = train_mod.start_training(body)
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_stop(self, body):
        _json_response(self, train_mod.stop_job())

    def handle_train_script(self, body):
        _json_response(self, {"ok": True, "script": train_mod.generate_unsloth_script(body)})

    def handle_train_ds_sessions(self, body):
        _json_response(self, train_mod.export_sessions_to_dataset(
            dataset_name=body.get("name", "sessions_sft.jsonl"),
            min_chars=int(body.get("min_chars", 8)),
            max_sessions=int(body.get("max_sessions", 100)),
        ))

    def handle_train_ds_append(self, body):
        res = train_mod.append_dataset_example(
            dataset_name=body.get("name", "custom_sft.jsonl"),
            instruction=body.get("instruction", ""),
            output=body.get("output", ""),
            inp=body.get("input", ""),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_ds_delete(self, body):
        _json_response(self, train_mod.delete_dataset(body.get("name", "")))

    def handle_train_ds_import(self, body):
        res = train_mod.import_dataset_file(body.get("filename", "imported.jsonl"), body.get("text", ""))
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_ds_synthesize(self, body):
        res = train_mod.generate_synthetic_dataset(
            topic=body.get("topic", ""),
            count=body.get("count", 10),
            dataset_name=body.get("name", "synthetic_sft.jsonl"),
            cfg=load(),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_autotune(self, body):
        _json_response(self, train_mod.autotune_for_gpu(
            model_id=body.get("model", "unsloth/Qwen3-4B-bnb-4bit"),
            method=body.get("method", "sft"),
        ))

    def handle_train_estimate(self, body):
        _json_response(self, train_mod.estimate_training_vram(
            model_id=body.get("model", "unsloth/Qwen3-4B-bnb-4bit"),
            max_seq_length=int(body.get("max_seq_length") or 2048),
            batch_size=int(body.get("batch_size") or 2),
            lora_r=int(body.get("lora_r") or 16),
            method=body.get("method", "sft"),
        ))

    def handle_train_eval(self, body):
        res = train_mod.eval_adapter(
            adapter_dir=body.get("adapter_dir", ""),
            prompt=body.get("prompt", ""),
            max_new_tokens=int(body.get("max_new_tokens") or 256),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_layers(self, body):
        _json_response(self, train_mod.inspect_model_layers(body.get("model", "")))

    def handle_train_hf_search(self, body):
        res = train_mod.search_hf_models(
            query=body.get("query", ""),
            min_size=body.get("min_size", 0),
            max_size=body.get("max_size", 0),
            limit=int(body.get("limit") or 15),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_hf_download(self, body):
        res = train_mod.download_hf_model(body.get("repo_id", ""))
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_quantize(self, body):
        res = train_mod.quantize_gguf_standalone(
            input_gguf=body.get("input_gguf", ""),
            output_gguf=body.get("output_gguf", ""),
            quant_type=body.get("quant_type", "Q4_K_M"),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_ds_clean(self, body):
        res = train_mod.clean_and_validate_dataset(body.get("name", ""))
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_train_benchmark_gguf(self, body):
        res = train_mod.benchmark_gguf_model(
            gguf_path=body.get("gguf_path", ""),
            threads=body.get("threads", 6),
            ngl=body.get("ngl", 99),
        )
        _json_response(self, res, 200 if res.get("ok") else 400)

    def handle_files_pair(self, body):
        cfg = load()
        try:
            res = files_mod.pair(cfg, self.client_address[0], body.get("code", ""))
            payload = json.dumps({"ok": True, "expires": res["expires"]}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", res["cookie"])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except files_mod.FilesError as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, exc.status)

    def handle_files_rotate(self, body):
        if not files_mod.is_local(self.client_address[0]):
            _json_response(self, {"ok": False, "error": "сброс кода доступен только с локального ПК"}, 403)
            return
        cfg = load()
        lan_url = "http://%s:%d" % (get_lan_ip(), int(cfg.get("port", 7788)))
        res = files_mod.rotate_pair(cfg)
        res = files_mod.desktop_pair_status(load(), lan_url=lan_url)
        _json_response(self, res)

    def handle_files_config(self, body):
        if not files_mod.is_local(self.client_address[0]):
            _json_response(self, {"ok": False, "error": "настройки редактора меняются только с локального ПК"}, 403)
            return
        try:
            cfg = load()
            files_mod.set_config(cfg, body)
            lan_url = "http://%s:%d" % (get_lan_ip(), int(cfg.get("port", 7788)))
            _json_response(self, files_mod.desktop_pair_status(load(), lan_url=lan_url))
        except files_mod.FilesError as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, exc.status)

    def handle_files_save(self, body):
        cfg = load()
        if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=True):
            _json_response(self, {"ok": False, "error": "нет прав на запись или не пройдено сопряжение"}, 403)
            return
        try:
            res = files_mod.save_file(cfg, body.get("path", ""), body.get("text", ""), body.get("revision"))
            _json_response(self, res)
        except files_mod.FilesError as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, exc.status)

    def handle_files_mkdir(self, body):
        cfg = load()
        if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=True):
            _json_response(self, {"ok": False, "error": "нет прав на создание папки"}, 403)
            return
        try:
            _json_response(self, files_mod.create_dir(cfg, body.get("path", "")))
        except files_mod.FilesError as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, exc.status)

    def handle_files_delete(self, body):
        cfg = load()
        if not files_mod.authorized(self.client_address[0], self.headers, cfg, write=True):
            _json_response(self, {"ok": False, "error": "нет прав на удаление"}, 403)
            return
        try:
            _json_response(self, files_mod.delete_entry(cfg, body.get("path", "")))
        except files_mod.FilesError as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, exc.status)

    def stream_events(self, sid, since):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        with RUNS_LOCK:
            run = RUNS.get(sid)
        if run is None:
            data = session_mod.load(sid)
            if not data:
                self.send_event({"type": "error", "text": "session not found", "_i": 0})
                return
            for i, ev in enumerate(data.get("events") or []):
                if i >= since:
                    ev = dict(ev)
                    ev["_i"] = i
                    self.send_event(ev)
            return

        idx = max(0, since)
        if run is not None and idx > len(run.events):
            # If since is beyond current events of this run (e.g. stale since from previous run), reset to 0
            idx = 0
        try:
            while True:
                with run.cond:
                    run.cond.wait_for(lambda: idx < len(run.events) or run.finished, timeout=1.0)
                    batch = run.events[idx:]
                    exhausted = run.finished and idx >= len(run.events)
                if batch:
                    for ev in batch:
                        self.send_event(ev)
                        idx = ev.get("_i", idx) + 1
                        if ev.get("type") in ("done", "stopped"):
                            return
                elif exhausted:
                    return
                else:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                        return
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def send_event(self, ev):
        try:
            data = "data: %s\n\n" % json.dumps(ev, ensure_ascii=False)
            self.wfile.write(data.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


def serve(cfg):
    """Слушать порт, перепривязываясь при смене lan_access из интерфейса."""
    try:
        while True:
            current = load()
            host, port = bind_address(current)
            server = make_server(current)

            def _stop(srv=server):
                time.sleep(0.35)  # дать ответу /api/config уйти клиенту
                try:
                    srv.shutdown()
                except OSError:
                    pass

            register_restarter(_stop)
            _RESTART.clear()
            print("LocalHarness server on http://%s:%d" % ("127.0.0.1", port))
            if host == "0.0.0.0":
                print("  LAN access enabled: http://%s:%d" % (get_lan_ip(), port))
            try:
                remote.autostart()
            except Exception as exc:
                print("  remote autostart: %s" % exc)
            try:
                dashboard_mod.start()
                scheduler_mod.start()
            except Exception:
                pass
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                return
            finally:
                register_restarter(None)
                server.server_close()
            if not _RESTART.is_set():
                return
    finally:
        register_restarter(None)