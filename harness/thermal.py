"""GPU thermal monitoring, token pacing (throttling), and emergency cooldown/unload."""
import json
import os
import shutil
import subprocess
import threading
import time

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_THERMAL_CACHE = {
    "at": 0.0,
    "value": None,
    "lock": threading.Lock(),
    "simulated_temp": None,
}
_THERMAL_TTL = 1.0  # Cache duration for sensor queries

_COOLDOWN_STATE = {
    "active": False,
    "started_at": 0.0,
    "duration": 180,
    "target_temp": 70,
    "initial_temp": None,
    "completed": False,
}
_COOLDOWN_LOCK = threading.Lock()


def set_simulated_temp(temp_c):
    """Sets a simulated GPU temperature for testing/demonstration, or None to reset."""
    with _THERMAL_CACHE["lock"]:
        _THERMAL_CACHE["simulated_temp"] = int(temp_c) if temp_c is not None else None
        _THERMAL_CACHE["at"] = 0.0  # Invalidate cache
        _THERMAL_CACHE["value"] = None


def get_simulated_temp():
    with _THERMAL_CACHE["lock"]:
        return _THERMAL_CACHE["simulated_temp"]


def read_gpu_sensors(force=False):
    """Queries hardware sensors (nvidia-smi or fallbacks) with thread-safe caching."""
    now = time.monotonic()
    with _THERMAL_CACHE["lock"]:
        if not force and _THERMAL_CACHE["value"] and (now - _THERMAL_CACHE["at"]) < _THERMAL_TTL:
            res = dict(_THERMAL_CACHE["value"])
            sim = _THERMAL_CACHE["simulated_temp"]
            if sim is not None:
                res["temp_c"] = sim
                res["simulated"] = True
            return res

    data = {
        "has_gpu": False,
        "gpu_name": "",
        "temp_c": None,
        "utilization_pct": None,
        "vram_used_mb": 0.0,
        "vram_total_mb": 0.0,
        "fan_speed": None,
        "power_w": None,
        "simulated": False,
    }

    # 1. Try NVIDIA-SMI (supports multi-GPU: picks the primary discrete GPU with highest VRAM/load)
    smi = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if smi:
        try:
            cmd = [
                smi,
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,fan.speed,power.draw",
                "--format=csv,noheader,nounits"
            ]
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=2.5, creationflags=NO_WINDOW)
            if proc.returncode == 0 and proc.stdout:
                lines = [ln.strip() for ln in proc.stdout.decode("utf-8", "replace").strip().splitlines() if ln.strip()]
                best = None
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 2:
                        continue
                    row = {
                        "has_gpu": True,
                        "gpu_name": parts[0],
                        "temp_c": None,
                        "utilization_pct": None,
                        "vram_used_mb": 0.0,
                        "vram_total_mb": 0.0,
                        "fan_speed": None,
                        "power_w": None,
                        "simulated": False,
                    }
                    try:
                        row["temp_c"] = int(float(parts[1]))
                    except (ValueError, TypeError):
                        pass
                    if len(parts) >= 3:
                        try:
                            row["utilization_pct"] = int(float(parts[2]))
                        except (ValueError, TypeError):
                            pass
                    if len(parts) >= 5:
                        try:
                            row["vram_used_mb"] = round(float(parts[3]), 1)
                            row["vram_total_mb"] = round(float(parts[4]), 1)
                        except (ValueError, TypeError):
                            pass
                    if len(parts) >= 6:
                        fan_raw = parts[5].strip()
                        if fan_raw and not fan_raw.startswith("[") and "N/A" not in fan_raw:
                            try:
                                row["fan_speed"] = int(float(fan_raw))
                            except (ValueError, TypeError):
                                row["fan_speed"] = fan_raw
                    if len(parts) >= 7:
                        pwr_raw = parts[6].strip()
                        if pwr_raw and not pwr_raw.startswith("[") and "N/A" not in pwr_raw:
                            try:
                                row["power_w"] = round(float(pwr_raw), 1)
                            except (ValueError, TypeError):
                                pass
                    if best is None or (row["vram_total_mb"], row["utilization_pct"] or 0, row["temp_c"] or 0) > (
                        best["vram_total_mb"], best["utilization_pct"] or 0, best["temp_c"] or 0
                    ):
                        best = row
                if best is not None:
                    data = best
        except Exception:
            pass

    # 2. AMD rocm-smi fallback if NVIDIA not present
    if not data["has_gpu"]:
        rocm = shutil.which("rocm-smi")
        if rocm:
            try:
                cmd = [rocm, "--showtemp", "--json"]
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=2.5, creationflags=NO_WINDOW)
                if proc.returncode == 0 and proc.stdout:
                    parsed = json.loads(proc.stdout.decode("utf-8", "replace"))
                    for card, vals in parsed.items():
                        if isinstance(vals, dict):
                            for k, v in vals.items():
                                if "Temperature" in k:
                                    try:
                                        data["temp_c"] = int(float(v))
                                        data["has_gpu"] = True
                                        data["gpu_name"] = "AMD Radeon GPU"
                                        break
                                    except (ValueError, TypeError):
                                        pass
            except Exception:
                pass

    with _THERMAL_CACHE["lock"]:
        _THERMAL_CACHE["value"] = dict(data)
        _THERMAL_CACHE["at"] = now
        sim = _THERMAL_CACHE["simulated_temp"]
        if sim is not None:
            data["temp_c"] = sim
            data["simulated"] = True
            if not data["has_gpu"]:
                data["has_gpu"] = True
                data["gpu_name"] = "Simulated GPU"

    return data


