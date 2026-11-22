"""Comprehensive tests for the major LocalHarness update:
- Self-updater (harness.updater)
- Windows Toast notifications (harness.notify)
- Local RAG knowledge base (harness.rag + Toolkit rag_search/rag_index)
- MCP stdio JSON-RPC 2.0 client (harness.mcp_client + Toolkit dispatch)
- XTTS v2 Voice Engine (harness.voice) & Author Plugin (xtts_voice_studio.py)
- Session edit / regenerate truncation (harness.session.truncate_from_user_turn)
- Personas in system prompt (harness.schemas.PERSONAS)
- Server HTTP endpoints & UI presence checks
"""
import base64
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import config as config_mod
from harness import mcp_client, notify, plugins, rag, schemas, server, session, tools, updater, voice


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    sess_dir = data_dir / "sessions"
    work_dir = tmp_path / "work"
    cfg_path = tmp_path / "config.json"
    data_dir.mkdir(parents=True)
    sess_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)

    monkeypatch.setattr(config_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(session, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(tools, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("LH_NO_TOAST", "1")
    cfg = config_mod.load()
    cfg["workspace"] = str(work_dir)
    config_mod.save(cfg)
    yield {"root": tmp_path, "data": data_dir, "work": work_dir, "cfg": cfg}
    mcp_client.stop_all()


# ---------- 1. Self-Updater ----------
def test_updater_version_compare():
    assert updater.parse_version("1.0.7") < updater.parse_version("1.1.0")
    assert updater.parse_version("v1.0.7-beta.1") < updater.parse_version("1.0.7")
    assert updater.is_newer("v1.2.0", "1.0.7") is True
    assert updater.is_newer("1.0.7", "1.0.7") is False
    assert updater.is_newer("1.0.6", "1.0.7") is False


def test_updater_check_and_domain_guard():
    fake_rel = json.dumps({
        "tag_name": "v2.0.0",
        "name": "LocalHarness 2.0",
        "html_url": "https://github.com/lenovolquser15-ai/Local-AI-IDE/releases/tag/v2.0.0",
        "body": "Major update",
        "assets": [
            {"name": "LocalHarness-Setup-2.0.0.exe", "browser_download_url": "https://github.com/lenovolquser15-ai/Local-AI-IDE/releases/download/v2.0.0/LocalHarness-Setup-2.0.0.exe"}
        ],
    }).encode("utf-8")

    class _FakeResp:
        def read(self):
            return fake_rel
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
        info = updater.check_update(force=True)
    assert info["ok"] is True
    assert info["available"] is True
    assert info["latest"] == "2.0.0"
    assert info["installer_name"] == "LocalHarness-Setup-2.0.0.exe"

    bad = updater.download_and_launch(installer_url="https://evil.example.com/setup.exe", launch=False)
    assert bad["ok"] is False


# ---------- 2. Windows Toast Notifications ----------
def test_notify_send_and_disable(isolated_env):
    assert notify.send("Test Title", "Test Body", cfg={"toast_notifications": False}) is False
    assert notify.send("Build Done", "All checks passed", cfg={"toast_notifications": True}) is True
    rec = notify.recent()
    assert any(n["title"] == "Build Done" and n["message"] == "All checks passed" for n in rec)


# ---------- 3. Local RAG ----------
def test_rag_index_search_remove_clear(isolated_env):
    docs = isolated_env["root"] / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text(
        "# LocalHarness Architecture\nXTTS v2 voice cloning engine and local RAG BM25 search.",
        encoding="utf-8",
    )
    (docs / "notes.py").write_text(
        "def quantum_flux_capacitor():\n    return 42\n",
        encoding="utf-8",
    )

    idx_res = rag.index_path(str(docs), cfg=isolated_env["cfg"])
    assert idx_res["ok"] is True
    assert idx_res["indexed_files"] == 2
    assert idx_res["chunks_count"] >= 2

    res = rag.search("XTTS voice cloning", top_k=3)
    assert res["ok"] is True
    assert res["count"] >= 1
    assert "architecture.md" in res["results"][0]["file"]

    tk = tools.Toolkit(isolated_env["cfg"])
    tool_res = tk.call("rag_search", {"query": "quantum_flux_capacitor", "top_k": 2})
    assert tool_res["ok"] is True
    assert any("notes.py" in r["file"] for r in tool_res["results"])

    rm = rag.remove_folder(str(docs))
    assert rm["files_count"] == 0
    assert rag.clear()["chunks_count"] == 0


# ---------- 4. MCP Stdio Client ----------
_MOCK_MCP_SCRIPT = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "mock", "version": "1.0"}}}
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string", "description": "text"}}, "required": ["msg"]}}]}}
    elif method == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "ECHO:" + str(args.get("msg", ""))}]}}
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""


