import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import changes as changes_mod
from harness import config as config_mod
from harness import llm as llm_mod
from harness import session as session_mod
from harness import tools as tools_mod
from harness.agent import Agent, Bridge


class CollectBridge(Bridge):
    __test__ = False

    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_universal_no_tool_use_dialects():
    known = {"shell", "read_file", "write_file", "edit_file", "list_dir", "grep", "web_search", "python"}

    # 1. Qwen / Hermes <tool_call>
    p1 = llm_mod.parse_json_protocol(
        '<think>Проверим файлы</think>Сейчас посмотрю.\n<tool_call>\n{"name": "shell", "arguments": {"command": "Get-ChildItem"}}\n</tool_call>',
        known_tools=known,
    )
    assert p1["kind"] == "tools"
    assert p1["calls"][0]["function"]["name"] == "shell"
    assert "Сейчас посмотрю" in p1["content"]
    assert "<tool_call>" not in p1["content"]

    # 2. DeepSeek tool markers
    p2 = llm_mod.parse_json_protocol(
        '<｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n```json\n{"file_path": "main.py"}\n```<｜tool▁call▁end｜>',
        known_tools=known,
    )
    assert p2["calls"] and p2["calls"][0]["function"]["name"] == "read_file"

    # 3. Mistral [TOOL_CALLS]
    p3 = llm_mod.parse_json_protocol(
        '[TOOL_CALLS] [{"name": "grep", "arguments": {"regex": "class Agent"}}]',
        known_tools=known,
    )
    assert p3["calls"] and p3["calls"][0]["function"]["name"] == "grep"

    # 4. Llama <function=...>
    p4 = llm_mod.parse_json_protocol(
        '<function=edit_file>{"file_path": "a.py", "old_str": "x=1", "new_str": "x=2"}</function>',
        known_tools=known,
    )
    assert p4["calls"] and p4["calls"][0]["function"]["name"] == "edit_file"

    # 5. Anthropic XML <invoke>
    p5 = llm_mod.parse_json_protocol(
        '<invoke name="write_file"><parameter name="file_path">hello.txt</parameter><parameter name="text">Привет</parameter></invoke>',
        known_tools=known,
    )
    assert p5["calls"] and p5["calls"][0]["function"]["name"] == "write_file"
    assert json.loads(p5["calls"][0]["function"]["arguments"]) == {"file_path": "hello.txt", "text": "Привет"}

    # 6. ReAct Action / Action Input with alias `ls` -> `list_dir`
    p6 = llm_mod.parse_json_protocol(
        'Thought: посмотрим содержимое\nAction: ls\nAction Input: {"dir": "src"}',
        known_tools=known,
    )
    assert p6["calls"] and p6["calls"][0]["function"]["name"] == "ls"

    # 7. Broken GGUF JSON: trailing comma + raw unescaped newline + Python single-quoted dict
    p7 = llm_mod.parse_json_protocol(
        '{"tool": "write_file", "args": {"path": "notes.md", "content": "line 1\nline 2",},}',
        known_tools=known,
    )
    assert p7["calls"] and p7["calls"][0]["function"]["name"] == "write_file"
    assert json.loads(p7["calls"][0]["function"]["arguments"])["content"] == "line 1\nline 2"

    p8 = llm_mod.parse_json_protocol(
        "{'tool': 'python', 'args': {'script': 'print(42)'}}",
        known_tools=known,
    )
    assert p8["calls"] and p8["calls"][0]["function"]["name"] == "python"

    # 8. Unclosed <think> tag followed by explicit <tool_call>
    p9 = llm_mod.parse_json_protocol(
        '<think>Думаю и забыл закрыть тег... <tool_call>{"name": "web_search", "arguments": {"q": "Vulkan llama.cpp"}}</tool_call>',
        known_tools=known,
    )
    assert p9["calls"] and p9["calls"][0]["function"]["name"] == "web_search"


def test_tool_and_param_alias_normalization(tmp_path):
    name, args = tools_mod.normalize_tool_call("bash", {"command": "ls -la"})
    assert name == "shell"
    assert args == {"cmd": "ls -la", "mode": "bash"}

    name2, args2 = tools_mod.normalize_tool_call("str_replace", {"file_path": "app.py", "old_str": "a", "new_str": "b"})
    assert name2 == "edit_file"
    assert args2 == {"path": "app.py", "find": "a", "replace": "b"}

    tk = tools_mod.Toolkit({"workspace": str(tmp_path)})
    jres = tk.call("jobs", {"action": "list"})
    assert jres["ok"] is True
    assert jres["jobs"] == []


