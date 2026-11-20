"""Tests for Obscura Rust stealth browser integration in search and browser tools."""
import os
import socket
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "test-obscura-config.json")

from harness import obscura_browser  # noqa: E402
from harness import search as search_mod  # noqa: E402
from harness import server as server_mod  # noqa: E402
from harness import tools as tools_mod  # noqa: E402


@pytest.fixture(autouse=True)
def reset_search():
    search_mod.reset_state()
    yield
    search_mod.reset_state()


def test_obscura_registered_in_search_engines_and_aliases():
    assert "obscura" in search_mod.ENGINES
    assert search_mod.STEALTH[0].__name__ == "obscura"
    assert search_mod.ALIASES["обскура"] == "obscura"
    assert search_mod.ALIASES["obscura"] == "obscura"

    st = obscura_browser.status({})
    assert isinstance(st, dict)
    for k in ("available", "installed", "running", "exe", "cdp_url", "stealth"):
        assert k in st


def test_obscura_search_ddg_results(monkeypatch):
    fake_session = MagicMock()
    fake_session.open.return_value = {"ok": True, "url": "https://html.duckduckgo.com/html/"}
    fake_session.evaluate.return_value = {
        "ok": True,
        "result": [
            {"title": "Obscura Browser on GitHub", "url": "https://github.com/h4ckf0r0day/obscura", "snippet": "Lightweight Rust headless browser"},
            {"title": "Rust CDP Guide", "url": "https://example.com/rust-cdp", "snippet": "Chrome DevTools Protocol in Rust"},
        ],
    }
    monkeypatch.setattr(obscura_browser, "get_session", lambda: fake_session)

    res = search_mod.search("obscura rust browser", count=5, engine="обскура")
    assert res["engine"] == "obscura"
    assert "obscura" in res["sources"]
    assert len(res["results"]) == 2
    assert res["results"][0]["source"] == "obscura"
    assert "github.com/h4ckf0r0day/obscura" in res["results"][0]["url"]


def test_obscura_search_bing_fallback(monkeypatch):
    fake_session = MagicMock()
    fake_session.open.return_value = {"ok": True}
    # First evaluate (DDG) returns empty, second evaluate (Bing) returns results
    fake_session.evaluate.side_effect = [
        {"ok": True, "result": []},
        {"ok": True, "result": [
            {"title": "Python 3.13 release notes", "url": "https://docs.python.org/3.13/whatsnew/", "snippet": "Official Python 3.13 docs"},
        ]},
    ]
    monkeypatch.setattr(obscura_browser, "get_session", lambda: fake_session)

    items = search_mod.obscura("python 3.13", count=5)
    assert len(items) == 1
    assert items[0]["source"] == "obscura"
    assert "docs.python.org" in items[0]["url"]


def test_toolkit_browser_obscura_engine(monkeypatch):
    fake_session = MagicMock()
    fake_session.open.return_value = {"ok": True, "engine": "obscura", "url": "https://example.com"}
    fake_session.read.return_value = {"ok": True, "engine": "obscura", "title": "Example", "url": "https://example.com", "text": "Hello from Obscura"}
    monkeypatch.setattr(obscura_browser, "get_session", lambda: fake_session)

    tk = tools_mod.Toolkit({"workspace": os.environ["LH_DATA"]})
    res = tk.browser(action="read", url="https://example.com", engine="obscura")
    assert res["ok"] is True
    assert res["engine"] == "obscura"
    assert "Hello from Obscura" in res["text"]


def test_server_obscura_endpoint_and_state(monkeypatch):
    monkeypatch.setattr(server_mod.dnsfix, "resolve", lambda host: "1.1.1.1")
    monkeypatch.setattr(server_mod, "cached_provider_status", lambda cfg: {"alive": True, "label": "test", "models": []})

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()

    cfg = server_mod.load()
    cfg.update({"provider": "ollama", "host": "127.0.0.1", "port": port, "lan_access": False})
    server_mod.save(cfg)

    thread = threading.Thread(target=server_mod.serve, args=(server_mod.load(),), daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port

    try:
        for _ in range(50):
            try:
                r = requests.get(base + "/api/thermal/status", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        st = requests.get(base + "/api/state", timeout=15).json()
        assert "obscura_status" in st
        assert "search_obscura" in st

        test_res = requests.post(base + "/api/obscura/test", json={"obscura_path": ""}, timeout=10).json()
        assert "status" in test_res
    finally:
        fn = server_mod._RESTARTER["fn"]
        if fn is not None:
            fn()
        thread.join(timeout=5)
