"""Real-model smoke check against a running LocalHarness (port 7788).

Sends one short prompt to the live LM Studio model and watches the event stream for
jinja-400 errors and for the context meter events.
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7788"


def post(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def read_events(sid, deadline):
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=30)
    buf = b""
    out = {"kinds": {}, "errors": [], "context": [], "done": False, "text": ""}
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
            out["kinds"][k] = out["kinds"].get(k, 0) + 1
            if k == "error":
                out["errors"].append(ev.get("text", ""))
            elif k == "context":
                out["context"].append(ev)
            elif k in ("done", "stopped"):
                out["done"] = True
                out["text"] = ev.get("text", "")
                stream.close()
                return out
    stream.close()
    return out


def main():
    deadline = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    sid = post("/api/chat", {"text": "Ответь одним словом: готов", "mode": "chat"}).get("session")
    print("session", sid)
    out = read_events(sid, deadline)
    print("kinds:", json.dumps(out["kinds"], ensure_ascii=False))
    print("errors:", out["errors"][:3])
    if out["context"]:
        print("context:", json.dumps(out["context"][-1], ensure_ascii=False))
    print("answer:", (out["text"] or "")[:200].replace("\n", " "))
    jinja = [e for e in out["errors"] if "jinja" in e.lower()]
    print("VERDICT:", "REAL OK" if (not jinja and out["done"] and out["context"]) else "REAL FAIL")
    return 0 if (not jinja and out["done"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
