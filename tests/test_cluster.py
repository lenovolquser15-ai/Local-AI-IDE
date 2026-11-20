"""Tests for LAN Wi-Fi Compute Cluster (mobile/browser workers + LAN LLM offload)."""
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
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "test-cluster-config.json")

from harness import cluster as cluster_mod  # noqa: E402
from harness import server as server_mod  # noqa: E402
from harness import tools as tools_mod  # noqa: E402


@pytest.fixture(autouse=True)
def reset_cluster():
    cluster_mod._reset()
    yield
    cluster_mod._reset()


def test_worker_register_heartbeat_unregister():
    w = cluster_mod.register_worker({
        "name": "Galaxy S24",
        "device_type": "mobile",
        "cores": 8,
        "max_threads": 2,
        "battery_pct": 85,
        "charging": True,
    }, client_ip="192.168.1.42")
    assert w["name"] == "Galaxy S24"
    assert w["max_threads"] == 2
    assert len(cluster_mod.list_workers()) == 1

    hb = cluster_mod.heartbeat_worker(w["id"], {"battery_pct": 19, "charging": False, "paused": True})
    assert hb["battery_pct"] == 19
    assert hb["paused"] is True
    assert cluster_mod.active_worker_count() == 0

    rem = cluster_mod.unregister_worker(w["id"])
    assert rem["removed"] is True
    assert len(cluster_mod.list_workers()) == 0


def test_distributed_map_across_multiple_workers():
    w1 = cluster_mod.register_worker({"name": "iPhone 15", "device_type": "mobile", "cores": 6, "max_threads": 2})
    w2 = cluster_mod.register_worker({"name": "iPad Air", "device_type": "mobile", "cores": 8, "max_threads": 2})

    stop = threading.Event()

    def fake_device(wid):
        while not stop.is_set():
            polled = cluster_mod.poll_task(wid, wait=0.2)
            task = polled.get("task")
            if task:
                res, ms = cluster_mod._execute_locally(task["kind"], task["payload"])
                time.sleep(0.02)
                cluster_mod.submit_result(task["id"], wid, ok=True, result=res, duration_ms=ms)

    t1 = threading.Thread(target=fake_device, args=(w1["id"],), daemon=True)
    t2 = threading.Thread(target=fake_device, args=(w2["id"],), daemon=True)
    t1.start()
    t2.start()

    try:
        items = [{"iterations": 2000, "seed": i} for i in range(8)]
        out = cluster_mod.map_tasks("compute_bench", items, timeout=10.0, allow_local_fallback=False)
        assert out["ok"] is True
        assert len(out["results"]) == 8
        assert out["executed_locally"] is False
        assert out["workers_used"] >= 1
        assert sum(out["per_worker"].values()) == 8
    finally:
        stop.set()
        t1.join(timeout=2)
        t2.join(timeout=2)


def test_local_fallback_when_no_workers():
    res = cluster_mod.run_cluster_benchmark(chunks=4, iterations=2000)
    assert res["ok"] is True
    assert res["executed_locally"] is True
    assert res["chunks"] == 4
    assert len(res["results"]) == 4


def test_hash_and_text_search_tasks():
    h = cluster_mod.submit_task("hash_batch", {"items": ["alpha", "beta"]})
    assert h["ok"] is True
    assert h["result"]["count"] == 2
    assert len(h["result"]["hashes"][0]) == 64

    s = cluster_mod.submit_task("text_search", {
        "query": "gpu thermal",
        "docs": [
            {"id": "a", "text": "unrelated note"},
            {"id": "b", "text": "GPU thermal throttling protects hardware"},
        ],
    })
    assert s["ok"] is True
    assert s["result"]["matches"][0]["id"] == "b"


def test_llm_node_thermal_offload_routing():
    with cluster_mod._LOCK:
        cluster_mod._LLM_NODES["http://192.168.1.50:11434/v1"] = {
            "url": "http://192.168.1.50:11434/v1",
            "label": "Ollama PC",
            "enabled": True,
            "alive": True,
            "models": ["qwen2.5:14b"],
            "latency_ms": 18.0,
        }

    cfg = {"cluster_enabled": True, "cluster_offload_on_hot": True}
    # Normal temp -> keep local GPU
    assert cluster_mod.pick_llm_node(cfg, {"status": "normal"}) is None
    # Hot temp -> offload to LAN node!
    picked = cluster_mod.pick_llm_node(cfg, {"status": "hot"})
    assert picked is not None
    assert picked["url"] == "http://192.168.1.50:11434/v1"
    assert picked["model"] == "qwen2.5:14b"
    # Disabled in config -> do not offload
    assert cluster_mod.pick_llm_node({"cluster_enabled": True, "cluster_offload_on_hot": False}, {"status": "critical"}) is None


def test_toolkit_cluster_run():
    tk = tools_mod.Toolkit({"workspace": os.environ["LH_DATA"]})
    st = tk.cluster_run(action="status")
    assert st["ok"] is True
    assert "workers" in st

    bench = tk.cluster_run(action="bench", chunks=3)
    assert bench["ok"] is True
    assert bench["chunks"] == 3


def test_server_cluster_endpoints_and_worker_page(monkeypatch):
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
                r = requests.get(base + "/api/cluster/status", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        # 1. Mobile worker page served at /worker
        page = requests.get(base + "/worker", timeout=5)
        assert page.status_code == 200
        assert "Wi-Fi Воркер" in page.text

        # 2. Register worker
        reg = requests.post(base + "/api/cluster/register",
                            json={"name": "TestPhone", "device_type": "mobile", "cores": 4, "max_threads": 1},
                            timeout=5).json()
        assert reg["ok"] is True
        wid = reg["worker"]["id"]

        # 3. Cluster status lists the worker
        st = requests.get(base + "/api/cluster/status", timeout=5).json()
        assert st["workers_online"] == 1
        assert st["workers"][0]["name"] == "TestPhone"

        # 4. Unregister worker
        unreg = requests.post(base + "/api/cluster/unregister", json={"id": wid}, timeout=5).json()
        assert unreg["ok"] is True
        assert unreg["removed"] is True

        # 5. Benchmark endpoint works (local fallback)
        bench = requests.post(base + "/api/cluster/benchmark", json={"chunks": 3, "iterations": 2000}, timeout=10).json()
        assert bench["ok"] is True
        assert len(bench["results"]) == 3
    finally:
        fn = server_mod._RESTARTER["fn"]
        if fn is not None:
            fn()
        thread.join(timeout=5)
