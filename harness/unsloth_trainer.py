"""Unsloth Fine-Tuning Studio 2.0 for LocalHarness:
- Environment & VRAM diagnostics + Auto-Tune hyperparameters for user's GPU
- One-click isolated venv installer (data/unsloth_venv with PyTorch CUDA + Unsloth + TRL + PEFT)
- Dataset manager: export from LocalHarness chat sessions, AI synthetic dataset generator via local LLM,
  JSONL/JSON/CSV upload & preview, HuggingFace Hub dataset support
- Unsloth 4-bit QLoRA script generator: SFT, GRPO (Reasoning RL), DPO, Vision (FastVisionModel),
  custom chat templates, rsLoRA, packing, GGUF export (q4_k_m/q8_0/f16) + Ollama Modelfile + LM Studio import
- Background job runner with real-time Loss curve history, GPU thermal guard, and adapter quick-eval.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import config as config_mod
from . import session as session_mod
from . import thermal as thermal_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.RLock()

PRESET_MODELS = [
    {
        "id": "unsloth/Qwen3-4B-bnb-4bit",
        "label": "Qwen3 4B (4-bit · Универсальная)",
        "vram_gb": 4.5,
        "params_b": 4.0,
        "vision": False,
        "recommended_for": "8 ГБ VRAM (быстрое обучение SFT / GRPO)",
    },
    {
        "id": "unsloth/Llama-3.2-3B-Instruct-bnb-4bit",
        "label": "Llama 3.2 3B Instruct (4-bit · Сверхбыстрая)",
        "vram_gb": 3.5,
        "params_b": 3.0,
        "vision": False,
        "recommended_for": "6–8 ГБ VRAM (диалоги, ассистент)",
    },
    {
        "id": "unsloth/gemma-3-4b-it-bnb-4bit",
        "label": "Gemma 3 4B IT (4-bit · Текст + Vision)",
        "vram_gb": 4.8,
        "params_b": 4.0,
        "vision": True,
        "recommended_for": "8 ГБ VRAM (русский язык, мультимодальность)",
    },
    {
        "id": "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit",
        "label": "Qwen2.5 Coder 7B (4-bit · Программирование)",
        "vram_gb": 6.8,
        "params_b": 7.0,
        "vision": False,
        "recommended_for": "8–12 ГБ VRAM (код, агенты, инструменты)",
    },
    {
        "id": "unsloth/DeepSeek-R1-Distill-Qwen-7B-bnb-4bit",
        "label": "DeepSeek-R1 Distill Qwen 7B (4-bit · Reasoning)",
        "vram_gb": 6.9,
        "params_b": 7.0,
        "vision": False,
        "recommended_for": "8–12 ГБ VRAM (GRPO / раздумья)",
    },
    {
        "id": "unsloth/Mistral-7B-Instruct-v0.3-bnb-4bit",
        "label": "Mistral 7B v0.3 (4-bit · Баланс)",
        "vram_gb": 6.8,
        "params_b": 7.0,
        "vision": False,
        "recommended_for": "8–12 ГБ VRAM (общие задачи)",
    },
    {
        "id": "unsloth/Qwen2.5-VL-7B-Instruct-bnb-4bit",
        "label": "Qwen2.5-VL 7B (4-bit · Vision VLM)",
        "vram_gb": 7.4,
        "params_b": 7.0,
        "vision": True,
        "recommended_for": "8–12 ГБ VRAM (дообучение на картинках)",
    },
]

PRESET_HF_DATASETS = [
    {"id": "hf://IlyaGusev/ru_turbo_alpaca", "label": "🇷🇺 IlyaGusev/ru_turbo_alpaca (Русский SFT)"},
    {"id": "hf://yahma/alpaca-cleaned", "label": "🌐 yahma/alpaca-cleaned (52k инструкций)"},
    {"id": "hf://mlabonne/FineTome-100k", "label": "🔥 mlabonne/FineTome-100k (Топ диалоги)"},
    {"id": "hf://openai/gsm8k", "label": "🧠 openai/gsm8k (Математика для GRPO)"},
]

CHAT_TEMPLATES = ["alpaca", "chatml", "llama-3.1", "qwen-2.5", "gemma-3", "mistral"]

_ACTIVE_JOB = {
    "id": None,
    "status": "idle",  # idle | running | done | failed | stopped
    "kind": "train",   # train | install | eval
    "model": "",
    "method": "sft",
    "dataset": "",
    "output_dir": "",
    "gguf_quant": "q4_k_m",
    "started_at": 0.0,
    "ended_at": None,
    "step": 0,
    "max_steps": 0,
    "epoch": 0.0,
    "loss": None,
    "lr": None,
    "gpu_temp": None,
    "rc": None,
    "error": "",
    "lines": [],
    "loss_history": [],
    "script_path": "",
    "gguf_files": [],
}
_ACTIVE_PROC = {"proc": None}


def training_dir():
    d = os.path.join(config_mod.DATA_DIR, "training")
    os.makedirs(d, exist_ok=True)
    return d


def datasets_dir():
    d = os.path.join(config_mod.DATA_DIR, "datasets")
    os.makedirs(d, exist_ok=True)
    return d


def venv_dir():
    return os.path.join(config_mod.DATA_DIR, "unsloth_venv")


def venv_python():
    vdir = venv_dir()
    cand = os.path.join(vdir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(vdir, "bin", "python")
    if os.path.isfile(cand):
        return cand
    return None


def training_python():
    vp = venv_python()
    if vp:
        return vp
    if not getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
        return sys.executable
    return shutil.which("python") or shutil.which("python3") or "python"


def _history_path():
    return os.path.join(training_dir(), "history.json")


def _load_history():
    p = _history_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(rows):
    p = _history_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows[:40], fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _guess_params_b(model_id):
    for p in PRESET_MODELS:
        if p["id"].lower() == str(model_id or "").lower():
            return float(p.get("params_b", 4.0))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", str(model_id or ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 7.0


def estimate_training_vram(model_id="unsloth/Qwen3-4B-bnb-4bit", max_seq_length=2048,
                           batch_size=2, lora_r=16, method="sft"):
    """Estimate peak VRAM usage (GB) for Unsloth 4-bit QLoRA training."""
    params_b = _guess_params_b(model_id)
    base_4bit = params_b * 0.62  # 4-bit weights + overhead
    lora_overhead = (lora_r / 16.0) * (params_b * 0.06)
    ctx_act = (max_seq_length / 2048.0) * max(1, int(batch_size or 1)) * (0.45 + params_b * 0.08)
    method_mult = 1.35 if method == "grpo" else (1.2 if method in ("dpo", "vision") else 1.0)
    est_gb = round((base_4bit + lora_overhead + ctx_act) * method_mult + 0.6, 2)

    gpu = thermal_mod.read_gpu_sensors(force=False)
    vram_total = round((gpu.get("vram_total_mb") or 0) / 1024.0, 1)
    fit = "ok"
    note = "Отлично помещается в VRAM"
    if vram_total > 0:
        if est_gb > vram_total * 0.95:
            fit = "oom_risk"
            note = "Высокий риск OOM: уменьши batch_size, max_seq_length или выбери модель 3B–4B"
        elif est_gb > vram_total * 0.80:
            fit = "tight"
            note = "Впритык к лимиту VRAM — рекомендуем batch_size=1 и gradient checkpointing"
    return {
        "ok": True,
        "estimated_vram_gb": est_gb,
        "gpu_vram_gb": vram_total,
        "fit": fit,
        "note": note,
    }


def autotune_for_gpu(model_id="unsloth/Qwen3-4B-bnb-4bit", method="sft"):
    """Auto-select safe, fast Unsloth hyperparameters for the detected GPU VRAM."""
    gpu = thermal_mod.read_gpu_sensors(force=False)
    vram_gb = (gpu.get("vram_total_mb") or 8192) / 1024.0
    params_b = _guess_params_b(model_id)

    if vram_gb <= 6.5:
        rec = {"batch_size": 1, "grad_accum": 8, "max_seq_length": 1024, "lora_r": 8, "lora_alpha": 16, "packing": False}
    elif vram_gb <= 9.0:
        if params_b >= 6.5 or method in ("grpo", "vision"):
            rec = {"batch_size": 1, "grad_accum": 8, "max_seq_length": 1536, "lora_r": 16, "lora_alpha": 16, "packing": False}
        else:
            rec = {"batch_size": 2, "grad_accum": 4, "max_seq_length": 2048, "lora_r": 16, "lora_alpha": 16, "packing": True}
    elif vram_gb <= 16.5:
        rec = {"batch_size": 2, "grad_accum": 4, "max_seq_length": 4096, "lora_r": 32, "lora_alpha": 32, "packing": True}
    else:
        rec = {"batch_size": 4, "grad_accum": 4, "max_seq_length": 4096, "lora_r": 64, "lora_alpha": 64, "packing": True}

    rec.update({
        "learning_rate": 5e-5 if method in ("grpo", "dpo") else 2e-4,
        "warmup_steps": 5,
        "lr_scheduler": "cosine",
        "use_rslora": rec["lora_r"] >= 32,
        "gguf_quant": "q4_k_m" if vram_gb <= 10 else "q8_0",
    })
    est = estimate_training_vram(model_id, rec["max_seq_length"], rec["batch_size"], rec["lora_r"], method)
    return {"ok": True, "gpu_vram_gb": round(vram_gb, 1), "recommended": rec, "estimate": est}


def env_status(force=False):
    py = training_python()
    using_venv = bool(venv_python())
    gpu = thermal_mod.read_gpu_sensors(force=force)
    vram_gb = round((gpu.get("vram_total_mb") or 0) / 1024.0, 1)

    probe_code = (
        "import json; out = {}; "
        "for p in ('torch','transformers','peft','trl','datasets','bitsandbytes','unsloth'):\n"
        "  try:\n"
        "    m = __import__(p); out[p] = getattr(m, '__version__', 'installed')\n"
        "  except Exception:\n"
        "    out[p] = None\n"
        "try:\n"
        "  import torch; out['cuda'] = bool(torch.cuda.is_available()); "
        "  out['cuda_ver'] = getattr(torch.version, 'cuda', None)\n"
        "except Exception:\n"
        "  out['cuda'] = False; out['cuda_ver'] = None\n"
        "print(json.dumps(out))"
    )
    pkgs = {"torch": None, "transformers": None, "peft": None, "trl": None, "datasets": None, "bitsandbytes": None, "unsloth": None, "cuda": False, "cuda_ver": None}
    try:
        proc = subprocess.run(
            [py, "-c", probe_code],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        if proc.returncode == 0 and proc.stdout:
            parsed = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
            if isinstance(parsed, dict):
                pkgs.update(parsed)
    except Exception:
        pass

    ready = bool(pkgs.get("unsloth") and pkgs.get("cuda"))
    if not gpu.get("has_gpu"):
        hint = "Дискретная NVIDIA GPU не обнаружена. Для быстрого обучения Unsloth нужна видеокарта NVIDIA (CUDA)."
    elif not pkgs.get("cuda"):
        hint = "Установлен PyTorch без CUDA (CPU-версия). Нажми «Установить Unsloth + CUDA» для создания изолированного venv."
    elif not pkgs.get("unsloth"):
        hint = "Unsloth не установлен. Нажми «Установить Unsloth + CUDA» — установим в data/unsloth_venv."
    else:
        hint = "Окружение готово к обучению Unsloth 4-bit QLoRA / GRPO / DPO / Vision!"

    return {
        "ok": True,
        "ready": ready,
        "python": py,
        "using_venv": using_venv,
        "gpu_name": gpu.get("gpu_name") or "",
        "has_gpu": bool(gpu.get("has_gpu")),
        "vram_gb": vram_gb,
        "gpu_temp": gpu.get("temp_c"),
        "packages": pkgs,
        "hint": hint,
        "presets": PRESET_MODELS,
        "hf_datasets": PRESET_HF_DATASETS,
    }


def list_datasets():
    ddir = datasets_dir()
    rows = []
    for fname in sorted(os.listdir(ddir)):
        if not fname.lower().endswith(".jsonl"):
            continue
        full = os.path.join(ddir, fname)
        try:
            size_kb = round(os.path.getsize(full) / 1024.0, 1)
            count = 0
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
            rows.append({
                "name": fname,
                "path": full,
                "rows": count,
                "size_kb": size_kb,
                "mtime": os.path.getmtime(full),
            })
        except OSError:
            continue
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return {"ok": True, "dir": ddir, "datasets": rows, "hf_datasets": PRESET_HF_DATASETS}


def preview_dataset(name_or_path, limit=12):
    target = str(name_or_path or "").strip()
    if not target:
        return {"ok": False, "error": "Не указан датасет"}
    if not os.path.isabs(target):
        target = os.path.join(datasets_dir(), os.path.basename(target))
    if not os.path.isfile(target):
        return {"ok": False, "error": "Файл не найден: %s" % target}
    samples = []
    total = 0
    bad = 0
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
                if len(samples) < limit:
                    samples.append(obj)
            except ValueError:
                bad += 1
    return {"ok": True, "name": os.path.basename(target), "path": target, "total": total, "bad_lines": bad, "samples": samples}


def delete_dataset(name):
    fname = os.path.basename(str(name or "").strip())
    if not fname or not fname.lower().endswith(".jsonl"):
        return {"ok": False, "error": "Некорректное имя файла датасета"}
    target = os.path.join(datasets_dir(), fname)
    if not os.path.isfile(target):
        return {"ok": False, "error": "Датасет не найден: %s" % fname}
    os.remove(target)
    return {"ok": True, "removed": fname, **list_datasets()}


def import_dataset_file(filename, raw_text):
    """Import .jsonl, .json (array of objects), or .csv into data/datasets/*.jsonl."""
    base = os.path.splitext(os.path.basename(str(filename or "imported")))[0]
    ext = os.path.splitext(str(filename or ""))[1].lower()
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", base)[:60] or "imported"
    out_path = os.path.join(datasets_dir(), safe + ".jsonl")

    rows = []
    text = str(raw_text or "").strip()
    if not text:
        return {"ok": False, "error": "Файл пуст"}

    if ext == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        for r in reader:
            inst = r.get("instruction") or r.get("prompt") or r.get("question") or ""
            out = r.get("output") or r.get("response") or r.get("answer") or ""
            inp = r.get("input") or r.get("context") or ""
            if inst and out:
                rows.append({"instruction": inst.strip(), "input": inp.strip(), "output": out.strip()})
    elif ext == ".json":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                parsed = parsed.get("data") or parsed.get("rows") or [parsed]
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        rows.append(item)
        except Exception as exc:
            return {"ok": False, "error": "Ошибка разбора JSON: %s" % exc}
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except ValueError:
                continue

    if not rows:
        return {"ok": False, "error": "Не найдено ни одной валидной строки для импорта"}

    with open(out_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"ok": True, "name": os.path.basename(out_path), "path": out_path, "count": len(rows)}


def export_sessions_to_dataset(dataset_name="sessions_sft.jsonl", min_chars=8, max_sessions=100):
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(dataset_name or "sessions_sft.jsonl").strip())
    if not safe.lower().endswith(".jsonl"):
        safe += ".jsonl"
    out_path = os.path.join(datasets_dir(), safe)
    sessions = session_mod.list_sessions(limit=max_sessions)
    pairs = []
    for meta in sessions:
        data = session_mod.load(meta["id"])
        if not data:
            continue
        msgs = data.get("messages") or []
        pending_user = None
        for m in msgs:
            role = m.get("role")
            content = m.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            content = str(content).strip()
            if role == "user" and len(content) >= min_chars:
                pending_user = content
            elif role == "assistant" and pending_user and len(content) >= min_chars:
                if not content.startswith(("Модель не ответила:", "Error:", "Остановлено.")):
                    pairs.append({
                        "instruction": pending_user[:4000],
                        "input": "",
                        "output": content[:8000],
                    })
                pending_user = None

    with open(out_path, "w", encoding="utf-8") as fh:
        for row in pairs:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "ok": True,
        "name": safe,
        "path": out_path,
        "count": len(pairs),
        "sessions_scanned": len(sessions),
    }


def append_dataset_example(dataset_name, instruction, output, inp=""):
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(dataset_name or "custom_sft.jsonl").strip())
    if not safe.lower().endswith(".jsonl"):
        safe += ".jsonl"
    instruction = str(instruction or "").strip()
    output = str(output or "").strip()
    if not instruction or not output:
        return {"ok": False, "error": "Нужны instruction и output"}
    out_path = os.path.join(datasets_dir(), safe)
    row = {"instruction": instruction, "input": str(inp or "").strip(), "output": output}
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True, "name": safe, "path": out_path}


def generate_synthetic_dataset(topic, count=10, dataset_name="synthetic_sft.jsonl", cfg=None):
    """Use the user's active local LLM to synthesize clean instruction/output training pairs."""
    topic = str(topic or "").strip()
    if not topic:
        return {"ok": False, "error": "Укажите тему для генерации синтетического датасета"}
    count = max(1, min(50, int(count or 10)))
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(dataset_name or "synthetic_sft.jsonl").strip())
    if not safe.lower().endswith(".jsonl"):
        safe += ".jsonl"
    out_path = os.path.join(datasets_dir(), safe)

    cfg = config_mod.load() if cfg is None else cfg
    from . import llm as llm_mod
    client = llm_mod.LLM(cfg)
    prompt = (
        f"Сгенерируй ровно {count} качественных обучающих примеров для дообучения ИИ-модели по теме: «{topic}».\n"
        "Верни ТОЛЬКО валидный JSON-массив объектов вида:\n"
        '[{"instruction": "вопрос или задача пользователя", "input": "", "output": "подробный, точный и полезный ответ"}]\n'
        "Без markdown-обёрток, без пояснений, только JSON-массив."
    )
    try:
        resp = client.chat(
            [{"role": "system", "content": "Ты — эксперт по созданию датасетов для SFT/LoRA. Отвечай строго валидным JSON-массивом."},
             {"role": "user", "content": prompt}],
            max_tokens=min(4096, max(1200, count * 220)),
            temperature=0.6,
        )
        raw = str(resp.get("content") or "").strip()
    except Exception as exc:
        return {"ok": False, "error": "Не удалось обратиться к локальной модели: %s" % exc}

    # Extract JSON array
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return {"ok": False, "error": "Модель вернула ответ не в формате JSON-массива", "raw": raw[:400]}
    try:
        items = json.loads(m.group(0))
    except Exception as exc:
        return {"ok": False, "error": "Не удалось распарсить JSON от модели: %s" % exc}

    added = 0
    with open(out_path, "a", encoding="utf-8") as fh:
        for item in items:
            if not isinstance(item, dict):
                continue
            inst = str(item.get("instruction") or item.get("prompt") or "").strip()
            out = str(item.get("output") or item.get("response") or "").strip()
            inp = str(item.get("input") or "").strip()
            if inst and out:
                fh.write(json.dumps({"instruction": inst, "input": inp, "output": out}, ensure_ascii=False) + "\n")
                added += 1

    if not added:
        return {"ok": False, "error": "Модель не сгенерировала валидных пар instruction/output"}
    return {"ok": True, "name": safe, "path": out_path, "added": added, "topic": topic}


def generate_unsloth_script(cfg):
    """Generate a complete Unsloth Python training script (SFT / GRPO / DPO / Vision + GGUF + Ollama Modelfile)."""
    model_name = str(cfg.get("model") or "unsloth/Qwen3-4B-bnb-4bit").strip()
    dataset_path = str(cfg.get("dataset_path") or "").strip()
    output_dir = str(cfg.get("output_dir") or os.path.join(training_dir(), "run_out")).strip()
    method = str(cfg.get("method") or "sft").lower()
    if method not in ("sft", "grpo", "dpo", "vision"):
        method = "sft"
    chat_template = str(cfg.get("chat_template") or "alpaca").strip().lower()
    max_seq = int(cfg.get("max_seq_length") or 2048)
    lora_r = int(cfg.get("lora_r") or 16)
    lora_alpha = int(cfg.get("lora_alpha") or lora_r)
    lora_dropout = float(cfg.get("lora_dropout") or 0.0)
    epochs = float(cfg.get("epochs") or 1)
    max_steps = int(cfg.get("max_steps") or 60)
    lr = float(cfg.get("learning_rate") or 2e-4)
    batch_size = int(cfg.get("batch_size") or 2)
    grad_accum = int(cfg.get("grad_accum") or 4)
    warmup_steps = int(cfg.get("warmup_steps") or 5)
    weight_decay = float(cfg.get("weight_decay") or 0.01)
    lr_scheduler = str(cfg.get("lr_scheduler") or "cosine").strip()
    use_rslora = bool(cfg.get("use_rslora", False))
    packing = bool(cfg.get("packing", False))
    export_gguf = bool(cfg.get("export_gguf", True))
    gguf_quant = str(cfg.get("gguf_quant") or "q4_k_m").strip()
    hf_limit = int(cfg.get("hf_limit") or 1000)

    is_hf_ds = dataset_path.startswith("hf://")
    hf_ds_name = dataset_path[5:] if is_hf_ds else ""

    ds_loader = (
        f'raw_ds = load_dataset({hf_ds_name!r}, split="train[:{hf_limit}]")'
        if is_hf_ds
        else 'raw_ds = load_dataset("json", data_files=DATASET_PATH, split="train")'
    )

    if method == "vision":
        model_loader = f'''from unsloth import FastVisionModel
model, tokenizer = FastVisionModel.from_pretrained(
    model_name=MODEL_NAME,
    load_in_4bit=True,
    use_gradient_checkpointing="unsloth",
)
model = FastVisionModel.get_peft_model(
    model,
    finetune_vision_layers=True,
    finetune_language_layers=True,
    finetune_attention_modules=True,
    finetune_mlp_modules=True,
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    bias="none",
    random_state=3407,
    use_rslora=USE_RSLORA,
)'''
    else:
        model_loader = '''from unsloth import FastLanguageModel
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=MODEL_NAME,
    max_seq_length=MAX_SEQ_LENGTH,
    dtype=None,
    load_in_4bit=True,
)
model = FastLanguageModel.get_peft_model(
    model,
    r=LORA_R,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=3407,
    use_rslora=USE_RSLORA,
)'''

    header = f'''# Auto-generated by LocalHarness Unsloth Fine-Tuning Studio 2.0
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_NAME = {model_name!r}
DATASET_PATH = {dataset_path!r}
OUTPUT_DIR = {output_dir!r}
METHOD = {method!r}
CHAT_TEMPLATE = {chat_template!r}
MAX_SEQ_LENGTH = {max_seq}
LORA_R = {lora_r}
LORA_ALPHA = {lora_alpha}
LORA_DROPOUT = {lora_dropout}
EPOCHS = {epochs}
MAX_STEPS = {max_steps}
LEARNING_RATE = {lr}
BATCH_SIZE = {batch_size}
GRAD_ACCUM = {grad_accum}
WARMUP_STEPS = {warmup_steps}
WEIGHT_DECAY = {weight_decay}
LR_SCHEDULER = {lr_scheduler!r}
USE_RSLORA = {use_rslora!r}
PACKING = {packing!r}
EXPORT_GGUF = {export_gguf!r}
GGUF_QUANT = {gguf_quant!r}

print(f"[LocalHarness-Unsloth] Starting {{METHOD.upper()}} fine-tuning: {{MODEL_NAME}}", flush=True)
print(f"[LocalHarness-Unsloth] Dataset: {{DATASET_PATH}} -> Output: {{OUTPUT_DIR}}", flush=True)

import torch
from datasets import load_dataset

if not torch.cuda.is_available():
    print("[ERROR] CUDA GPU is not available in this Python environment.", flush=True)
    sys.exit(2)

{model_loader}

{ds_loader}
print(f"[LocalHarness-Unsloth] Loaded {{len(raw_ds)}} training rows.", flush=True)
'''

    if method == "grpo":
        body = '''
from trl import GRPOConfig, GRPOTrainer

def _to_grpo(example):
    prompt = example.get("instruction") or example.get("question") or example.get("prompt") or ""
    inp = example.get("input") or ""
    if inp:
        prompt = f"{prompt}\\n\\nКонтекст:\\n{inp}"
    return {
        "prompt": [
            {"role": "system", "content": "Рассуждай шаг за шагом и дай точный итоговый ответ."},
            {"role": "user", "content": prompt},
        ],
        "answer": str(example.get("output") or example.get("answer") or ""),
    }

train_ds = raw_ds.map(_to_grpo)

def reasoning_reward(completions, answer, **kwargs):
    rewards = []
    for comp, ref in zip(completions, answer):
        text = comp[0]["content"] if isinstance(comp, list) and comp else str(comp)
        score = 0.0
        if len(text.strip()) >= 20:
            score += 0.5
        if ref and ref.strip().lower()[:40] in text.lower():
            score += 1.5
        rewards.append(score)
    return rewards

args = GRPOConfig(
    output_dir=OUTPUT_DIR,
    learning_rate=LEARNING_RATE,
    per_device_train_batch_size=max(1, BATCH_SIZE),
    gradient_accumulation_steps=GRAD_ACCUM,
    num_generations=4,
    max_prompt_length=min(512, MAX_SEQ_LENGTH // 2),
    max_completion_length=min(512, MAX_SEQ_LENGTH // 2),
    max_steps=MAX_STEPS if MAX_STEPS > 0 else 60,
    warmup_steps=WARMUP_STEPS,
    lr_scheduler_type=LR_SCHEDULER,
    logging_steps=1,
    optim="adamw_8bit",
    report_to="none",
)

trainer = GRPOTrainer(
    model=model,
    processing_class=tokenizer,
    reward_funcs=[reasoning_reward],
    args=args,
    train_dataset=train_ds,
)
trainer.train()
'''
    elif method == "dpo":
        body = '''
from trl import DPOConfig, DPOTrainer
from unsloth import PatchDPOTrainer
PatchDPOTrainer()

def _to_dpo(example):
    prompt = example.get("prompt") or example.get("instruction") or ""
    chosen = example.get("chosen") or example.get("output") or ""
    rejected = example.get("rejected") or "Я не знаю."
    return {"prompt": prompt, "chosen": chosen, "rejected": rejected}

train_ds = raw_ds.map(_to_dpo)
args = DPOConfig(
    output_dir=OUTPUT_DIR,
    per_device_train_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LEARNING_RATE,
    warmup_steps=WARMUP_STEPS,
    weight_decay=WEIGHT_DECAY,
    lr_scheduler_type=LR_SCHEDULER,
    num_train_epochs=EPOCHS,
    max_steps=MAX_STEPS if MAX_STEPS > 0 else -1,
    logging_steps=1,
    optim="adamw_8bit",
    fp16=not torch.cuda.is_bf16_supported(),
    bf16=torch.cuda.is_bf16_supported(),
    report_to="none",
)
trainer = DPOTrainer(
    model=model,
    ref_model=None,
    tokenizer=tokenizer,
    train_dataset=train_ds,
    args=args,
    beta=0.1,
    max_length=MAX_SEQ_LENGTH,
    max_prompt_length=MAX_SEQ_LENGTH // 2,
)
trainer.train()
'''
    elif method == "vision":
        body = '''
from unsloth.trainer import UnslothVisionDataCollator
from trl import SFTTrainer, SFTConfig

FastVisionModel.for_training(model)
args = SFTConfig(
    output_dir=OUTPUT_DIR,
    per_device_train_batch_size=max(1, BATCH_SIZE),
    gradient_accumulation_steps=GRAD_ACCUM,
    warmup_steps=WARMUP_STEPS,
    num_train_epochs=EPOCHS,
    max_steps=MAX_STEPS if MAX_STEPS > 0 else 60,
    learning_rate=LEARNING_RATE,
    fp16=not torch.cuda.is_bf16_supported(),
    bf16=torch.cuda.is_bf16_supported(),
    logging_steps=1,
    optim="adamw_8bit",
    weight_decay=WEIGHT_DECAY,
    lr_scheduler_type=LR_SCHEDULER,
    remove_unused_columns=False,
    dataset_text_field="",
    dataset_kwargs={"skip_prepare_dataset": True},
    max_seq_length=MAX_SEQ_LENGTH,
    report_to="none",
)
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    data_collator=UnslothVisionDataCollator(model, tokenizer),
    train_dataset=raw_ds,
    args=args,
)
trainer.train()
'''
    else:
        body = '''
from trl import SFTTrainer
from transformers import TrainingArguments

if CHAT_TEMPLATE and CHAT_TEMPLATE != "alpaca":
    try:
        from unsloth.chat_templates import get_chat_template
        tokenizer = get_chat_template(tokenizer, chat_template=CHAT_TEMPLATE)
    except Exception:
        pass

EOS = tokenizer.eos_token or ""
PROMPT_TMPL = (
    "### Инструкция:\\n{instruction}\\n\\n"
    "### Входные данные:\\n{input}\\n\\n"
    "### Ответ:\\n{output}"
)

def _format_prompts(batch):
    texts = []
    if "conversations" in batch or "messages" in batch:
        col = batch.get("conversations") or batch.get("messages")
        for conv in col:
            try:
                texts.append(tokenizer.apply_chat_template(conv, tokenize=False, add_generation_prompt=False))
            except Exception:
                texts.append(str(conv) + EOS)
        return {"text": texts}
    insts = batch.get("instruction") or batch.get("prompt") or [""] * len(batch.get("output", []))
    inps = batch.get("input") or [""] * len(insts)
    outs = batch.get("output") or batch.get("response") or [""] * len(insts)
    for inst, inp, out in zip(insts, inps, outs):
        texts.append(PROMPT_TMPL.format(instruction=inst or "", input=inp or "", output=out or "") + EOS)
    return {"text": texts}

train_ds = raw_ds.map(_format_prompts, batched=True)

args = TrainingArguments(
    output_dir=OUTPUT_DIR,
    per_device_train_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    warmup_steps=WARMUP_STEPS,
    num_train_epochs=EPOCHS,
    max_steps=MAX_STEPS if MAX_STEPS > 0 else -1,
    learning_rate=LEARNING_RATE,
    fp16=not torch.cuda.is_bf16_supported(),
    bf16=torch.cuda.is_bf16_supported(),
    logging_steps=1,
    optim="adamw_8bit",
    weight_decay=WEIGHT_DECAY,
    lr_scheduler_type=LR_SCHEDULER,
    seed=3407,
    report_to="none",
)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=train_ds,
    dataset_text_field="text",
    max_seq_length=MAX_SEQ_LENGTH,
    dataset_num_proc=2,
    packing=PACKING,
    args=args,
)
trainer.train()
'''

    footer = '''
os.makedirs(OUTPUT_DIR, exist_ok=True)
lora_dir = os.path.join(OUTPUT_DIR, "lora_adapter")
model.save_pretrained(lora_dir)
tokenizer.save_pretrained(lora_dir)
print(f"[LocalHarness-Unsloth] Saved LoRA adapter to {lora_dir}", flush=True)

if EXPORT_GGUF and METHOD != "vision":
    gguf_dir = os.path.join(OUTPUT_DIR, "gguf")
    print(f"[LocalHarness-Unsloth] Exporting GGUF ({GGUF_QUANT}) to {gguf_dir} ...", flush=True)
    model.save_pretrained_gguf(gguf_dir, tokenizer, quantization_method=GGUF_QUANT)
    # Generate Ollama Modelfile alongside GGUF
    ggufs = [f for f in os.listdir(gguf_dir) if f.lower().endswith(".gguf")] if os.path.isdir(gguf_dir) else []
    if ggufs:
        with open(os.path.join(gguf_dir, "Modelfile"), "w", encoding="utf-8") as mf:
            mf.write(f"FROM ./{ggufs[0]}\\nPARAMETER temperature 0.4\\n")
    print(f"[LocalHarness-Unsloth] GGUF export finished: {gguf_dir}", flush=True)

print("[LocalHarness-Unsloth] TRAINING_COMPLETE", flush=True)
'''
    return header + body + footer


def _parse_metrics_line(line):
    updates = {}
    m_loss = re.search(r"['\"]loss['\"]\s*:\s*([0-9.eE\-]+)", line)
    if m_loss:
        try:
            updates["loss"] = round(float(m_loss.group(1)), 4)
        except ValueError:
            pass
    m_lr = re.search(r"['\"]learning_rate['\"]\s*:\s*([0-9.eE\-]+)", line)
    if m_lr:
        try:
            updates["lr"] = float(m_lr.group(1))
        except ValueError:
            pass
    m_ep = re.search(r"['\"]epoch['\"]\s*:\s*([0-9.]+)", line)
    if m_ep:
        try:
            updates["epoch"] = round(float(m_ep.group(1)), 2)
        except ValueError:
            pass
    m_step = re.search(r"(\d+)\s*/\s*(\d+)\s*\[", line)
    if m_step:
        try:
            updates["step"] = int(m_step.group(1))
            updates["max_steps"] = int(m_step.group(2))
        except ValueError:
            pass
    return updates


def job_status():
    with _LOCK:
        snap = {k: (list(v) if isinstance(v, list) else v) for k, v in _ACTIVE_JOB.items()}
    return {"ok": True, "job": snap, "history": _load_history()[:20]}


def stop_job():
    with _LOCK:
        proc = _ACTIVE_PROC.get("proc")
        if not proc or proc.poll() is not None:
            return {"ok": False, "error": "Нет активной задачи обучения"}
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _ACTIVE_JOB["status"] = "stopped"
        _ACTIVE_JOB["ended_at"] = time.time()
        _ACTIVE_JOB["lines"].append("[LocalHarness-Unsloth] Остановлено пользователем.")
        _ACTIVE_PROC["proc"] = None
    return job_status()


def _run_subprocess_job(cmd, job_meta, auto_import_lmstudio=False, thermal_guard=True):
    def _worker():
        rc = None
        err = ""
        last_temp_check = 0.0
        try:
            env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=NO_WINDOW,
            )
            with _LOCK:
                _ACTIVE_PROC["proc"] = proc

            for raw in proc.stdout:
                text = raw.decode("utf-8", "replace").replace("\r", "\n")
                for ln in text.split("\n"):
                    ln = ln.strip()
                    if not ln:
                        continue
                    metrics = _parse_metrics_line(ln)
                    with _LOCK:
                        _ACTIVE_JOB["lines"].append(ln[:400])
                        del _ACTIVE_JOB["lines"][:-200]
                        if metrics:
                            _ACTIVE_JOB.update(metrics)
                            if "loss" in metrics:
                                pt = {
                                    "step": _ACTIVE_JOB["step"] or (len(_ACTIVE_JOB["loss_history"]) + 1),
                                    "loss": metrics["loss"],
                                    "lr": metrics.get("lr"),
                                }
                                _ACTIVE_JOB["loss_history"].append(pt)
                                del _ACTIVE_JOB["loss_history"][:-300]

                # Periodic GPU thermal guard during training
                now = time.time()
                if thermal_guard and job_meta.get("kind") == "train" and (now - last_temp_check) >= 5.0:
                    last_temp_check = now
                    try:
                        cfg = config_mod.load()
                        th = thermal_mod.get_thermal_status(cfg, force=False)
                        with _LOCK:
                            _ACTIVE_JOB["gpu_temp"] = th.get("temp_c")
                        if th.get("status") == "critical" and cfg.get("gpu_thermal_enabled", True):
                            with _LOCK:
                                _ACTIVE_JOB["lines"].append(
                                    "[THERMAL GUARD] Критический перегрев GPU (%s°C) — останавливаю обучение для защиты железа!" % th.get("temp_c")
                                )
                                _ACTIVE_JOB["status"] = "stopped"
                            proc.terminate()
                            break
                    except Exception:
                        pass

            rc = proc.wait(timeout=60)
        except Exception as exc:
            err = str(exc)[:300]
            rc = -1

        gguf_files = []
        out_dir = job_meta.get("output_dir") or ""
        if out_dir and os.path.isdir(out_dir):
            for root, _, files in os.walk(out_dir):
                for fn in files:
                    if fn.lower().endswith(".gguf"):
                        gguf_files.append(os.path.join(root, fn))

        with _LOCK:
            _ACTIVE_PROC["proc"] = None
            was_stopped = _ACTIVE_JOB["status"] == "stopped"
            _ACTIVE_JOB["rc"] = rc
            _ACTIVE_JOB["ended_at"] = time.time()
            _ACTIVE_JOB["gguf_files"] = gguf_files
            if not was_stopped:
                _ACTIVE_JOB["status"] = "done" if rc == 0 else "failed"
            if err:
                _ACTIVE_JOB["error"] = err
            final_snap = dict(_ACTIVE_JOB)

        if job_meta.get("kind") == "train":
            hist = _load_history()
            hist.insert(0, {
                "id": final_snap["id"],
                "model": final_snap["model"],
                "method": final_snap["method"],
                "dataset": final_snap["dataset"],
                "status": final_snap["status"],
                "loss": final_snap["loss"],
                "step": final_snap["step"],
                "output_dir": final_snap["output_dir"],
                "gguf_files": gguf_files,
                "loss_history": list(final_snap.get("loss_history") or [])[-80:],
                "started_at": final_snap["started_at"],
                "ended_at": final_snap["ended_at"],
            })
            _save_history(hist)
            try:
                from . import notify as notify_mod
                st_txt = "завершено успешно!" if final_snap["status"] == "done" else ("остановлено" if final_snap["status"] == "stopped" else "ошибка")
                notify_mod.send(
                    "🧬 Unsloth обучение " + st_txt,
                    "%s (%s) · loss=%s" % (final_snap["model"], final_snap["method"].upper(), final_snap["loss"]),
                    cfg=config_mod.load(),
                )
            except Exception:
                pass
            if auto_import_lmstudio and gguf_files and final_snap["status"] == "done":
                try:
                    from . import lms as lms_mod
                    lms_mod.import_model(gguf_files[0], mode="copy", cfg=config_mod.load())
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True, name="lh-unsloth-job").start()


