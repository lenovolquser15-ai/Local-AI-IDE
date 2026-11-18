"""Tests for GPU thermal monitoring, throttling governor, emergency cooldown, and server APIs."""
import os
import sys
import threading
import time
import requests
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "test-thermal-config.json")

from harness import thermal
from harness import llm
from harness import tools
from harness import server as server_mod


@pytest.fixture(autouse=True)
def cleanup_thermal():
    thermal.set_simulated_temp(None)
    thermal.cancel_cooldown()
    yield
    thermal.set_simulated_temp(None)
    thermal.cancel_cooldown()


def test_sensor_reading_and_simulation():
    # Real or fallback sensor read
    sensors = thermal.read_gpu_sensors(force=True)
    assert isinstance(sensors, dict)
    assert "has_gpu" in sensors
    assert "temp_c" in sensors

    # Simulation override
    thermal.set_simulated_temp(88)
    sim = thermal.read_gpu_sensors(force=True)
    assert sim["temp_c"] == 88
    assert sim["simulated"] is True
    assert sim["has_gpu"] is True

    # Reset
    thermal.set_simulated_temp(None)
    reset = thermal.read_gpu_sensors(force=True)
    assert reset["simulated"] is False


def test_thermal_status_categories():
    cfg = {
        "gpu_thermal_enabled": True,
        "gpu_temp_warn": 80,
        "gpu_temp_throttle": 85,
        "gpu_temp_critical": 95,
        "gpu_cooldown_duration": 180,
        "gpu_target_temp": 70,
    }

    # Normal (< 80)
    thermal.set_simulated_temp(55)
    st_normal = thermal.get_thermal_status(cfg, force=True)
    assert st_normal["status"] == "normal"
    assert st_normal["throttle_active"] is False
    assert st_normal["cooldown_recommended"] is False
    assert st_normal["throttle_delay"] == 0.0

    # Warm (80..84)
    thermal.set_simulated_temp(82)
    st_warm = thermal.get_thermal_status(cfg, force=True)
    assert st_warm["status"] == "warm"
    assert st_warm["throttle_active"] is False
    assert st_warm["cooldown_recommended"] is False

    # Hot (85..94) -> Throttling active
    thermal.set_simulated_temp(89)
    st_hot = thermal.get_thermal_status(cfg, force=True)
    assert st_hot["status"] == "hot"
    assert st_hot["throttle_active"] is True
    assert st_hot["throttle_delay"] > 0.03
    assert st_hot["cooldown_recommended"] is False

    # Critical (>= 95) -> Emergency cooldown recommended
    thermal.set_simulated_temp(102)
    st_crit = thermal.get_thermal_status(cfg, force=True)
    assert st_crit["status"] == "critical"
    assert st_crit["throttle_active"] is True
    assert st_crit["cooldown_recommended"] is True
    assert st_crit["throttle_delay"] > 0.15


def test_stream_delay_governor():
    cfg = {"gpu_thermal_enabled": True, "gpu_temp_throttle": 85, "gpu_temp_critical": 95}

    thermal.set_simulated_temp(60)
    assert thermal.get_stream_delay(cfg) == 0.0

    thermal.set_simulated_temp(90)
    delay = thermal.get_stream_delay(cfg)
    assert delay > 0.03

    # If disabled in config
    cfg_disabled = {"gpu_thermal_enabled": False, "gpu_temp_throttle": 85}
    assert thermal.get_stream_delay(cfg_disabled) == 0.0


def test_cooldown_lifecycle():
    thermal.set_simulated_temp(99)
    res = thermal.start_cooldown(duration=120, target_temp=70, unload=False)
    assert res["active"] is True
    assert res["duration"] == 120
    assert res["remaining"] <= 120
    assert res["target_temp"] == 70

    status = thermal.get_cooldown_status()
    assert status["active"] is True

    cancel = thermal.cancel_cooldown()
    assert cancel["active"] is False


def test_speedmeter_includes_gpu_thermal():
    thermal.set_simulated_temp(89)
    meter = llm.SpeedMeter()
    meter.piece("тестовый фрагмент для замера скорости")
    snap = meter.snapshot()
    assert snap.get("gpu_temp") == 89
    assert snap.get("gpu_throttled") is True


def test_sysinfo_gpu_action():
    thermal.set_simulated_temp(82)
    tk = tools.Toolkit({"workspace": os.environ["LH_DATA"]})
    res = tk.sysinfo(action="gpu")
    assert res.get("ok") is True
    assert "thermal" in res
    assert res["thermal"]["temp_c"] == 82


def test_server_thermal_endpoints(monkeypatch):
    monkeypatch.setattr(server_mod.dnsfix, "resolve", lambda host: "1.1.1.1")
    monkeypatch.setattr(server_mod, "cached_provider_status", lambda cfg: {"alive": True, "label": "test", "models": []})
    import socket
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
        # Wait for server to start
        for _ in range(50):
            try:
                r = requests.get(base + "/api/thermal/status", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        # 1. GET /api/thermal/status
        st = requests.get(base + "/api/thermal/status", timeout=5).json()
        assert "has_gpu" in st
        assert "status" in st

        # 2. POST /api/thermal/simulate with 103 C
        sim = requests.post(base + "/api/thermal/simulate", json={"temp": 103}, timeout=5).json()
        assert sim["temp_c"] == 103
        assert sim["status"] == "critical"
        assert sim["cooldown_recommended"] is True

        # 3. GET /api/state contains gpu_thermal
        state = requests.get(base + "/api/state", timeout=15).json()
        assert "gpu_thermal" in state
        assert state["gpu_thermal"]["temp_c"] == 103
        assert state["gpu_thermal"]["status"] == "critical"

        # 4. POST /api/thermal/cooldown/start
        cd = requests.post(base + "/api/thermal/cooldown/start",
                           json={"duration": 90, "target_temp": 68, "unload": False},
                           timeout=10).json()
        assert cd["active"] is True
        assert cd["duration"] == 90

        # 5. POST /api/thermal/cooldown/cancel
        cc = requests.post(base + "/api/thermal/cooldown/cancel", json={}, timeout=10).json()
        assert cc["active"] is False

        # 6. Reset simulation
        reset = requests.post(base + "/api/thermal/simulate", json={"temp": None}, timeout=10).json()
        assert reset["simulated"] is False

    finally:
        fn = server_mod._RESTARTER["fn"]
        if fn is not None:
            fn()
        thread.join(timeout=5)
