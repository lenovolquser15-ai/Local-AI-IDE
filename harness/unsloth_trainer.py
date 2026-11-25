"""Unsloth & Universal LoRA Fine-Tuning Studio 3.0 for LocalHarness:
- Dual Training Backends:
  1. Unsloth 4-bit QLoRA (SFT, GRPO Reasoning RL, DPO, Vision VLM)
  2. Universal Transformers + PEFT (NVIDIA CUDA, AMD ROCm, and multi-core CPU MKLDNN/AVX2 with OOM fallback,
     multi-turn ShareGPT & Alpaca assistant-only loss masking, LoRA merge_and_unload, and native GGUF pipeline)
- Dynamic Model Layer Inspector (0-VRAM safetensors/config/HF architecture -> target_modules checkboxes)
- HuggingFace Hub Search & Multi-Worker Model Downloader (data/models/)
- Standalone GGUF Quantizer powered by llama.cpp/llama-quantize.exe (all 15 K-quants & legacy quants) + llama-bench
- Smart CUDA/ROCm/CPU isolated venv installer (auto-detects exact CUDA version from nvidia-smi or AMD hipInfo)
- Dataset Studio: sessions export, AI synthetic generator, CSV/JSON/JSONL import, preview, HF Hub datasets
- Real-time Loss curve, step/sec speed, ETA, GPU thermal guard, and LoRA adapter eval playground.
"""
import csv
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

from . import config as config_mod
from . import session as session_mod
from . import thermal as thermal_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.RLock()

QUANT_TYPES = [
    {"id": "Q4_K_M", "label": "Q4_K_M — Рекомендуемый баланс размера и качества (4-5 бит)"},
    {"id": "Q5_K_M", "label": "Q5_K_M — Высокое качество, чуть крупнее (5-6 бит)"},
    {"id": "Q8_0",   "label": "Q8_0 — Почти без потерь (8 бит)"},
    {"id": "Q6_K",   "label": "Q6_K — Отличное качество, минимальные потери (6-7 бит)"},
    {"id": "Q4_K_S", "label": "Q4_K_S — Компактный и быстрый (4 бита)"},
    {"id": "Q5_K_S", "label": "Q5_K_S — Компактный 5-битный"},
    {"id": "Q3_K_M", "label": "Q3_K_M — Для слабых ПК / малого VRAM (3-4 бита)"},
    {"id": "Q3_K_L", "label": "Q3_K_L — Улучшенный 3-битный"},
    {"id": "Q3_K_S", "label": "Q3_K_S — Сверхкомпактный 3-битный"},
    {"id": "Q2_K",   "label": "Q2_K — Экстремальное сжатие (2.5-3 бита)"},
    {"id": "f16",    "label": "f16 — Без сжатия (16-бит оригинал)"},
    {"id": "Q4_0",   "label": "Q4_0 — Legacy 4-bit"},
    {"id": "Q4_1",   "label": "Q4_1 — Legacy 4-bit+"},
    {"id": "Q5_0",   "label": "Q5_0 — Legacy 5-bit"},
    {"id": "Q5_1",   "label": "Q5_1 — Legacy 5-bit+"},
]

DEFAULT_LORA_LAYERS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

ARCH_LAYERS_MAP = {
    "llama": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj", "lm_head"],
    "mistral": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj", "lm_head"],
    "qwen": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj", "lm_head"],
    "gemma": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    "phi": ["qkv_proj", "o_proj", "gate_up_proj", "down_proj", "q_proj", "k_proj", "v_proj"],
    "gpt2": ["c_attn", "c_proj", "c_fc"],
    "falcon": ["query_key_value", "dense", "dense_h_to_4h", "dense_4h_to_h"],
    "bloom": ["query_key_value", "dense", "dense_h_to_4h", "dense_4h_to_h"],
}

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
    "kind": "train",   # train | install | quantize | download
    "backend": "auto",
    "model": "",
    "method": "sft",
    "dataset": "",
    "output_dir": "",
    "gguf_quant": "Q4_K_M",
    "started_at": 0.0,
    "ended_at": None,
    "step": 0,
    "max_steps": 0,
    "epoch": 0.0,
    "loss": None,
    "lr": None,
    "speed_it_s": None,
    "eta_min": None,
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
    # Auto-seed dataset_Ami.jsonl from lora_train_reference if present and not yet copied
    ref_ds = os.path.join(config_mod.ROOT, "lora_train_reference", "dataset_Ami.jsonl")
    dst_ds = os.path.join(d, "dataset_Ami.jsonl")
    if os.path.isfile(ref_ds) and not os.path.isfile(dst_ds):
        try:
            shutil.copy2(ref_ds, dst_ds)
        except OSError:
            pass
    return d


def models_dir():
    d = os.path.join(config_mod.DATA_DIR, "models")
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


def find_llama_cpp_dir():
    """Locate bundled llama.cpp tools (from lora_train_reference/llama.cpp or data/llama.cpp)."""
    candidates = [
        os.path.join(config_mod.ROOT, "lora_train_reference", "llama.cpp"),
        os.path.join(config_mod.DATA_DIR, "llama.cpp"),
        os.path.join(config_mod.ROOT, "llama.cpp"),
    ]
    for c in candidates:
        if os.path.isdir(c) and (
            os.path.isfile(os.path.join(c, "llama-quantize.exe"))
            or os.path.isfile(os.path.join(c, "llama-quantize"))
        ):
            return c
    return None


def find_llama_tool(tool_name):
    """Find a specific llama.cpp binary (e.g. 'llama-quantize', 'llama-bench', 'ggml-rpc-server')."""
    ext = ".exe" if os.name == "nt" and not tool_name.lower().endswith(".exe") else ""
    fname = tool_name + ext
    ldir = find_llama_cpp_dir()
    if ldir:
        cand = os.path.join(ldir, fname)
        if os.path.isfile(cand):
            return cand
    vp = venv_python()
    if vp:
        vcand = os.path.join(os.path.dirname(vp), fname)
        if os.path.isfile(vcand):
            return vcand
    return shutil.which(fname) or shutil.which(tool_name)


