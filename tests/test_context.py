"""Context-compression proof: normalize_messages never trips the Ministral jinja template,
and the infinite goal loop (goal_rounds<=0) runs until finish_goal / stop / stall / idle guard.

The jinja oracle below is a faithful Python replica of the alternation + content checks in
the model's GGUF chat_template (mradermacher/Ministral-3-8B-Reasoning-2512-Uncensored), so we
don't need jinja2 installed. No GPU, no new dependencies.
"""
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "C:/LocalHarness")
os.environ["LH_DATA"] = "C:/LocalHarness/tests/.tmp"
from harness import llm as llm_mod  # noqa: E402
from harness.agent import Agent, Bridge, fold, fold_hard, _goal_rounds_limit  # noqa: E402
from harness import session as session_mod  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("%s %s" % ("ok  " if cond else "FAIL", name), detail)
    if not cond:
        FAILURES.append(name)


def _parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return [{"type": "text", "text": str(content)}]


def jinja_oracle(messages):
    """Raise AssertionError exactly where the Ministral GGUF template would raise_exception.
    Mirrors: one leading system; counted user/assistant-text must alternate from user;
    assistant tool_calls and tool results are transparent; user/assistant content non-empty."""
    if not messages:
        return
    loop = messages
    if messages[0].get("role") == "system":
        if len(_parts(messages[0].get("content"))) == 0:
            raise AssertionError("empty system content")
        loop = messages[1:]
    index = 0
    for msg in loop:
        role = msg.get("role")
        calls = msg.get("tool_calls") or []
        if role == "system":
            raise AssertionError("system message after the first position")
        if role not in ("user", "assistant", "tool"):
            raise AssertionError("unsupported role: %s" % role)
        counted = role == "user" or (role == "assistant" and not calls)
        if counted:
            if (role == "user") != (index % 2 == 0):
                raise AssertionError("alternation broken at index %d (role %s)" % (index, role))
            index += 1
        content = msg.get("content")
        if role == "user":
            if not ((isinstance(content, str) and content != "") or (isinstance(content, list) and len(content) > 0)):
                raise AssertionError("user message has empty content")
        elif role == "assistant":
            empty = content is None or content == "" or (isinstance(content, (list, str)) and len(content) == 0)
            if empty and not calls:
                raise AssertionError("assistant message has neither content nor tool_calls")


def _call(cid, name="f"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_normalize_cases():
    cases = {
        "orphan_tool": [{"role": "system", "content": "s"},
                        {"role": "tool", "tool_call_id": "x", "content": "res"},
                        {"role": "user", "content": "hi"}],
        "two_assistant_via_toolblock": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "A", "tool_calls": [_call("1")]},
            {"role": "tool", "tool_call_id": "1", "content": "r"},
            {"role": "assistant", "content": "B"},
            {"role": "assistant", "content": "C", "tool_calls": [_call("2")]},
            {"role": "tool", "tool_call_id": "2", "content": "r2"},
            {"role": "assistant", "content": "D"}],
        "two_user_via_toolblock": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_call("1")]},
            {"role": "tool", "tool_call_id": "1", "content": "r"},
            {"role": "user", "content": "q2"}],
        "double_user": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        "double_assistant": [{"role": "user", "content": "a"},
                             {"role": "assistant", "content": "b"},
                             {"role": "assistant", "content": "c"}],
        "empty_user": [{"role": "system", "content": "s"}, {"role": "user", "content": ""}],
        "empty_assistant": [{"role": "user", "content": "a"}, {"role": "assistant", "content": ""}],
        "mid_system_digest": [{"role": "system", "content": "s"},
                              {"role": "user", "content": "a"},
                              {"role": "system", "content": "digest"},
                              {"role": "assistant", "content": "b"},
                              {"role": "user", "content": "c"}],
        "starts_assistant": [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "hi"}],
        "starts_tool": [{"role": "tool", "tool_call_id": "z", "content": "r"}, {"role": "user", "content": "hi"}],
        "fold_hard_tail": [{"role": "system", "content": "head"},
                           {"role": "system", "content": "digest"},
                           {"role": "user", "content": "task"},
                           {"role": "assistant", "content": "almost"},
                           {"role": "assistant", "content": None, "tool_calls": [_call("9")]},
                           {"role": "tool", "tool_call_id": "9", "content": "r"},
                           {"role": "assistant", "content": "done"}],
        "image_user": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
        "trailing_tool_calls": [{"role": "user", "content": "q"},
                                {"role": "assistant", "content": None, "tool_calls": [_call("1"), _call("2")]},
                                {"role": "tool", "tool_call_id": "1", "content": "r1"}],
    }
    for name, hist in cases.items():
        out = llm_mod.normalize_messages(hist)
        try:
            jinja_oracle(out)
            check("normalize " + name, True)
        except AssertionError as exc:
            check("normalize " + name, False, "%s | %s" % (exc, [(m["role"], bool(m.get("tool_calls"))) for m in out]))


