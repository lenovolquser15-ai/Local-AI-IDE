"""Live proof of goal-mode on a running LocalHarness (port 7788).

The mock provider validates every prompt with the same rules as the Ministral GGUF chat
template (see tests/test_context.py:jinja_oracle) and answers HTTP 400 with the same
jinja error text when the history violates them. The run proves three things at once:
no jinja-400 from the agent, no stop at the old 6-approach ceiling, and finish_goal
closes the goal.

Usage: start the app (`python -m harness serve`), then `python tests/live_goal_proof.py`.
The app's config.json is snapshotted and restored afterwards.
"""
import importlib.util
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "http://127.0.0.1:7788"
MOCK_PORT = 12977
STATE = {"calls": 0, "bad": [], "tools": 0, "approaches": 0, "runaway": False}
CALL_CAP = 300   # страховка: больше вызовов, чем нужно восьми подходам, — это зацикливание

spec = importlib.util.spec_from_file_location("tctx", "C:/LocalHarness/tests/test_context.py")
tctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tctx)
jinja_oracle = tctx.jinja_oracle


def post(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def call_msg(name, args, cid):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json({"object": "list", "data": [{"id": "mock/model"}]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        msgs = body.get("messages") or []
        try:
            jinja_oracle(msgs)
        except AssertionError as exc:
            STATE["bad"].append(str(exc))
            return self._json({"error": {"message": "Error rendering prompt with jinja template: %s" % exc}}, 400)
        STATE["calls"] += 1
        if STATE["calls"] > CALL_CAP:
            # серверный агент в бесконечном режиме цели пережил бы выход теста, поэтому
            # закрываем цель принудительно и помечаем прогон провальным
            STATE["runaway"] = True
            out = call_msg("finish_goal", {"achieved": True, "summary": "runaway guard",
                                           "evidence": "превышен лимит вызовов"}, "c_runaway")
            return self._json({"choices": [{"message": out, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if not body.get("tools"):  # refine_goal
            out = {"role": "assistant", "content": json.dumps(
                {"objective": "проверить папку и закрыть цель",
                 "criteria": "1. list_dir выполнен", "plan": ["проверить", "закрыть"]}, ensure_ascii=False)}
        else:
            last = (msgs[-1] or {}).get("role")
            text = msgs[-1].get("content") if isinstance(msgs[-1], dict) else ""
            if last == "user" and isinstance(text, str) and "ЦЕЛЬ НЕ ЗАКРЫТА" in text:
                # начало нового подхода: харнес вставил напоминание о незакрытой цели
                STATE["approaches"] += 1
                if STATE["approaches"] >= 8:   # восемь подходов позади — закрываем цель
                    out = call_msg("finish_goal", {"achieved": True, "summary": "готово",
                                                    "evidence": "list_dir выполнялся 8+ раз"}, "c_finish")
                else:
                    out = call_msg("list_dir", {"path": "C:/LocalHarness/tests/.tmp"}, "c_list_%d" % STATE["calls"])
            elif last == "tool":
                # результат инструмента: подход закончен, дальше харнес сам добавит напоминание
                out = {"role": "assistant", "content": "папку проверил, продолжаю"}
            else:
                out = call_msg("list_dir", {"path": "C:/LocalHarness/tests/.tmp"}, "c_list_%d" % STATE["calls"])
        return self._json({"choices": [{"message": out, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def read_events(sid, deadline=180):
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=30)
    buf = b""
    kinds, goals, errors, tools = {}, [], [], []
    context = []
    end = time.time() + deadline
    while time.time() < end:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            line = block.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            k = ev.get("type")
            kinds[k] = kinds.get(k, 0) + 1
            if k == "goal":
                goals.append(ev.get("state"))
            elif k == "context":
                context.append(ev)
            elif k == "error":
                errors.append(ev.get("text", ""))
            elif k == "tool":
                tools.append((ev.get("tool"), ev.get("ok")))
            elif k in ("done", "stopped"):
                stream.close()
                return kinds, goals, errors, tools, context
    stream.close()
    return kinds, goals, errors, tools, context


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    # конфиг приложения снимаем через API: собранный exe читает dist/LocalHarness/config.json,
    # а исходники — корневой config.json, и путь угадывать нельзя
    saved = post("/api/config", {"config": {}}).get("config") or {}
    sid = None
    try:
        post("/api/config", {"config": {
            "provider": "mocklive",
            "providers": {"mocklive": {"label": "MockLive", "base_url": "http://127.0.0.1:%d/v1" % MOCK_PORT,
                                       "api_key": "x", "autostart": False}},
            "permissions": {"mode": "unrestricted"},
            "tool_protocol": "native",
            "goal_rounds": 0,
            "goal_idle_limit": 4,
        }})
        sid = post("/api/chat", {"text": "проверь папку и закрой цель", "mode": "goal", "criteria": ""}).get("session")
        print("session", sid)
        kinds, goals, errors, tools, context = read_events(sid)
        print("kinds:", json.dumps(kinds, ensure_ascii=False))
        print("goal states:", goals)
        print("tools:", tools[:14])
        print("errors:", errors[:3])
        print("jinja rejects from mock:", STATE["bad"][:3], "calls:", STATE["calls"])
        print("approaches:", STATE["approaches"], "runaway:", STATE["runaway"])
        if context:
            print("context events:", len(context))
            print("context first:", json.dumps(context[0], ensure_ascii=False))
            print("context last: ", json.dumps(context[-1], ensure_ascii=False))
        ok = (not errors and not STATE["bad"] and not STATE["runaway"]
              and "exhausted" not in goals and "closed" in goals
              and goals.count("continue") >= 6
              and bool(context) and context[0].get("tokens", 0) > 0
              and 0 < context[0].get("threshold", 0) <= context[0].get("window", 0))
        print("VERDICT:", "LIVE OK" if ok else "LIVE FAIL")
        return 0 if ok else 1
    finally:
        # агент живёт в серверном потоке и пережил бы выход теста — гасим его явно
        if sid:
            try:
                post("/api/stop", {"session": sid}, timeout=10)
            except Exception:
                pass
        post("/api/config", {"config": saved})
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
