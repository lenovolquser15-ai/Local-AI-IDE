"""Real-time System Dashboard 2.0: 60-minute 1800-point telemetry ring buffer,
GPU power/temp/VRAM, CPU/RAM, Disk & Network KB/s throughput, TPS & tool counters,
top processes inspector, and CSV export."""
import collections
import csv
import io
import os
import threading
import time

import psutil

from . import config as config_mod
from . import thermal as thermal_mod

# Prime psutil CPU counter once so the very first snapshot doesn't return 0.0%
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None

# 60 minutes at 2s intervals = 1800 points (fixes truncation on 15/30/60m windows!)
_MAX_POINTS = 1800
_INTERVAL = 2.0
_HISTORY = collections.deque(maxlen=_MAX_POINTS)

# Cumulative counters
_TOKENS_TOTAL = 0
_SESSIONS_TOTAL = 0
_MODEL_LOADS = 0
_ACTIVE_SESSIONS = 0
_PEAK_GPU_TEMP = 0.0
_GPU_TEMP_SUM = 0.0
_GPU_TEMP_COUNT = 0
_LAST_TPS = None
_PEAK_TPS = 0.0
_TPS_SUM = 0.0
_TPS_COUNT = 0
_TOOL_CALLS = 0
_TOOL_ERRORS = 0
_STARTED_AT = 0.0

_LAST_NET = {"ts": 0.0, "rx": 0, "tx": 0}


def _collect_once():
    """Take a single telemetry snapshot and append it to the 60-minute ring buffer."""
    global _PEAK_GPU_TEMP, _GPU_TEMP_SUM, _GPU_TEMP_COUNT

    gpu = {
        "name": "",
        "temp_c": None,
        "utilization_pct": None,
        "vram_used_mb": 0,
        "vram_total_mb": 0,
        "power_w": None,
        "fan_speed": None,
    }
    try:
        raw = thermal_mod.read_gpu_sensors()
        gpu["name"] = raw.get("gpu_name") or ""
        gpu["temp_c"] = raw.get("temp_c")
        gpu["utilization_pct"] = raw.get("utilization_pct")
        gpu["vram_used_mb"] = raw.get("vram_used_mb", 0)
        gpu["vram_total_mb"] = raw.get("vram_total_mb", 0)
        gpu["power_w"] = raw.get("power_w")
        gpu["fan_speed"] = raw.get("fan_speed")
    except Exception:
        pass

    ram_pct = 0.0
    ram_free_gb = 0.0
    ram_total_gb = 0.0
    cpu_pct = 0.0
    cpu_cores = 0
    disk_pct = 0.0
    disk_free_gb = 0.0
    net_rx_kbps = 0.0
    net_tx_kbps = 0.0
    now = time.time()

    try:
        vm = psutil.virtual_memory()
        ram_pct = round(vm.percent, 1)
        ram_free_gb = round(vm.available / (1024 ** 3), 2)
        ram_total_gb = round(vm.total / (1024 ** 3), 1)
        cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        cpu_cores = psutil.cpu_count(logical=True) or 0
    except Exception:
        pass

    try:
        du = psutil.disk_usage(config_mod.ROOT)
        disk_pct = round(du.percent, 1)
        disk_free_gb = round(du.free / (1024 ** 3), 1)
    except Exception:
        pass

    try:
        nio = psutil.net_io_counters()
        if nio:
            if _LAST_NET["ts"] > 0:
                dt = max(0.2, now - _LAST_NET["ts"])
                net_rx_kbps = round(max(0, nio.bytes_recv - _LAST_NET["rx"]) / 1024.0 / dt, 1)
                net_tx_kbps = round(max(0, nio.bytes_sent - _LAST_NET["tx"]) / 1024.0 / dt, 1)
            _LAST_NET["ts"] = now
            _LAST_NET["rx"] = nio.bytes_recv
            _LAST_NET["tx"] = nio.bytes_sent
    except Exception:
        pass

    with _LOCK:
        snap = {
            "ts": now,
            "gpu_name": gpu["name"],
            "gpu_temp": gpu["temp_c"],
            "gpu_util": gpu["utilization_pct"],
            "gpu_power_w": gpu["power_w"],
            "gpu_fan": gpu["fan_speed"],
            "vram_used_mb": gpu["vram_used_mb"],
            "vram_total_mb": gpu["vram_total_mb"],
            "ram_used_pct": ram_pct,
            "ram_free_gb": ram_free_gb,
            "ram_total_gb": ram_total_gb,
            "cpu_pct": cpu_pct,
            "cpu_cores": cpu_cores,
            "disk_used_pct": disk_pct,
            "disk_free_gb": disk_free_gb,
            "net_rx_kbps": net_rx_kbps,
            "net_tx_kbps": net_tx_kbps,
            "tps": _LAST_TPS,
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
    while not _STOP.is_set():
        try:
            _collect_once()
        except Exception:
            pass
        _STOP.wait(_INTERVAL)


def start():
    global _THREAD, _STARTED_AT
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        if not _STARTED_AT:
            _STARTED_AT = time.time()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="dashboard-collect")
        _THREAD.start()


def stop():
    global _THREAD
    _STOP.set()
    with _LOCK:
        t = _THREAD
    if t is not None:
        t.join(timeout=5)
    with _LOCK:
        _THREAD = None


def get_history(minutes=10):
    with _LOCK:
        empty = not _HISTORY
    if empty:
        _collect_once()
    cutoff = time.time() - float(minutes or 10) * 60.0
    with _LOCK:
        return [dict(s) for s in _HISTORY if s["ts"] >= cutoff]


