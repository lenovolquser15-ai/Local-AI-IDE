"""Offline tests for repo converter v2, the codex-security plugin and context clamping."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("LH_DATA", os.path.join(ROOT, "tests", ".tmp"))
os.environ.setdefault("LH_CONFIG", os.path.join(ROOT, "tests", ".tmp", "test-converter-config.json"))

from harness import config as config_mod  # noqa: E402
from harness import plugins, repo_converter  # noqa: E402


def _patch_repo(monkeypatch, tree, files, meta):
    monkeypatch.setattr(repo_converter, "fetch_github_json",
                        lambda path, token=None: tree if "git/trees" in path else meta)
    monkeypatch.setattr(repo_converter, "fetch_raw_file",
                        lambda owner, repo, branch, path: files[path])


NPM_TREE = {"tree": [
    {"path": "README.md", "type": "blob"},
    {"path": "package.json", "type": "blob"},
    {"path": "src/cli.ts", "type": "blob"},
]}
NPM_FILES = {
    "README.md": "# Codex Security\n\nScan a code base for vulnerabilities.\n",
    "package.json": json.dumps({"name": "@openai/codex-security",
                                "bin": {"cs": "dist/cli.js", "codex-security": "dist/cli.js"}}),
}
NPM_META = {"description": "security scanner", "default_branch": "main",
            "stargazers_count": 42, "language": "TypeScript"}


def test_convert_npm_repo_generates_working_tools(monkeypatch):
    _patch_repo(monkeypatch, NPM_TREE, NPM_FILES, NPM_META)
    res = repo_converter.convert_repo("openai/codex-security", save=False)

    assert res["ok"] is True
    assert res["mode"] == "cli"
    assert res["package"] == "@openai/codex-security"
    assert res["cli"] == ["cs", "codex-security"]
    assert res["tools"] == ["codex_security_info", "codex_security_files",
                            "codex_security_read", "codex_security_cli"]
    assert res["security"]["clean"] is True
    assert "Node.js" in res["install_hint"]
    compile(res["code"], "generated.py", "exec")

    ns = {}
    exec(res["code"], ns)
    ns["_api"] = lambda path: {"stargazers_count": 42, "language": "TypeScript",
                               "description": "security scanner", "topics": []}
    info = ns["_repo_info"]({}, {})
    assert info["ok"] is True and info["stars"] == 42

    ns["_api"] = lambda path: {"tree": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}]}
    files = ns["_repo_files"]({}, {"limit": 2})
    assert files["total"] == 3 and files["shown"] == 2 and files["truncated"] is True

    ns["_get"] = lambda url: b"print('hi')\n"
    read = ns["_repo_read"]({}, {"path": "a.py"})
    assert read["ok"] is True and read["content"].startswith("print")

    assert ns["_repo_read"]({}, {"path": "../secret"})["ok"] is False
    blocked = ns["_cli"]({}, {"args": "--help & calc"})
    assert blocked["ok"] is False and "запрещены" in blocked["error"]


def test_convert_python_repo_detects_console_script(monkeypatch):
    tree = {"tree": [{"path": "pyproject.toml", "type": "blob"},
                     {"path": "pkg/tools.py", "type": "blob"}]}
    files = {
        "pyproject.toml": '[project]\nname = "demo-tools"\n\n[project.scripts]\ndemo = "pkg.tools:main"\n',
        "pkg/tools.py": 'def scrape(url, depth=2):\n    """Scrape a page."""\n    return url\n',
    }
    _patch_repo(monkeypatch, tree, files, {"default_branch": "main", "description": ""})
    res = repo_converter.convert_repo("demo/toolkit", custom_name="demo_toolkit", save=False)

    assert res["ok"] is True and res["mode"] == "cli"
    assert res["package"] == "demo-tools"
    assert "demo" in res["cli"]
    assert res["tools"][-1] == "demo_toolkit_cli"
    assert "scrape" in res["python_functions"]
    compile(res["code"], "generated.py", "exec")


def test_convert_read_only_repo_skips_cli(monkeypatch):
    _patch_repo(monkeypatch, {"tree": [{"path": "lib.py", "type": "blob"}]},
                {"lib.py": "def helper():\n    pass\n"},
                {"default_branch": "main", "description": "plain library"})
    res = repo_converter.convert_repo("pure/lib", save=False)

    assert res["ok"] is True and res["mode"] == "read"
    assert res["tools"] == ["lib_info", "lib_files", "lib_read"]
    assert res["security"]["clean"] is True


def test_converted_plugin_installs_and_unloads(monkeypatch):
    _patch_repo(monkeypatch, NPM_TREE, NPM_FILES, NPM_META)
    res = repo_converter.convert_repo("openai/codex-security", custom_name="conv_install_probe")
    try:
        assert res["ok"] is True and res["installed"] is True
        assert "conv_install_probe_info" in plugins.REGISTRY
    finally:
        plugins.remove_file("conv_install_probe.py")
    assert "conv_install_probe_info" not in plugins.REGISTRY


def test_codex_security_plugin_is_loaded():
    info = plugins.scan(force=True).get("codex_security.py") or {}
    assert info.get("error") is None
    assert info.get("name") == "codex_security"
    assert {"codex_security_status", "codex_security_scan",
            "codex_security_scans", "codex_security_cli"} <= set(info.get("tools") or [])

    cfg = config_mod.load()
    status = plugins.call("codex_security_status", cfg, {})
    assert status["ok"] is True
    assert status["package"] == "@openai/codex-security"

    blocked = plugins.call("codex_security_scan", cfg, {"diff": "a & calc"})
    assert blocked["ok"] is False and "запрещены" in blocked["error"]
    empty = plugins.call("codex_security_cli", cfg, {"args": ""})
    assert empty["ok"] is False


def test_context_values_are_clamped(monkeypatch, tmp_path):
    assert config_mod.normalize_context(10 ** 9) == config_mod.CONTEXT_MAX
    assert config_mod.normalize_context(1) == config_mod.CONTEXT_MIN
    assert config_mod.normalize_context("junk", 8192) == 8192
    assert config_mod.normalize_context(16384) == 16384
    assert config_mod.as_int("12.9") == 12
    assert config_mod.as_int("junk", 7) == 7

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"context_tokens": 10 ** 9, "lms_context_length": 1,
                                "max_tokens": 0}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(path))
    cfg = config_mod.load()
    assert cfg["context_tokens"] == config_mod.CONTEXT_MAX
    assert cfg["lms_context_length"] == config_mod.CONTEXT_MIN
    assert cfg["max_tokens"] == config_mod.MAX_TOKENS_MIN


def test_agent_clamps_absurd_window(monkeypatch):
    from harness.agent import Agent, Bridge
    from harness import session as session_mod

    cfg = config_mod.load()
    cfg["context_tokens"] = 10 ** 9
    cfg["max_tokens"] = -5
    agent = Agent(cfg, Bridge(), session_mod.create("ctx-clamp-test"))
    assert agent.window_units == config_mod.CONTEXT_MAX * 3.0