def test_mcp_stdio_roundtrip_and_toolkit_dispatch(isolated_env):
    script_path = isolated_env["root"] / "mock_mcp.py"
    script_path.write_text(_MOCK_MCP_SCRIPT, encoding="utf-8")

    add_res = mcp_client.add_server("mock", sys.executable, [str(script_path)], connect=True)
    assert add_res["ok"] is True
    assert add_res["connected"] is True
    assert "echo" in add_res["tools"]

    entries = mcp_client.tool_entries()
    assert any(e["name"] == "mcp__mock__echo" for e in entries)

    # Verify schemas.entries includes MCP tools
    all_specs = schemas.entries("core", isolated_env["cfg"])
    assert any(s["name"] == "mcp__mock__echo" for s in all_specs)

    # Verify Toolkit.call dispatches to MCP tool
    tk = tools.Toolkit(isolated_env["cfg"])
    call_res = tk.call("mcp__mock__echo", {"msg": "hello_mcp"})
    assert call_res["ok"] is True
    assert call_res["output"] == "ECHO:hello_mcp"

    rm = mcp_client.remove_server("mock")
    assert rm["ok"] is True
    assert mcp_client.list_servers()["servers"] == []


# ---------- 5. Voice Engine (XTTS v2) & Author Plugin ----------
def test_voice_presets_clone_and_xtts_mock(isolated_env):
    assert voice.clean_speech_text("Hello ```py\nprint(1)\n``` **world**!") == "Hello [блок кода] world!"

    fake_wav = b"RIFF" + b"\x00" * 128
    b64 = base64.b64encode(fake_wav).decode("ascii")
    saved = voice.save_sample("MyVoice", b64)
    assert saved["ok"] is True
    assert saved["id"] == "sample:MyVoice.wav"
    assert any(p["id"] == "sample:MyVoice.wav" for p in voice.list_presets())

    with mock.patch.object(voice, "check_xtts_server", return_value={"alive": True, "url": "http://127.0.0.1:8020"}), \
         mock.patch.object(voice, "_synthesize_xtts_http", return_value={"ok": True, "audio": fake_wav, "engine_used": "xtts_v2"}):
        res = voice.synthesize("Привет мир", cfg=isolated_env["cfg"], voice=saved["id"], save_file=True)
    assert res["ok"] is True
    assert res["engine_used"] == "xtts_v2"
    assert res["audio_b64"].startswith("data:audio/wav;base64,")
    assert os.path.isfile(res["file"])

    deleted = voice.delete_sample(saved["id"])
    assert deleted["ok"] is True


def test_author_plugin_xtts_voice_studio_loaded(isolated_env):
    plugins.scan(force=True)
    assert "xtts_voice_studio.py" in plugins.PLUGINS
    info = plugins.PLUGINS["xtts_voice_studio.py"]
    assert info["error"] is None
    assert "voice_speak" in info["tools"]
    assert "voice_clone_sample" in info["tools"]
    assert "voice_list_presets" in info["tools"]

    res = plugins.call("voice_list_presets", isolated_env["cfg"], {})
    assert res["ok"] is True
    assert len(res["presets"]) >= 3


