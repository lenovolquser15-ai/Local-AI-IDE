import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import config as config_mod
from harness import server as server_mod
from harness import session as session_mod


def _post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_session_workspace_grouping_and_optional_mode(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    sess_dir = data_dir / "sessions"
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("LH_DATA", str(data_dir))
    monkeypatch.setenv("LH_CONFIG", str(cfg_file))
    monkeypatch.setattr(config_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(session_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(server_mod, "DATA_DIR", str(data_dir))

    proj_a = tmp_path / "ProjectAlpha"
    proj_a.mkdir()
    proj_b = tmp_path / "ProjectBeta"  # Not created yet — test create_dir=True!

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. Default state: no workspace selected (optional mode!)
        ws0 = _get_json(base + "/api/workspaces")
        assert ws0["ok"] is True
        assert ws0["active"] == ""

        new_general = _post_json(base + "/api/new", {"title": "Общий вопрос без папки"})
        sid_general = new_general["session"]
        assert new_general["workspace"] == ""

        # 2. Select ProjectAlpha as active workspace & create chat inside it
        sel_a = _post_json(base + "/api/workspace/select", {"path": str(proj_a), "name": "Alpha App"})
        assert sel_a["ok"] is True
        assert os.path.normcase(sel_a["workspace"]) == os.path.normcase(str(proj_a))

        new_a = _post_json(base + "/api/new", {"title": "Фича в Alpha"})
        sid_a = new_a["session"]
        assert os.path.normcase(new_a["workspace"]) == os.path.normcase(str(proj_a))

        # 3. Create & select ProjectBeta with create_dir=True
        sel_b = _post_json(base + "/api/workspace/select", {"path": str(proj_b), "create_dir": True})
        assert sel_b["ok"] is True
        assert proj_b.is_dir()

        new_b = _post_json(base + "/api/new", {"title": "Бэкенд Beta"})
        sid_b = new_b["session"]
        assert os.path.normcase(new_b["workspace"]) == os.path.normcase(str(proj_b))

        # 4. Switch back to optional «Без рабочего пространства» (`path: ""`)
        sel_none = _post_json(base + "/api/workspace/select", {"path": ""})
        assert sel_none["ok"] is True
        assert sel_none["workspace"] == ""

        # Verify all 3 sessions keep their own distinct workspace assignments!
        rows = {r["id"]: r for r in session_mod.list_sessions()}
        assert rows[sid_general]["workspace"] == ""
        assert os.path.normcase(rows[sid_a]["workspace"]) == os.path.normcase(str(proj_a))
        assert os.path.normcase(rows[sid_b]["workspace"]) == os.path.normcase(str(proj_b))

        # Search by workspace name works
        alpha_hits = session_mod.list_sessions(query="projectalpha")
        assert [r["id"] for r in alpha_hits] == [sid_a]

        # 5. Move general chat into ProjectAlpha, then detach it back to ""
        mv1 = _post_json(base + "/api/session/workspace", {"session": sid_general, "workspace": str(proj_a)})
        assert mv1["ok"] is True
        assert os.path.normcase(session_mod.load(sid_general)["meta"]["workspace"]) == os.path.normcase(str(proj_a))

        mv2 = _post_json(base + "/api/session/workspace", {"session": sid_general, "workspace": ""})
        assert mv2["ok"] is True
        assert session_mod.load(sid_general)["meta"]["workspace"] == ""

        # 6. Remove ProjectBeta from saved workspaces (files on disk stay intact; sessions detach cleanly)
        marker = proj_b / "keep_me.txt"
        marker.write_text("safe", encoding="utf-8")
        rm = _post_json(base + "/api/workspace/remove", {"path": str(proj_b), "detach_sessions": True})
        assert rm["ok"] is True
        assert marker.read_text(encoding="utf-8") == "safe"
        assert session_mod.load(sid_b)["meta"]["workspace"] == ""
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_each_session_executes_tools_in_its_own_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    sess_dir = data_dir / "sessions"
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("LH_DATA", str(data_dir))
    monkeypatch.setenv("LH_CONFIG", str(cfg_file))
    monkeypatch.setattr(config_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(session_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(server_mod, "DATA_DIR", str(data_dir))

    ws_one = tmp_path / "WS_One"
    ws_two = tmp_path / "WS_Two"
    ws_one.mkdir()
    ws_two.mkdir()

    # Fake LLM that writes `hello.txt` using a relative path, then finishes
    turns = {"n": 0}

    class FakeLLM(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "mock-model"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            step = turns["n"] % 2
            turns["n"] += 1
            if step == 0:
                msg = {"role": "assistant", "content": '{"tool": "write_file", "args": {"path": "hello.txt", "content": "ok"}}'}
            else:
                msg = {"role": "assistant", "content": '{"final": "Записано"}'}
            body = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    llm_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    llm_port = llm_srv.server_address[1]
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()

    cfg = config_mod.load()
    cfg["providers"]["fake"] = {"label": "Fake", "base_url": f"http://127.0.0.1:{llm_port}/v1", "api_key": "x"}
    cfg["provider"] = "fake"
    cfg["model"] = "mock-model"
    cfg["tool_protocol"] = "json"
    cfg["permissions"] = {"mode": "unrestricted"}
    cfg["workspace"] = ""  # Global config has NO workspace selected!
    config_mod.save(cfg)

    app_srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    app_port = app_srv.server_address[1]
    threading.Thread(target=app_srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{app_port}"
        s1 = _post_json(base + "/api/new", {"title": "Chat 1", "workspace": str(ws_one)})["session"]
        s2 = _post_json(base + "/api/new", {"title": "Chat 2", "workspace": str(ws_two)})["session"]

        _post_json(base + "/api/chat", {"session": s1, "text": "создай hello.txt"})
        import time
        for _ in range(40):
            if (ws_one / "hello.txt").is_file():
                break
            time.sleep(0.05)
        assert (ws_one / "hello.txt").read_text(encoding="utf-8") == "ok"
        assert not (ws_two / "hello.txt").exists()

        _post_json(base + "/api/chat", {"session": s2, "text": "создай hello.txt"})
        for _ in range(40):
            if (ws_two / "hello.txt").is_file():
                break
            time.sleep(0.05)
        assert (ws_two / "hello.txt").read_text(encoding="utf-8") == "ok"
    finally:
        app_srv.shutdown()
        app_srv.server_close()
        llm_srv.shutdown()
        llm_srv.server_close()
