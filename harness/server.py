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
from . import plugins
from . import schemas
from . import session as session_mod
from . import sizing as sizing_mod
from . import vision as vision_mod
from .agent import Agent, Bridge
from .config import DATA_DIR, ROOT, LOCK as CONFIG_LOCK, load, save, _merge

RUNS = {}
RUNS_LOCK = threading.Lock()
STARTED = time.time()

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
    ctx_len = int(cfg.get("lms_context_length") or cfg.get("context_tokens") or 8192)
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
            proc = subprocess.Popen([cli] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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


def _json_response(handler, data, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/state":
            cfg = load()
            ps = cached_provider_status(cfg)
            try:
                dns_probe = {"ok": True, "ip": dnsfix.resolve("html.duckduckgo.com")}
            except Exception as exc:
                dns_probe = {"ok": False, "error": str(exc)}
            pl = plugins.status()
            tools_list = [spec["name"] for spec in schemas.entries(cfg.get("tool_profile", "core"))]
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
                "sessions": session_mod.list_sessions(),
                "tools": tools_list,
                "plugins": pl,
                "provider_status": ps,
                "dns_probe": dns_probe,
            })
            return

        if path == "/api/model/readiness":
            _json_response(self, lms_mod.readiness(load()))
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

        if path in ("/", "/index.html"):
            self.serve_file("webui/index.html", "text/html")
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
            "/api/lms/load": self.handle_lms_load,
            "/api/lms/unload": self.handle_lms_unload,
            "/api/lms/download": self.handle_lms_download,
            "/api/lms/import": self.handle_lms_import,
            "/api/lms/estimate": self.handle_lms_estimate,
        }
        if path == "/api/new":
            _json_response(self, {"session": session_mod.create(str(body.get("title") or ""))})
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
        criteria = body.get("criteria", "")

        def run_agent():
            goal = {"objective": text, "criteria": criteria} if is_goal else None
            try:
                agent.run(text, history=history, goal=goal, image=image, images=images)
            except Exception as exc:
                bridge.emit({"type": "error", "text": str(exc)})
                bridge.emit({"type": "done", "text": "Error: %s" % exc, "steps": 0, "protocol": "json",
                             "speed": {}, "rounds": 0})

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
        with CONFIG_LOCK:
            cfg = load()
            cfg = _merge(cfg, body.get("config", {}))
            save(cfg)
        with _PROBE["lock"]:
            _PROBE["at"] = 0.0
        _json_response(self, {"ok": True, "config": cfg})

    def handle_plugin_toggle(self, body):
        _json_response(self, plugins.set_enabled(body.get("file"), body.get("enabled", True)))

    def handle_plugin_remove(self, body):
        _json_response(self, plugins.remove_file(body.get("file")))

    def handle_plugin_install(self, body):
        _json_response(self, plugins.install_catalog(body.get("id", ""), body.get("url", "")))

    def handle_lms_load(self, body):
        cfg = load()
        model = body.get("model")
        if not model:
            _json_response(self, {"ok": False, "error": "не указана модель"}, 400)
            return
        context_length = body.get("context_length", cfg.get("lms_context_length", 8192))
        ttl = body.get("ttl", cfg.get("lms_ttl", 0))
        args = ["load", model, "--context-length", str(context_length)]
        try:
            if int(ttl) >= 1:
                args += ["--ttl", str(int(ttl))]
        except (TypeError, ValueError):
            pass
        loaded, _ = lms_mod.loaded_models(cfg)
        for m in loaded:
            if model in (m.get("identifier"), lms_mod.model_key(m), m.get("path")):
                lms_mod.run_cli(["unload", str(m.get("identifier") or model)], cfg, timeout=30)
                lms_mod.invalidate()
                break
        result = start_lms_job("load", args)
        _json_response(self, result)

    def handle_lms_unload(self, body):
        model = body.get("model")
        args = ["unload", model] if model else ["unload", "--all"]
        _json_response(self, start_lms_job("unload", args))

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
        ctx = int(body.get("context_length") or cfg.get("lms_context_length") or 8192)
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

    def stream_events(self, sid, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
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
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 7788))
    server = ThreadingHTTPServer((host, port), Handler)
    print("LocalHarness server on http://%s:%d" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()