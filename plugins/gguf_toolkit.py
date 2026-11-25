"""Author Plugin: GGUF & llama.cpp Studio Toolkit for LocalHarness.

Provides AI agent and UI tools to inspect GGUF v2/v3 model headers & metadata without loading weights,
quantize GGUF models into 15 formats (Q4_K_M, Q5_K_M, IQ4_XS, Q8_0, etc.) via llama-quantize,
and benchmark token generation speed via llama-bench.
"""
import json
import os
import struct
import subprocess

from harness import unsloth_trainer as ut

NAME = "gguf_toolkit"
AUTHOR = "LocalHarness Core Team"
VERSION = "1.0"
DESC = "Авторский инструментарий GGUF и llama.cpp: быстрый инспектор заголовков .gguf, квантизатор (15 форматов) и бенчмарк скорости tok/s."

SETTINGS = [
    {"key": "default_quant", "label": "Формат квантизации по умолчанию", "type": "string", "default": "Q4_K_M"},
    {"key": "bench_threads", "label": "Потоки CPU для llama-bench", "type": "number", "default": 6},
    {"key": "bench_ngl", "label": "Слои на GPU (-ngl) для llama-bench", "type": "number", "default": 99},
]

# GGUF metadata value types
_GGUF_TYPES = {
    0: ("<B", 1),   # UINT8
    1: ("<b", 1),   # INT8
    2: ("<H", 2),   # UINT16
    3: ("<h", 2),   # INT16
    4: ("<I", 4),   # UINT32
    5: ("<i", 4),   # INT32
    6: ("<f", 4),   # FLOAT32
    7: ("<?", 1),   # BOOL
    10: ("<Q", 8),  # UINT64
    11: ("<q", 8),  # INT64
    12: ("<d", 8),  # FLOAT64
}


def _read_gguf_str(fh):
    raw_len = fh.read(8)
    if len(raw_len) < 8:
        raise EOFError("unexpected EOF reading string length")
    slen = struct.unpack("<Q", raw_len)[0]
    if slen > 2_000_000:
        fh.seek(slen, os.SEEK_CUR)
        return f"<large string {slen} bytes>"
    return fh.read(slen).decode("utf-8", "replace")


def _read_gguf_val(fh, vtype, depth=0):
    if vtype == 8:  # STRING
        return _read_gguf_str(fh)
    if vtype == 9:  # ARRAY
        hdr = fh.read(12)
        if len(hdr) < 12:
            raise EOFError("unexpected EOF reading array header")
        elem_type, count = struct.unpack("<IQ", hdr)
        if count > 32 or depth > 1:
            # Skip large arrays (e.g. tokenizer vocab) cleanly if fixed-width or string
            if elem_type in _GGUF_TYPES:
                fh.seek(_GGUF_TYPES[elem_type][1] * count, os.SEEK_CUR)
            elif elem_type == 8:
                for _ in range(count):
                    slen = struct.unpack("<Q", fh.read(8))[0]
                    fh.seek(slen, os.SEEK_CUR)
            else:
                raise ValueError("nested dynamic array")
            return f"<array[{count}]>"
        return [_read_gguf_val(fh, elem_type, depth + 1) for _ in range(count)]
    if vtype in _GGUF_TYPES:
        fmt, sz = _GGUF_TYPES[vtype]
        buf = fh.read(sz)
        if len(buf) < sz:
            raise EOFError("unexpected EOF reading scalar")
        return struct.unpack(fmt, buf)[0]
    raise ValueError(f"unknown GGUF value type {vtype}")