def detect_cuda_wheel_index():
    """Detect optimal PyTorch index URL (cu128 / cu124 / cu121 / cu118 / rocm / cpu) like Project_V1.0.py."""
    if os.name == "nt":
        hip = shutil.which("hipInfo.exe")
        if hip:
            try:
                r = subprocess.run([hip], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
                if "device#" in r.stdout or "Name:" in r.stdout:
                    return {"vendor": "amd", "cuda_version": "ROCm", "index": "rocm6.2", "url": "https://download.pytorch.org/whl/nightly/rocm6.2"}
            except Exception:
                pass
    smi = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if smi:
        try:
            r = subprocess.run([smi], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
            m = re.search(r"CUDA\s*Version:\s*([0-9]+\.[0-9]+)", r.stdout or "")
            if m:
                cver = m.group(1)
                major_minor = tuple(int(x) for x in cver.split(".")[:2])
                if major_minor >= (12, 8):
                    idx = "cu128"
                elif major_minor >= (12, 4):
                    idx = "cu124"
                elif major_minor >= (12, 1):
                    idx = "cu121"
                else:
                    idx = "cu118"
                return {"vendor": "nvidia", "cuda_version": cver, "index": idx, "url": f"https://download.pytorch.org/whl/{idx}"}
        except Exception:
            pass
    return {"vendor": "cpu", "cuda_version": None, "index": "cpu", "url": "https://download.pytorch.org/whl/cpu"}


def inspect_model_layers(model_path_or_id):
    """Zero-VRAM inspection of linear layer names (target_modules) for a local folder or HF model ID."""
    target = str(model_path_or_id or "").strip()
    if not target:
        return {"ok": False, "error": "Укажите путь к папке модели или HuggingFace ID"}

    found_layers = set()
    arch = "AutoModelForCausalLM"
    source = "heuristic"

    # 1. Local directory inspection (safetensors index / safetensors header / config.json)
    cand_dir = target
    if not os.path.isdir(cand_dir):
        local_sub = os.path.join(models_dir(), target.replace("/", "_"))
        if os.path.isdir(local_sub):
            cand_dir = local_sub

    if os.path.isdir(cand_dir):
        cfg_file = os.path.join(cand_dir, "config.json")
        if os.path.isfile(cfg_file):
            try:
                cfg_data = json.loads(open(cfg_file, "r", encoding="utf-8").read())
                archs = cfg_data.get("architectures") or []
                arch = archs[0] if archs else str(cfg_data.get("model_type") or arch)
            except Exception:
                pass
        idx_file = os.path.join(cand_dir, "model.safetensors.index.json")
        if os.path.isfile(idx_file):
            try:
                wmap = (json.loads(open(idx_file, "r", encoding="utf-8").read()).get("weight_map") or {})
                for key in wmap.keys():
                    if key.endswith(".weight") and ("layers." in key or "h." in key or "block." in key or "lm_head" in key):
                        short = key.split(".")[-2]
                        if not short.isdigit() and "norm" not in short.lower() and "embed" not in short.lower():
                            found_layers.add(short)
                if found_layers:
                    source = "safetensors_index"
            except Exception:
                pass
        if not found_layers:
            for fn in sorted(os.listdir(cand_dir)):
                if fn.endswith(".safetensors"):
                    sf_path = os.path.join(cand_dir, fn)
                    try:
                        with open(sf_path, "rb") as fh:
                            hlen = struct.unpack("<Q", fh.read(8))[0]
                            if 0 < hlen < 10_000_000:
                                meta = json.loads(fh.read(hlen).decode("utf-8", "replace"))
                                for key in meta.keys():
                                    if key == "__metadata__":
                                        continue
                                    if key.endswith(".weight") and ("layers." in key or "h." in key or "lm_head" in key):
                                        short = key.split(".")[-2]
                                        if not short.isdigit() and "norm" not in short.lower() and "embed" not in short.lower():
                                            found_layers.add(short)
                        if found_layers:
                            source = "safetensors_header"
                            break
                    except Exception:
                        pass

    # 2. Match by architecture / model name
    if not found_layers:
        low = (arch + " " + target).lower()
        for k, mods in ARCH_LAYERS_MAP.items():
            if k in low:
                found_layers.update(mods)
                source = f"arch:{k}"
                break
    if not found_layers:
        found_layers.update(DEFAULT_LORA_LAYERS)

    ordered = sorted(found_layers, key=lambda x: (x not in DEFAULT_LORA_LAYERS, x))
    recommended = [x for x in ordered if x in DEFAULT_LORA_LAYERS] or ordered[:4]
    return {
        "ok": True,
        "model": target,
        "architecture": arch,
        "source": source,
        "layers": ordered,
        "recommended": recommended,
    }


def list_local_models():
    """List downloaded HF models in data/models and GGUF files in data/training & LM Studio."""
    mdir = models_dir()
    hf_dirs = []
    for name in sorted(os.listdir(mdir)):
        full = os.path.join(mdir, name)
        if os.path.isdir(full):
            size_mb = 0.0
            for r, _, files in os.walk(full):
                for fn in files:
                    try:
                        size_mb += os.path.getsize(os.path.join(r, fn)) / (1024 * 1024)
                    except OSError:
                        pass
            hf_dirs.append({"name": name, "path": full, "size_gb": round(size_mb / 1024.0, 2)})

    ggufs = []
    for scan_root in (training_dir(), mdir):
        for r, _, files in os.walk(scan_root):
            for fn in files:
                if fn.lower().endswith(".gguf"):
                    fp = os.path.join(r, fn)
                    try:
                        ggufs.append({"name": fn, "path": fp, "size_gb": round(os.path.getsize(fp) / (1024 ** 3), 2)})
                    except OSError:
                        pass
    return {"ok": True, "hf_models": hf_dirs, "gguf_files": ggufs}


def search_hf_models(query, min_size="", max_size="", limit=25):
    """Search HuggingFace Hub models (like Tab 3 of Project_V1.0.py)."""
    q = str(query or "").strip()
    limit = max(1, min(60, int(limit or 25)))
    param_filter = None
    min_s = str(min_size or "").strip()
    max_s = str(max_size or "").strip()
    if min_s and min_s != "Любой" and max_s and max_s != "Любой":
        param_filter = f"min:{min_s},max:{max_s}"
    elif min_s and min_s != "Любой":
        param_filter = f"min:{min_s}"
    elif max_s and max_s != "Любой":
        param_filter = f"max:{max_s}"

    try:
        from huggingface_hub import HfApi
        api = HfApi()
        kw = {"search": q or "Instruct", "sort": "downloads", "limit": limit}
        if param_filter:
            kw["num_parameters"] = param_filter
        models = api.list_models(**kw)
        out = []
        for m in models:
            mid = getattr(m, "id", None) or getattr(m, "modelId", "")
            if not mid:
                continue
            out.append({
                "id": mid,
                "downloads": getattr(m, "downloads", 0) or 0,
                "likes": getattr(m, "likes", 0) or 0,
                "pipeline_tag": getattr(m, "pipeline_tag", "") or "",
                "gguf": any("gguf" in str(t).lower() for t in (getattr(m, "tags", None) or [])),
            })
        return {"ok": True, "query": q, "results": out}
    except Exception:
        # Lightweight HTTP REST fallback to huggingface.co/api/models
        try:
            url = "https://huggingface.co/api/models?" + urllib.parse.urlencode({
                "search": q or "Instruct",
                "sort": "downloads",
                "direction": "-1",
                "limit": limit,
            })
            req = urllib.request.Request(url, headers={"User-Agent": "LocalHarness/1.0.8"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows = json.loads(resp.read().decode("utf-8", "replace"))
            out = [
                {
                    "id": r.get("id") or r.get("modelId"),
                    "downloads": r.get("downloads", 0),
                    "likes": r.get("likes", 0),
                    "pipeline_tag": r.get("pipeline_tag", ""),
                    "gguf": "gguf" in str(r.get("tags") or "").lower(),
                }
                for r in rows if isinstance(r, dict) and (r.get("id") or r.get("modelId"))
            ]
            return {"ok": True, "query": q, "results": out}
        except Exception as exc:
            return {"ok": False, "error": "Ошибка поиска HuggingFace: %s" % exc, "results": []}


def download_hf_model(repo_id):
    """Download a HuggingFace model snapshot into data/models/<repo> in the background."""
    repo_id = str(repo_id or "").strip()
    if not repo_id or "/" not in repo_id:
        return {"ok": False, "error": "Укажите корректный HuggingFace ID (например Qwen/Qwen2.5-3B-Instruct)"}
    safe_folder = re.sub(r"[^A-Za-z0-9_.\-]", "_", repo_id)
    dest = os.path.join(models_dir(), safe_folder)

    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["kind"]}
        job_id = "dl-" + uuid.uuid4().hex[:6]
        _ACTIVE_JOB.update({
            "id": job_id,
            "status": "running",
            "kind": "download",
            "backend": "hf_hub",
            "model": repo_id,
            "method": "download",
            "dataset": "",
            "output_dir": dest,
            "started_at": time.time(),
            "ended_at": None,
            "step": 0,
            "max_steps": 1,
            "epoch": 0.0,
            "loss": None,
            "lr": None,
            "speed_it_s": None,
            "eta_min": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": [f"[HF-Download] Скачивание {repo_id} -> {dest} ..."],
            "loss_history": [],
            "script_path": "",
            "gguf_files": [],
        })

    dl_code = (
        "import os\n"
        "from huggingface_hub import snapshot_download\n"
        f"repo = {repo_id!r}\n"
        f"dest = {dest!r}\n"
        "try:\n"
        "    import hf_transfer; os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'\n"
        "except Exception:\n"
        "    pass\n"
        "print(f'[HF-Download] Загрузка файлов модели {repo} (8 потоков)...', flush=True)\n"
        "path = snapshot_download(\n"
        "    repo_id=repo,\n"
        "    local_dir=dest,\n"
        "    max_workers=8,\n"
        "    allow_patterns=['*.json', '*.model', '*.safetensors', '*.bin', '*.gguf', '*.txt', '*.tiktoken'],\n"
        "    ignore_patterns=['*.h5', '*.ot', '*.msgpack'],\n"
        ")\n"
        "print(f'[HF-Download] Готово! Модель сохранена в: {path}', flush=True)\n"
    )
    py = training_python()
    _run_subprocess_job([py, "-c", dl_code], {"kind": "download", "output_dir": dest}, thermal_guard=False)
    return job_status()


def quantize_gguf_standalone(input_gguf, output_gguf="", quant_type="Q4_K_M"):
    """Standalone GGUF quantization via bundled llama.cpp/llama-quantize.exe (Tab 4 from Project_V1.0.py)."""
    src = str(input_gguf or "").strip().strip('"')
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "Исходный .gguf файл не найден: %s" % src}
    qtype = str(quant_type or "Q4_K_M").strip()
    valid_ids = {q["id"].upper(): q["id"] for q in QUANT_TYPES}
    qtype = valid_ids.get(qtype.upper(), qtype)

    quant_exe = find_llama_tool("llama-quantize")
    if not quant_exe:
        return {"ok": False, "error": "llama-quantize.exe не найден (проверьте папку lora_train_reference/llama.cpp)"}

    if not output_gguf:
        stem = re.sub(r"[-_.]?(f16|fp16|bf16|q\d[_a-z0-9]*)$", "", os.path.splitext(os.path.basename(src))[0], flags=re.I)
        output_gguf = os.path.join(os.path.dirname(src), f"{stem}-{qtype}.gguf")
    dst = str(output_gguf).strip().strip('"')
    if not dst.lower().endswith(".gguf"):
        dst += ".gguf"
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["kind"]}
        job_id = "quant-" + uuid.uuid4().hex[:6]
        _ACTIVE_JOB.update({
            "id": job_id,
            "status": "running",
            "kind": "quantize",
            "backend": "llama.cpp",
            "model": os.path.basename(src),
            "method": qtype,
            "dataset": "",
            "output_dir": os.path.dirname(os.path.abspath(dst)),
            "gguf_quant": qtype,
            "started_at": time.time(),
            "ended_at": None,
            "step": 0,
            "max_steps": 1,
            "epoch": 0.0,
            "loss": None,
            "lr": None,
            "speed_it_s": None,
            "eta_min": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": [
                f"[llama-quantize] Бинарник: {quant_exe}",
                f"[llama-quantize] Вход: {src}",
                f"[llama-quantize] Выход ({qtype}): {dst}",
            ],
            "loss_history": [],
            "script_path": "",
            "gguf_files": [],
        })

    _run_subprocess_job([quant_exe, src, dst, qtype], {"kind": "quantize", "output_dir": os.path.dirname(os.path.abspath(dst))}, thermal_guard=False)
    return job_status()


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
    params_b = _guess_params_b(model_id)
    base_4bit = params_b * 0.62
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
            note = "Высокий риск OOM: уменьши batch_size, max_seq_length или выбери модель 3B–4B (либо движок Universal PEFT с CPU fallback)"
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
        "gguf_quant": "Q4_K_M" if vram_gb <= 10 else "Q8_0",
    })
    est = estimate_training_vram(model_id, rec["max_seq_length"], rec["batch_size"], rec["lora_r"], method)
    return {"ok": True, "gpu_vram_gb": round(vram_gb, 1), "recommended": rec, "estimate": est}


