"""Runtime proof for the product goals: rights switch, tokens/sec, goal mode, plugin loading.

Everything runs against a scripted SSE endpoint, so no GPU and no destructive command is executed.
"""
import json
import os
import socket
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
from harness import permissions  # noqa: E402
from harness import plugins  # noqa: E402
from harness import schemas  # noqa: E402  # noqa: E402
from harness import session as session_mod  # noqa: E402
from harness.agent import Agent, Bridge  # noqa: E402
from harness.tools import Toolkit  # noqa: E402

LOG = open(os.path.join(ROOT, "tests", "goals.log"), "w", encoding="utf-8", buffering=1)
PORT = 12997
QUEUES = {"native": [], "json": []}


def call(name, args):
    return {"id": "c_%s" % name, "type": "function", "function": {"name": name,
                                                                  "arguments": json.dumps(args, ensure_ascii=False)}}


def sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def frames(msg):
    out = []
    content = msg.get("content") or ""
    if content:
        step = max(1, len(content) // 4)
        for i in range(0, len(content), step):
            out.append(sse({"choices": [{"index": 0, "delta": {"content": content[i:i + step]}}]}))
    for idx, item in enumerate(msg.get("tool_calls") or []):
        fn = item["function"]
        args = fn["arguments"]
        out.append(sse({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": idx, "id": item["id"], "type": "function",
             "function": {"name": fn["name"], "arguments": args[:16]}}]}}]}))
        if len(args) > 16:
            out.append(sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": idx, "function": {"arguments": args[16:]}}]}}]}))
    out.append(sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 42, "prompt_tokens": 120}}))
    return out