def install_unsloth_env():
    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["kind"]}
        base_py = sys.executable if (not getattr(sys, "frozen", False) and os.path.isfile(sys.executable)) else (shutil.which("python") or "python")
        vdir = venv_dir()
        vpy = os.path.join(vdir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(vdir, "bin", "python")
        install_script = (
            "import os, subprocess, sys; "
            f"vdir = {vdir!r}; vpy = {vpy!r}; "
            "print('[LocalHarness-Unsloth] Creating venv:', vdir, flush=True); "
            "if not os.path.isfile(vpy): subprocess.check_call([sys.executable, '-m', 'venv', vdir]); "
            "print('[LocalHarness-Unsloth] Upgrading pip...', flush=True); "
            "subprocess.check_call([vpy, '-m', 'pip', 'install', '--upgrade', 'pip']); "
            "print('[LocalHarness-Unsloth] Installing PyTorch CUDA + Unsloth...', flush=True); "
            "subprocess.check_call([vpy, '-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio', '--index-url', 'https://download.pytorch.org/whl/cu128']); "
            "subprocess.check_call([vpy, '-m', 'pip', 'install', 'unsloth', 'trl', 'peft', 'datasets', 'accelerate', 'bitsandbytes']); "
            "print('[LocalHarness-Unsloth] INSTALL_COMPLETE', flush=True)"
        )
        job_id = "install-" + uuid.uuid4().hex[:6]
        _ACTIVE_JOB.update({
            "id": job_id,
            "status": "running",
            "kind": "install",
            "model": "Unsloth + PyTorch CUDA (venv)",
            "method": "install",
            "dataset": "",
            "output_dir": vdir,
            "started_at": time.time(),
            "ended_at": None,
            "step": 0,
            "max_steps": 0,
            "epoch": 0.0,
            "loss": None,
            "lr": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": ["[LocalHarness-Unsloth] Запуск установки в " + vdir],
            "loss_history": [],
            "script_path": "",
            "gguf_files": [],
        })
    _run_subprocess_job([base_py, "-c", install_script], {"kind": "install", "output_dir": vdir}, thermal_guard=False)
    return job_status()


def start_training(params):
    params = dict(params or {})
    dataset_path = str(params.get("dataset_path") or "").strip()
    if not dataset_path:
        return {"ok": False, "error": "Укажите JSONL-датасет или HuggingFace датасет (hf://...)"}
    if not dataset_path.startswith("hf://"):
        if not os.path.isabs(dataset_path):
            cand = os.path.join(datasets_dir(), dataset_path)
            if os.path.isfile(cand):
                dataset_path = cand
        if not os.path.isfile(dataset_path):
            return {"ok": False, "error": "Файл датасета не найден: %s" % dataset_path}

    model_name = str(params.get("model") or "unsloth/Qwen3-4B-bnb-4bit").strip()
    method = str(params.get("method") or "sft").lower()
    run_id = "ft-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:5])
    out_dir = os.path.join(training_dir(), run_id)
    os.makedirs(out_dir, exist_ok=True)

    cfg = dict(params, model=model_name, dataset_path=dataset_path, output_dir=out_dir, method=method)
    script_text = generate_unsloth_script(cfg)
    script_path = os.path.join(out_dir, "train_unsloth.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script_text)

    if params.get("dry_run"):
        return {"ok": True, "dry_run": True, "script_path": script_path, "script": script_text, "output_dir": out_dir}

    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["id"]}
        _ACTIVE_JOB.update({
            "id": run_id,
            "status": "running",
            "kind": "train",
            "model": model_name,
            "method": method,
            "dataset": dataset_path if dataset_path.startswith("hf://") else os.path.basename(dataset_path),
            "output_dir": out_dir,
            "gguf_quant": str(params.get("gguf_quant") or "q4_k_m"),
            "started_at": time.time(),
            "ended_at": None,
            "step": 0,
            "max_steps": int(params.get("max_steps") or 60),
            "epoch": 0.0,
            "loss": None,
            "lr": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": ["[LocalHarness-Unsloth] Скрипт сохранён: " + script_path],
            "loss_history": [],
            "script_path": script_path,
            "gguf_files": [],
        })

    py = training_python()
    _run_subprocess_job(
        [py, script_path],
        {"kind": "train", "output_dir": out_dir},
        auto_import_lmstudio=bool(params.get("auto_import_lmstudio", False)),
        thermal_guard=bool(params.get("thermal_guard", True)),
    )
    return job_status()


def eval_adapter(adapter_dir, prompt, max_new_tokens=256):
    """Run a quick inference test on a trained Unsloth LoRA adapter directory."""
    adapter_dir = str(adapter_dir or "").strip()
    if os.path.isdir(os.path.join(adapter_dir, "lora_adapter")):
        adapter_dir = os.path.join(adapter_dir, "lora_adapter")
    if not adapter_dir or not os.path.isdir(adapter_dir):
        return {"ok": False, "error": "Папка LoRA-адаптера не найдена: %s" % adapter_dir}
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "Введите тестовый промпт"}

    code = (
        "import json, sys\n"
        f"adapter = {adapter_dir!r}\n"
        f"prompt = {prompt!r}\n"
        f"max_tok = {int(max_new_tokens or 256)}\n"
        "from unsloth import FastLanguageModel\n"
        "model, tokenizer = FastLanguageModel.from_pretrained(model_name=adapter, max_seq_length=2048, dtype=None, load_in_4bit=True)\n"
        "FastLanguageModel.for_inference(model)\n"
        "text = f'### Инструкция:\\n{prompt}\\n\\n### Входные данные:\\n\\n\\n### Ответ:\\n'\n"
        "inputs = tokenizer([text], return_tensors='pt').to('cuda')\n"
        "out = model.generate(**inputs, max_new_tokens=max_tok, use_cache=True)\n"
        "decoded = tokenizer.batch_decode(out, skip_special_tokens=True)[0]\n"
        "ans = decoded.split('### Ответ:')[-1].strip()\n"
        "print(json.dumps({'ok': True, 'answer': ans}, ensure_ascii=False))\n"
    )
    py = training_python()
    try:
        proc = subprocess.run([py, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, timeout=120, creationflags=NO_WINDOW)
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0 and out:
            for ln in reversed(out.splitlines()):
                if ln.startswith("{"):
                    return json.loads(ln)
        return {"ok": False, "error": (err or out or "Eval failed")[:400]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
