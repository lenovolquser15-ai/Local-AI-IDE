"""Runtime proof: the loop, tools, guardrails and both wire protocols, no GPU needed."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "C:/LocalHarness")
# sessions must not land in the app's real data/sessions
os.environ["LH_DATA"] = "C:/LocalHarness/tests/.tmp"
from harness import schemas  # noqa: E402
from harness.agent import Agent, Bridge  # noqa: E402
from harness import session as session_mod  # noqa: E402

LOG = open("C:/LocalHarness/tests/run.log", "w", encoding="utf-8", buffering=1)
STATE = {"native": 0, "json": 0}
SCRIPT_NATIVE = [
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a1", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"cmd": "Get-Location"})}}]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a2", "type": "function", "function": {"name": "python", "arguments": json.dumps({"code": "1+1"})}}]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a3", "type": "function", "function": {"name": "shell",
            "arguments": json.dumps({"cmd": "Remove-Item -Recurse -Force C:\\Windows\\Temp"})}}]},
    {"role": "assistant", "content": "FINAL_NATIVE_ANSWER"},
]
SCRIPT_JSON = [
    {"role": "assistant", "content": 'Решаю задачу. {"tool":"web_search","args":{"query":"LM Studio server port"}}'},
    {"role": "assistant", "content": '```json\n{"tools":[{"tool":"list_dir","args":{"path":"C:/LocalHarness"}}],"note":"смотрю файлы"}\n```'},
    {"role": "assistant", "content": '{"final":"FINAL_JSON_ANSWER"}'},
]


class Scripted(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        idx = STATE["native"] if native else STATE["json"]
        script = SCRIPT_NATIVE if native else SCRIPT_JSON
        STATE["native" if native else "json"] = idx + 1
        message = script[min(idx, len(script) - 1)]
        LOG.write("server turn native=%s idx=%d\n" % (native, idx))
        self._send({"choices": [{"message": message}]})


class TestBridge(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.events = []
        self.approvals = []

    def emit(self, event):
        self.events.append(event)
        LOG.write("event %-12s %s\n" % (event.get("type"), json.dumps(event, ensure_ascii=False)[:220]))
        if event.get("type") == "approve":
            self.approvals.append(event)


def wait_for_approval(bridge, allow=True):
    def loop():
        for _ in range(600):
            if bridge.approvals:
                event = bridge.approvals.pop(0)
                bridge.respond(event["key"], {"allow": allow})
                return
            time.sleep(0.05)
    threading.Thread(target=loop, daemon=True).start()


def run_case(cfg, protocol, bridge):
    sid = session_mod.create("тест")
    agent = Agent(cfg, bridge, sid)
    agent.llm.probe_protocol = lambda tools, model=None: protocol
    result = agent.run("сделай проверку", history=[])
    return sid, result


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", 12998), Scripted)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = load_cfg = json.load(open("C:/LocalHarness/config.json", encoding="utf-8"))
    base["providers"]["scripted"] = {"label": "Scripted", "base_url": "http://127.0.0.1:12998/v1", "api_key": "x"}
    base["provider"] = "scripted"
    base["model"] = "scripted/model"
    base["max_steps"] = 8
    base["permissions"] = {"mode": "auto"}

    failures = []

    bridge = TestBridge()
    wait_for_approval(bridge, allow=False)
    _, res = run_case(dict(base), "native", bridge)
    names = [e.get("tool") for e in bridge.events if e.get("type") == "tool"]
    LOG.write("native tools: %s\n" % names)
    if "shell" not in names or "python" not in names:
        failures.append("native: инструменты не дошли до исполнения")
    if not bridge.approvals and not any(e["type"] == "approve" for e in bridge.events):
        failures.append("native: опасная команда не спросила подтверждение")
    if "FINAL_NATIVE_ANSWER" not in (res.get("answer") or ""):
        failures.append("native: финальный ответ не пойман (%r)" % res.get("answer"))
    if "отказано пользователем" not in json.dumps(bridge.events, ensure_ascii=False).lower():
        failures.append("native: отказ пользователя не вернулся модели")

    bridge2 = TestBridge()
    _, res2 = run_case(dict(base), "json", bridge2)
    names2 = [e.get("tool") for e in bridge2.events if e.get("type") == "tool"]
    LOG.write("json tools: %s\n" % names2)
    if "web_search" not in names2 or "list_dir" not in names2:
        failures.append("json: парсер не вытащил вызовы (%s)" % names2)
    if "FINAL_JSON_ANSWER" not in (res2.get("answer") or ""):
        failures.append("json: финальный ответ не пойман (%r)" % res2.get("answer"))

    from harness.agent import fold
    fat = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": "x" * 4000} for _ in range(12)]
    folded, dropped = fold(fat, 30000)
    if dropped == 0 or folded[1].get("role") != "system":
        failures.append("fold: сжатие не сработало (%s)" % dropped)
    from harness.agent import _estimate_units, fold_hard
    oversized = [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "x" * 100000}]
    hard, _ = fold_hard(oversized, 1200)
    if _estimate_units(hard) > 1200:
        failures.append("fold_hard: результат превышает бюджет (%s)" % _estimate_units(hard))

    stop_probe = {"called": False}
    stop_bridge = Bridge()
    stop_bridge.set_stop_callback(lambda: stop_probe.__setitem__("called", True))
    stop_bridge.request_stop()
    if not stop_probe["called"] or not stop_bridge.stop.is_set():
        failures.append("stop: callback не вызван немедленно")

    from harness import llm as llm_mod
    parsed = llm_mod.parse_json_protocol('Думаю <think>mm</think> {"tool":"shell","args":{"cmd":"dir"},"note":"раз"}')
    if not parsed["calls"] or parsed["calls"][0]["function"]["name"] != "shell":
        failures.append("parse_json_protocol: не разобрал простой вызов")
    if "<think>" in llm_mod.strip_reasoning("a<think>b</think>c"):
        failures.append("strip_reasoning:.reasoning не срезан")

    print("FAILURES: %d" % len(failures))
    for i, item in enumerate(failures, 1):
        print(" - #%d (см. tests/run.log)" % i)
    LOG.write("done failures=%d %s\n" % (len(failures), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
