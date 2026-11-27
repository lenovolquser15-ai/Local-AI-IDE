"""Hardware profiling, model sizing, memory fit calculation, and tokens/sec speed estimation."""
import os
import re
import shutil
import subprocess
import threading
import time

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_HW_CACHE = {"at": 0.0, "value": None, "lock": threading.Lock()}
_HW_TTL = 8.0

# Approximate memory bandwidth for common GPUs (in GB/s)
GPU_BANDWIDTH_MAP = {
    "5090": 1792.0, "5080": 1024.0, "5070": 672.0, "5060": 320.0,
    "4090": 1008.0, "4080": 716.0, "4070": 504.0, "4060": 272.0,
    "3090": 936.0, "3080": 760.0, "3070": 448.0, "3060": 360.0,
    "2080": 448.0, "2070": 448.0, "2060": 336.0, "1080": 320.0,
    "A100": 1935.0, "H100": 3350.0, "V100": 900.0, "A10": 600.0, "A6000": 768.0,
    "Apple M1": 68.0, "Apple M2": 100.0, "Apple M3": 150.0, "Apple M4": 150.0,
    "Apple M1 Pro": 200.0, "Apple M2 Pro": 200.0, "Apple M3 Pro": 150.0,
    "Apple M1 Max": 400.0, "Apple M2 Max": 400.0, "Apple M3 Max": 400.0,
    "Apple M1 Ultra": 800.0, "Apple M2 Ultra": 800.0,
}

CPU_RAM_BANDWIDTH = 45.0  # DDR4 / DDR5 dual-channel average ~35-65 GB/s


