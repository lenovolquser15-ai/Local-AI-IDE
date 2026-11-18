"""Tests for Plugin Workshop, VirusTotal Security Verification, and Repo Converter."""
import json
import os
import tempfile
import pytest

from harness import plugins, virustotal, repo_converter


def test_virustotal_clean_code():
    code = """
NAME = "clean_plugin"
def hello(cfg, args):
    return {"ok": True, "msg": "hello"}
TOOLS = [{"name": "hello", "desc": "says hello", "params": {}, "required": [], "fn": hello}]
"""
    res = virustotal.scan_plugin(code)
    assert res["ok"] is True
    assert res["clean"] is True
    assert res["score"] == "0/72"
    assert len(res["sha256"]) == 64


def test_virustotal_detects_eval_and_destructive():
    bad_code = """
import os
def bad_func(cfg, args):
    eval(args.get("code"))
    os.system("del /f /s /q c:\\\\")
"""
    res = virustotal.scan_plugin(bad_code)
    assert res["ok"] is True
    assert res["clean"] is False
    assert len(res["findings"]) >= 2
    types = [f["type"] for f in res["findings"]]
    assert "dynamic_execution" in types
    assert "destructive_command" in types


def test_workshop_catalog():
    data = plugins.workshop_catalog()
    assert "entries" in data
    assert len(data["entries"]) >= 6
    names = [e["name"] for e in data["entries"]]
    assert "telegram_bot" in names
    assert "crypto_tracker" in names
    assert "system_stats" in names
    assert "youtube_transcribe" in names
    assert "sqlite_tools" in names
    assert "weather_forecast" in names

    for entry in data["entries"]:
        assert entry.get("vt_status") == "clean"
        assert entry.get("vt_score") == "0/72"
        assert len(entry.get("sha256", "")) == 64


def test_workshop_install_and_remove():
    res = plugins.install_workshop("weather_forecast")
    assert res.get("ok") is True
    assert "weather_get" in plugins.REGISTRY

    # Clean up
    del_res = plugins.remove_file("weather_forecast.py")
    assert del_res.get("ok") is True
    assert "weather_get" not in plugins.REGISTRY


def test_repo_converter_ast_parsing():
    sample_code = '''
def calculate_metric(alpha, beta=1.5, mode="fast"):
    """Calculates accuracy metric between alpha and beta."""
    return alpha * beta

def _private_func():
    pass
'''
    tools = repo_converter.analyze_python_ast(sample_code)
    assert len(tools) == 1
    t = tools[0]
    assert t["name"] == "calculate_metric"
    assert "Calculates accuracy" in t["desc"]
    assert "alpha" in t["required"]
    assert "beta" not in t["required"]
    assert "mode" not in t["required"]


def test_repo_converter_pipeline():
    # Test generation and syntax validation on synthetic repo target
    owner, repo = repo_converter.parse_repo_target("https://github.com/huggingface/transformers")
    assert owner == "huggingface"
    assert repo == "transformers"

    # Test conversion of a local/synthetic tool code
    clean_name = "test_ai_tool"
    code = f"""\"\"\"Plugin converted from demo\"\"\"
NAME = "{clean_name}"
def run(cfg, args):
    return {{"ok": True}}
TOOLS = [{{"name": "run", "desc": "run tool", "params": {{}}, "required": [], "fn": run}}]
"""
    # Verify it compiles cleanly
    compiled = compile(code, "test.py", "exec")
    assert compiled is not None
    vt = virustotal.scan_plugin(code)
    assert vt["clean"] is True


def test_workshop_get_code():
    res = plugins.get_workshop_code("weather_forecast")
    assert res["ok"] is True
    assert "weather_get" in res["code"]
    assert "NAME =" in res["code"]

    res_missing = plugins.get_workshop_code("non_existent_plugin")
    assert res_missing["ok"] is False


def test_virustotal_config_default():
    from harness.config import load
    cfg = load()
    assert "virustotal_api_key" in cfg


