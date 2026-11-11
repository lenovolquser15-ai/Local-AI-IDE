"""Drives the running harness over HTTP and logs every event; auto-answers prompts."""
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:7788"
LOG = open("C:/LocalHarness/tests/real_run.log", "w", encoding="utf-8", buffering=1)


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))


def listen(session, seconds=420):
    end = time.time() + seconds
    req = urllib.request.Request("%s/api/events?session=%s&since=0" % (BASE, session))
    with urllib.request.urlopen(req, timeout=seconds + 30) as resp:
        buffer = ""
        while time.time() < end:
            chunk = resp.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            LOG.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), json.dumps(ev, ensure_ascii=False)[:900]))
            if ev.get("type") in ("approve", "question"):
                threading.Thread(target=answer, args=(session, ev), daemon=True).start()
            if ev.get("type") in ("done", "stopped"):
                return ev
    return None


def answer(session, ev):
    value = {"allow": True} if ev["type"] == "approve" else {"text": "делай как договорились"}
    try:
        post("/api/respond", {"session": session, "key": ev["key"], "value": value})
        LOG.write("answered %s\n" % ev["type"])
    except Exception as exc:
        LOG.write("answer failed: %s\n" % exc)


if __name__ == "__main__":
    import sys
    task = " ".join(sys.argv[1:]) or "Сколько свободной памяти на диске C:? Ответь одним числом с единицами."
    started = time.time()
    res = post("/api/chat", {"session": None, "text": task})
    LOG.write("task: %s\nsession: %s\n" % (task, res["session"]))
    final = listen(res["session"])
    LOG.write("TOTAL %.1fs final=%s\n" % (time.time() - started, json.dumps(final, ensure_ascii=False) if final else None))
    print("done in %.1fs, see tests/real_run.log" % (time.time() - started))
