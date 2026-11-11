"""Live check: does the harness really use the internet when the local path doesn't work?

Goal for the real model: find the current UTC time on the internet (e.g. via worldtimeapi.org)
and write it to a file. The task is deliberately unknowable from memory, so the model has to
search and fetch. We watch which tools it calls — web_search must appear, the file must exist,
and the goal must be closed with finish_goal.
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7788"
TARGET = "C:/LocalHarness/work/pyver.txt"


def post(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def read_events(sid, deadline):
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=30)
    buf = b""
    out = {"tools": [], "errors": [], "goals": [], "context": [], "done": False, "answer": ""}
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
            if k == "tool":
                out["tools"].append((ev.get("tool"), ev.get("ok")))
            elif k == "error":
                out["errors"].append(ev.get("text", ""))
            elif k == "goal":
                out["goals"].append(ev.get("state"))
            elif k == "context":
                out["context"].append(ev)
            elif k in ("done", "stopped"):
                out["done"] = True
                out["answer"] = ev.get("text", "")
                stream.close()
                return out
    stream.close()
    return out


def main():
    deadline = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    if os.path.exists(TARGET):
        os.remove(TARGET)
    goal = ("Открой в интернете главную страницу Хабра (https://habr.com/ru/), возьми заголовок "
            "главной новости и запиши его в файл %s" % TARGET)
    sid = post("/api/chat", {"text": goal, "mode": "goal",
                             "criteria": "файл %s создан и содержит заголовок новости с Хабра" % TARGET}).get("session")
    print("session", sid, flush=True)
    out = read_events(sid, deadline)
    names = [t for t, _ in out["tools"]]
    print("tools:", names)
    print("goal states:", out["goals"])
    print("errors:", out["errors"][:3])
    if out["context"]:
        print("context last:", json.dumps(out["context"][-1], ensure_ascii=False))
    written = os.path.exists(TARGET)
    body = open(TARGET, encoding="utf-8").read().strip() if written else ""
    print("file:", written, repr(body[:120]))
    print("answer:", (out["answer"] or "")[:300].replace("\n", " "))
    used_web = any(n in ("web_search", "web_fetch") for n in names)
    ok = used_web and written and "closed" in out["goals"] and not out["errors"]
    print("VERDICT:", "WEB OK" if ok else "WEB FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
