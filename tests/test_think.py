"""Runtime proof of the reasoning channel: stream split, agent events, UTF-8 stream decode."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# sessions must not land in the app's real data/sessions
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
# config.json is gitignored and absent on a fresh clone: load() must return defaults
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")
from harness import config as config_mod  # noqa: E402
from harness import llm as llm_mod  # noqa: E402
from harness import session as session_mod  # noqa: E402
from harness.agent import Agent, QueueBridge  # noqa: E402

PORT = 12996
STATE = {"n": 0}
T_OPEN = "<" + "think" + ">"
T_CLOSE = "<" + "/" + "think" + ">"
UTF8_TEXT = "Проверка кодировки: русский текст, символ «ё» и стрелка → работают."
failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


def sse(handler, deltas):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    for delta in deltas:
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        handler.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
        handler.wfile.flush()
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


class Streamer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json({"object": "list", "data": [{"id": "think/mock", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length) or b"{}")
        STATE["n"] += 1
        if STATE["n"] == 1:
            return sse(self, [{"reasoning_content": "Думаю: сначала проверю "}, {"reasoning_content": "папку. "},
                               {"content": "Готово: "}, {"content": "папка пустая."}])
        if STATE["n"] == 2:
            return sse(self, [{"content": p} for p in [T_OPEN + "Рассуждаю вслух", " и добавляю ещё",
                                                        " текста." + T_CLOSE, "Ответ без раздумий."]])
        if STATE["n"] == 3:
            return sse(self, [{"content": "Готово."}, {"content": " " + T_OPEN + "хвост раздумий"}])
        return sse(self, [{"content": UTF8_TEXT[i:i + 1]} for i in range(len(UTF8_TEXT))])


class RecordingBridge(QueueBridge):
    def drain(self):
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except Exception:
                return events


def llm_for(cfg):
    client = llm_mod.LLM(cfg)
    client.model = "think/mock"
    return client


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Streamer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    cfg = config_mod.load()
    cfg["providers"]["thinkmock"] = {"label": "ThinkMock", "base_url": "http://127.0.0.1:%d/v1" % PORT,
                                     "api_key": "x", "autostart": False}
    cfg["provider"] = "thinkmock"
    cfg["model"] = "think/mock"
    cfg["tool_protocol"] = "json"
    cfg["max_steps"] = 4

    # 1. native reasoning channel arrives separately from the answer
    msg = llm_for(cfg).complete([{"role": "user", "content": "проверь"}], tools=None, protocol="json")
    check("Готово: папка пустая." in (msg.get("content") or ""), "native: ответ не собран из стрима")
    check("Думаю: сначала проверю папку." in (msg.get("reasoning") or ""), "native: reasoning_content не пойман")

    # 2. inline
    splitter = llm_mod.ThinkSplitter()
    visible = "".join(splitter.feed(p) for p in [T_OPEN + "Рассуждаю вслух", " и добавляю ещё",
                                                 " текста." + T_CLOSE, "Ответ без раздумий."])
    check(visible == "Ответ без раздумий.", "splitter: раздумья не отделились от ответа (%r)" % visible)
    check(splitter.reason == "Рассуждаю вслух и добавляю ещё текста.",
          "splitter: раздумья не собраны (%r)" % splitter.reason)

    # 3. reasoning never reaches the answer channel, even unclosed
    STATE["n"] = 2
    msg2 = llm_for(cfg).complete([{"role": "user", "content": "ещё"}], tools=None, protocol="json")
    check(T_OPEN not in (msg2.get("content") or ""), "стрим: метка раздумий осталась в ответе")
    check(msg2.get("content") == "Готово.", "стрим: ответ испорчен (%r)" % msg2.get("content"))
    check("хвост раздумий" in (msg2.get("reasoning") or ""), "стрим: незакрытые раздумья не пойманы")

    # 4. cyrillic survives the chunked stream
    STATE["n"] = 3
    msg3 = llm_for(cfg).complete([{"role": "user", "content": "кодировка"}], tools=None, protocol="json")
    check(msg3.get("content") == UTF8_TEXT, "стрим: кириллица побилась (%r)" % msg3.get("content"))

    # 5. the agent splits events into reason (live) and thinking (durable)
    STATE["n"] = 0
    bridge = RecordingBridge()
    agent = Agent(dict(cfg), bridge, session_mod.create("тест раздумий"))
    agent.llm.probe_protocol = lambda tools, model=None: "json"
    result = agent.run("проверь раздумья", history=[])
    events = bridge.drain()
    kinds = [e.get("type") for e in events]
    reason_events = [e for e in events if e.get("type") == "reason"]
    thinking_events = [e for e in events if e.get("type") == "thinking"]
    check("Думаю: сначала проверю папку." in "".join(e["text"] for e in reason_events),
          "агент: живые раздумья не дошли до моста")
    check(thinking_events and "Думаю" in thinking_events[0]["text"], "агент: durable thinking-событие не создано")
    check("Думаю" not in (result.get("answer") or ""), "агент: раздумья попали в ответ")
    check(all("Думаю" not in e.get("text", "") for e in events if e.get("type") == "delta"),
          "агент: раздумья утекли в delta")

    # 6. the web bridge does not journal transient events
    from harness.server import SseBridge
    from harness.server import Run

    class SilentRun(Run):
        def broadcast(self, event):
            pass

    run = SilentRun("sid-test-%d" % int(time.time() * 1000))
    web = SseBridge(run)
    for kind in ("delta", "reason", "speed", "tool_start", "thinking"):
        web.emit({"type": kind, "text": "x"})
    saved = session_mod.load(run.sid)
    types = [e.get("type") for e in (saved or {}).get("events", [])]
    check(types == ["thinking"], "server: транзиентные события записались в сессию (%s)" % types)

    print("FAILURES: %d" % len(failures))
    for item in failures:
        print(" -", item)
    httpd.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