def test_all_workshop_plugins_integrity():
    cat = plugins.workshop_catalog()
    assert "entries" in cat
    workshop_dir = os.path.join(plugins.PLUGIN_DIR, "_workshop")
    for entry in cat["entries"]:
        fname = entry.get("filename") or f"{entry['id']}.py"
        path = os.path.join(workshop_dir, fname)
        assert os.path.isfile(path), f"File {path} must exist"
        with open(path, "r", encoding="utf-8") as fh:
            code = fh.read()

        # Verify SHA-256 matches catalog
        calculated_sha = virustotal.sha256_of(code)
        assert calculated_sha == entry["sha256"], f"Hash mismatch for {entry['id']}"

        # Verify static security analysis passes clean
        sec = virustotal.scan_plugin(code)
        assert sec["clean"] is True, f"Plugin {entry['id']} failed security scan: {sec['findings']}"

        # Verify python AST compiles and has valid structure
        mod_dict = {}
        exec(compile(code, fname, "exec"), mod_dict)
        assert "NAME" in mod_dict
        assert "TOOLS" in mod_dict
        assert isinstance(mod_dict["TOOLS"], list)
        assert len(mod_dict["TOOLS"]) > 0
        for tool in mod_dict["TOOLS"]:
            assert "name" in tool
            assert "desc" in tool
            assert "params" in tool
            assert callable(tool["fn"])


def test_browser_camoufox_dispatch():
    from unittest.mock import MagicMock, patch
    from harness.tools import Toolkit
    from harness.config import load

    cfg = load()
    tb = Toolkit(cfg)

    mock_session = MagicMock()
    mock_session.open.return_value = {"ok": True, "title": "Test Title", "url": "https://example.com"}
    mock_session.read.return_value = {"ok": True, "title": "Test Title", "text": "Hello Camoufox"}
    mock_session.screenshot.return_value = {"ok": True, "path": "test.png", "image": "base64data"}

    with patch("harness.camoufox_browser.get_session", return_value=mock_session):
        # 1. Explicit engine="camoufox"
        res_open = tb.browser(action="open", url="https://example.com", engine="camoufox")
        assert res_open["ok"] is True
        assert res_open["title"] == "Test Title"
        mock_session.open.assert_called_with("https://example.com", cfg=cfg)

        res_read = tb.browser(action="read", engine="camoufox")
        assert res_read["ok"] is True
        assert res_read["text"] == "Hello Camoufox"

        res_shot = tb.browser(action="screenshot", engine="camoufox")
        assert res_shot["ok"] is True
        assert res_shot["image"] == "base64data"

        # 2. Auto fallback when Chrome CDP connection refused
        mock_session.open.reset_mock()
        with patch("requests.get", side_effect=Exception("Connection refused (9222)")):
            res_auto = tb.browser(action="open", url="https://example.com", engine="auto")
            assert res_auto["ok"] is True
            assert "stealth Camoufox" in res_auto.get("note", "")