def test_normalize_fuzz():
    rng = random.Random(20261108)
    roles = ["user", "assistant", "assistant_calls", "tool", "system"]
    broken = 0
    for _ in range(400):
        hist = []
        for _ in range(rng.randint(1, 14)):
            r = rng.choice(roles)
            if r == "system":
                hist.append({"role": "system", "content": rng.choice(["s", "digest", ""])})
            elif r == "user":
                hist.append({"role": "user", "content": rng.choice(["q", "", "задача"])})
            elif r == "assistant":
                hist.append({"role": "assistant", "content": rng.choice(["answer", "", None])})
            elif r == "assistant_calls":
                hist.append({"role": "assistant", "content": rng.choice([None, "plan"]),
                             "tool_calls": [_call("c%d" % rng.randint(0, 3))]})
            else:
                hist.append({"role": "tool", "tool_call_id": "c%d" % rng.randint(0, 3), "content": "r"})
        out = llm_mod.normalize_messages(hist)
        try:
            jinja_oracle(out)
        except AssertionError as exc:
            broken += 1
            if broken <= 3:
                print("   fuzz break:", exc, [(m["role"], bool(m.get("tool_calls"))) for m in out])
    check("normalize fuzz 400 histories", broken == 0, "broken=%d" % broken)


def test_fold_then_normalize():
    """fold/fold_hard produce a mid-history system digest and trimmed tails; normalize must
    still pass the template and stay within budget."""
    rng = random.Random(7)
    for budget in (1200, 8000, 30000):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(40):
            msgs.append({"role": "user", "content": "u" * rng.randint(50, 2000)})
            msgs.append({"role": "assistant", "content": None, "tool_calls": [_call("k%d" % i)]})
            msgs.append({"role": "tool", "tool_call_id": "k%d" % i, "content": "r" * rng.randint(50, 2000)})
            msgs.append({"role": "assistant", "content": "a" * rng.randint(50, 1500)})
        soft, _ = fold(msgs, budget)
        hard, _ = fold_hard(msgs, budget)
        for label, folded in (("fold", soft), ("fold_hard", hard)):
            out = llm_mod.normalize_messages(folded)
            try:
                jinja_oracle(out)
                check("%s->normalize budget %d" % (label, budget), True)
            except AssertionError as exc:
                check("%s->normalize budget %d" % (label, budget), False, str(exc))
    oversized = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 100000}]
    hard, _ = fold_hard(oversized, 1200)
    from harness.agent import _estimate_units
    check("fold_hard respects budget", _estimate_units(hard) <= 1200, str(_estimate_units(hard)))


def test_fold_hard_repeated_goal_rounds():
    """Между подходами цели fold_hard вызывается снова и снова. Самое свежее сообщение
    (напоминание о цели) должно переживать каждое сжатие, а дайджесты не должны копиться:
    живой прогон 2026-11-10 ушёл в runaway, потому что хвост выбрасывался и последним
    сообщением навсегда оставалась исходная задача."""
    from harness.agent import _estimate_units
    budget = 1500
    task = "ЦЕЛЬ: проверить папку и закрыть цель"
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": task}]
    for round_no in range(12):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [_call("r%d" % round_no, "list_dir")]})
        msgs.append({"role": "tool", "tool_call_id": "r%d" % round_no,
                     "content": '{"ok": true, "entries": [%s]}' % (", " * 2000)})
        msgs.append({"role": "assistant", "content": "папку проверил, продолжаю"})
        nudge = "ЦЕЛЬ НЕ ЗАКРЫТА: finish_goal не вызван. подход %d" % round_no
        msgs.append({"role": "user", "content": nudge})
        folded, _ = fold_hard(msgs, budget)
        check("repeat fold round %d budget" % round_no, _estimate_units(folded) <= budget,
              str(_estimate_units(folded)))
        check("repeat fold round %d keeps newest" % round_no,
              bool(folded) and folded[-1].get("content") == nudge)
        check("repeat fold round %d keeps task" % round_no,
              any(m.get("role") == "user" and m.get("content") == task for m in folded))
        digests = [m for m in folded if m.get("role") == "system"
                   and str(m.get("content") or "").startswith("ЖЁСТКОЕ СЖАТИЕ:")]
        check("repeat fold round %d single digest" % round_no, len(digests) == 1, str(len(digests)))
        msgs = folded


def test_rounds_limit_helper():
    check("rounds 0 = infinite", _goal_rounds_limit({"rounds": 0}, 6) == 0)
    check("rounds 2 kept", _goal_rounds_limit({"rounds": 2}, 6) == 2)
    check("rounds None -> default", _goal_rounds_limit({"rounds": None}, 6) == 6)
    check("rounds empty -> default", _goal_rounds_limit({"rounds": ""}, 6) == 6)
    check("rounds junk -> default", _goal_rounds_limit({"rounds": "x"}, 6) == 6)
    check("default 0 = infinite", _goal_rounds_limit({}, 0) == 0)


