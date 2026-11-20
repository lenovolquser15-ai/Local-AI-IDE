"""Background task scheduler & automations (interval / daily schedules for agent prompts or commands)."""
import datetime
import json
import os
import subprocess
import threading
import time
import uuid

from . import config as config_mod
from . import session as session_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD = None
_RUNNING_IDS = set()
_HISTORY_MAX = 50


def _store_path():
    return os.path.join(config_mod.DATA_DIR, "schedules.json")


def _load_store():
    path = _store_path()
    if not os.path.isfile(path):
        return {"tasks": [], "history": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("tasks", [])
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"tasks": [], "history": []}


def _save_store(data):
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _compute_next_run(task, now=None):
    now = now or time.time()
    stype = task.get("schedule_type", "interval")
    if stype == "daily":
        raw_t = str(task.get("daily_time") or "09:00").strip()
        try:
            hh, mm = [int(x) for x in raw_t.split(":", 1)]
            hh = max(0, min(23, hh))
            mm = max(0, min(59, mm))
        except Exception:
            hh, mm = 9, 0
        dt = datetime.datetime.fromtimestamp(now)
        target = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target.timestamp() <= now + 1:
            target += datetime.timedelta(days=1)
        return target.timestamp()

    mins = max(1, min(10080, int(task.get("interval_min") or 60)))
    return now + mins * 60


def list_tasks():
    with _LOCK:
        store = _load_store()
        tasks = []
        for t in store.get("tasks", []):
            item = dict(t)
            item["running"] = item.get("id") in _RUNNING_IDS
            tasks.append(item)
        return {
            "ok": True,
            "tasks": tasks,
            "history": list(store.get("history", []))[:30],
            "scheduler_alive": bool(_THREAD and _THREAD.is_alive()),
        }


def add_task(name, payload, kind="prompt", schedule_type="interval",
             interval_min=60, daily_time="09:00", enabled=True):
    name = str(name or "").strip() or "Автозадача"
    payload = str(payload or "").strip()
    if not payload:
        return {"ok": False, "error": "укажите текст промпта или команду"}
    kind = "shell" if str(kind).lower() == "shell" else "prompt"
    schedule_type = "daily" if str(schedule_type).lower() == "daily" else "interval"
    now = time.time()
    task = {
        "id": uuid.uuid4().hex[:10],
        "name": name[:80],
        "kind": kind,
        "payload": payload[:4000],
        "schedule_type": schedule_type,
        "interval_min": max(1, min(10080, int(interval_min or 60))),
        "daily_time": str(daily_time or "09:00")[:5],
        "enabled": bool(enabled),
        "created_at": now,
        "last_run_at": None,
        "last_status": None,
        "next_run_at": _compute_next_run(
            {"schedule_type": schedule_type, "interval_min": interval_min, "daily_time": daily_time}, now
        ),
        "runs_count": 0,
    }
    with _LOCK:
        store = _load_store()
        store["tasks"].append(task)
        _save_store(store)
    return {"ok": True, "task": task}


def toggle_task(task_id, enabled):
    with _LOCK:
        store = _load_store()
        for t in store["tasks"]:
            if t.get("id") == task_id:
                t["enabled"] = bool(enabled)
                if t["enabled"]:
                    t["next_run_at"] = _compute_next_run(t)
                _save_store(store)
                return {"ok": True, "task": dict(t)}
    return {"ok": False, "error": "задача не найдена"}


def delete_task(task_id):
    with _LOCK:
        store = _load_store()
        before = len(store["tasks"])
        store["tasks"] = [t for t in store["tasks"] if t.get("id") != task_id]
        if len(store["tasks"]) == before:
            return {"ok": False, "error": "задача не найдена"}
        _save_store(store)
        return {"ok": True}


def _execute_task_sync(task):
    """Execute a single scheduled task (prompt via Agent or shell command)."""
    t0 = time.time()
    ok = False
    output = ""
    sid = None
    try:
        if task.get("kind") == "shell":
            proc = subprocess.run(
                task["payload"],
                shell=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
                creationflags=NO_WINDOW,
            )
            out = (proc.stdout or b"").decode("utf-8", "replace").strip()
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            ok = proc.returncode == 0
            output = (out or err or ("exit code %d" % proc.returncode))[:2000]
        else:
            from .agent import Agent, Bridge
            cfg = config_mod.load()
            sid = session_mod.create("⏰ " + task.get("name", "Автозадача"))
            bridge = Bridge()
            agent = Agent(cfg, bridge, sid)
            res = agent.run(task["payload"], history=[])
            ok = True
            output = str((res or {}).get("answer") or "выполнено")[:2000]
    except Exception as exc:
        ok = False
        output = "Ошибка: %s" % exc

    elapsed = round(time.time() - t0, 2)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "task_id": task.get("id"),
        "task_name": task.get("name"),
        "kind": task.get("kind"),
        "ok": ok,
        "output": output,
        "session_id": sid,
        "ran_at": time.time(),
        "elapsed_s": elapsed,
    }
    with _LOCK:
        _RUNNING_IDS.discard(task.get("id"))
        store = _load_store()
        for t in store["tasks"]:
            if t.get("id") == task.get("id"):
                t["last_run_at"] = entry["ran_at"]
                t["last_status"] = "ok" if ok else "error"
                t["runs_count"] = int(t.get("runs_count") or 0) + 1
                t["next_run_at"] = _compute_next_run(t)
                break
        store["history"].insert(0, entry)
        store["history"] = store["history"][:_HISTORY_MAX]
        _save_store(store)
    return entry


def run_now(task_id, sync=False):
    with _LOCK:
        store = _load_store()
        task = next((dict(t) for t in store["tasks"] if t.get("id") == task_id), None)
        if not task:
            return {"ok": False, "error": "задача не найдена"}
        if task_id in _RUNNING_IDS:
            return {"ok": False, "error": "задача уже выполняется"}
        _RUNNING_IDS.add(task_id)

    if sync:
        entry = _execute_task_sync(task)
        return {"ok": entry["ok"], "entry": entry}

    threading.Thread(target=_execute_task_sync, args=(task,), daemon=True, name="sched-run-" + task_id).start()
    return {"ok": True, "started": True, "task_id": task_id}


def _tick_once():
    now = time.time()
    due = []
    with _LOCK:
        store = _load_store()
        for t in store.get("tasks", []):
            if not t.get("enabled"):
                continue
            tid = t.get("id")
            if tid in _RUNNING_IDS:
                continue
            nxt = float(t.get("next_run_at") or 0)
            if nxt <= now:
                _RUNNING_IDS.add(tid)
                due.append(dict(t))
    for task in due:
        threading.Thread(target=_execute_task_sync, args=(task,), daemon=True).start()


def _loop():
    while not _STOP.is_set():
        try:
            _tick_once()
        except Exception:
            pass
        _STOP.wait(15.0)


def start():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="lh-scheduler")
        _THREAD.start()


def stop():
    global _THREAD
    _STOP.set()
    with _LOCK:
        t = _THREAD
    if t is not None:
        t.join(timeout=3)
    with _LOCK:
        _THREAD = None
