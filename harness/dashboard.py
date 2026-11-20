"""Real-time system dashboard: periodic metric snapshots in a ring buffer."""
import collections
import threading
import time

import psutil

from . import thermal as thermal_mod

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None

# ring buffer: up to 300 data points (10 min at 2 s intervals)
_MAX_POINTS = 300
_INTERVAL = 2.0
_HISTORY = collections.deque(maxlen=_MAX_POINTS)

# cumulative counters
_TOKENS_TOTAL = 0
_SESSIONS_TOTAL = 0
_MODEL_LOADS = 0
_ACTIVE_SESSIONS = 0
_PEAK_GPU_TEMP = 0.0
_GPU_TEMP_SUM = 0.0
_GPU_TEMP_COUNT = 0
_STARTED_AT = 0.0


def _collect_once():
    """Take a single snapshot and append it to the ring buffer."""
    global _PEAK_GPU_TEMP, _GPU_TEMP_SUM, _GPU_TEMP_COUNT

    gpu = {"name": "", "temp_c": None, "utilization_pct": None, "vram_used_mb": 0, "vram_total_mb": 0}
    try:
        raw = thermal_mod.read_gpu_sensors()
        gpu["name"] = raw.get("gpu_name") or ""
        gpu["temp_c"] = raw.get("temp_c")
        gpu["utilization_pct"] = raw.get("utilization_pct")
        gpu["vram_used_mb"] = raw.get("vram_used_mb", 0)
        gpu["vram_total_mb"] = raw.get("vram_total_mb", 0)
    except Exception:
        pass

    ram_pct = 0.0
    ram_free_gb = 0.0
    cpu_pct = 0.0
    cpu_cores = 0
    try:
        vm = psutil.virtual_memory()
        ram_pct = round(vm.percent, 1)
        ram_free_gb = round(vm.available / (1024 ** 3), 2)
        cpu_pct = psutil.cpu_percent(interval=0)
        cpu_cores = psutil.cpu_count(logical=True) or 0
    except Exception:
        pass

    with _LOCK:
        snap = {
            "ts": time.time(),
            "gpu_name": gpu["name"],
            "gpu_temp": gpu["temp_c"],
            "gpu_util": gpu["utilization_pct"],
            "vram_used_mb": gpu["vram_used_mb"],
            "vram_total_mb": gpu["vram_total_mb"],
            "ram_used_pct": ram_pct,
            "ram_free_gb": ram_free_gb,
            "cpu_pct": cpu_pct,
            "cpu_cores": cpu_cores,
            "tokens_total": _TOKENS_TOTAL,
            "active_sessions": _ACTIVE_SESSIONS,
        }
        _HISTORY.append(snap)

        if gpu["temp_c"] is not None:
            temp = gpu["temp_c"]
            if temp > _PEAK_GPU_TEMP:
                _PEAK_GPU_TEMP = temp
            _GPU_TEMP_SUM += temp
            _GPU_TEMP_COUNT += 1


def _loop():
    """Background collection loop — never crashes."""
    while not _STOP.is_set():
        try:
            _collect_once()
        except Exception:
            pass
        _STOP.wait(_INTERVAL)


def start():
    """Start the background collection thread (idempotent)."""
    global _THREAD, _STARTED_AT
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _STARTED_AT = time.time()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="dashboard-collect")
        _THREAD.start()


def stop():
    """Stop the background collection thread."""
    global _THREAD
    _STOP.set()
    with _LOCK:
        t = _THREAD
    if t is not None:
        t.join(timeout=5)
    with _LOCK:
        _THREAD = None


def get_history(minutes=10):
    """Return snapshots within the given time window."""
    cutoff = time.time() - minutes * 60
    with _LOCK:
        return [s for s in _HISTORY if s["ts"] >= cutoff]


def get_latest():
    """Return the most recent snapshot, collecting once if buffer is empty."""
    with _LOCK:
        if _HISTORY:
            return dict(_HISTORY[-1])
    _collect_once()
    with _LOCK:
        if _HISTORY:
            return dict(_HISTORY[-1])
        return {}


def get_summary():
    """Aggregate stats for the dashboard header."""
    with _LOCK:
        uptime = time.time() - _STARTED_AT if _STARTED_AT else 0
        avg_gpu = round(_GPU_TEMP_SUM / _GPU_TEMP_COUNT, 1) if _GPU_TEMP_COUNT else None
        peak_gpu = _PEAK_GPU_TEMP if _GPU_TEMP_COUNT else None
        return {
            "uptime": round(uptime, 1),
            "total_tokens": _TOKENS_TOTAL,
            "total_sessions": _SESSIONS_TOTAL,
            "peak_gpu_temp": peak_gpu,
            "avg_gpu_temp": avg_gpu,
            "model_loads": _MODEL_LOADS,
        }


def record_tokens(n):
    """Add *n* to the cumulative token counter."""
    global _TOKENS_TOTAL
    with _LOCK:
        _TOKENS_TOTAL += int(n)


def record_session():
    """Increment the session counter and active-sessions gauge."""
    global _SESSIONS_TOTAL, _ACTIVE_SESSIONS
    with _LOCK:
        _SESSIONS_TOTAL += 1
        _ACTIVE_SESSIONS += 1


def record_session_end():
    """Decrement the active-sessions gauge."""
    global _ACTIVE_SESSIONS
    with _LOCK:
        _ACTIVE_SESSIONS = max(0, _ACTIVE_SESSIONS - 1)


def record_model_load():
    """Increment the model load counter."""
    global _MODEL_LOADS
    with _LOCK:
        _MODEL_LOADS += 1


def _reset():
    """Reset all state (for tests)."""
    global _TOKENS_TOTAL, _SESSIONS_TOTAL, _MODEL_LOADS, _ACTIVE_SESSIONS
    global _PEAK_GPU_TEMP, _GPU_TEMP_SUM, _GPU_TEMP_COUNT, _STARTED_AT, _THREAD
    stop()
    with _LOCK:
        _HISTORY.clear()
        _TOKENS_TOTAL = 0
        _SESSIONS_TOTAL = 0
        _MODEL_LOADS = 0
        _ACTIVE_SESSIONS = 0
        _PEAK_GPU_TEMP = 0.0
        _GPU_TEMP_SUM = 0.0
        _GPU_TEMP_COUNT = 0
        _STARTED_AT = 0.0
        _THREAD = None
