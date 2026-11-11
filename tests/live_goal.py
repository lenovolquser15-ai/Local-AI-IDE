"""Live goal-mode run against the real model: approvals auto-allowed, finish_goal expected."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:7788"
TARGET = "C:/LocalHarness/data/goal_proof.txt"
OBJECTIVE = ("Создай файл %s со строкой READY внутри, потом прочитай его и убедись, что там READY. "
             "Закончи вызовом finish_goal." % TARGET)
CRITERIA = "файл %s существует и содержит READY" % TARGET


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))


def main():
    if os.path.exists(TARGET):
        os.remove(TARGET)
    post("/api/config", {"config": {"permissions": {"mode": "unrestricted"}}})
    sid = post("/api/chat", {"text": OBJECTIVE, "mode": "goal", "criteria": CRITERIA, "rounds": 4}).get("session")
    print("session", sid)
    kinds, last_goal, answer = {}, None, ""
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=1800)
    buf = b""
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            line = block.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            kind = ev.get("type")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "approve":
                post("/api/respond", {"session": sid, "key": ev["key"], "value": {"allow": True}})
            if kind == "question":
                post("/api/respond", {"session": sid, "key": ev["key"], "value": {"text": "делай сам, как считаешь нужным"}})
            if kind == "tool":
                print("  tool %-12s ok=%s %s" % (ev.get("tool"), ev.get("ok"),
                                                 (ev.get("result") or "")[:90].replace("\n", " ")))
            if kind == "goal":
                last_goal = ev
                print("  goal [%s] %s" % (ev.get("state"), (ev.get("summary") or ev.get("objective") or "")[:120]))
            if kind in ("done", "error"):
                answer = ev.get("text") or ""
                print("  %s: %s" % (kind, answer[:200].replace("\n", " ")))
                stream.close()
                return verdict(kinds, last_goal, answer)
    return verdict(kinds, last_goal, answer)


def verdict(kinds, goal, answer):
    exists = os.path.exists(TARGET)
    content = open(TARGET, encoding="utf-8", errors="replace").read().strip() if exists else ""
    print("kinds:", json.dumps(kinds, ensure_ascii=False))
    print("file exists=%s content=%r" % (exists, content[:80]))
    print("goal:", json.dumps(goal, ensure_ascii=False)[:300] if goal else "нет событий цели")
    ok = bool(goal and goal.get("achieved")) and "READY" in content
    print("VERDICT:", "GOAL OK" if ok else "GOAL FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
