"""Runtime proof: the model that misses the protocol format gets pulled back instead of ending the run.

The scenario mirrors the real failure: a local model starts rewriting the tool observations
instead of answering, or answers with nothing at all. Before this recovery the run just closed
with an empty answer; now the harness states the required format and gives the model another turn.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
# config.json is gitignored and absent on a fresh clone: load() must return defaults
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")
from harness import config as config_mod  # noqa: E402
from harness.agent import Agent, Bridge  # noqa: E402
from harness import session as session_mod  # noqa: E402

LOG = open(os.path.join(ROOT, "tests", "format_retry.log"), "w", encoding="utf-8", buffering=1)

# what a confused local model produces: the observation transcript, not an answer
ECHO = ("user: [web_search] -> results for LM Studio\n"
        "[web_fetch] -> page body\n"
        "user: [shell] -> ok\n")
EMPTY = ""

SCRIPT_JSON = [
    {"role": "assistant", "content": ECHO},      # 1st turn: echoes observations
    {"role": "assistant", "content": ECHO},      # 2nd turn: still confused
    {"role": "assistant", "content": '{"final":"JSON_RECOVERED"}'},
]
SCRIPT_JSON_EMPTY = [
    {"role": "assistant", "content": EMPTY},
    {"role": "assistant", "content": '{"final":"JSON_RECOVERED_AFTER_EMPTY"}'},
]
SCRIPT_NATIVE = [
    {"role": "assistant", "content": ECHO},
    {"role": "assistant", "content": "NATIVE_RECOVERED"},
]


class Scripted(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script = []
    state = {"n": 0}

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send({"object": "list", "data": [{"id": "scripted/model", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        native = bool(req.get("tools"))
        script = self.script
        idx = self.state["n"]
        self.state["n"] = idx + 1
        message = script[min(idx, len(script) - 1)]
        LOG.write("turn native=%s idx=%d -> %s\n" % (native, idx, json.dumps(message, ensure_ascii=False)[:200]))
        self._send({"choices": [{"message": message}]})


class TestBridge(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.events = []

    def emit(self, event):
        self.events.append(event)
        LOG.write("event %-12s %s\n" % (event.get("type"), json.dumps(event, ensure_ascii=False)[:220]))


def run_case(protocol, script, port):
    Scripted.script = script
    Scripted.state = {"n": 0}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Scripted)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = config_mod.load()
    base["providers"]["scripted"] = {"label": "Scripted", "base_url": "http://127.0.0.1:%d/v1" % port, "api_key": "x"}
    base["provider"] = "scripted"
    base["model"] = "scripted/model"
    base["max_steps"] = 8
    base["permissions"] = {"mode": "auto"}
    bridge = TestBridge()
    sid = session_mod.create("тест формата")
    agent = Agent(base, bridge, sid)
    agent.llm.probe_protocol = lambda tools, model=None: protocol
    try:
        result = agent.run("ответь на задачу", history=[])
    finally:
        httpd.shutdown()
    return bridge, result


def main():
    failures = []

    bridge, res = run_case("json", SCRIPT_JSON, 12996)
    notices = [e.get("text") for e in bridge.events if e.get("type") == "notice"]
    LOG.write("json notices: %s\n" % notices)
    if not notices:
        failures.append("json: харнес не подсказал формат")
    elif "не распознан" not in " ".join(notices):
        failures.append("json: подсказа про формат нет (%s)" % notices)
    if "JSON_RECOVERED" not in (res.get("answer") or ""):
        failures.append("json: прогон не восстановился, ответ=%r" % res.get("answer"))
    if "не дала ответа" in (res.get("answer") or ""):
        failures.append("json: прогон закончился заглушкой вместо ответа")

    bridge2, res2 = run_case("json", SCRIPT_JSON_EMPTY, 12997)
    if "JSON_RECOVERED_AFTER_EMPTY" not in (res2.get("answer") or ""):
        failures.append("json: пустой ответ не вытащил модель (%r)" % res2.get("answer"))

    bridge3, res3 = run_case("native", SCRIPT_NATIVE, 12995)
    notices3 = [e.get("text") for e in bridge3.events if e.get("type") == "notice"]
    if not notices3:
        failures.append("native: харнес не подсказал формат")
    if "NATIVE_RECOVERED" not in (res3.get("answer") or ""):
        failures.append("native: прогон не восстановился, ответ=%r" % res3.get("answer"))

    print("FAILURES: %d" % len(failures))
    for i, item in enumerate(failures, 1):
        print(" - #%d %s" % (i, item))
    LOG.write("done failures=%d %s\n" % (len(failures), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