def get_cached_thermal(cfg=None):
    """Returns calculated thermal status (using cached sensors)."""
    return get_thermal_status(cfg=cfg, force=False)


def get_thermal_status(cfg=None, force=False):
    """Calculates full thermal status, status label, throttling state, and cooldown info."""
    cfg = cfg or {}
    sensor = read_gpu_sensors(force=force)
    temp_c = sensor.get("temp_c")

    enabled = cfg.get("gpu_thermal_enabled", True)
    warn_th = int(cfg.get("gpu_temp_warn", 80))
    throttle_th = int(cfg.get("gpu_temp_throttle", 85))
    critical_th = int(cfg.get("gpu_temp_critical", 95))
    target_th = int(cfg.get("gpu_target_temp", 70))
    duration_cfg = int(cfg.get("gpu_cooldown_duration", 180))

    status = "normal"
    status_label = "Норма"
    throttle_active = False
    throttle_delay = 0.0
    cooldown_recommended = False
    message = "Температура видеокарты в норме."

    if not sensor.get("has_gpu") or temp_c is None:
        status = "no_sensor"
        status_label = "Датчик не обнаружен"
        message = "Датчик температуры видеокарты не обнаружен или GPU не установлена."
    elif temp_c < warn_th:
        status = "normal"
        status_label = "Норма (%d°C)" % temp_c
        message = "Температура видеокарты в норме (%d°C)." % temp_c
    elif warn_th <= temp_c < throttle_th:
        status = "warm"
        status_label = "Повышенная (%d°C)" % temp_c
        message = "Повышенная температура GPU (%d°C). Нагрузка возрастает." % temp_c
    elif throttle_th <= temp_c < critical_th:
        status = "hot"
        status_label = "Троттлинг (%d°C)" % temp_c
        if enabled:
            throttle_active = True
            excess = temp_c - throttle_th
            # Delay scales from ~0.035s up to ~0.15s
            throttle_delay = round(min(0.20, 0.035 + (excess * 0.012)), 3)
            message = "Высокая температура GPU (%d°C). Скорость токенов снижена для охлаждения чипа." % temp_c
        else:
            message = "Высокая температура GPU (%d°C). Термо-троттлинг отключён в настройках." % temp_c
    else:  # temp_c >= critical_th (e.g. 95°C / 100°C+)
        status = "critical"
        status_label = "Критический перегрев (%d°C)" % temp_c
        cooldown_recommended = True
        if enabled:
            throttle_active = True
            excess = max(0, temp_c - critical_th)
            throttle_delay = round(min(0.35, 0.18 + (excess * 0.015)), 3)
        message = "Экстремальная температура GPU (%d°C)! Опасность перегрева. Рекомендуется выгрузить модель." % temp_c

    cooldown_info = get_cooldown_status(cfg)

    return {
        "has_gpu": sensor.get("has_gpu", False),
        "gpu_name": sensor.get("gpu_name", ""),
        "temp_c": temp_c,
        "utilization_pct": sensor.get("utilization_pct"),
        "vram_used_mb": sensor.get("vram_used_mb"),
        "vram_total_mb": sensor.get("vram_total_mb"),
        "fan_speed": sensor.get("fan_speed"),
        "power_w": sensor.get("power_w"),
        "simulated": sensor.get("simulated", False),
        "status": status,
        "status_label": status_label,
        "throttle_active": throttle_active,
        "throttle_delay": throttle_delay,
        "cooldown_recommended": cooldown_recommended,
        "message": message,
        "cooldown": cooldown_info,
        "thresholds": {
            "enabled": enabled,
            "warn": warn_th,
            "throttle": throttle_th,
            "critical": critical_th,
            "target": target_th,
            "duration": duration_cfg,
        }
    }


