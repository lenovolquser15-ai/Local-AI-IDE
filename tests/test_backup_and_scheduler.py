"""Tests for Profile .zip Backup (Export/Import) and Task Scheduler / Automations."""
import base64
import io
import os
import socket
import sys
import threading
import time
import zipfile

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "test-backup-sched-config.json")

from harness import backup as backup_mod  # noqa: E402
from harness import config as config_mod  # noqa: E402
from harness import scheduler as scheduler_mod  # noqa: E402
from harness import server as server_mod  # noqa: E402


@pytest.fixture(autouse=True)
def clean_scheduler():
    scheduler_mod.stop()
    p = scheduler_mod._store_path()
    if os.path.isfile(p):
        os.remove(p)
    yield
    scheduler_mod.stop()
    if os.path.isfile(p):
        os.remove(p)


def test_backup_export_inspect_and_import():
    cfg = config_mod.load()
    cfg["system_prompt"] = "backup-test-prompt-marker"
    config_mod.save(cfg)

    raw = backup_mod.export_profile(include_sessions=True, include_plugins=True, include_memory=True)
    assert isinstance(raw, bytes) and len(raw) > 50

    info = backup_mod.inspect_backup(raw)
    assert info["ok"] is True
    assert info["has_config"] is True
    assert info["manifest"]["app"] == "LocalHarness"

    # Change config and restore from backup
    cfg["system_prompt"] = "changed-temp"
    config_mod.save(cfg)
    res = backup_mod.import_profile(raw, restore_config=True, restore_plugins=False, restore_sessions=False)
    assert res["ok"] is True
    assert res["restored"]["config"] is True
    assert config_mod.load()["system_prompt"] == "backup-test-prompt-marker"


def test_backup_rejects_zip_slip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "boom")
    res = backup_mod.inspect_backup(buf.getvalue())
    assert res["ok"] is False
    assert "небезопасный" in res["error"]


def test_scheduler_add_toggle_run_delete():
    added = scheduler_mod.add_task(
        name="Echo test",
        kind="shell",
        payload="echo hello-scheduler",
        schedule_type="interval",
        interval_min=30,
    )
    assert added["ok"] is True
    tid = added["task"]["id"]

    listing = scheduler_mod.list_tasks()
    assert len(listing["tasks"]) == 1

    tog = scheduler_mod.toggle_task(tid, False)
    assert tog["ok"] is True
    assert tog["task"]["enabled"] is False

    ran = scheduler_mod.run_now(tid, sync=True)
    assert ran["ok"] is True
    assert "hello-scheduler" in ran["entry"]["output"]

    listing2 = scheduler_mod.list_tasks()
    assert len(listing2["history"]) == 1
    assert listing2["tasks"][0]["runs_count"] == 1

    deleted = scheduler_mod.delete_task(tid)
    assert deleted["ok"] is True
    assert len(scheduler_mod.list_tasks()["tasks"]) == 0


def test_scheduler_daily_next_run():
    now = time.time()
    nxt = scheduler_mod._compute_next_run({"schedule_type": "daily", "daily_time": "09:30"}, now=now)
    assert nxt > now
    assert nxt <= now + 86400 + 5


def test_server_backup_and_scheduler_endpoints(monkeypatch):
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
                r = requests.get(base + "/api/scheduler/list", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        # 1. Export zip backup
        exp = requests.get(base + "/api/backup/export?sessions=0&plugins=0&memory=0", timeout=10)
        assert exp.status_code == 200
        assert "application/zip" in exp.headers.get("Content-Type", "")
        assert len(exp.content) > 50

        # 2. Import zip backup
        b64 = base64.b64encode(exp.content).decode("ascii")
        imp = requests.post(base + "/api/backup/import", json={"data_b64": b64, "restore_plugins": False}, timeout=10).json()
        assert imp["ok"] is True
        assert imp["restored"]["config"] is True

        # 3. Scheduler CRUD + run
        add = requests.post(base + "/api/scheduler/add", json={
            "name": "HTTP Shell Check",
            "kind": "shell",
            "payload": "echo ok-http",
            "schedule_type": "interval",
            "interval_min": 15,
        }, timeout=5).json()
        assert add["ok"] is True
        tid = add["task"]["id"]

        run_res = requests.post(base + "/api/scheduler/run", json={"id": tid, "sync": True}, timeout=10).json()
        assert run_res["ok"] is True
        assert "ok-http" in run_res["entry"]["output"]

        lst = requests.get(base + "/api/scheduler/list", timeout=5).json()
        assert len(lst["tasks"]) == 1
        assert len(lst["history"]) == 1

        rm = requests.post(base + "/api/scheduler/delete", json={"id": tid}, timeout=5).json()
        assert rm["ok"] is True
    finally:
        fn = server_mod._RESTARTER["fn"]
        if fn is not None:
            fn()
        thread.join(timeout=5)