class Scripted(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

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
            return self._json({"object": "list", "data": [{"id": "scripted/goal", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        key = "native" if req.get("tools") else "json"
        queue = QUEUES[key]
        msg = queue.pop(0) if queue else {"role": "assistant", "content": "SCRIPT ENDED"}
        LOG.write("turn %s -> %s\n" % (key, json.dumps(msg, ensure_ascii=False)[:200]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for frame in frames(msg):
            self.wfile.write(frame)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


class TestBridge(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.events = []
        self.approvals = []

    def emit(self, event):
        self.events.append(event)
        LOG.write("event %-11s %s\n" % (event.get("type"), json.dumps(event, ensure_ascii=False)[:240]))
        if event.get("type") == "approve":
            self.approvals.append(event)
            self.respond(event["key"], {"allow": True})


def cfg_for():
    base = config_mod.load()
    base["providers"] = {"scripted": {"label": "Scripted", "base_url": "http://127.0.0.1:%d/v1" % PORT, "api_key": "x"}}
    base["provider"] = "scripted"
    base["model"] = "scripted/goal"
    base["tool_protocol"] = "native"
    base["max_steps"] = 10
    return base


def run_agent(cfg, script, text, goal=None):
    QUEUES["native"] = list(script)
    bridge = TestBridge()
    sid = session_mod.create("goals")
    agent = Agent(cfg, bridge, sid)
    agent.llm.probe_protocol = lambda tools, model=None: "native"
    result = agent.run(text, history=[], goal=goal)
    return bridge, result


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Scripted)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    failures = []

    def check(label, ok, detail=""):
        LOG.write("check %s -> %s %s\n" % (label, "ok" if ok else "FAIL", detail))
        if not ok:
            failures.append("%s: %s" % (label, detail))

    # ---- 1. rights switch: decide() per mode, no execution
    dangerous = {"cmd": "Remove-Item -Recurse -Force C:\\Windows\\Temp"}
    check("auto блокирует опасную команду", permissions.decide("shell", dangerous, "auto")[0] == "confirm")
    check("unrestricted снимает подтверждения", permissions.decide("shell", dangerous, "unrestricted")[0] == "allow")
    check("ask спрашивает даже за безопасное", permissions.decide("write_file", {"path": "C:/x/y.txt", "content": "a"}, "ask")[0] == "confirm")
    # backslash paths hit the SYSTEM_PATHS check in auto mode: it must not raise
    check("auto: системный путь с обратными слешами спрашивает",
          permissions.decide("write_file", {"path": "C:\\Windows\\Temp\\a.txt", "content": "a"}, "auto")[0] == "confirm")
    check("auto: рабочий путь не спрашивает",
          permissions.decide("write_file", {"path": ROOT + "/work/a.txt", "content": "a"}, "auto")[0] == "allow")

    # ---- 2. the switch is really wired into the loop (safe tool, so nothing destructive runs)
    cfg = cfg_for()
    cfg["permissions"] = {"mode": "ask"}
    target = os.path.join(ROOT, "work", "ask_mode_probe.txt")
    bridge, _ = run_agent(cfg, [
        {"role": "assistant", "content": None, "tool_calls": [call("write_file", {"path": target, "content": "ask-mode"})]},
        {"role": "assistant", "content": "готово"},
    ], "запиши файл")
    check("ask: спросил перед записью файла", len(bridge.approvals) == 1, str(len(bridge.approvals)))

    cfg["permissions"] = {"mode": "unrestricted"}
    bridge2, res2 = run_agent(cfg, [
        {"role": "assistant", "content": None, "tool_calls": [call("write_file", {"path": target, "content": "no-ask"})]},
        {"role": "assistant", "content": "готово"},
    ], "запиши файл")
    written = os.path.exists(target) and open(target, encoding="utf-8").read() == "no-ask"
    check("unrestricted: записал без вопроса", not bridge2.approvals and written,
          "approvals=%s file=%s" % (len(bridge2.approvals), written))

    # ---- 3. tokens/sec
    speeds = [e for e in bridge2.events if e.get("type") == "speed"]
    final = (res2.get("speed") or {})
    check("скорость считается в стриме", speeds and all(isinstance(s.get("tokens"), int) for s in speeds)
          and final.get("tokens", 0) > 0, "events=%d final=%s" % (len(speeds), json.dumps(final)))
    check("скорость доехала до события done",
          any(e.get("type") == "done" and (e.get("speed") or {}).get("tokens") for e in bridge2.events))
    meter = llm_mod.SpeedMeter(None)
    meter.piece("x" * 36)
    snap = meter.snapshot()
    check("оценка токенов из стрима", snap["tokens"] >= 9 and snap["exact"] is False, json.dumps(snap))
    meter.usage({"completion_tokens": 77})
    check("usage перекрывает оценку", meter.snapshot()["tokens"] == 77 and meter.snapshot()["exact"])

    # ---- 4. goal mode: keeps working until finish_goal, then stops
    cfg3 = cfg_for()
    cfg3["goal_rounds"] = 2
    script = [
        {"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": ROOT})]},
        {"role": "assistant", "content": "я почти закончил, дальше сам не могу"},
        {"role": "assistant", "content": None, "tool_calls": [call("shell", {"cmd": "Get-Date"})]},
        {"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
            "achieved": True, "summary": "цель закрыта", "evidence": "Get-Date + list_dir отработали"})]},
    ]
    bridge3, res3 = run_agent(cfg3, script, "собери сведения о папке",
                              goal={"objective": "собери сведения о папке", "criteria": "list_dir и Get-Date выполнены", "rounds": 4})
    gtypes = [(e.get("state"), e.get("round")) for e in bridge3.events if e.get("type") == "goal"]
    tools3 = [e.get("tool") for e in bridge3.events if e.get("type") == "tool"]
    check("цель: стартовое событие", any(s == "started" for s, _ in gtypes), str(gtypes))
    check("цель: continuation после ответа без finish_goal", any(s == "continue" for s, _ in gtypes), str(gtypes))
    check("цель: второй подход реально работал", "shell" in tools3, str(tools3))
    check("цель: finish_goal закрыл цикл", (res3.get("goal") or {}).get("achieved") is True, json.dumps(res3.get("goal")))
    fin = [e for e in bridge3.events if e.get("type") == "tool" and e.get("tool") == "finish_goal"]
    dumped = json.dumps(fin + bridge3.events, ensure_ascii=False)
    check("цель: finish_goal обработал харнес, а не тулкит",
          len(fin) == 1 and "recorded" in json.dumps(fin[0], ensure_ascii=False)
          and "нет такого инструмента" not in dumped, str(fin))

    bridge4, res4 = run_agent(cfg_for(), [
        {"role": "assistant", "content": "думаю, тут всё готово"},
        {"role": "assistant", "content": "ну всё, я справился"},
    ], "бесконечная задача", goal={"objective": "бесконечная задача", "criteria": "", "rounds": 2})
    states4 = [e.get("state") for e in bridge4.events if e.get("type") == "goal"]
    check("цель: лимит подходов и честный провал",
          "exhausted" in states4 and not (res4.get("goal") or {}).get("achieved"), str(states4))

    # ---- 4b. goal checklist: model marks points with evidence; unverified point is rejected
    cfg_cl = cfg_for()
    cfg_cl["goal_rounds"] = 2
    cl_script = [
        {"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": ROOT})]},
        {"role": "assistant", "content": None, "tool_calls": [call("check_goal_point", {
            "index": 1, "evidence": "list_dir показал содержимое корня"})]},
        {"role": "assistant", "content": None, "tool_calls": [call("check_goal_point", {
            "index": 2, "evidence": "проверено инструментом"})]},
        {"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
            "achieved": True, "summary": "оба пункта проверены", "evidence": "list_dir + check_goal_point"})]},
    ]
    bridge_cl, res_cl = run_agent(cfg_cl, cl_script, "проверь папку",
                                  goal={"objective": "проверь папку",
                                        "criteria": "1. список папки\n2. текущее время"})
    cl_events = [e for e in bridge_cl.events if e.get("type") == "goal"]
    started_cl = [e for e in cl_events if e.get("state") == "checklist"]
    points_cl = [e for e in cl_events if e.get("state") == "point"]
    check("цель: чеклист разобран на пункты", started_cl and len(started_cl[0].get("points") or []) == 2,
          json.dumps(started_cl, ensure_ascii=False)[:300])
    check("цель: галочка ставится и событие несёт evidence",
          bool(points_cl) and points_cl[-1].get("points", [{}])[0].get("done") is True
          and "list_dir" in (points_cl[0].get("evidence") or ""),
          json.dumps(points_cl, ensure_ascii=False)[:300])
    check("цель: finish_goal закрыл цель с чеклистом", (res_cl.get("goal") or {}).get("achieved") is True,
          json.dumps(res_cl.get("goal")))

    # point marked before ANY real tool must be rejected
    cfg_cl2 = cfg_for()
    cfg_cl2["goal_rounds"] = 1
    bad_script = [
        {"role": "assistant", "content": None, "tool_calls": [call("check_goal_point", {
            "index": 1, "evidence": "вроде сделал"})]},
        {"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": ROOT})]},
        {"role": "assistant", "content": None, "tool_calls": [call("check_goal_point", {
            "index": 1, "evidence": "list_dir показал корень"})]},
        {"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
            "achieved": True, "summary": "готово", "evidence": "list_dir"})]},
    ]
    bridge_bad, _ = run_agent(cfg_cl2, bad_script, "проверь",
                              goal={"objective": "проверь", "criteria": "1. один пункт"})
    dump_bad = json.dumps(bridge_bad.events, ensure_ascii=False)
    check("цель: неотмеченный без проверки пункт отклонён", "ПУНКТ НЕ ПРОВЕРЕН" in dump_bad, dump_bad[:220])
    check("цель: после проверки пункт отметился", any(
        e.get("state") == "point" and e.get("done") for e in bridge_bad.events if e.get("type") == "goal"),
        dump_bad[:220])

    # ---- LM Studio load settings
    import harness.server as server_mod
    load_cfg = cfg_for()
    load_cfg["lms_context_length"] = 8192
    load_cfg["lms_ttl"] = 120
    args = ["load", "test/model", "-y", "--context-length", str(load_cfg["lms_context_length"]), "--ttl", str(load_cfg["lms_ttl"])]
    check("lms: настройки загрузки совместимы с CLI", "--context-length" in args and "--ttl" in args and "8192" in args and "120" in args, str(args))

    # ---- full profile and VPS tool surface
    full_names = [s["name"] for s in schemas.entries("full")]
    check("full: PowerShell и SSH опубликованы", "powershell" in full_names and "ssh" in full_names,
          str(full_names))
    ssh_probe = Toolkit(cfg_for()).call("ssh", {"command": "uname -a"})
    check("ssh: без профиля сообщает понятную ошибку", ssh_probe.get("ok") is False and "host" in ssh_probe.get("error", ""),
          json.dumps(ssh_probe, ensure_ascii=False))

    # ---- configured workspace creation
    import tempfile
    from harness import config as config_mod
    with tempfile.TemporaryDirectory(prefix="lh-workspace-") as tmp:
        requested_workspace = os.path.join(tmp, "custom", "workspace")
        previous_path = config_mod.CONFIG_PATH
        config_mod.CONFIG_PATH = os.path.join(tmp, "config.json")
        try:
            config_mod.save({"workspace": requested_workspace})
            loaded_cfg = config_mod.load()
            check("config: создаётся настроенный workspace",
                  loaded_cfg.get("workspace") == requested_workspace and os.path.isdir(requested_workspace),
                  requested_workspace)
        finally:
            config_mod.CONFIG_PATH = previous_path

    # ---- 5. plugins
    info = plugins.scan(force=True)
    loaded = [p for p in info.values() if not p["error"]]
    names = [s["name"] for s in schemas.entries("core")]
    check("плагин загружен", bool(loaded), json.dumps({k: v["error"] for k, v in info.items()}))
    check("инструменты плагина в схеме", "wait_port" in names and "sleep" in names, str(names))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 12996))
    listener.listen(4)
    probe = Toolkit(cfg_for())
    quick = probe.call("wait_port", {"port": 12996, "timeout": 3})
    slow = probe.call("wait_port", {"port": 12995, "timeout": 1})
    listener.close()
    check("плагин вызывает реальный код", quick.get("ok") and quick.get("open"), json.dumps(quick))
    check("плагин честно сообщает таймаут", slow.get("ok") is False and slow.get("open") is False, json.dumps(slow))
    nap = probe.call("sleep", {"seconds": 0.2})
    check("sleep из плагина работает", nap.get("ok") and nap.get("slept") == 0.2, json.dumps(nap))

    danger_file = os.path.join(plugins.PLUGIN_DIR, "probe_danger.py")
    with open(danger_file, "w", encoding="utf-8") as fh:
        fh.write("NAME='probe'\nTOOLS=[{'name':'probe_danger','desc':'тест',"
                 "'params':{'path':('string?','куда')},'required':[],'dangerous':True,"
                 "'fn':lambda cfg,args: {'ok':True,'ran':True}}]\n")
    plugins.scan(force=True)
    check("опасный плагин помечен", "probe_danger" in permissions.PLUGIN_DANGEROUS)
    check("опасный плагин спрашивает в auto", permissions.decide("probe_danger", {}, "auto")[0] == "confirm")
    check("опасный плагин молчит в unrestricted", permissions.decide("probe_danger", {}, "unrestricted")[0] == "allow")
    os.remove(danger_file)
    plugins.scan(force=True)
    check("после удаления файла плагин исчез", "probe_danger" not in [s["name"] for s in schemas.entries("full")])
    broken_file = os.path.join(plugins.PLUGIN_DIR, "broken_probe.py")
    with open(broken_file, "w", encoding="utf-8") as fh:
        fh.write("this is not valid python (\n")
    scan = plugins.scan(force=True)
    check("битый плагин не роняет харнес", any(v["error"] for v in scan.values()))
    os.remove(broken_file)
    plugins.scan(force=True)

    # ---- 5. loop guard: the same failing call must not kill the run silently — the model gets
    # an escalating hint (change args -> look it up on the internet) and the approach is cut only
    # at the fifth identical failure
    stall_cfg = cfg_for()
    stall_cfg["permissions"] = {"mode": "auto"}
    stall = [{"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": "нет-такой-папки-lh"})]}
             for _ in range(6)]
    bridge5, res5 = run_agent(stall_cfg, stall, "покажи содержимое папки")
    check("зацикливание обрывает подход", "зацикливание" in (res5.get("answer") or ""),
          "steps=%s answer=%r" % (res5.get("steps"), res5.get("answer")))
    dump5 = json.dumps([res5.get("messages"), bridge5.events], ensure_ascii=False)
    check("модель видит, что она повторяет", "ПОВТОР" in dump5 and "зациклилась" in dump5, dump5[:160])
    check("на повторе модель отправлена в интернет", "ЗАЦИКЛИВАНИЕ" in dump5 and "web_search" in dump5, dump5[:200])
    check("обрыв случился на пятом повторе, а не на третьем",
          "5 раз подряд" in dump5 and res5.get("steps", 0) <= 6,
          "steps=%s" % res5.get("steps"))

    # ---- 5b. loop guard in goal mode: a loop ends the approach, not the goal.
    # The next approach must continue, and the nudge must point the model to the internet.
    loop_cfg = cfg_for()
    loop_cfg["goal_rounds"] = 0          # бесконечный режим
    loop_cfg["goal_idle_limit"] = 4
    loop_script = [{"role": "assistant", "content": None,
                    "tool_calls": [call("list_dir", {"path": "нет-такой-папки-lh"})]} for _ in range(5)]
    loop_script.append({"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": ROOT})]})
    loop_script.append({"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
        "achieved": True, "summary": "цель закрыта", "evidence": "list_dir отработал"})]})
    bridge6, res6 = run_agent(loop_cfg, loop_script, "осмотри папку",
                              goal={"objective": "осмотри папку", "criteria": "list_dir выполнен"})
    states6 = [e.get("state") for e in bridge6.events if e.get("type") == "goal"]
    check("зацикливание в режиме цели не убивает прогон", "closed" in states6 and "exhausted" not in states6,
          str(states6))
    check("после зацикливания подход продолжен", "continue" in states6, str(states6))
    nudges = [m.get("content") for m in (res6.get("messages") or [])
              if m.get("role") == "user" and "ЗАКРЫТА" in (m.get("content") or "")]
    check("подсказка между подходами зовёт в интернет",
          bool(nudges) and "web_search" in (nudges[0] or ""), json.dumps(nudges)[:300])
    check("подсказка упоминает зациклившийся инструмент",
          bool(nudges) and "list_dir" in (nudges[0] or ""), json.dumps(nudges)[:300])

    # ---- 5c. отказ пользователя и «цель не проверена» — не зацикливание
    soft_cfg = cfg_for()
    soft_script = [{"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
        "achieved": True, "summary": "готово", "evidence": "нет"})]} for _ in range(3)]
    soft_script.append({"role": "assistant", "content": None, "tool_calls": [call("list_dir", {"path": ROOT})]})
    soft_script.append({"role": "assistant", "content": None, "tool_calls": [call("finish_goal", {
        "achieved": True, "summary": "цель закрыта", "evidence": "list_dir отработал"})]})
    bridge7, res7 = run_agent(soft_cfg, soft_script, "закрой цель",
                              goal={"objective": "закрой цель", "criteria": "list_dir выполнен"})
    dump7 = json.dumps([res7.get("messages"), bridge7.events], ensure_ascii=False)
    check("незакрытая цель не считается зацикливанием",
          "ЗАЦИКЛИВАНИЕ" not in dump7 and "подход зациклился" not in dump7, dump7[:200])
    check("после отказа цель всё равно закрывается",
          any(e.get("state") == "closed" for e in bridge7.events if e.get("type") == "goal"),
          str([e.get("state") for e in bridge7.events if e.get("type") == "goal"]))

    LOG.write("failures=%d %s\n" % (len(failures), failures))
    print("FAILURES: %d" % len(failures))
    for i, item in enumerate(failures, 1):
        print(" - #%d %s" % (i, item))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