def env_status(force=False):
    py = training_python()
    using_venv = bool(venv_python())
    gpu = thermal_mod.read_gpu_sensors(force=force)
    vram_gb = round((gpu.get("vram_total_mb") or 0) / 1024.0, 1)
    accel = detect_cuda_wheel_index()
    quant_exe = find_llama_tool("llama-quantize")

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
    peft_ready = bool(pkgs.get("torch") and pkgs.get("transformers") and pkgs.get("peft"))
    if ready:
        hint = "Окружение полностью готово: Unsloth CUDA 4-bit QLoRA + Universal PEFT + llama-quantize!"
    elif peft_ready:
        hint = "Готов Universal PEFT (CPU/CUDA/ROCm). Для 2× ускорения 4-bit на NVIDIA нажми «Установить Unsloth + CUDA»."
    elif not gpu.get("has_gpu") and accel["vendor"] == "cpu":
        hint = "NVIDIA/AMD GPU не найдена — можно обучать небольшие модели на CPU через движок Universal PEFT."
    else:
        hint = f"Обнаружен драйвер {accel['vendor'].upper()} ({accel.get('cuda_version') or ''}). Нажми «Установить Unsloth + PyTorch ({accel['index']})» для подготовки venv."

    return {
        "ok": True,
        "ready": ready,
        "peft_ready": peft_ready,
        "python": py,
        "using_venv": using_venv,
        "gpu_name": gpu.get("gpu_name") or "",
        "has_gpu": bool(gpu.get("has_gpu")),
        "vram_gb": vram_gb,
        "gpu_temp": gpu.get("temp_c"),
        "detected_wheel": accel,
        "llama_quantize": quant_exe or "",
        "packages": pkgs,
        "hint": hint,
        "presets": PRESET_MODELS,
        "hf_datasets": PRESET_HF_DATASETS,
        "quant_types": QUANT_TYPES,
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
            fmt = "alpaca"
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    if not s:
                        continue
                    if count == 0:
                        try:
                            first = json.loads(s)
                            if "conversations" in first or "conversation" in first or "messages" in first:
                                fmt = "sharegpt"
                        except Exception:
                            pass
                    count += 1
            rows.append({
                "name": fname,
                "path": full,
                "rows": count,
                "format": fmt,
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


def generate_peft_universal_script(cfg):
    """Generate Universal Transformers + PEFT training script (CUDA / AMD ROCm / CPU fallback +
    ShareGPT/Alpaca assistant-only loss masking + merge_and_unload + llama-quantize GGUF pipeline)."""
    model_name = str(cfg.get("model") or "Qwen/Qwen2.5-1.5B-Instruct").strip()
    local_cand = os.path.join(models_dir(), model_name.replace("/", "_"))
    if os.path.isdir(local_cand):
        model_name = local_cand
    dataset_path = str(cfg.get("dataset_path") or "").strip()
    output_dir = str(cfg.get("output_dir") or os.path.join(training_dir(), "run_out")).strip()
    max_seq = int(cfg.get("max_seq_length") or 1024)
    lora_r = int(cfg.get("lora_r") or 16)
    lora_alpha = int(cfg.get("lora_alpha") or lora_r * 2)
    lora_dropout = float(cfg.get("lora_dropout") or 0.05)
    epochs = max(1, int(float(cfg.get("epochs") or 1)))
    max_steps = int(cfg.get("max_steps") or 0)
    lr = float(cfg.get("learning_rate") or 5e-5)
    batch_size = max(1, int(cfg.get("batch_size") or 2))
    grad_accum = max(1, int(cfg.get("grad_accum") or 4))
    target_modules = list(cfg.get("target_modules") or DEFAULT_LORA_LAYERS)
    export_gguf = bool(cfg.get("export_gguf", True))
    gguf_quant = str(cfg.get("gguf_quant") or "Q4_K_M").strip()
    quant_exe = find_llama_tool("llama-quantize") or ""

    return f'''# Auto-generated by LocalHarness Studio 3.0 (Universal PEFT: CUDA / ROCm / CPU + LoRA Merge + GGUF)
import gc
import json
import math
import multiprocessing
import os
import subprocess
import sys
import time
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import LoraConfig, get_peft_model, PeftModel, TaskType

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
MODEL_PATH = {model_name!r}
DATASET_PATH = {dataset_path!r}
OUTPUT_DIR = {output_dir!r}
LORA_DIR = os.path.join(OUTPUT_DIR, "lora_adapter")
MERGED_DIR = os.path.join(OUTPUT_DIR, "merged_f16")
MAX_LEN = {max_seq}
LORA_R = {lora_r}
LORA_ALPHA = {lora_alpha}
DROPOUT = {lora_dropout}
EPOCHS = {epochs}
MAX_STEPS = {max_steps}
LR = {lr}
BATCH_SIZE = {batch_size}
GRAD_ACCUM = {grad_accum}
TARGET_MODULES = {target_modules!r}
EXPORT_GGUF = {export_gguf!r}
GGUF_QUANT = {gguf_quant!r}
QUANT_EXE = {quant_exe!r}

device = "cuda" if torch.cuda.is_available() else "cpu"
if device == "cuda":
    try:
        t = torch.randn(8, 8, device="cuda")
        del t
        torch.cuda.empty_cache()
        if getattr(torch.version, "cuda", None) is None:
            os.environ["PYTORCH_ROCM_ALLOC_CONF"] = "garbage_collection_threshold:0.8"
            print("[Universal-PEFT] Устройство: AMD ROCm GPU", flush=True)
        else:
            print(f"[Universal-PEFT] Устройство: NVIDIA CUDA ({{torch.cuda.get_device_name(0)}})", flush=True)
    except Exception as e:
        print(f"[Universal-PEFT] GPU недоступен ({{e}}), переключаюсь на CPU", flush=True)
        device = "cpu"

if device == "cpu":
    cores = max(1, multiprocessing.cpu_count())
    torch.set_num_threads(cores)
    os.environ["OMP_NUM_THREADS"] = str(cores)
    os.environ["MKL_NUM_THREADS"] = str(cores)
    torch.backends.mkldnn.enabled = True
    print(f"[Universal-PEFT] Устройство: CPU ({{cores}} потоков, MKLDNN включён)", flush=True)

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

dtype = torch.float16 if device == "cuda" else torch.float32
try:
    model = AutoModelForCausalLM.from_pretrained(MODEL_PATH, torch_dtype=dtype, low_cpu_mem_usage=True, trust_remote_code=True).to(device)
except Exception as e:
    if device == "cuda":
        print(f"[Universal-PEFT] Не хватило VRAM ({{e}}) -> откат на CPU!", flush=True)
        device = "cpu"
        dtype = torch.float32
        torch.cuda.empty_cache()
        model = AutoModelForCausalLM.from_pretrained(MODEL_PATH, torch_dtype=dtype, low_cpu_mem_usage=True, trust_remote_code=True).to(device)
    else:
        raise

model.config.use_cache = False
try:
    model.gradient_checkpointing_enable()
except Exception:
    pass

lora_cfg = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=DROPOUT,
    target_modules=TARGET_MODULES,
    bias="none",
    task_type=TaskType.CAUSAL_LM,
)
model = get_peft_model(model, lora_cfg)

class SmartChatDataset(Dataset):
    """Supports multi-turn ShareGPT and Alpaca JSONL with assistant-only loss masking (-100 on prompt)."""
    def __init__(self, path, tok, max_len):
        self.samples = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                convs = row.get("conversations") or row.get("conversation") or row.get("messages")
                if isinstance(convs, list) and convs:
                    prompt_parts = []
                    sys_msg = row.get("system") or ""
                    if sys_msg:
                        prompt_parts.append(f"<|im_start|>system\\n{{sys_msg}}<|im_end|>")
                    ans_text = ""
                    for m in convs:
                        role = str(m.get("from") or m.get("role") or "").lower()
                        val = str(m.get("value") or m.get("content") or "").strip()
                        if role == "system":
                            prompt_parts.append(f"<|im_start|>system\\n{{val}}<|im_end|>")
                        elif role in ("human", "user"):
                            prompt_parts.append(f"<|im_start|>user\\n{{val}}<|im_end|>")
                        elif role in ("gpt", "assistant") and val:
                            ans_text = val
                            break
                    if prompt_parts and ans_text:
                        p_str = "\\n".join(prompt_parts) + "\\n<|im_start|>assistant\\n"
                        full_str = p_str + ans_text + "<|im_end|>"
                        self.samples.append((p_str, full_str))
                else:
                    inst = str(row.get("instruction") or row.get("prompt") or "").strip()
                    inp = str(row.get("input") or "").strip()
                    out = str(row.get("output") or row.get("response") or "").strip()
                    if out:
                        if inp:
                            p_str = f"<|im_start|>system\\n{{inst}}<|im_end|>\\n<|im_start|>user\\n{{inp}}<|im_end|>\\n<|im_start|>assistant\\n"
                        else:
                            p_str = f"<|im_start|>user\\n{{inst}}<|im_end|>\\n<|im_start|>assistant\\n"
                        full_str = p_str + out + "<|im_end|>"
                        self.samples.append((p_str, full_str))
        self.tok = tok
        self.max_len = max_len
        print(f"[Universal-PEFT] Загружено обучающих пар: {{len(self.samples)}}", flush=True)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        p_str, full_str = self.samples[idx]
        enc = self.tok(full_str, truncation=True, max_length=self.max_len, padding="max_length", return_tensors="pt")
        p_ids = self.tok(p_str, truncation=True, max_length=self.max_len, add_special_tokens=False)["input_ids"]
        input_ids = enc["input_ids"].squeeze(0)
        attn = enc["attention_mask"].squeeze(0)
        labels = input_ids.clone()
        mask_upto = min(len(p_ids), self.max_len - 1)
        labels[:mask_upto] = -100
        labels[attn == 0] = -100
        return {{"input_ids": input_ids, "attention_mask": attn, "labels": labels}}

ds = SmartChatDataset(DATASET_PATH, tokenizer, MAX_LEN)
if len(ds) == 0:
    print("[ERROR] В датасете не найдено валидных примеров!", flush=True)
    sys.exit(2)

loader = DataLoader(ds, batch_size=BATCH_SIZE, shuffle=True, pin_memory=(device == "cuda"))
total_batches = len(loader) * EPOCHS
target_updates = MAX_STEPS if MAX_STEPS > 0 else max(1, math.ceil(total_batches / GRAD_ACCUM))

optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=0.01)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, target_updates))
scaler = torch.amp.GradScaler("cuda", enabled=(device == "cuda" and dtype == torch.float16))

model.train()
update_step = 0
micro_step = 0
accum_loss = 0.0
t0 = time.time()
optimizer.zero_grad()

print(f"[Universal-PEFT] Старт обучения: целевых шагов={{target_updates}} (grad_accum={{GRAD_ACCUM}})", flush=True)
done_training = False
for ep in range(EPOCHS):
    for batch in loader:
        micro_step += 1
        with torch.amp.autocast(device, enabled=(device == "cuda" and dtype == torch.float16)):
            out = model(
                input_ids=batch["input_ids"].to(device, non_blocking=True),
                attention_mask=batch["attention_mask"].to(device, non_blocking=True),
                labels=batch["labels"].to(device, non_blocking=True),
            )
            loss = out.loss / GRAD_ACCUM
        accum_loss += float(out.loss.item())
        if scaler.is_enabled():
            scaler.scale(loss).backward()
        else:
            loss.backward()

        if micro_step % GRAD_ACCUM == 0:
            if scaler.is_enabled():
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                scaler.step(optimizer)
                scaler.update()
            else:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            update_step += 1
            avg_l = accum_loss / GRAD_ACCUM
            accum_loss = 0.0
            elapsed = max(0.001, time.time() - t0)
            spd = update_step / elapsed
            eta_m = max(0.0, (target_updates - update_step) / max(1e-6, spd)) / 60.0
            cur_lr = scheduler.get_last_lr()[0]
            ep_flt = round(ep + (micro_step / max(1, len(loader))), 2)
            print(f"{{update_step}}/{{target_updates}} [loss] {{'loss': {{avg_l:.4f}}, 'learning_rate': {{cur_lr:.6g}}, 'epoch': {{ep_flt}}}} | Скорость: {{spd:.2f}} шаг/сек | Осталось: {{eta_m:.1f}}мин", flush=True)
            if update_step >= target_updates:
                done_training = True
                break
    if done_training:
        break

os.makedirs(LORA_DIR, exist_ok=True)
model.save_pretrained(LORA_DIR)
tokenizer.save_pretrained(LORA_DIR)
print(f"[Universal-PEFT] LoRA адаптер сохранён: {{LORA_DIR}}", flush=True)

del model
gc.collect()
if device == "cuda":
    torch.cuda.empty_cache()

if EXPORT_GGUF:
    print("[Universal-PEFT] Слияние LoRA с базовой моделью (merge_and_unload)...", flush=True)
    base = AutoModelForCausalLM.from_pretrained(MODEL_PATH, torch_dtype=torch.float16, low_cpu_mem_usage=True, trust_remote_code=True)
    merged = PeftModel.from_pretrained(base, LORA_DIR).merge_and_unload()
    pad_id = getattr(merged.config, "pad_token_id", None)
    if pad_id is None or (isinstance(pad_id, int) and pad_id < 0):
        eos_id = getattr(merged.config, "eos_token_id", 0)
        if isinstance(eos_id, (list, tuple)):
            eos_id = eos_id[0] if eos_id else 0
        merged.config.pad_token_id = eos_id if isinstance(eos_id, int) and eos_id >= 0 else 0
    os.makedirs(MERGED_DIR, exist_ok=True)
    merged.save_pretrained(MERGED_DIR, safe_serialization=True)
    tokenizer.save_pretrained(MERGED_DIR)
    del merged, base
    gc.collect()
    print(f"[Universal-PEFT] Слитая F16 модель сохранена: {{MERGED_DIR}}", flush=True)

    gguf_dir = os.path.join(OUTPUT_DIR, "gguf")
    os.makedirs(gguf_dir, exist_ok=True)
    f16_gguf = os.path.join(gguf_dir, "model-f16.gguf")
    converted = False
    try:
        from llama_cpp import convert_hf_to_gguf
        ok, res_msg = convert_hf_to_gguf(model_path_or_name=MERGED_DIR, output_dir=gguf_dir, output_filename="model-f16.gguf", outtype="f16")
        converted = bool(ok and os.path.isfile(f16_gguf))
    except Exception as e:
        print(f"[Universal-PEFT] Примечание: пакет llama_cpp convert_hf_to_gguf недоступен ({{e}}), слитая HF-модель остаётся в {{MERGED_DIR}}", flush=True)

    if converted and GGUF_QUANT.lower() != "f16" and QUANT_EXE and os.path.isfile(QUANT_EXE):
        q_gguf = os.path.join(gguf_dir, f"model-{{GGUF_QUANT}}.gguf")
        print(f"[Universal-PEFT] Квантование через llama-quantize.exe -> {{GGUF_QUANT}}...", flush=True)
        subprocess.run([QUANT_EXE, f16_gguf, q_gguf, GGUF_QUANT], check=False)

print("[LocalHarness-Unsloth] TRAINING_COMPLETE", flush=True)
'''


def generate_unsloth_script(cfg):
    """Generate training script: either Unsloth 4-bit QLoRA (default) or Universal PEFT when backend='peft_universal'."""
    backend = str(cfg.get("backend") or "unsloth").lower()
    if backend == "peft_universal":
        return generate_peft_universal_script(cfg)

    model_name = str(cfg.get("model") or "unsloth/Qwen3-4B-bnb-4bit").strip()
    local_cand = os.path.join(models_dir(), model_name.replace("/", "_"))
    if os.path.isdir(local_cand):
        model_name = local_cand
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
    gguf_quant = str(cfg.get("gguf_quant") or "q4_k_m").strip().lower()
    target_modules = list(cfg.get("target_modules") or DEFAULT_LORA_LAYERS)
    hf_limit = int(cfg.get("hf_limit") or 1000)

    is_hf_ds = dataset_path.startswith("hf://")
    hf_ds_name = dataset_path[5:] if is_hf_ds else ""

    ds_loader = (
        f'raw_ds = load_dataset({hf_ds_name!r}, split="train[:{hf_limit}]")'
        if is_hf_ds
        else 'raw_ds = load_dataset("json", data_files=DATASET_PATH, split="train")'
    )

    if method == "vision":
        model_loader = '''from unsloth import FastVisionModel
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
    target_modules=TARGET_MODULES,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=3407,
    use_rslora=USE_RSLORA,
)'''

    header = f'''# Auto-generated by LocalHarness Unsloth Fine-Tuning Studio 3.0
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

MODEL_NAME = {model_name!r}
DATASET_PATH = {dataset_path!r}
OUTPUT_DIR = {output_dir!r}
METHOD = {method!r}
CHAT_TEMPLATE = {chat_template!r}
MAX_SEQ_LENGTH = {max_seq}
LORA_R = {lora_r}
LORA_ALPHA = {lora_alpha}
LORA_DROPOUT = {lora_dropout}
TARGET_MODULES = {target_modules!r}
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
print(f"[LocalHarness-Unsloth] Target modules: {{TARGET_MODULES}}", flush=True)
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
    if "conversations" in batch or "conversation" in batch or "messages" in batch:
        col = batch.get("conversations") or batch.get("conversation") or batch.get("messages")
        for conv in col:
            norm = []
            for m in (conv or []):
                r = str(m.get("role") or m.get("from") or "user").lower()
                r = "user" if r in ("human", "user") else ("assistant" if r in ("gpt", "assistant") else "system")
                norm.append({"role": r, "content": str(m.get("content") or m.get("value") or "")})
            try:
                texts.append(tokenizer.apply_chat_template(norm, tokenize=False, add_generation_prompt=False))
            except Exception:
                texts.append("\\n".join(f"<|im_start|>{x['role']}\\n{x['content']}<|im_end|>" for x in norm) + EOS)
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
    m_spd = re.search(r"Скорость:\s*([0-9.]+)\s*шаг/сек", line)
    if m_spd:
        try:
            updates["speed_it_s"] = round(float(m_spd.group(1)), 2)
        except ValueError:
            pass
    else:
        m_tqdm_spd = re.search(r"([0-9.]+)\s*it/s", line)
        m_tqdm_sit = re.search(r"([0-9.]+)\s*s/it", line)
        if m_tqdm_spd:
            try:
                updates["speed_it_s"] = round(float(m_tqdm_spd.group(1)), 2)
            except ValueError:
                pass
        elif m_tqdm_sit:
            try:
                sec = float(m_tqdm_sit.group(1))
                if sec > 0:
                    updates["speed_it_s"] = round(1.0 / sec, 3)
            except ValueError:
                pass
    m_eta = re.search(r"Осталось:\s*([0-9.]+)\s*мин", line)
    if m_eta:
        try:
            updates["eta_min"] = round(float(m_eta.group(1)), 1)
        except ValueError:
            pass
    else:
        m_tqdm_eta = re.search(r"<(\d+):(\d{2})(?::(\d{2}))?", line)
        if m_tqdm_eta:
            try:
                if m_tqdm_eta.group(3) is not None:
                    secs = int(m_tqdm_eta.group(1)) * 3600 + int(m_tqdm_eta.group(2)) * 60 + int(m_tqdm_eta.group(3))
                else:
                    secs = int(m_tqdm_eta.group(1)) * 60 + int(m_tqdm_eta.group(2))
                updates["eta_min"] = round(secs / 60.0, 1)
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
            return {"ok": False, "error": "Нет активной задачи"}
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
        _ACTIVE_JOB["lines"].append("[LocalHarness] Задача остановлена пользователем.")
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
                        del _ACTIVE_JOB["lines"][:-220]
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

        if job_meta.get("kind") in ("train", "quantize"):
            hist = _load_history()
            hist.insert(0, {
                "id": final_snap["id"],
                "kind": job_meta.get("kind"),
                "backend": final_snap.get("backend", "unsloth"),
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
                    "🧬 Обучение / Квантование " + st_txt,
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
    """Create isolated data/unsloth_venv and install matching CUDA/ROCm/CPU PyTorch + Unsloth + PEFT + TRL."""
    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["kind"]}
        base_py = sys.executable if (not getattr(sys, "frozen", False) and os.path.isfile(sys.executable)) else (shutil.which("python") or "python")
        vdir = venv_dir()
        vpy = os.path.join(vdir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(vdir, "bin", "python")
        accel = detect_cuda_wheel_index()
        idx_url = accel["url"]
        vendor = accel["vendor"]

        install_script = (
            "import os, subprocess, sys\n"
            f"vdir = {vdir!r}; vpy = {vpy!r}; idx_url = {idx_url!r}; vendor = {vendor!r}\n"
            "print('[Installer] Создание venv:', vdir, flush=True)\n"
            "if not os.path.isfile(vpy): subprocess.check_call([sys.executable, '-m', 'venv', vdir])\n"
            "print('[Installer] Обновление pip...', flush=True)\n"
            "subprocess.check_call([vpy, '-m', 'pip', 'install', '--upgrade', 'pip'])\n"
            "print(f'[Installer] Установка PyTorch ({vendor}: {idx_url})...', flush=True)\n"
            "rc = subprocess.call([vpy, '-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio', '--index-url', idx_url])\n"
            "if rc != 0:\n"
            "    print('[Installer] Основной индекс не встал, пробуем стабильный cu124...', flush=True)\n"
            "    rc = subprocess.call([vpy, '-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio', '--index-url', 'https://download.pytorch.org/whl/cu124'])\n"
            "if rc != 0:\n"
            "    print('[Installer] Откат на CPU-сборку PyTorch...', flush=True)\n"
            "    subprocess.check_call([vpy, '-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio'])\n"
            "print('[Installer] Установка PEFT, TRL, Datasets, Accelerate, HuggingFace Hub, Safetensors...', flush=True)\n"
            "subprocess.check_call([vpy, '-m', 'pip', 'install', 'transformers>=4.56,<6', 'peft>=0.14,<1', 'trl', 'datasets', 'accelerate', 'huggingface_hub', 'safetensors', 'sentencepiece', 'protobuf', 'gguf'])\n"
            "if vendor == 'nvidia':\n"
            "    print('[Installer] Установка Unsloth + bitsandbytes для NVIDIA CUDA...', flush=True)\n"
            "    subprocess.call([vpy, '-m', 'pip', 'install', 'bitsandbytes', 'unsloth'])\n"
            "print('[LocalHarness-Unsloth] INSTALL_COMPLETE', flush=True)\n"
        )
        job_id = "install-" + uuid.uuid4().hex[:6]
        _ACTIVE_JOB.update({
            "id": job_id,
            "status": "running",
            "kind": "install",
            "backend": accel["index"],
            "model": f"Unsloth + PyTorch ({accel['index']})",
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
            "speed_it_s": None,
            "eta_min": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": [f"[Installer] Старт подготовки изолированного окружения ({accel['index']}) в {vdir}"],
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

    backend = str(params.get("backend") or "auto").lower()
    if backend == "auto":
        if dataset_path.startswith("hf://"):
            backend = "unsloth"
        else:
            env = env_status(force=False)
            backend = "unsloth" if env.get("ready") else ("peft_universal" if env.get("peft_ready") else "unsloth")

    model_name = str(params.get("model") or "unsloth/Qwen3-4B-bnb-4bit").strip()
    method = str(params.get("method") or "sft").lower()
    run_id = "ft-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:5])
    out_dir = os.path.join(training_dir(), run_id)
    os.makedirs(out_dir, exist_ok=True)

    cfg = dict(params, backend=backend, model=model_name, dataset_path=dataset_path, output_dir=out_dir, method=method)
    script_text = generate_unsloth_script(cfg)
    script_path = os.path.join(out_dir, "train_unsloth.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script_text)

    if params.get("dry_run"):
        return {"ok": True, "dry_run": True, "backend": backend, "script_path": script_path, "script": script_text, "output_dir": out_dir}

    with _LOCK:
        if _ACTIVE_JOB["status"] == "running":
            return {"ok": False, "error": "Уже выполняется задача: %s" % _ACTIVE_JOB["id"]}
        _ACTIVE_JOB.update({
            "id": run_id,
            "status": "running",
            "kind": "train",
            "backend": backend,
            "model": model_name,
            "method": method,
            "dataset": dataset_path if dataset_path.startswith("hf://") else os.path.basename(dataset_path),
            "output_dir": out_dir,
            "gguf_quant": str(params.get("gguf_quant") or "Q4_K_M"),
            "started_at": time.time(),
            "ended_at": None,
            "step": 0,
            "max_steps": int(params.get("max_steps") or 60),
            "epoch": 0.0,
            "loss": None,
            "lr": None,
            "speed_it_s": None,
            "eta_min": None,
            "gpu_temp": None,
            "rc": None,
            "error": "",
            "lines": [f"[Studio 3.0] Движок: {backend} · Скрипт сохранён: {script_path}"],
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
    adapter_dir = str(adapter_dir or "").strip()
    if os.path.isdir(os.path.join(adapter_dir, "lora_adapter")):
        adapter_dir = os.path.join(adapter_dir, "lora_adapter")
    if not adapter_dir or not os.path.isdir(adapter_dir):
        return {"ok": False, "error": "Папка LoRA-адаптера не найдена: %s" % adapter_dir}
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "Введите тестовый промпт"}

    code = (
        "import json, os, sys, torch\n"
        f"adapter = {adapter_dir!r}\n"
        f"prompt = {prompt!r}\n"
        f"max_tok = {int(max_new_tokens or 256)}\n"
        "try:\n"
        "    from unsloth import FastLanguageModel\n"
        "    model, tokenizer = FastLanguageModel.from_pretrained(model_name=adapter, max_seq_length=2048, dtype=None, load_in_4bit=True)\n"
        "    FastLanguageModel.for_inference(model)\n"
        "    dev = 'cuda'\n"
        "except Exception:\n"
        "    from peft import AutoPeftModelForCausalLM\n"
        "    from transformers import AutoTokenizer\n"
        "    dev = 'cuda' if torch.cuda.is_available() else 'cpu'\n"
        "    tokenizer = AutoTokenizer.from_pretrained(adapter)\n"
        "    model = AutoPeftModelForCausalLM.from_pretrained(adapter, torch_dtype=torch.float16 if dev=='cuda' else torch.float32).to(dev)\n"
        "    model.eval()\n"
        "text = f'<|im_start|>user\\n{prompt}<|im_end|>\\n<|im_start|>assistant\\n'\n"
        "inputs = tokenizer([text], return_tensors='pt').to(dev)\n"
        "with torch.no_grad():\n"
        "    out = model.generate(**inputs, max_new_tokens=max_tok, use_cache=True)\n"
        "decoded = tokenizer.batch_decode(out, skip_special_tokens=True)[0]\n"
        "ans = decoded.split('assistant')[-1].strip() if 'assistant' in decoded else decoded.strip()\n"
        "print(json.dumps({'ok': True, 'answer': ans}, ensure_ascii=False))\n"
    )
    py = training_python()
    try:
        proc = subprocess.run([py, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, timeout=150, creationflags=NO_WINDOW)
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0 and out:
            for ln in reversed(out.splitlines()):
                if ln.startswith("{"):
                    return json.loads(ln)
        return {"ok": False, "error": (err or out or "Eval failed")[:400]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