def get_hardware_profile(force=False):
    """Возвращает характеристики хоста: RAM и GPU VRAM с пропускной способностью."""
    now = time.monotonic()
    with _HW_CACHE["lock"]:
        if not force and _HW_CACHE["value"] and (now - _HW_CACHE["at"]) < _HW_TTL:
            return dict(_HW_CACHE["value"])

    profile = {
        "ram_total_gb": 16.0,
        "ram_free_gb": 8.0,
        "has_gpu": False,
        "gpu_name": "",
        "vram_total_gb": 0.0,
        "vram_free_gb": 0.0,
        "gpu_bandwidth_gbps": 0.0,
        "cpu_bandwidth_gbps": CPU_RAM_BANDWIDTH,
    }

    try:
        import psutil
        vm = psutil.virtual_memory()
        profile["ram_total_gb"] = round(vm.total / (1024 ** 3), 2)
        profile["ram_free_gb"] = round(vm.available / (1024 ** 3), 2)
    except Exception:
        pass

    # Попытка опросить NVIDIA GPU через nvidia-smi
    smi = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if smi:
        try:
            proc = subprocess.run(
                [smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=3.0, creationflags=NO_WINDOW
            )
            if proc.returncode == 0 and proc.stdout:
                line = proc.stdout.decode("utf-8", "replace").strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    gpu_name = parts[0]
                    total_mb = float(parts[1])
                    free_mb = float(parts[2])
                    profile["has_gpu"] = True
                    profile["gpu_name"] = gpu_name
                    profile["vram_total_gb"] = round(total_mb / 1024.0, 2)
                    profile["vram_free_gb"] = round(free_mb / 1024.0, 2)

                    # Оценка пропускной способности GPU
                    bandwidth = 320.0
                    for key, bw in GPU_BANDWIDTH_MAP.items():
                        if key.lower() in gpu_name.lower():
                            bandwidth = bw
                            break
                    profile["gpu_bandwidth_gbps"] = bandwidth
        except Exception:
            pass

    # Fallback для AMD Radeon / Intel Arc / Apple Silicon через hardware-survey LM Studio (Vulkan / ROCm / Metal)
    if not profile["has_gpu"]:
        try:
            import json
            idx_path = os.path.join(os.path.expanduser("~"), ".lmstudio", ".internal", "internal-engine-index.json")
            if os.path.isfile(idx_path):
                with open(idx_path, "r", encoding="utf-8") as fh:
                    raw_idx = json.load(fh)
                entries = raw_idx.get("json") if isinstance(raw_idx, dict) else raw_idx
                best_vram = 0
                best_name = ""
                for item in (entries or []):
                    if not isinstance(item, dict):
                        continue
                    compat = (item.get("backendCompatibility") or {}).get("status")
                    mem = item.get("memoryInfo") or {}
                    vram_b = int(mem.get("vramCapacity") or 0)
                    if compat == "compatible" and vram_b > best_vram:
                        best_vram = vram_b
                        gpus = (((item.get("hardwareSurveyResult") or {}).get("gpuSurveyResult") or {}).get("gpuInfo") or [])
                        if gpus and isinstance(gpus[0], dict):
                            best_name = str(gpus[0].get("name") or gpus[0].get("deviceName") or "GPU (Vulkan)")
                if best_vram > 0:
                    vram_gb = round(best_vram / (1024 ** 3), 2)
                    profile["has_gpu"] = True
                    profile["gpu_name"] = best_name or "Vulkan GPU"
                    profile["vram_total_gb"] = vram_gb
                    profile["vram_free_gb"] = round(max(0.5, vram_gb * 0.88), 2)
                    profile["gpu_bandwidth_gbps"] = 280.0
        except Exception:
            pass

    with _HW_CACHE["lock"]:
        _HW_CACHE["value"] = dict(profile)
        _HW_CACHE["at"] = now

    return profile


def parse_model_params(model_item):
    """Извлекает количество параметров (в миллиардах) и квантование."""
    params_str = str(model_item.get("paramsString") or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*B", params_str, re.I)
    if match:
        params_b = float(match.group(1))
    else:
        # Поиск в имени файла или ключе
        name = str(model_item.get("displayName") or model_item.get("modelKey") or model_item.get("path") or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*B\b", name, re.I)
        if match:
            params_b = float(match.group(1))
        elif "70b" in name.lower():
            params_b = 70.0
        elif "32b" in name.lower():
            params_b = 32.0
        elif "14b" in name.lower():
            params_b = 14.0
        elif "8b" in name.lower() or "8-b" in name.lower():
            params_b = 8.0
        elif "7b" in name.lower():
            params_b = 7.0
        elif "3b" in name.lower():
            params_b = 3.0
        elif "1.5b" in name.lower() or "1_5b" in name.lower():
            params_b = 1.5
        else:
            size_b = float(model_item.get("sizeBytes") or 0)
            if size_b > 25 * (1024 ** 3):
                params_b = 70.0
            elif size_b > 15 * (1024 ** 3):
                params_b = 32.0
            elif size_b > 7 * (1024 ** 3):
                params_b = 14.0
            elif size_b > 3 * (1024 ** 3):
                params_b = 8.0
            elif size_b > 1.5 * (1024 ** 3):
                params_b = 3.0
            else:
                params_b = 1.5

    quant = model_item.get("quantization") or {}
    q_name = quant.get("name") if isinstance(quant, dict) else str(quant)
    if not q_name:
        for q_cand in ("Q4_K_M", "Q4_K_S", "Q5_K_M", "Q5_K_S", "Q6_K", "Q8_0", "Q3_K_M", "Q2_K", "FP16"):
            if q_cand.lower() in str(model_item.get("path") or "").lower():
                q_name = q_cand
                break
    q_name = q_name or "Q4_K_M"
    return params_b, q_name


def estimate_model_fit(model_item, context_length=8192, hw=None, gpu_offload="max", kv_quant="f16"):
    """Вычисляет требования модели к памяти, процент offload, скорость tok/s и риск лагов UI."""
    if hw is None:
        hw = get_hardware_profile()

    size_bytes = int(model_item.get("sizeBytes") or 0)
    params_b, q_name = parse_model_params(model_item)
    ctx_len = max(512, min(int(context_length or 8192), 262144))

    # 1. Вес модели в памяти (ГБ) с буфером на mmap overhead
    if size_bytes > 0:
        weights_gb = round(size_bytes / (1024 ** 3), 2)
    else:
        # Оценка по параметрам: 4.5 бит на параметр для Q4
        bits_per_param = 4.5 if "q4" in q_name.lower() else (5.5 if "q5" in q_name.lower() else 8.5)
        weights_gb = round((params_b * 1e9 * (bits_per_param / 8.0)) / (1024 ** 3), 2)

    # 2. KV-кэш контекста в памяти (ГБ) с учетом квантования K/V кэша (f16 / q8_0 / q4_0)
    # У компактных 2B-4B моделей (Qwen3/Qwen3-VL/Llama-3.2) слоёв почти столько же (28-36),
    # поэтому KV-кэш не падает линейно ниже ~0.55x от 8B модели.
    kv_q = str(kv_quant or "f16").strip().lower()
    kv_mult = 0.55 if "q8" in kv_q else (0.32 if "q4" in kv_q else 1.0)
    kv_scale = max(0.58, params_b / 8.0)
    kv_factor = 0.000105 * kv_scale * kv_mult
    kv_cache_gb = round(max(0.10, ctx_len * kv_factor), 2)

    # Пользовательский лимит выгрузки на GPU (off / 0.0..1.0 / max)
    req_ratio = 1.0
    g_raw = str(gpu_offload if gpu_offload is not None else "max").strip().lower()
    if g_raw in ("off", "0", "0.0", "cpu", "none"):
        req_ratio = 0.0
    elif g_raw not in ("max", "auto", ""):
        try:
            val = float(g_raw)
            req_ratio = max(0.0, min(1.0, val / 100.0 if val > 1.0 else val))
        except (TypeError, ValueError):
            req_ratio = 1.0

    # Runtime/CUDA/Vulkan overhead
    use_gpu = bool(hw["has_gpu"] and req_ratio > 0.0)
    overhead_gb = 0.55 if use_gpu else 0.25
    total_req_gb = round(weights_gb + kv_cache_gb + overhead_gb, 2)
    est_gpu_gb = round((weights_gb * req_ratio) + (kv_cache_gb * (1.0 if req_ratio >= 0.5 else req_ratio)) + (0.45 if use_gpu else 0.0), 2) if use_gpu else 0.0

    vram_free = hw["vram_free_gb"]
    ram_free = hw["ram_free_gb"]
    gpu_bw = hw["gpu_bandwidth_gbps"]
    cpu_bw = hw["cpu_bandwidth_gbps"]

    # 3. Вердикт совместимости (Fit)
    if not use_gpu and total_req_gb <= (ram_free - 1.5):
        fit = "cpu_only"
        fit_label = "Только CPU (RAM)"
        fit_color = "blue"
        offload_pct = 0
        ui_lag_risk = "medium"
        ui_comfort = "Умеренная нагрузка CPU (GPU выключен)"
        base_speed = (cpu_bw / max(1.0, weights_gb)) * 0.50
        min_tok = max(4, int(base_speed * 0.7))
        max_tok = max(min_tok + 2, int(base_speed * 1.2))
        tok_speed_str = "%d-%d т/с" % (min_tok, max_tok)
        recommendation = "Выбрана загрузка только в оперативную память (GPU Offload = Off)."

    elif use_gpu and req_ratio >= 0.99 and total_req_gb <= (vram_free - 0.2):
        fit = "gpu_full"
        fit_label = "100% GPU"
        fit_color = "green"
        offload_pct = 100
        ui_lag_risk = "none"
        ui_comfort = "100% плавный интерфейс (0% лагов)"
        # Скорость: bandwidth / weights_gb * эффективность инференса (~0.6-0.75)
        eff = 0.70
        base_speed = (gpu_bw / max(1.0, weights_gb)) * eff
        min_tok = max(15, int(base_speed * 0.8))
        max_tok = max(min_tok + 5, int(base_speed * 1.15))
        tok_speed_str = "%d-%d т/с" % (min_tok, max_tok)
        recommendation = "Модель полностью помещается в видеопамять (%s). Максимальная скорость, нулевые задержки интерфейса." % hw.get("gpu_name", "GPU")

    elif use_gpu and est_gpu_gb <= (vram_free + 0.1) and total_req_gb <= (vram_free + ram_free - 2.0) and req_ratio < 0.99:
        fit = "hybrid"
        offload_pct = int(round(req_ratio * 100))
        fit_label = "Гибрид (%d%% GPU)" % offload_pct
        fit_color = "yellow"
        ui_lag_risk = "low"
        ui_comfort = "Частичный оффлоад на GPU (%d%%)" % offload_pct
        hybrid_bw = (gpu_bw * req_ratio) + (cpu_bw * (1.0 - req_ratio))
        base_speed = (hybrid_bw / max(1.0, weights_gb)) * 0.45
        min_tok = max(8, int(base_speed * 0.75))
        max_tok = max(min_tok + 3, int(base_speed * 1.2))
        tok_speed_str = "%d-%d т/с" % (min_tok, max_tok)
        recommendation = "Ручное распределение: %d%% слоев в VRAM (%.2f ГБ), остаток — в системной RAM." % (offload_pct, est_gpu_gb)

    elif hw["has_gpu"] and vram_free >= 2.0 and total_req_gb <= (vram_free + ram_free - 2.5):
        fit = "hybrid"
        fit_label = "Гибрид (GPU+RAM)"
        fit_color = "yellow"
        offload_pct = int(min(90, max(20, (vram_free / total_req_gb) * 100)))
        ui_lag_risk = "low"
        ui_comfort = "Стабильный интерфейс (минимальная нагрузка)"
        # Скорость гибридная: упирается в шину PCIe и RAM
        hybrid_bw = (gpu_bw * (offload_pct / 100.0)) + (cpu_bw * ((100 - offload_pct) / 100.0))
        base_speed = (hybrid_bw / max(1.0, weights_gb)) * 0.40
        min_tok = max(8, int(base_speed * 0.75))
        max_tok = max(min_tok + 3, int(base_speed * 1.2))
        tok_speed_str = "%d-%d т/с" % (min_tok, max_tok)
        recommendation = "Часть слоев (%d%%) пойдет в видеопамять, остаток — в RAM. Достаточная скорость, интерфейс отзывчив." % offload_pct

    elif total_req_gb <= (ram_free - 2.5):
        fit = "cpu_only"
        fit_label = "Только CPU (RAM)"
        fit_color = "blue"
        offload_pct = 0
        ui_lag_risk = "medium"
        ui_comfort = "Умеренная нагрузка CPU"
        base_speed = (cpu_bw / max(1.0, weights_gb)) * 0.50
        min_tok = max(4, int(base_speed * 0.7))
        max_tok = max(min_tok + 2, int(base_speed * 1.2))
        tok_speed_str = "%d-%d т/с" % (min_tok, max_tok)
        recommendation = "Генерация полностью на процессоре. Рекомендуется использовать квантование Q4_K_M для плавной работы."

    elif total_req_gb <= (ram_free + (vram_free if hw["has_gpu"] else 0.0) - 0.6):
        fit = "tight"
        fit_label = "На пределе RAM"
        fit_color = "orange"
        offload_pct = int((vram_free / total_req_gb) * 100) if hw["has_gpu"] else 0
        ui_lag_risk = "high"
        ui_comfort = "Возможны микрофризы интерфейса"
        tok_speed_str = "3-7 т/с"
        recommendation = "Свободной памяти мало (останется менее 2 ГБ). Закройте тяжелые программы и ограничьте контекст до 4096 токенов."

    else:
        fit = "oom"
        fit_label = "Не поместится (OOM)"
        fit_color = "red"
        offload_pct = 0
        ui_lag_risk = "critical"
        ui_comfort = "Критический риск зависания системы"
        tok_speed_str = "< 1 т/с (своп)"
        recommendation = "Требуется %.1f ГБ памяти, а свободно всего %.1f ГБ (RAM %.1f ГБ + VRAM %.1f ГБ). Загрузка вызовет жесткий своп диска. Выберите модель меньшего размера (3B/7B)." % (
            total_req_gb, ram_free + vram_free, ram_free, vram_free
        )

    return {
        "fit": fit,
        "fit_label": fit_label,
        "fit_color": fit_color,
        "offload_pct": offload_pct,
        "tok_per_sec": tok_speed_str,
        "ui_lag_risk": ui_lag_risk,
        "ui_comfort": ui_comfort,
        "weights_gb": weights_gb,
        "kv_cache_gb": kv_cache_gb,
        "est_gpu_gb": min(est_gpu_gb, total_req_gb),
        "total_req_gb": total_req_gb,
        "kv_quant": kv_q,
        "gpu_offload": g_raw or "max",
        "context_length": ctx_len,
        "recommendation": recommendation,
        "hardware": {
            "gpu_name": hw.get("gpu_name") or "Не обнаружен",
            "vram_free_gb": hw.get("vram_free_gb", 0.0),
            "vram_total_gb": hw.get("vram_total_gb", 0.0),
            "ram_free_gb": hw.get("ram_free_gb", 0.0),
            "ram_total_gb": hw.get("ram_total_gb", 0.0),
        }
    }


def enrich_model_item(item, context_length=8192, hw=None):
    """Обогащает словарь модели метриками совместимости и производительности."""
    if not isinstance(item, dict):
        return item
    enriched = dict(item)
    enriched["estimate"] = estimate_model_fit(item, context_length=context_length, hw=hw)
    return enriched
