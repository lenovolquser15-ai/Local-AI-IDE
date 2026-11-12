"""Upgrades proof: SSH hardening/profiles/download, PowerShell UTF-8, screenshot regions, goal refinement."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
# config.json is gitignored and absent on a fresh clone: load() must return defaults
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")
from harness import config as config_mod  # noqa: E402
from harness import schemas  # noqa: E402
from harness import tools as tools_mod  # noqa: E402
from harness.agent import Agent, Bridge, refine_goal  # noqa: E402
from harness import session as session_mod  # noqa: E402

TMP = os.path.join(ROOT, "tests", ".tmp").replace("\\", "/")
FAILURES = []


def check(name, cond, detail=""):
    print("%s %s" % ("ok  " if cond else "FAIL", name), detail)
    if not cond:
        FAILURES.append(name)


def fake_run(captured):
    def run(argv, **kw):
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")
    return run


def test_ssh():
    cfg = {"workspace": TMP, "ssh_profiles": {"vps": {"host": "1.2.3.4", "user": "root", "port": 2201}}}
    tk = tools_mod.Toolkit(cfg)
    cap = []
    with patch.object(tools_mod.subprocess, "run", fake_run(cap)):
        res = tk.ssh(command="uptime", host="h", user="u", port=2222, key="K")
    argv = cap[-1]
    check("ssh ok", res.get("ok"))
    check("ssh BatchMode", "BatchMode=yes" in argv)
    check("ssh accept-new", "StrictHostKeyChecking=accept-new" in argv)
    check("ssh keepalive", "ServerAliveInterval=15" in argv)
    check("ssh port/key/target", argv[argv.index("-p") + 1] == "2222" and "u@h" in argv)
    check("ssh выдуманный ключ заменён на реальный",
          "-i" in argv and os.path.isfile(argv[argv.index("-i") + 1])
          and "не найден" in str(res.get("note") or ""), str(res.get("note") or "")[:80])

    real_key = os.path.join(TMP, "real_key")
    open(real_key, "w").write("x")
    cap.clear()
    with patch.object(tools_mod.subprocess, "run", fake_run(cap)):
        tk.ssh(command="uptime", host="h", user="u", port=2222, key=real_key)
    argv = cap[-1]
    check("ssh существующий ключ не трогаем", argv[argv.index("-i") + 1] == real_key)

    cap.clear()
    with patch.object(tools_mod.subprocess, "run", fake_run(cap)):
        tk.ssh(command="uptime", profile="vps")
    argv = cap[-1]
    check("profile port respected", argv[argv.index("-p") + 1] == "2201", str(argv[:6]))
    check("profile target", "root@1.2.3.4" in argv)

    key_dir = os.path.join(TMP, "fakehome")
    os.makedirs(os.path.join(key_dir, ".ssh"), exist_ok=True)
    key_path = os.path.join(key_dir, ".ssh", "id_ed25519")
    open(key_path, "w").write("x")
    orig = os.path.expanduser
    cap.clear()
    with patch.object(tools_mod.os.path, "expanduser", lambda p: key_dir if p == "~" else orig(p)):
        with patch.object(tools_mod.subprocess, "run", fake_run(cap)):
            tk.ssh(command="uptime", host="h")
    argv = cap[-1]
    check("auto key from ~/.ssh", argv[argv.index("-i") + 1] == key_path)

    cap.clear()
    with patch.object(tools_mod.subprocess, "run", fake_run(cap)):
        res = tk.ssh_download(remote_path="/etc/hostname", local_path=os.path.join(TMP, "dl.txt"),
                              host="h", user="u")
    argv = cap[-1]
    check("scp download order", argv[-2] == "u@h:/etc/hostname" and argv[-1].endswith("dl.txt"), str(argv[-2:]))
    check("scp download port flag", "-P" in argv and "-p" not in argv)

    cfg_path = os.path.join(TMP, "upgrades_config.json")
    tk2 = tools_mod.Toolkit({"workspace": TMP, "ssh_profiles": {}})
    with patch.object(config_mod, "CONFIG_PATH", cfg_path):
        res = tk2.ssh_profile(action="save", name="myvps", host="5.6.7.8", user="admin", port=22)
        listed = tk2.ssh_profile(action="list")
    saved = json.load(open(cfg_path, encoding="utf-8"))
    check("profile save", res.get("ok") and saved["ssh_profiles"]["myvps"]["host"] == "5.6.7.8")
    check("profile list", listed["profiles"]["myvps"]["user"] == "admin")


def fake_capture(captured):
    def capture(argv, timeout=None, cwd=None, flags=0):
        captured.append(list(argv))
        return 0, b"ok", b"", False
    return capture


def test_powershell():
    tk = tools_mod.Toolkit({"workspace": TMP})
    cap = []
    with patch.object(tools_mod, "run_capture", fake_capture(cap)):
        tk.shell(cmd="Write-Output 'x'", mode="powershell")
    check("ps utf8 prepend", cap[-1][-1].startswith("[Console]::OutputEncoding"), cap[-1][-1][:60])
    res = tk.shell(cmd="Write-Output 'привет мир'", timeout=30)
    check("ps real cyrillic", res.get("ok") and "привет мир" in res.get("stdout", ""), res.get("stdout", "")[:60])


def test_screenshot():
    tk = tools_mod.Toolkit({"workspace": TMP})
    res = tk.screenshot(region="rect", x=0, y=0, w=120, h=80)
    ok = res.get("ok") and os.path.isfile(res.get("path", ""))
    if ok:
        from PIL import Image
        with Image.open(res["path"]) as im:
            ok = im.size == (120, 80)
    check("screenshot rect", ok, str(res.get("size") or res.get("error")))
    wins = tk.windows(action="list").get("windows", [])
    if wins:
        res = tk.screenshot(region="window", hwnd=wins[0]["hwnd"], focus=False)
        check("screenshot window", res.get("ok") and os.path.isfile(res.get("path", "")),
              str(res.get("size") or res.get("error")))
    else:
        check("screenshot window", False, "нет окон для теста")
    res = tk.screenshot(region="rect", x=0, y=0)
    check("screenshot rect validation", not res.get("ok"))
    res = tk.screenshot(region="bogus")
    check("screenshot bad region", not res.get("ok"))


class FakeLLM:
    def __init__(self, content):
        self.content = content

    def complete(self, messages, **kw):
        return {"content": self.content}


def test_refine_unit():
    goal = {"objective": "залей сайт на впс", "criteria": ""}
    good = FakeLLM('```json\n{"objective":"Развернуть сайт на VPS","criteria":"1. nginx отвечает 200\\n2.index.html на месте","plan":["залить","проверить"]}\n```')
    out = refine_goal(good, goal)
    check("refine parse", out and out["objective"] == "Развернуть сайт на VPS" and "nginx" in out["criteria"] and len(out["plan"]) == 2)
    check("refine fallback junk", refine_goal(FakeLLM("просто текст без json"), goal) is None)
    check("refine fallback empty obj", refine_goal(FakeLLM('{"objective":""}'), goal) is None)

    class Boom:
        def complete(self, *a, **kw):
            raise RuntimeError("нет связи")
    check("refine fallback error", refine_goal(Boom(), goal) is None)


def test_finish_guard():
    cfg = config_mod.load()
    cfg["workspace"] = TMP
    sid = session_mod.create("guard")
    agent = Agent(cfg, Bridge(), sid)
    agent.goal = {"objective": "тест"}
    call = {"function": {"name": "finish_goal", "arguments": json.dumps({"achieved": True, "summary": "готово"})}}
    _, _, res = agent._record_call(call)
    check("finish without check rejected", not res.get("ok") and "НЕ ПРОВЕРЕНА" in res.get("error", ""))
    check("goal_state still open", agent.goal_state is None)
    call = {"function": {"name": "list_dir", "arguments": json.dumps({"path": TMP})}}
    agent._record_call(call)
    check("real tool counted", agent._real_tools == 1)
    call = {"function": {"name": "finish_goal", "arguments": json.dumps({"achieved": True, "summary": "готово", "evidence": "вывод"})}}
    _, _, res = agent._record_call(call)
    check("finish after check accepted", res.get("ok") and agent.goal_state["achieved"])


STATE = {"i": 0}
SCRIPT = [
    {"content": '{"objective":"Создать файл goal_proof.txt со строкой OK","criteria":"1. файл существует\\n2. содержит OK","plan":["создать","прочитать","закрыть"]}'},
    {"content": '{"tool":"write_file","args":{"path":"%s/goal_proof.txt","content":"OK"}}' % TMP},
    {"content": '{"tool":"read_file","args":{"path":"%s/goal_proof.txt"}}' % TMP},
    {"content": '{"tool":"finish_goal","args":{"achieved":true,"summary":"файл создан и проверен","evidence":"read_file вернул OK"}}'},
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
        self.rfile.read(length)
        idx = STATE["i"]
        STATE["i"] += 1
        self._send({"choices": [{"message": SCRIPT[min(idx, len(SCRIPT) - 1)]}]})


class Collect(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_goal_flow():
    httpd = ThreadingHTTPServer(("127.0.0.1", 12993), Scripted)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    cfg = config_mod.load()
    cfg["providers"]["scripted"] = {"label": "Scripted", "base_url": "http://127.0.0.1:12993/v1", "api_key": "x"}
    cfg["provider"] = "scripted"
    cfg["model"] = "scripted/model"
    cfg["workspace"] = TMP
    cfg["max_steps"] = 10
    cfg["permissions"] = {"mode": "auto"}
    bridge = Collect()
    sid = session_mod.create("goal-flow")
    agent = Agent(cfg, bridge, sid)
    agent.llm.probe_protocol = lambda tools, model=None: "json"
    proof = os.path.join(TMP, "goal_proof.txt")
    if os.path.exists(proof):
        os.remove(proof)
    result = agent.run("создай файл с OK", history=[], goal={"objective": "создай файл с OK", "criteria": ""})
    goals = [e for e in bridge.events if e.get("type") == "goal"]
    states = [g["state"] for g in goals]
    check("goal refined emitted", "refined" in states, str(states))
    refined = next((g for g in goals if g["state"] == "refined"), {})
    check("refined criteria", "существует" in refined.get("criteria", ""))
    started = next((g for g in goals if g["state"] == "started"), {})
    check("started uses refined objective", started.get("objective") == "Создать файл goal_proof.txt со строкой OK")
    check("plan emitted", any(e.get("type") == "plan" and "План:" in e.get("text", "") for e in bridge.events))
    check("goal achieved", result.get("goal", {}).get("achieved") is True, str(result.get("goal")))
    check("proof file exists", os.path.isfile(proof))
    httpd.shutdown()


def test_machine_guards():
    """Команды, уносящие машину, запрещены даже в режиме unrestricted."""
    from harness import permissions as perm_mod
    for cmd in ["Restart-Computer -Force", "shutdown /r /t 0", "netsh winsock reset",
                "Stop-Computer", "wmic os where 1=1 call reboot"]:
        decision, reason = perm_mod.decide("powershell", {"cmd": cmd}, "unrestricted")
        check("guard: %s не заблокирован" % cmd, decision == "deny", "%s / %s" % (decision, reason))
    decision, _ = perm_mod.decide("shell", {"cmd": "Get-ChildItem"}, "unrestricted")
    check("guard: обычная команда разрешена", decision == "allow", decision)
    decision, _ = perm_mod.decide("shell", {"cmd": "Restart-Computer"}, "auto")
    check("guard: блокировка и в режиме auto", decision == "deny", decision)


def test_agent_blocks_reboot():
    """Агент не должен выполнить перезагрузку, даже если её попросила модель."""
    import json as _json
    from harness.agent import Agent, Bridge
    cfg = config_mod.load()
    cfg["workspace"] = TMP
    cfg["permissions"] = {"mode": "unrestricted"}
    agent = Agent(cfg, Bridge(), "test-guard")
    called = []
    agent.tools.call = lambda name, args: called.append((name, args)) or {"ok": True}
    name, args, result = agent._record_call({"function": {"name": "powershell",
                                                          "arguments": _json.dumps({"cmd": "Restart-Computer -Force"})}})
    check("guard: команда перезагрузки не выполнена", not called, str(called))
    check("guard: модели вернули объяснение", "ЗАПРЕЩЕНО" in str(result.get("error") or ""),
          str(result.get("error"))[:120])
    check("guard: в ответе есть запасной путь",
          "web_fetch" in str(result.get("error") or "") or "ask_user" in str(result.get("error") or ""),
          str(result.get("error"))[:120])


def test_browser_hint():
    """Падение CDP должно объяснять, что делать, а не провоцировать перезагрузку."""
    tk = tools_mod.Toolkit({"workspace": TMP, "chrome_cdp": "http://127.0.0.1:9222"})
    res = tk.browser(action="open", url="https://example.com")
    text = str(res.get("error") or "")
    check("browser: нет внятной подсказки (%s)" % text[:80],
          "нельзя" in text.lower() and ("web_fetch" in text or "web_search" in text))


def test_browser_ws_handshake():
    """_connect обязан вернуть фрейм с сокетом и сохранить хвост после заголовков."""
    from harness import browser as browser_mod

    class FakeSock:
        def __init__(self, data):
            self.data = data
            self.sent = []

        def sendall(self, b):
            self.sent.append(b)

        def settimeout(self, t):
            pass

        def recv(self, n):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def close(self):
            pass

    frame_bytes = b"\x81\x05hello"
    resp = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n" + frame_bytes
    sock = FakeSock(resp)
    with patch.object(browser_mod.socket, "create_connection", return_value=sock):
        got_sock, frame = browser_mod._connect("ws://127.0.0.1:9222/devtools/page/x")
    check("ws: фрейм получил сокет", got_sock is sock and frame.sock is sock)
    check("ws: хвост handshake не потерян", frame.read_frame(1) == b"hello")
    check("ws: upgrade-запрос отправлен", sock.sent and b"Upgrade: websocket" in sock.sent[0])


def test_config_patch_race():
    """Одновременные patch конфига не должны перетирать друг друга (load→update→save под lock)."""
    import tempfile
    from harness import config as c
    old_path = c.CONFIG_PATH
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    c.CONFIG_PATH = path
    try:
        c.save({})
        errors = []

        def patch(k, v):
            try:
                with c.LOCK:
                    cfg = c.load()
                    cfg[k] = v
                    c.save(cfg)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=patch, args=("key%d" % i, i), daemon=True) for i in range(12)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        cfg = c.load()
        check("config race: все ключи на месте", all(cfg.get("key%d" % i) == i for i in range(12)),
              str(sorted(k for k in cfg if k.startswith("key"))))
        check("config race: без ошибок", not errors, str(errors))
    finally:
        c.CONFIG_PATH = old_path
        os.remove(path)


def main():
    os.makedirs(TMP, exist_ok=True)
    test_ssh()
    test_powershell()
    test_screenshot()
    test_refine_unit()
    test_finish_guard()
    test_goal_flow()
    test_machine_guards()
    test_agent_blocks_reboot()
    test_browser_hint()
    test_browser_ws_handshake()
    test_config_patch_race()
    print("FAILURES:", len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())