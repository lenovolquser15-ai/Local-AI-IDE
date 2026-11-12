"""Регрессы: картинки из чата (кнопка отправки) и зависания shell/ssh.

Картинки: несколько файлов за один запрос -> несколько image_url у модели, сервер принимает
images без текста, веб-морда в обоих каталогах одинакова и шлёт body.images.
Зависания: команда, ждущая stdin, и команда с таймаутом не висят вечно; ssh не ждёт пароль.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
# config.json is gitignored and absent on a fresh clone: load() must return defaults
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")
from harness import config as config_mod  # noqa: E402
from harness import server as server_mod  # noqa: E402
from harness import session as session_mod  # noqa: E402
from harness import tools as tools_mod  # noqa: E402
from harness.agent import Agent, QueueBridge  # noqa: E402

PORT = 12997
MOCK_PORT = 12998
failures = []
STATE = {"bodies": [], "n": 0}


def check(cond, label):
    if not cond:
        failures.append(label)


def sse(handler, pieces):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    for piece in pieces:
        chunk = {"choices": [{"index": 0, "delta": piece, "finish_reason": None}]}
        handler.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
        handler.wfile.flush()
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


class MockLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            return self._json({"object": "list", "data": [{"id": "img/mock", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE["bodies"].append(body)
        STATE["n"] += 1
        return sse(self, [{"content": '{"final": "картинки приняты"}'}])


class RecordingBridge(QueueBridge):
    def drain(self):
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except Exception:
                return events


class FakeToolkit:
    def call(self, name, args):
        return {"ok": True}


def png_data_url(color):
    from PIL import Image
    import base64
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def count_images(messages):
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            total += sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
    return total


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body or "{}")
        except ValueError:
            return exc.code, {"raw": body}


def main():
    # ---------- 1. веб-морда: кнопка, разные копии, отправка images ----------
    ui_dirs = [os.path.join(ROOT, "webui"), os.path.join(ROOT, "harness", "webui")]
    for name in ("index.html", "app.js", "style.css"):
        blobs = []
        for path in ui_dirs:
            full = os.path.join(path, name)
            check(os.path.isfile(full), "webui: нет %s в %s" % (name, path))
            blobs.append(open(full, "rb").read() if os.path.isfile(full) else b"")
        check(len(set(blobs)) == 1, "webui: копии %s разошлись (%s vs %s)" % (name, ui_dirs[0], ui_dirs[1]))
    html = open(os.path.join(ui_dirs[0], "index.html"), encoding="utf-8").read()
    app = open(os.path.join(ui_dirs[0], "app.js"), encoding="utf-8").read()
    check('id="attachBtn"' in html, "webui: нет кнопки прикрепления картинок")
    check('id="attachFile"' in html and 'id="attachFile" accept="image/png,image/jpeg,image/webp,image/gif" multiple' in html,
          "webui: нет входа файлов с multiple")
    check("body.images" in app, "webui: картинки не уходят в запрос")
    check("addFiles" in app and "renderAttached" in app, "webui: нет превью картинок")

    # ---------- 2. сервер принимает images (и без текста) ----------
    captured = {}

    class FakeAgent:
        def __init__(self, cfg, bridge, sid):
            pass

        def run(self, text, history=None, goal=None, image=None, images=None):
            captured["text"] = text
            captured["image"] = image
            captured["images"] = images

    old_agent = server_mod.Agent
    server_mod.Agent = FakeAgent
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server_mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        img1, img2 = png_data_url((220, 30, 30)), png_data_url((30, 30, 220))
        status, body = post_json("http://127.0.0.1:%d/api/chat" % PORT,
                                 {"text": "", "images": [img1, img2]})
        check(status == 200 and body.get("ok"), "server: запрос с картинками без текста отклонён (%s)" % status)
        check(captured.get("images") == [img1, img2], "server: картинки не дошли до агента")
        status2, body2 = post_json("http://127.0.0.1:%d/api/chat" % PORT, {"text": "   "})
        check(status2 == 400, "server: пустой запрос не отклонён (%s)" % status2)
    finally:
        server_mod.Agent = old_agent
        httpd.shutdown()

    # ---------- 3. агент: несколько картинок доезжают до модели ----------
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        cfg = config_mod.load()
        cfg["providers"]["imgmock"] = {"label": "ImgMock", "base_url": "http://127.0.0.1:%d/v1" % MOCK_PORT,
                                       "api_key": "x", "autostart": False}
        cfg["provider"] = "imgmock"
        cfg["model"] = "img/mock"
        cfg["vision"] = True
        cfg["tool_protocol"] = "json"
        cfg["max_steps"] = 2
        STATE.update(bodies=[], n=0)
        bridge = RecordingBridge()
        agent = Agent(dict(cfg), bridge, session_mod.create("img multi"), toolkit=FakeToolkit())
        agent.llm.probe_protocol = lambda tools, model=None: "json"
        result = agent.run("что на картинках?", history=[],
                           images=[png_data_url((10, 200, 10)), png_data_url((200, 200, 10))])
        check(result["answer"] == "картинки приняты", "агент: ответ не получен (%r)" % result["answer"])
        check(STATE["bodies"] and count_images(STATE["bodies"][0]["messages"]) == 2,
              "агент: модели ушло не две картинки")
        events = bridge.drain()
        user_events = [e for e in events if e.get("type") == "user"]
        check(any(e.get("images") == 2 for e in user_events),
              "агент: в UI не отчиталось число картинок")
    finally:
        mock.shutdown()

    # ---------- 4. shell/ssh не висят ----------
    tk = tools_mod.Toolkit({"workspace": os.path.join(ROOT, "tests", ".tmp")})
    bash = shutil.which("bash")
    if bash:
        started = time.time()
        res = tk.shell(cmd="cat", mode="bash", timeout=8)
        elapsed = time.time() - started
        check(elapsed < 8, "shell: команда, ждущая stdin, провисела %.1f с" % elapsed)
        check("таймаут" not in str(res.get("error", "")), "shell: stdin-команда упала по таймауту (%r)" % res.get("error"))

    started = time.time()
    res = tk.shell(cmd="sleep 45", mode="bash", timeout=4) if bash else tk.shell(cmd="Start-Sleep -Seconds 45",
                                                                                 mode="powershell", timeout=4)
    elapsed = time.time() - started
    check(elapsed < 20, "shell: таймаут не отработал (%.1f с)" % elapsed)
    check(not res.get("ok") and "убита вместе с дочерними" in str(res.get("error", "")),
          "shell: таймаут вернул не тот результат (%r)" % res)

    opts = tk._ssh_opts(20)
    check("BatchMode=yes" in opts, "ssh: нет BatchMode=yes — клиент будет ждать пароль")
    env = tools_mod.child_env()
    check(env.get("SSH_ASKPASS_REQUIRE") == "force", "ssh: нет SSH_ASKPASS_REQUIRE=force")
    check(env.get("SSH_ASKPASS") and os.path.isfile(env.get("SSH_ASKPASS")), "ssh: нет askpass-скрипта")

    started = time.time()
    res = tk.ssh(command="echo hi", host="127.0.0.1", user="root", port=1, timeout=15)
    elapsed = time.time() - started
    check(elapsed < 20, "ssh: завис на недоступном порту (%.1f с)" % elapsed)
    check(not res.get("ok"), "ssh: подключение к пустому порту прошло успешно")

    key, note = tk._resolve_key("/home/что-то/.ssh/id_rsa")
    check(note and "не найден" in note, "ssh: выдуманный путь к ключу не отмечен")
    if key:
        check(os.path.isfile(os.path.expanduser(str(key))), "ssh: подставлен несуществующий ключ")

    print("FAILURES: %d" % len(failures))
    for item in failures:
        print(" -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