def get_latest():
    with _LOCK:
        if _HISTORY:
            return dict(_HISTORY[-1])
    _collect_once()
    with _LOCK:
        if _HISTORY:
            return dict(_HISTORY[-1])
        return {}


def get_summary():
    with _LOCK:
        uptime = time.time() - _STARTED_AT if _STARTED_AT else 0
        avg_gpu = round(_GPU_TEMP_SUM / _GPU_TEMP_COUNT, 1) if _GPU_TEMP_COUNT else None
        peak_gpu = _PEAK_GPU_TEMP if _GPU_TEMP_COUNT else None
        avg_tps = round(_TPS_SUM / _TPS_COUNT, 1) if _TPS_COUNT else None
        peak_tps = round(_PEAK_TPS, 1) if _TPS_COUNT else None
        return {
            "uptime": round(uptime, 1),
            "total_tokens": _TOKENS_TOTAL,
            "total_sessions": _SESSIONS_TOTAL,
            "active_sessions": _ACTIVE_SESSIONS,
            "peak_gpu_temp": peak_gpu,
            "avg_gpu_temp": avg_gpu,
            "model_loads": _MODEL_LOADS,
            "last_tps": _LAST_TPS,
            "avg_tps": avg_tps,
            "peak_tps": peak_tps,
            "tool_calls": _TOOL_CALLS,
            "tool_errors": _TOOL_ERRORS,
        }


def get_top_processes(limit=8):
    """Return top processes by RAM/CPU for the live Dashboard process inspector."""
    limit = max(1, min(25, int(limit or 8)))
    rows = []
    try:
        for p in psutil.process_iter(["pid", "name", "memory_info", "memory_percent", "cpu_percent"]):
            try:
                info = p.info
                mem_mb = round(((info.get("memory_info") and info["memory_info"].rss) or 0) / (1024 * 1024), 1)
                rows.append({
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "ram_mb": mem_mb,
                    "rss_mb": mem_mb,
                    "mem_pct": round(float(info.get("memory_percent") or 0.0), 1),
                    "cpu_pct": round(float(info.get("cpu_percent") or 0.0), 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        rows.sort(key=lambda r: (r["ram_mb"], r["cpu_pct"]), reverse=True)
    except Exception:
        pass
    return {"ok": True, "processes": rows[:limit]}


def export_csv(minutes=60):
    """Export recent telemetry history as CSV string."""
    rows = get_history(minutes=minutes)
    buf = io.StringIO()
    fields = [
        "ts", "gpu_temp", "gpu_util", "gpu_power_w", "vram_used_mb", "vram_total_mb",
        "cpu_pct", "ram_used_pct", "ram_free_gb", "disk_used_pct", "net_rx_kbps", "net_tx_kbps",
        "tps", "tokens_total", "active_sessions",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def record_tokens(n, tps=None):
    global _TOKENS_TOTAL, _LAST_TPS, _PEAK_TPS, _TPS_SUM, _TPS_COUNT
    with _LOCK:
        _TOKENS_TOTAL += max(0, int(n or 0))
        if tps is not None:
            try:
                val = float(tps)
                if val > 0:
                    _LAST_TPS = round(val, 1)
                    if val > _PEAK_TPS:
                        _PEAK_TPS = val
                    _TPS_SUM += val
                    _TPS_COUNT += 1
            except (TypeError, ValueError):
                pass


def record_tool_call(tool_name=None, ok=True):
    global _TOOL_CALLS, _TOOL_ERRORS
    if isinstance(tool_name, bool):
        ok = tool_name
    with _LOCK:
        _TOOL_CALLS += 1
        if not ok:
            _TOOL_ERRORS += 1


def record_session():
    global _SESSIONS_TOTAL, _ACTIVE_SESSIONS
    with _LOCK:
        _SESSIONS_TOTAL += 1
        _ACTIVE_SESSIONS += 1


def record_session_end():
    global _ACTIVE_SESSIONS
    with _LOCK:
        _ACTIVE_SESSIONS = max(0, _ACTIVE_SESSIONS - 1)


def record_model_load():
    global _MODEL_LOADS
    with _LOCK:
        _MODEL_LOADS += 1


def reset_counters():
    """Reset cumulative counters without stopping the background collector."""
    global _TOKENS_TOTAL, _SESSIONS_TOTAL, _MODEL_LOADS
    global _PEAK_GPU_TEMP, _GPU_TEMP_SUM, _GPU_TEMP_COUNT
    global _LAST_TPS, _PEAK_TPS, _TPS_SUM, _TPS_COUNT, _TOOL_CALLS, _TOOL_ERRORS
    with _LOCK:
        _HISTORY.clear()
        _TOKENS_TOTAL = 0
        _SESSIONS_TOTAL = 0
        _MODEL_LOADS = 0
        _PEAK_GPU_TEMP = 0.0
        _GPU_TEMP_SUM = 0.0
        _GPU_TEMP_COUNT = 0
        _LAST_TPS = None
        _PEAK_TPS = 0.0
        _TPS_SUM = 0.0
        _TPS_COUNT = 0
        _TOOL_CALLS = 0
        _TOOL_ERRORS = 0
    return {"ok": True}


def _reset():
    global _ACTIVE_SESSIONS, _STARTED_AT, _THREAD
    stop()
    reset_counters()
    with _LOCK:
        _ACTIVE_SESSIONS = 0
        _STARTED_AT = 0.0
        _THREAD = None
        _LAST_NET.update({"ts": 0.0, "rx": 0, "tx": 0})