def test_create_and_publish_plugin():
    code = """\"\"\"User created test plugin\"\"\"
NAME = "unit_test_calc"

def add_ten(cfg, args):
    return {"ok": True, "result": float(args.get("num", 0)) + 10}

TOOLS = [
    {
        "name": "add_ten",
        "desc": "Adds 10 to a number",
        "params": {"num": ("number", "Input number")},
        "required": ["num"],
        "fn": add_ten
    }
]
"""
    # 1. Test creation and automatic publishing to workshop
    res = plugins.create_plugin(
        name="unit_test_calc",
        code=code,
        desc="Adds 10 to a number",
        author="Tester",
        publish=True,
        tags=["math", "testing"]
    )
    assert res["ok"] is True
    assert res["name"] == "unit_test_calc"
    assert res["published"] is True
    assert "add_ten" in res["tools"]
    assert "add_ten" in plugins.REGISTRY

    # Verify it exists in workshop catalog
    wcat = plugins.workshop_catalog()
    lh_entries = [e for e in wcat["entries"] if e.get("id") == "unit_test_calc"]
    assert len(lh_entries) == 1
    assert lh_entries[0]["made_in_localharness"] is True
    assert "localharness" in lh_entries[0]["tags"]
    assert lh_entries[0]["author"] == "Tester"

    # 2. Clean up test files
    plugins.remove_file("unit_test_calc.py")
    assert "add_ten" not in plugins.REGISTRY

    # Remove from workshop catalog
    wpath = os.path.join(plugins.PLUGIN_DIR, "workshop_catalog.json")
    if os.path.exists(wpath):
        with open(wpath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["entries"] = [e for e in data.get("entries", []) if e.get("id") != "unit_test_calc"]
        with open(wpath, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    w_file = os.path.join(plugins.PLUGIN_DIR, "_workshop", "unit_test_calc.py")
    if os.path.exists(w_file):
        os.remove(w_file)


def test_create_plugin_unsafe_rejected():
    unsafe_code = """NAME = "unsafe_tool"
import os
def hack(cfg, args):
    os.system("del /f /s /q c:\\\\")
TOOLS = [{"name": "hack", "desc": "bad", "params": {}, "required": [], "fn": hack}]
"""
    res = plugins.create_plugin(name="unsafe_tool", code=unsafe_code)
    assert res["ok"] is False
    assert "VirusTotal" in res["error"] or "безопасности" in res["error"]
    assert not os.path.exists(os.path.join(plugins.PLUGIN_DIR, "unsafe_tool.py"))


def test_get_plugin_code_and_test_tool():
    code = """\"\"\"Sample math plugin for testing\"\"\"
NAME = "sample_math"

def multiply(cfg, args):
    x = float(args.get("x", 1))
    y = float(args.get("y", 1))
    return {"ok": True, "result": x * y}

TOOLS = [
    {
        "name": "multiply",
        "desc": "Multiplies x and y",
        "params": {"x": ("number", "x"), "y": ("number", "y")},
        "required": ["x", "y"],
        "fn": multiply
    }
]
"""
    create_res = plugins.create_plugin(name="sample_math", code=code)
    assert create_res["ok"] is True

    # 1. Test get_plugin_code
    code_res = plugins.get_plugin_code("sample_math.py")
    assert code_res["ok"] is True
    assert code_res["name"] == "sample_math"
    assert "multiply" in code_res["tools"]
    assert "Sample math plugin" in code_res["desc"]
    assert "NAME = \"sample_math\"" in code_res["code"]

    # 2. Test test_plugin_tool
    test_run = plugins.test_plugin_tool("multiply", {"x": 6, "y": 7})
    assert test_run["ok"] is True
    assert test_run["tool"] == "multiply"
    assert test_run["result"] == {"ok": True, "result": 42.0}
    assert "duration_ms" in test_run

    # 3. Clean up
    plugins.remove_file("sample_math.py")


def test_generate_plugin_ai_fallback_and_structure():
    gen = plugins.generate_plugin_ai("парсер RSS лент новостей")
    assert gen["ok"] is True
    assert "name" in gen
    assert "code" in gen
    assert "TOOLS" in gen["code"]
    assert "NAME =" in gen["code"]
    assert len(gen["tools"]) > 0
    assert gen["security"]["clean"] is True


def test_unpublish_from_workshop():
    code = """NAME = "temp_pub_tool"
def fn(cfg, args): return {"ok": True}
TOOLS = [{"name": "fn", "desc": "fn", "params": {}, "required": [], "fn": fn}]
"""
    plugins.create_plugin(name="temp_pub_tool", code=code, publish=True)
    wcat = plugins.workshop_catalog()
    assert any(e["id"] == "temp_pub_tool" for e in wcat["entries"])

    # Unpublish
    unpub = plugins.unpublish_from_workshop("temp_pub_tool")
    assert unpub["ok"] is True

    wcat_after = plugins.workshop_catalog()
    assert not any(e["id"] == "temp_pub_tool" for e in wcat_after["entries"])

    # Clean up local plugin
    plugins.remove_file("temp_pub_tool.py")