# ---------- 6. Session Edit/Regenerate Truncation & Personas ----------
def test_session_truncate_from_user_turn(isolated_env):
    sid = session.create("Test Edit")
    session.message(sid, "user", "First question")
    session.event(sid, {"type": "user", "text": "First question"})
    session.message(sid, "assistant", "First answer")
    session.event(sid, {"type": "done", "text": "First answer"})
    session.message(sid, "user", "Second question")
    session.event(sid, {"type": "user", "text": "Second question"})
    session.message(sid, "assistant", "Second answer")
    session.event(sid, {"type": "done", "text": "Second answer"})

    # Truncate last turn (-1)
    tr = session.truncate_from_user_turn(sid, -1)
    assert tr["ok"] is True
    assert tr["removed_text"] == "Second question"
    assert len(tr["remaining_messages"]) == 2

    loaded = session.load(sid)
    assert len(loaded["messages"]) == 2
    assert len(loaded["events"]) == 2

    # Truncate first turn (0)
    tr0 = session.truncate_from_user_turn(sid, 0)
    assert tr0["ok"] is True
    assert tr0["removed_text"] == "First question"
    assert session.load(sid)["messages"] == []


def test_personas_in_system_prompt(isolated_env):
    cfg = dict(isolated_env["cfg"], persona="coder")
    prompt = schemas.build_system_prompt(cfg, profile="core", tools_doc=False)
    assert "Senior Разработчик" in prompt

    cfg_v = dict(isolated_env["cfg"], persona="voice")
    prompt_v = schemas.build_system_prompt(cfg_v, profile="core", tools_doc=False)
    assert "Голосовой собеседник" in prompt_v