def get_stream_delay(cfg=None):
    """Calculates stream pacing delay in seconds during token generation."""
    try:
        st = get_thermal_status(cfg)
        if st.get("throttle_active"):
            return float(st.get("throttle_delay", 0.0))
    except Exception:
        pass
    return 0.0


def unload_model(cfg=None):
    """Unloads active models from GPU memory (supporting both LM Studio and Ollama)."""
    cfg = cfg or {}
    provider = cfg.get("provider", "lmstudio")
    results = {"ok": True, "provider": provider, "actions": []}

    # 1. LM Studio
    try:
        from . import lms as lms_mod
        lms_mod.invalidate()
        cli = lms_mod.cli_path(cfg)
        if cli and os.path.exists(cli):
            cmd = [cli, "unload", "--all"]
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, creationflags=NO_WINDOW)
            results["actions"].append({
                "target": "lmstudio",
                "code": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace").strip(),
            })
    except Exception as exc:
        results["actions"].append({"target": "lmstudio", "error": str(exc)})

    # 2. Ollama
    try:
        prov = (cfg.get("providers") or {}).get("ollama") or {}
        base_url = prov.get("base_url") or "http://127.0.0.1:11434/v1"
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3].rstrip("/")
        import urllib.request
        req = urllib.request.Request(root + "/api/ps", headers={"User-Agent": "LocalHarness"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in (data.get("models") or []):
                name = m.get("name") or m.get("model")
                if name:
                    un_req = urllib.request.Request(
                        root + "/api/generate",
                        data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"),
                        headers={"Content-Type": "application/json", "User-Agent": "LocalHarness"},
                        method="POST"
                    )
                    with urllib.request.urlopen(un_req, timeout=2.5):
                        results["actions"].append({"target": "ollama", "model": name, "status": "unloaded"})
    except Exception:
        pass

    return results


def start_cooldown(duration=None, target_temp=None, unload=True, cfg=None):
    """Initiates an emergency or manual cooldown cycle."""
    cfg = cfg or {}
    if duration is None:
        duration = int(cfg.get("gpu_cooldown_duration", 180))
    if target_temp is None:
        target_temp = int(cfg.get("gpu_target_temp", 70))

    cur = get_thermal_status(cfg, force=True)
    initial_temp = cur.get("temp_c")

    unload_res = None
    if unload:
        unload_res = unload_model(cfg)

    with _COOLDOWN_LOCK:
        _COOLDOWN_STATE.update({
            "active": True,
            "started_at": time.time(),
            "duration": max(30, int(duration)),
            "target_temp": int(target_temp),
            "initial_temp": initial_temp,
            "completed": False,
        })

    status = get_cooldown_status(cfg)
    status["unload_result"] = unload_res
    return status


def cancel_cooldown():
    """Cancels active cooldown."""
    with _COOLDOWN_LOCK:
        _COOLDOWN_STATE["active"] = False
        _COOLDOWN_STATE["completed"] = False
        return dict(_COOLDOWN_STATE)


def get_cooldown_status(cfg=None):
    """Returns dynamic cooldown timer, remaining seconds, and target temperature."""
    with _COOLDOWN_LOCK:
        if not _COOLDOWN_STATE["active"]:
            return dict(_COOLDOWN_STATE)

        elapsed = time.time() - _COOLDOWN_STATE["started_at"]
        duration = _COOLDOWN_STATE["duration"]
        remaining = max(0, int(duration - elapsed))

        sensor = read_gpu_sensors(force=False)
        cur_temp = sensor.get("temp_c")

        completed = False
        # If timer finished OR temperature dropped below target after at least 10s
        if remaining <= 0:
            completed = True
        elif cur_temp is not None and cur_temp <= _COOLDOWN_STATE["target_temp"] and elapsed >= 10:
            completed = True

        if completed:
            _COOLDOWN_STATE["completed"] = True
            _COOLDOWN_STATE["active"] = False

        return {
            "active": _COOLDOWN_STATE["active"],
            "completed": _COOLDOWN_STATE["completed"],
            "started_at": _COOLDOWN_STATE["started_at"],
            "duration": duration,
            "remaining": remaining,
            "elapsed": int(elapsed),
            "target_temp": _COOLDOWN_STATE["target_temp"],
            "initial_temp": _COOLDOWN_STATE["initial_temp"],
            "current_temp": cur_temp,
        }
