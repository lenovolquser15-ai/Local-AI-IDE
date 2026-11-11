"""Живая проверка поиска внутри собранного exe: модель сама ищет и отвечает по найденному."""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7788"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROMPT = ("Найди в интернете, какая самая свежая стабильная версия Python на August 2026. "
          "Используй web_search (или web_fetch по найденной ссылке) и назви версию.")


def post(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def read_events(sid, deadline):
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=30)
    buf = b""
    out = {"kinds": {}, "errors": [], "tools": [], "notices": [], "done": False, "text": ""}
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
            elif k == "tool":
                out["tools"].append(ev.get("tool"))
            elif k == "notice":
                out["notices"].append(ev.get("text", "")[:120])
            elif k in ("done", "stopped"):
                out["done"] = True
                out["text"] = ev.get("text", "")
                stream.close()
                return out
    stream.close()
    return out


def main():
    deadline = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    sid = post("/api/chat", {"text": PROMPT, "mode": "chat"}).get("session")
    print("session", sid)
    out = read_events(sid, deadline)
    print("kinds:", json.dumps(out["kinds"], ensure_ascii=False))
    print("tools:", out["tools"])
    print("notices:", out["notices"])
    print("errors:", out["errors"][:3])
    print("answer:", (out["text"] or "")[:400].replace("\n", " "))
    used = any(t in ("web_search", "web_fetch") for t in out["tools"])
    ok = out["done"] and used and not out["errors"]
    print("VERDICT:", "SEARCH LIVE OK" if ok else "SEARCH LIVE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