# ---------- 7. Server HTTP Integration & UI Checks ----------
def _http_json(base, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_server_new_endpoints(isolated_env):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d" % port
    try:
        st = _http_json(base, "/api/state")
        assert "theme" in st and "personas" in st and "rag" in st and "voice" in st and "mcp_servers" in st

        nt = _http_json(base, "/api/notify/test", {"title": "HTTP Toast", "message": "ok"})
        assert nt["ok"] is True

        v_st = _http_json(base, "/api/voice/status")
        assert v_st["ok"] is True and len(v_st["presets"]) >= 3

        mcp_list = _http_json(base, "/api/mcp/list")
        assert mcp_list["ok"] is True

        rag_st = _http_json(base, "/api/rag/status")
        assert rag_st["ok"] is True

        sid = session.create("Truncate HTTP")
        session.message(sid, "user", "Hello")
        session.event(sid, {"type": "user", "text": "Hello"})
        tr = _http_json(base, "/api/session/truncate", {"session": sid, "turn_index": -1})
        assert tr["ok"] is True and tr["removed_text"] == "Hello"
    finally:
        srv.shutdown()
        srv.server_close()


def test_ui_files_synced_and_contain_new_features():
    for folder in ("webui", os.path.join("harness", "webui")):
        html = open(os.path.join(config_mod.ROOT, folder, "index.html"), encoding="utf-8").read()
        css = open(os.path.join(config_mod.ROOT, folder, "style.css"), encoding="utf-8").read()
        js = open(os.path.join(config_mod.ROOT, folder, "app.js"), encoding="utf-8").read()

        assert 'id="personaSelect"' in html
        assert 'id="voiceCallDialog"' in html
        assert 'id="themeToggleBtn"' in html
        assert 'id="ragBlock"' in html
        assert 'id="mcpBlock"' in html
        assert 'id="updateBlock"' in html

        assert 'html[data-theme="light"]' in css
        assert ".code-copy-btn" in css
        assert ".voice-call-modal" in css

        assert "code-copy-btn" in js
        assert "editAndResend" in js
        assert "regenerateTurn" in js
        assert "openVoiceCall" in js
        assert "applyTheme" in js
        assert "refreshThermalLive" in js
        assert "AUTO_GOAL_RE" in js


def test_detect_goal_intent_from_message():
    from harness.agent import detect_goal_intent

    r1 = detect_goal_intent("поставь цель: создать лендинг и проверить его в браузере")
    assert r1["is_goal"] is True
    assert r1["objective"] == "создать лендинг и проверить его в браузере"

    r2 = detect_goal_intent("Задай цель настроить сервер. Критерий: порт 8080 отвечает 200")
    assert r2["is_goal"] is True
    assert "настроить сервер" in r2["objective"]
    assert "порт 8080" in r2["criteria"]

    r3 = detect_goal_intent("Пожалуйста, включи режим цели и почини тесты")
    assert r3["is_goal"] is True

    r4 = detect_goal_intent("Привет, какая погода сегодня?")
    assert r4["is_goal"] is False


def test_search_three_tier_priority(monkeypatch):
    from harness import search as search_mod

    calls = []

    def fake_camoufox(q, c, t):
        calls.append("camoufox")
        raise RuntimeError("camoufox offline")

    def fake_obscura(q, c, t):
        calls.append("obscura")
        raise RuntimeError("obscura offline")

    def fake_sys_browser(q, c, t):
        calls.append("system_browser")
        return [{"title": "Local Chrome Result", "url": "https://example.org/python", "snippet": "python guide", "source": "system_browser"}]

    def fake_ddg(q, c, t):
        calls.append("duckduckgo")
        return [{"title": "DDG Python Result", "url": "https://ddg.example/python", "snippet": "python", "source": "duckduckgo"}]

    fake_camoufox.__name__ = "camoufox"
    fake_obscura.__name__ = "obscura"
    fake_sys_browser.__name__ = "system_browser"
    fake_ddg.__name__ = "duckduckgo"

    monkeypatch.setattr(search_mod, "PRIMARY_BROWSERS", [fake_camoufox, fake_obscura])
    monkeypatch.setattr(search_mod, "SECONDARY_BROWSERS", [fake_sys_browser])
    monkeypatch.setattr(search_mod, "GENERAL", [fake_ddg])
    monkeypatch.setattr(search_mod, "VERTICALS", [])
    search_mod._cache.clear()
    search_mod._cooldown.clear()

    res = search_mod.search("python", count=1, engine="auto")
    assert calls == ["camoufox", "obscura", "system_browser"]
    assert res["results"][0]["source"] == "system_browser"

    # When count=2, Tier 3 (duckduckgo) fills the remaining slot
    calls.clear()
    search_mod._cache.clear()
    search_mod._cooldown.clear()
    res2 = search_mod.search("python", count=2, engine="auto")
    assert calls == ["camoufox", "obscura", "system_browser", "duckduckgo"]
    assert [r["source"] for r in res2["results"]] == ["system_browser", "duckduckgo"]


def test_thermal_multi_gpu_picks_discrete_and_parses_power(monkeypatch):
    from harness import thermal as thermal_mod

    fake_out = (
        "Intel UHD Graphics, 35, 0, 128, 512, N/A, N/A\n"
        "NVIDIA GeForce RTX 5060 Laptop GPU, 61, 48, 4200, 8151, N/A, 52.4\n"
    ).encode("utf-8")

    class _FakeProc:
        returncode = 0
        stdout = fake_out

    monkeypatch.setattr(thermal_mod.shutil, "which", lambda n: "nvidia-smi" if "nvidia" in n else None)
    monkeypatch.setattr(thermal_mod.subprocess, "run", lambda *a, **kw: _FakeProc())
    thermal_mod.set_simulated_temp(None)
    st = thermal_mod.get_thermal_status({}, force=True)
    assert st["gpu_name"] == "NVIDIA GeForce RTX 5060 Laptop GPU"
    assert st["temp_c"] == 61
    assert st["utilization_pct"] == 48
    assert st["power_w"] == 52.4