def _tool_inspect(cfg, args):
    path = str(args.get("gguf_path") or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"GGUF-файл не найден: {path}"}
    size_mb = round(os.path.getsize(path) / (1024 * 1024), 2)
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
            if magic != b"GGUF":
                return {"ok": False, "error": f"Файл не является GGUF (magic={magic!r})"}
            version = struct.unpack("<I", fh.read(4))[0]
            if version < 2:
                return {"ok": True, "path": path, "version": version, "size_mb": size_mb}
            tensor_count, kv_count = struct.unpack("<QQ", fh.read(16))
            meta = {}
            max_kv = min(int(kv_count), 120)
            for _ in range(max_kv):
                try:
                    key = _read_gguf_str(fh)
                    vtype = struct.unpack("<I", fh.read(4))[0]
                    val = _read_gguf_val(fh, vtype)
                    if not key.startswith("tokenizer.ggml.tokens") and not key.startswith("tokenizer.ggml.merges"):
                        meta[key] = val
                except Exception:
                    break
        arch = meta.get("general.architecture", "unknown")
        return {
            "ok": True,
            "path": path,
            "size_mb": size_mb,
            "gguf_version": version,
            "tensor_count": tensor_count,
            "kv_count": kv_count,
            "architecture": arch,
            "model_name": meta.get("general.name", os.path.basename(path)),
            "context_length": meta.get(f"{arch}.context_length"),
            "embedding_length": meta.get(f"{arch}.embedding_length"),
            "block_count": meta.get(f"{arch}.block_count"),
            "quantization_version": meta.get("general.quantization_version"),
            "file_type": meta.get("general.file_type"),
            "metadata": meta,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка чтения GGUF: {exc}"}


def _tool_quantize(cfg, args):
    p_settings = ((cfg or {}).get("plugin_settings") or {}).get(NAME) or {}
    in_path = str(args.get("input_gguf") or "").strip().strip('"')
    out_path = str(args.get("output_gguf") or "").strip().strip('"')
    qtype = str(args.get("quant_type") or p_settings.get("default_quant") or "Q4_K_M").strip().upper()
    return ut.quantize_gguf_standalone(in_path, output_gguf=out_path, quant_type=qtype)


def _tool_benchmark(cfg, args):
    p_settings = ((cfg or {}).get("plugin_settings") or {}).get(NAME) or {}
    gguf_path = str(args.get("gguf_path") or "").strip().strip('"')
    if not gguf_path or not os.path.isfile(gguf_path):
        return {"ok": False, "error": f"GGUF-файл не найден: {gguf_path}"}
    bench_bin = ut.find_llama_tool("llama-bench")
    if not bench_bin:
        return {"ok": False, "error": "Утилита llama-bench не найдена в lora_train_reference/llama.cpp"}
    threads = int(args.get("threads") or p_settings.get("bench_threads") or 6)
    ngl = int(args.get("ngl") if args.get("ngl") is not None else (p_settings.get("bench_ngl") or 99))
    cmd = [bench_bin, "-m", gguf_path, "-p", "128", "-n", "64", "-r", "1", "-t", str(threads), "-ngl", str(ngl), "-o", "json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=os.path.dirname(bench_bin),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "llama-bench failed")[-500:]}
        runs = json.loads(proc.stdout)
        summary = [
            {
                "test": "pp128" if r.get("n_prompt") else "tg64",
                "avg_ts": round(float(r.get("avg_ts") or 0.0), 2),
                "gpu_info": r.get("gpu_info", ""),
            }
            for r in (runs if isinstance(runs, list) else [])
        ]
        return {"ok": True, "gguf_path": gguf_path, "results": summary}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка запуска llama-bench: {exc}"}


TOOLS = [
    {
        "name": "gguf_inspect",
        "desc": "Мгновенно прочитать заголовок, архитектуру, контекст и метаданные GGUF-файла без загрузки в VRAM",
        "params": {
            "gguf_path": ("string", "путь к файлу .gguf"),
        },
        "required": ["gguf_path"],
        "fn": _tool_inspect,
    },
    {
        "name": "gguf_quantize",
        "desc": "Квантизировать GGUF-модель в формат Q4_K_M, Q5_K_M, Q8_0, IQ4_XS и др. через llama-quantize",
        "params": {
            "input_gguf": ("string", "путь к исходному .gguf файлу (например F16/BF16/Q8_0)"),
            "quant_type": ("string?", "тип квантизации: Q4_K_M, Q5_K_M, Q6_K, Q8_0, IQ4_XS, Q3_K_M"),
            "output_gguf": ("string?", "опциональный путь выходного .gguf файла"),
        },
        "required": ["input_gguf"],
        "fn": _tool_quantize,
    },
    {
        "name": "gguf_benchmark",
        "desc": "Замерить реальную скорость генерации токенов (tok/s) GGUF-модели через llama-bench",
        "params": {
            "gguf_path": ("string", "путь к файлу .gguf"),
            "threads": ("number?", "число потоков CPU (по умолчанию 6)"),
            "ngl": ("number?", "число слоёв на GPU (по умолчанию 99)"),
        },
        "required": ["gguf_path"],
        "fn": _tool_benchmark,
    },
]
