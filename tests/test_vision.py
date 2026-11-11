"""Vision support proof: message normalization, multimodal folding, screenshot delivery in both protocols."""
import base64
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "C:/LocalHarness")
os.environ["LH_DATA"] = "C:/LocalHarness/tests/.tmp"
from harness import llm as llm_mod  # noqa: E402
from harness import lms as lms_mod  # noqa: E402
from harness import session as session_mod  # noqa: E402
from harness import vision as vision_mod  # noqa: E402
from harness.agent import Agent, QueueBridge, fold, fold_hard, _estimate_units  # noqa: E402

PORT = 12995
STATE = {"n": 0, "bodies": [], "mode": "json"}
failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


def red_png(path):
    from PIL import Image
    Image.new("RGB", (8, 8), (220, 30, 30)).save(path)
    return path


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


class VisionMock(BaseHTTPRequestHandler):
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
            return self._json({"object": "list", "data": [{"id": "vision/mock", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE["bodies"].append(body)
        STATE["n"] += 1
        n = STATE["n"]
        if STATE["mode"] == "json":
            if n == 1:
                return sse(self, [{"content": '{"tool": "screenshot", "args": {}}'}])
            return sse(self, [{"content": '{"final": "вижу красное"}'}])
        if n == 1:
            return sse(self, [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                               "function": {"name": "screenshot", "arguments": "{}"}}]}])
        if n == 2:
            return sse(self, [{"content": "Ожидаю скриншот."}])
        return sse(self, [{"content": "вижу красное"}])


class FakeToolkit:
    def __init__(self, shot_path):
        self.shot_path = shot_path
        self.calls = []

    def call(self, name, args):
        self.calls.append(name)
        if name == "screenshot":
            return {"ok": True, "path": self.shot_path, "note": "скриншот сохранён"}
        return {"ok": True}


class RecordingBridge(QueueBridge):
    def drain(self):
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except Exception:
                return events


def has_image(messages):
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(p.get("type") == "image_url" for p in content if isinstance(p, dict)):
            return True
    return False


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), VisionMock)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    # 1. normalize_messages: строгий шаблон не должен падать
    msgs = [
        {"role": "system", "content": "первый"},
        {"role": "user", "content": "привет"},
        {"role": "system", "content": "второй"},
        {"role": "user", "content": "ещё"},
        {"role": "assistant", "content": "раз"},
        {"role": "assistant", "content": "два"},
        {"role": "tool", "tool_call_id": "сирота", "content": "без вызова"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ок"},
        {"role": "user", "content": "дальше"},
    ]
    out = llm_mod.normalize_messages(msgs)
    check(sum(1 for m in out if m["role"] == "system") == 1, "normalize: system не склеен в один")
    check(out[0]["role"] == "system" and "первый" in out[0]["content"] and "второй" in out[0]["content"],
          "normalize: system не в начале или потерян текст")
    check(out[1]["role"] == "user" and "привет" in out[1]["content"] and "ещё" in out[1]["content"],
          "normalize: соседние user не склеены")
    assistants = [m for m in out if m["role"] == "assistant" and not m.get("tool_calls")]
    check(any("раз" in str(m.get("content")) and "два" in str(m.get("content")) for m in assistants),
          "normalize: соседние assistant не склеены")
    check(any(m["role"] == "user" and "сирота" not in str(m) and "без вызова" in str(m.get("content")) for m in out),
          "normalize: сиротский tool не превращён в наблюдение")
    c2 = [m for m in out if m.get("tool_call_id") == "c2"]
    check(c2 and c2[0]["content"] == "(прервано)", "normalize: неотвеченный tool_call не закрыт заглушкой")
    idx_call = next(i for i, m in enumerate(out) if m.get("tool_calls"))
    check(out[idx_call + 1]["role"] == "tool" and out[idx_call + 2]["role"] == "tool",
          "normalize: tool-результаты не идут сразу за вызовом")
    check(out[-1]["role"] in ("user", "tool"), "normalize: хвост не пользовательский/инструментальный")
    img_msgs = [{"role": "user", "content": [{"type": "text", "text": "смотри"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
                {"role": "user", "content": "и ещё"}]
    merged = llm_mod.normalize_messages(img_msgs)
    check(len(merged) == 1 and isinstance(merged[0]["content"], list)
          and any(p.get("type") == "image_url" for p in merged[0]["content"]),
          "normalize: картинка потеряна при склейке user")

    # 2. vision helpers
    check(vision_mod.estimate_content("абв") == 3, "vision: оценка текста")
    parts = [{"type": "text", "text": "текст"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    check(vision_mod.estimate_content(parts) == len("текст") + vision_mod.IMAGE_COST, "vision: картинка не учтена в бюджете")
    check("изображений: 1" in vision_mod.text_only(parts), "vision: text_only без пометки")
    check(vision_mod.content_text(parts) == "текст", "vision: content_text")
    check(vision_mod.is_data_url("data:image/png;base64,AAAA"), "vision: is_data_url ложно отрицательный")
    check(not vision_mod.is_data_url("data:text/html;base64,AAAA"), "vision: is_data_url принимает не-картинку")
    check(vision_mod.normalize_image("data:text/html;base64,AAAA") is None, "vision: normalize принял html")
    check(vision_mod.supports_vision({"vision": True}, "anything"), "vision: явный флаг true не работает")
    check(not vision_mod.supports_vision({"vision": False}, "qwen2-vl"), "vision: явный флаг false не работает")
    old_disk = lms_mod.disk_models
    lms_mod.disk_models = lambda cfg=None, force=False, timeout=20: ([], "")
    try:
        check(vision_mod.supports_vision({"vision": "auto"}, "ministral-3-8b-reasoning"),
              "vision: эвристика по имени не сработала")
        check(not vision_mod.supports_vision({"vision": "auto"}, "deepseek-r1-qwen3-8b"),
              "vision: текстовая модель ложно vision")
        lms_mod.disk_models = lambda cfg=None, force=False, timeout=20: (
            [{"modelKey": "some-model", "vision": True, "displayName": "Some"}], "")
        check(vision_mod.supports_vision({"vision": "auto"}, "some-model"),
              "vision: флаг vision из lms ls не подхвачен")
    finally:
        lms_mod.disk_models = old_disk

    tmp = "C:/LocalHarness/tests/.tmp"
    os.makedirs(tmp, exist_ok=True)
    shot = red_png(os.path.join(tmp, "vision_test_shot.png"))
    data_url = vision_mod.encode_path(shot)
    check(data_url and data_url.startswith("data:image/"), "vision: encode_path не дал data URL")
    check(vision_mod.normalize_image(shot) is not None, "vision: normalize_image локального пути")
    check(vision_mod.normalize_image(os.path.join(tmp, "нет_файла.png")) is None, "vision: несуществующий файл принят")

    # 3. fold не режет base64 и не падает на списках
    big = [{"role": "system", "content": "sys"},
           {"role": "user", "content": [{"type": "text", "text": "задача " + "я" * 500},
                                         vision_mod.image_part(data_url)]},
           {"role": "assistant", "content": "ответ " + "ю" * 500},
           {"role": "user", "content": "наблюдение " + "э" * 500}]
    folded, dropped = fold(big, 400, keep_tail=2)
    check(dropped >= 0, "fold: упал на multimodal")
    hard, dropped2 = fold_hard(big, 600)
    check(_estimate_units(hard) <= 600 or not dropped2, "fold_hard: бюджет не соблюдён")
    for msg in hard:
        for part in vision_mod.content_images(msg.get("content")):
            url = part["image_url"]["url"]
            check(url == data_url or url.startswith("data:image/"), "fold_hard: base64 порезан пополам")

    # 4. [THINK]-маркеры этого семейства моделей
    splitter = llm_mod.ThinkSplitter()
    visible = splitter.feed("[THINK]думаю вслух[/THINK]Ответ.")
    check(visible == "Ответ." and splitter.reason == "думаю вслух", "splitter: [THINK] не отделён (%r)" % visible)
    check("[THINK]" not in llm_mod.strip_reasoning("[THINK]секрет[/THINK]видно"), "strip_reasoning: [THINK] остался")

    # 5. JSON-протокол: скриншот приходит модели картинкой в блоке наблюдений
    cfg = json.load(open("C:/LocalHarness/config.json", encoding="utf-8"))
    cfg["providers"]["visionmock"] = {"label": "VisionMock", "base_url": "http://127.0.0.1:%d/v1" % PORT,
                                      "api_key": "x", "autostart": False}
    cfg["provider"] = "visionmock"
    cfg["model"] = "vision/mock"
    cfg["vision"] = True
    cfg["tool_protocol"] = "json"
    cfg["max_steps"] = 4
    STATE.update(n=0, bodies=[], mode="json")
    bridge = RecordingBridge()
    agent = Agent(dict(cfg), bridge, session_mod.create("vision json"), toolkit=FakeToolkit(shot))
    agent.llm.probe_protocol = lambda tools, model=None: "json"
    result = agent.run("сделай скриншот и скажи цвет", history=[])
    check(result["answer"] == "вижу красное", "json-протокол: ответ не получен (%r)" % result["answer"])
    check(len(STATE["bodies"]) >= 2 and has_image(STATE["bodies"][1]["messages"]),
          "json-протокол: скриншот не передан модели картинкой")
    events = bridge.drain()
    check(any(e.get("type") == "tool" and e.get("image") for e in events),
          "json-протокол: UI-событие без пути картинки")

    # 6. Native-протокол: скриншот отдаётся user-сообщением ПОСЛЕ ответа модели (требование jinja)
    STATE.update(n=0, bodies=[], mode="native")
    bridge2 = RecordingBridge()
    agent2 = Agent(dict(cfg), bridge2, session_mod.create("vision native"), toolkit=FakeToolkit(shot))
    agent2.llm.probe_protocol = lambda tools, model=None: "native"
    cfg2 = dict(cfg, tool_protocol="native")
    agent2.cfg = cfg2
    result2 = agent2.run("сделай скриншот и скажи цвет", history=[])
    check(result2["answer"] == "вижу красное", "native: ответ не получен (%r)" % result2["answer"])
    check(len(STATE["bodies"]) >= 3, "native: не было промежуточного хода под скриншот")
    if len(STATE["bodies"]) >= 3:
        second = STATE["bodies"][1]["messages"]
        third = STATE["bodies"][2]["messages"]
        check(all(isinstance(m.get("content"), str) for m in second if m["role"] == "tool"),
              "native: tool-content стал списком (LM Studio такое отвергает)")
        check(has_image(third), "native: скриншот не дошёл до модели")
        roles = [m["role"] for m in third]
        img_idx = next(i for i, m in enumerate(third)
                       if isinstance(m.get("content"), list)
                       and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict)))
        check(roles[img_idx] == "user" and roles[img_idx - 1] == "assistant",
              "native: картинка стоит сразу после tool — строгий шаблон упадёт")

    print("FAILURES: %d" % len(failures))
    for item in failures:
        print(" -", item)
    httpd.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())