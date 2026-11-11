"""Live proof: картинка, отправленная из чата, доезжает до живой модели.

Отправляет в работающий LocalHarness (порт 7788) запрос с картинкой и смотрит
в ленте событий: модель должна ответить по картинке, а не «не вижу».
"""
import base64
import io
import json
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
    out = {"user": [], "answer": "", "errors": [], "notices": [], "done": False}
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
            kind = ev.get("type")
            if kind == "user":
                out["user"].append(ev)
            elif kind == "error":
                out["errors"].append(str(ev.get("text")))
            elif kind == "notice":
                out["notices"].append(str(ev.get("text")))
            elif kind == "done":
                out["answer"] = str(ev.get("text") or "")
                out["done"] = True
                return out
    return out


def sample_png():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 140), (200, 25, 35))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 220, 120], outline=(255, 255, 255), width=4)
    draw.text((60, 62), "HARNESS", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def main():
    data_url = sample_png()
    body = post("/api/chat", {"text": "Что на картинке? Ответь коротко.", "images": [data_url]})
    sid = body.get("session")
    out = read_events(sid, deadline=time.time() + 240)
    print("session", sid)
    print("user events:", out["user"])
    print("notices:", out["notices"])
    print("errors:", out["errors"])
    print("answer:", out["answer"][:300])
    ok = True
    if not out["done"]:
        print("FAIL: прогона не было")
        ok = False
    if not any(int(ev.get("images") or 0) == 1 for ev in out["user"]):
        print("FAIL: UI не увидел картинку")
        ok = False
    if any("не удалось прочитать" in n or "без vision" in n for n in out["notices"]):
        print("FAIL: картинка не дошла до модели")
        ok = False
    if out["errors"]:
        print("FAIL: были ошибки")
        ok = False
    answer = out["answer"].lower()
    if not any(word in answer for word in ("красн", "harness", "прямоугольн", "рамк", "квадрат")):
        print("FAIL: ответ не похож на описание картинки")
        ok = False
    print("VERDICT: IMAGE OK" if ok else "VERDICT: IMAGE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
