"""Tests for real-time system dashboard metrics collector and HTTP endpoints."""
import os
import socket
import sys
import threading
import time

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "test-dashboard-config.json")

from harness import dashboard as dashboard_mod  # noqa: E402
from harness import llm as llm_mod  # noqa: E402
from harness import server as server_mod  # noqa: E402
from harness import thermal as thermal_mod  # noqa: E402


@pytest.fixture(autouse=True)
def reset_dashboard():
    dashboard_mod._reset()
    thermal_mod.set_simulated_temp(None)
    yield
    dashboard_mod._reset()
    thermal_mod.set_simulated_temp(None)


def test_dashboard_collection_starts_and_stops():
    dashboard_mod.start()
    time.sleep(2.4)
    dashboard_mod.stop()

    hist = dashboard_mod.get_history(minutes=10)
    assert len(hist) >= 1
    first = hist[0]
    for key in ("ts", "gpu_temp", "gpu_util", "vram_used_mb", "vram_total_mb",
                "ram_used_pct", "ram_free_gb", "cpu_pct", "tokens_total", "active_sessions"):
        assert key in first


def test_record_tokens_and_sessions():
    dashboard_mod.record_tokens(150)
    dashboard_mod.record_tokens(45)
    dashboard_mod.record_session()
    dashboard_mod.record_session()
    dashboard_mod.record_session_end()
    dashboard_mod.record_model_load()

    summary = dashboard_mod.get_summary()
    assert summary["total_tokens"] == 195
    assert summary["total_sessions"] == 2
    assert summary["model_loads"] == 1

    latest = dashboard_mod.get_latest()
    assert latest["tokens_total"] == 195
    assert latest["active_sessions"] == 1


def test_speedmeter_finish_records_tokens():
    meter = llm_mod.SpeedMeter()
    meter.usage({"completion_tokens": 77})
    meter.piece("hello world")
    meter.finish()

    summary = dashboard_mod.get_summary()
    assert summary["total_tokens"] == 77


def test_get_history_time_filter():
    now = time.time()
    with dashboard_mod._LOCK:
        dashboard_mod._HISTORY.append({"ts": now - 600, "cpu_pct": 10.0})
        dashboard_mod._HISTORY.append({"ts": now - 60, "cpu_pct": 25.0})
        dashboard_mod._HISTORY.append({"ts": now - 5, "cpu_pct": 40.0})

    last_2 = dashboard_mod.get_history(minutes=2)
    assert len(last_2) == 2
    assert [s["cpu_pct"] for s in last_2] == [25.0, 40.0]

    last_15 = dashboard_mod.get_history(minutes=15)
    assert len(last_15) == 3


def test_get_latest_returns_dict():
    latest = dashboard_mod.get_latest()
    assert isinstance(latest, dict)
    assert "ts" in latest
    assert "ram_used_pct" in latest
    assert "cpu_pct" in latest
    assert "vram_used_mb" in latest


def test_get_summary_fields():
    thermal_mod.set_simulated_temp(72)
    dashboard_mod.start()
    time.sleep(0.3)
    dashboard_mod._collect_once()
    thermal_mod.set_simulated_temp(84)
    dashboard_mod._collect_once()
    dashboard_mod.stop()

    summary = dashboard_mod.get_summary()
    for key in ("uptime", "total_tokens", "total_sessions", "peak_gpu_temp", "avg_gpu_temp", "model_loads"):
        assert key in summary
    assert summary["peak_gpu_temp"] >= 84
    assert summary["avg_gpu_temp"] is not None
    assert summary["uptime"] >= 0


def test_server_dashboard_endpoints(monkeypatch):
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
                r = requests.get(base + "/api/dashboard/latest", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        latest_resp = requests.get(base + "/api/dashboard/latest", timeout=10)
        assert latest_resp.status_code == 200
        latest = latest_resp.json()
        assert "ts" in latest
        assert "ram_used_pct" in latest

        hist_resp = requests.get(base + "/api/dashboard/history?minutes=10", timeout=10)
        assert hist_resp.status_code == 200
        payload = hist_resp.json()
        assert payload["ok"] is True
        assert isinstance(payload["history"], list)
        assert len(payload["history"]) >= 1
        assert "uptime" in payload["summary"]
        assert "total_tokens" in payload["summary"]
    finally:
        fn = server_mod._RESTARTER["fn"]
        if fn is not None:
            fn()
        thread.join(timeout=5)