def test_workspace_tracker_and_goal_changes_summary(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    existing = ws / "debug_client.sh"
    existing.write_text("#!/bin/bash\necho old1\necho old2\n", encoding="utf-8")

    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setenv("LH_CONFIG", str(cfg_file))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LH_DATA", str(tmp_path / "data"))

    # Scripted non-tool-use GGUF model completing a 2-file goal via XML <tool_call> + aliased params
    responses = [
        # refine_goal response
        {"role": "assistant", "content": '{"objective":"Обновить скрипт и конфиг","criteria":"1. Обновлён debug_client.sh\\n2. Создан readme.md","checklist":["Обновлён debug_client.sh","Создан readme.md"],"plan":["Правка скрипта","Создание readme"]}'},
        # Step 1: model uses <tool_call> with OpenAI/Claude-style param aliases!
        {"role": "assistant", "content": '<think>Обновляю скрипт</think>Правлю файл.\n<tool_call>{"name": "str_replace", "arguments": {"file_path": "debug_client.sh", "old_str": "echo old1", "new_str": "echo new1\\necho new2\\necho new3"}}</tool_call>'},
        # Step 2: model creates a second file via Python-style single quotes
        {"role": "assistant", "content": "{'tool': 'create_file', 'args': {'file': 'readme.md', 'text': '# Готово\\nОписание проекта\\n'}}"},
        # Step 3: mark checklist points & finish_goal
        {"role": "assistant", "content": '{"tools": [{"tool": "check_goal_point", "args": {"point": 1, "proof": "edit_file ok"}}, {"tool": "check_goal_point", "args": {"point": 2, "proof": "write_file ok"}}, {"tool": "finish_goal", "args": {"done": true, "result": "Оба файла успешно обновлены и проверены!", "proof": "2 файла записаны"}}]}'},
    ]
    idx_box = {"i": 0}

    class FakeHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "abliterated-no-tools-gguf"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            i = idx_box["i"]
            idx_box["i"] = i + 1
            msg = responses[min(i, len(responses) - 1)]
            body = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        cfg = config_mod.load()
        cfg["providers"]["fake"] = {"label": "Fake", "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "x"}
        cfg["provider"] = "fake"
        cfg["model"] = "abliterated-no-tools-gguf"
        cfg["tool_protocol"] = "json"
        cfg["workspace"] = str(ws)
        cfg["permissions"] = {"mode": "unrestricted"}

        bridge = CollectBridge()
        sid = session_mod.create("goal-diff-test")
        agent = Agent(cfg, bridge, sid)
        res = agent.run(
            "обнови проект",
            history=[],
            goal={"objective": "Обновить скрипт и конфиг", "criteria": "", "rounds": 3},
        )

        assert res["goal"]["achieved"] is True
        tool_evs = [e for e in bridge.events if e.get("type") == "tool" and e.get("file_edit")]
        assert len(tool_evs) == 2
        sh_edit = tool_evs[0]["file_edit"]
        assert sh_edit["name"] == "debug_client.sh"
        assert sh_edit["added"] == 3
        assert sh_edit["removed"] == 1

        done_ev = next(e for e in reversed(bridge.events) if e.get("type") == "done")
        assert "changes" in done_ev
        ch = done_ev["changes"]
        assert ch["files_changed"] == 2
        assert ch["insertions"] == 5
        assert ch["deletions"] == 1
        assert done_ev["goal"]["achieved"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_changes_tracker_catches_shell_edits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    f1 = ws / "a.txt"
    f1.write_text("one\ntwo\n", encoding="utf-8")

    tracker = changes_mod.WorkspaceTracker(str(ws))
    # Simulate shell script modifying a.txt and creating b.txt
    f1.write_text("one\ntwo_modified\nthree\n", encoding="utf-8")
    (ws / "b.txt").write_text("brand new\n", encoding="utf-8")

    summary = tracker.finalize()
    assert summary["files_changed"] == 2
    assert summary["insertions"] == 3
    assert summary["deletions"] == 1