# ---- scripted endpoint for the infinite goal loop ----
class Collect(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _server(script, port):
    state = {"i": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
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
                return self._send({"object": "list", "data": [{"id": "scripted/model"}]})
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            i = state["i"]
            state["i"] += 1
            msg = script(i)
            self._send({"choices": [{"message": msg}]})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return httpd


def _cfg(port):
    cfg = json.load(open("C:/LocalHarness/config.json", encoding="utf-8"))
    cfg["providers"]["scripted"] = {"label": "Scripted", "base_url": "http://127.0.0.1:%d/v1" % port, "api_key": "x"}
    cfg["provider"] = "scripted"
    cfg["model"] = "scripted/model"
    cfg["workspace"] = "C:/LocalHarness/tests/.tmp"
    cfg["max_steps"] = 6
    cfg["permissions"] = {"mode": "unrestricted"}
    cfg["goal_rounds"] = 0
    return cfg


def _call_msg(name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c_" + name, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_infinite_until_finish():
    # Many approaches (> old default 6): each approach = one real tool call, then a text answer
    # (no finish_goal) so the turn ends and run() nudges into the next approach. Finally finish_goal.
    tmp = "C:/LocalHarness/tests/.tmp"

    def script(i):
        if i == 0:  # refine_goal
            return {"content": '{"objective":"создать и проверить файл","criteria":"1. файл есть","plan":["создать","прочитать","закрыть"]}'}
        if i <= 18:  # 9 approaches x (tool call + text answer) = more than the old ceiling of 6
            if i % 2 == 1:
                return _call_msg("list_dir", {"path": tmp})
            return {"role": "assistant", "content": "проверил папку, продолжаю дальше"}
        return _call_msg("finish_goal", {"achieved": True, "summary": "готово после многих подходов",
                                         "evidence": "list_dir отработал многократно"})

    httpd = _server(script, 12991)
    try:
        bridge = Collect()
        agent = Agent(_cfg(12991), bridge, session_mod.create("inf-finish"))
        agent.llm.probe_protocol = lambda tools, model=None: "native"
        res = agent.run("работай долго", history=[], goal={"objective": "работай долго", "criteria": ""})
        states = [e["state"] for e in bridge.events if e.get("type") == "goal"]
        check("infinite: no exhausted before finish", "exhausted" not in states, str(states))
        check("infinite: many continues (>6)", states.count("continue") >= 6, str(states.count("continue")))
        check("infinite: closed by finish_goal", (res.get("goal") or {}).get("achieved") is True, json.dumps(res.get("goal")))
    finally:
        httpd.shutdown()


def test_infinite_idle_guard():
    # Model only chats, never calls a tool nor finish_goal. Idle guard must stop it (no infinite spin).
    def script(i):
        if i == 0:
            return {"content": '{"objective":"болтовня","criteria":"","plan":[]}'}
        return {"role": "assistant", "content": "думаю, всё готово, но ничего не делаю"}

    httpd = _server(script, 12990)
    try:
        cfg = _cfg(12990)
        cfg["goal_idle_limit"] = 3
        bridge = Collect()
        agent = Agent(cfg, bridge, session_mod.create("inf-idle"))
        agent.llm.probe_protocol = lambda tools, model=None: "json"
        res = agent.run("болтай", history=[], goal={"objective": "болтай", "criteria": ""})
        states = [e["state"] for e in bridge.events if e.get("type") == "goal"]
        check("idle guard stops empty spinning", "exhausted" in states and not (res.get("goal") or {}).get("achieved"), str(states))
        check("idle guard bounded approaches", states.count("continue") <= 4, str(states.count("continue")))
    finally:
        httpd.shutdown()


def test_infinite_user_stop():
    def script(i):
        if i == 0:
            return {"content": '{"objective":"долгая задача","criteria":"","plan":[]}'}
        return _call_msg("list_dir", {"path": "C:/LocalHarness/tests/.tmp"})

    httpd = _server(script, 12989)
    try:
        bridge = Collect()
        agent = Agent(_cfg(12989), bridge, session_mod.create("inf-stop"))
        agent.llm.probe_protocol = lambda tools, model=None: "native"
        # stop after the first real tool observation lands
        orig_emit = bridge.emit

        def emit(ev):
            orig_emit(ev)
            if ev.get("type") == "tool":
                bridge.request_stop()
        bridge.emit = emit
        res = agent.run("работай", history=[], goal={"objective": "работай", "criteria": ""})
        states = [e["state"] for e in bridge.events if e.get("type") == "goal"]
        check("user stop halts infinite loop", "exhausted" not in states and not (res.get("goal") or {}).get("achieved"), str(states))
        check("user stop reported", any(e.get("type") == "stopped" for e in bridge.events) or "Остановлено" in (res.get("answer") or ""),
              res.get("answer", "")[:60])
    finally:
        httpd.shutdown()


def main():
    os.makedirs("C:/LocalHarness/tests/.tmp", exist_ok=True)
    test_normalize_cases()
    test_normalize_fuzz()
    test_fold_then_normalize()
    test_fold_hard_repeated_goal_rounds()
    test_rounds_limit_helper()
    test_infinite_until_finish()
    test_infinite_idle_guard()
    test_infinite_user_stop()
    print("FAILURES:", len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())