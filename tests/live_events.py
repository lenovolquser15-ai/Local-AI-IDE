"""Live check against the running harness + real LM Studio model: event mix of a chat turn."""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:7788"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Сколько будет 2+2? Ответь одной строкой."


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))


def main():
    sid = post("/api/chat", {"text": PROMPT}).get("session")
    print("session", sid)
    kinds = {}
    samples = {}
    stream = urllib.request.urlopen(BASE + "/api/events?session=%s&since=0" % sid, timeout=900)
    buf = b""
    while True:
        chunk = stream.read(2048)
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
            if kind in ("reason", "thinking", "delta", "done", "error", "speed") and kind not in samples:
                samples[kind] = ev
            if kind == "done":
                stream.close()
                return report(kinds, samples)
            if kind == "error":
                stream.close()
                return report(kinds, samples)


def report(kinds, samples):
    print("kinds:", json.dumps(kinds, ensure_ascii=False))
    for kind in ("reason", "thinking", "delta", "done", "error"):
        ev = samples.get(kind)
        if not ev:
            continue
        text = ev.get("text") or ""
        print("%-9s %s" % (kind, text[:220].replace("\n", " ")))
    if samples.get("speed"):
        print("speed  ", json.dumps(samples["speed"], ensure_ascii=False)[:220])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
