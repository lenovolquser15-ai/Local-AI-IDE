"""LM Studio CLI helpers: disk/loaded model lists, server control, local import.

`lms ls --json` answers even when no model is loaded, so the UI can offer
downloaded models instead of an empty dropdown. Results are cached because a
cold CLI call wakes the LM Studio daemon and can take tens of seconds.
"""
import json
import os
import shutil
import subprocess
import threading
import time

from .config import load

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
OK_TTL = 30.0
ERR_TTL = 60.0

_CACHE = {"lock": threading.Lock(), "disk": None, "disk_at": 0.0, "disk_err": "",
          "ps": None, "ps_at": 0.0, "ps_err": ""}


def cli_path(cfg=None):
    cfg = load() if cfg is None else cfg
    prov = (cfg.get("providers") or {}).get(cfg.get("provider")) or {}
    cli = prov.get("lms_cli")
    if cli and os.path.isfile(cli):
        return cli
    home = os.path.expanduser("~")
    executable = "lms.exe" if os.name == "nt" else "lms"
    candidate = os.path.join(home, ".lmstudio", "bin", executable)
    if os.path.isfile(candidate):
        return candidate
    cli = shutil.which("lms")
    if cli:
        return cli

    # Probe only known install locations; never walk user/program directories.
    directories = []
    local = os.environ.get("LOCALAPPDATA")
    roots = [os.path.join(local, "Programs", "LM Studio"),
             os.path.join(local, "LM Studio")] if local else []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if root:
            roots.append(os.path.join(root, "LM Studio"))
    for root in roots:
        directories.extend((root, os.path.join(root, "bin"),
                            os.path.join(root, "resources", "app", ".webpack")))
    if os.name != "nt":
        for root in ("/Applications", os.path.join(home, "Applications")):
            directories.append(os.path.join(root, "LM Studio.app", "Contents",
                                            "Resources", "app", ".webpack"))
    for directory in directories:
        for name in (executable, "LM Studio CLI"):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def run_cli(args, cfg=None, timeout=30):
    cli = cli_path(cfg)
    if not cli:
        return None, "lms.exe не найден (config providers.lmstudio.lms_cli)"
    try:
        # Без явного DEVNULL консольное приложение без валидной консоли (трей, автозапуск)
        # падает на наследованном stdin с WinError 6.
        proc = subprocess.run([cli] + list(args), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace")
            return None, (out or err)[:500]
        return out, ""
    except subprocess.TimeoutExpired:
        return None, "lms %s: таймаут %s с (демон LM Studio не отвечает)" % (args[0] if args else "", timeout)
    except Exception as exc:
        return None, str(exc)[:300]


def json_cli(args, cfg=None, timeout=30):
    out, err = run_cli(args, cfg, timeout)
    if err:
        return [], err
    try:
        return json.loads(out), ""
    except ValueError:
        pass
    for i, ch in enumerate(out or ""):
        if ch in "[{":
            try:
                return json.loads(out[i:]), ""
            except ValueError:
                break
    return [], "lms вернул не JSON: %s" % (out or "")[:200]


def model_key(item):
    return str(item.get("modelKey") or item.get("path") or item.get("identifier") or "")


def disk_models(cfg=None, force=False, timeout=20):
    now = time.time()
    with _CACHE["lock"]:
        ttl = OK_TTL if not _CACHE["disk_err"] else ERR_TTL
        if not force and _CACHE["disk"] is not None and now - _CACHE["disk_at"] < ttl:
            return _CACHE["disk"], _CACHE["disk_err"]
    items, err = json_cli(["ls", "--json"], cfg, timeout)
    if not isinstance(items, list):
        items, err = [], err or "неожиданный ответ lms ls"
    with _CACHE["lock"]:
        _CACHE.update(disk=items, disk_at=time.time(), disk_err=err)
    return items, err


def loaded_models(cfg=None, force=False, timeout=20):
    now = time.time()
    with _CACHE["lock"]:
        ttl = OK_TTL if not _CACHE["ps_err"] else ERR_TTL
        if not force and _CACHE["ps"] is not None and now - _CACHE["ps_at"] < ttl:
            return _CACHE["ps"], _CACHE["ps_err"]
    items, err = json_cli(["ps", "--json"], cfg, timeout)
    if not isinstance(items, list):
        items, err = [], err or "неожиданный ответ lms ps"
    if not items and not err:
        http_items = _http_models(cfg)
        if http_items:
            items = http_items
    with _CACHE["lock"]:
        cache_ttl = OK_TTL if items else 4.0
        _CACHE.update(ps=items, ps_at=time.time() - (OK_TTL - cache_ttl), ps_err=err)
    return items, err


def _http_models(cfg=None):
    """Query LM Studio REST API for active models when lms ps is empty."""
    prov = ((cfg or {}).get("providers") or {}).get((cfg or {}).get("provider", "lmstudio")) or {}
    base_url = prov.get("base_url") or "http://127.0.0.1:1234/v1"
    root_url = base_url.rstrip("/")
    if root_url.endswith("/v1"):
        root_url = root_url[:-3].rstrip("/")

    import urllib.request
    # Prefer native LM Studio REST API (/api/v1/models or /api/v0/models) which reports
    # exact load state; /v1/models lists all disk models even when JIT-unloaded.
    for ep in ("/api/v1/models", "/api/v0/models"):
        try:
            req = urllib.request.Request(root_url + ep, headers={"User-Agent": "LocalHarness"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw = data.get("models") if isinstance(data.get("models"), list) else (data.get("data") or [])
                loaded_items = []
                try:
                    disk, _ = disk_models(cfg)
                    disk_map = {}
                    for d in (disk or []):
                        for k in ("modelKey", "path", "identifier", "indexedModelIdentifier"):
                            if d.get(k):
                                disk_map[str(d[k])] = d
                except Exception:
                    disk_map = {}

                for m in raw:
                    if not isinstance(m, dict):
                        continue
                    mid = m.get("id") or m.get("key") or m.get("modelKey")
                    mtype = str(m.get("type") or "").lower()
                    if not mid or mtype in ("embedding", "embeddings") or "embedding" in str(mid).lower():
                        continue
                    loaded_inst = m.get("loaded_instances")
                    if isinstance(loaded_inst, list):
                        is_loaded = len(loaded_inst) > 0
                    else:
                        is_loaded = m.get("state") == "loaded"
                    if not is_loaded:
                        continue
                    d_meta = disk_map.get(mid) or {}
                    inst_cfg = (loaded_inst[0].get("config") or {}) if (isinstance(loaded_inst, list) and loaded_inst and isinstance(loaded_inst[0], dict)) else {}
                    inst_id = (loaded_inst[0].get("id") if (isinstance(loaded_inst, list) and loaded_inst and isinstance(loaded_inst[0], dict)) else None) or mid
                    ctx_len = (m.get("loaded_context_length")
                               or inst_cfg.get("context_length")
                               or inst_cfg.get("contextLength")
                               or d_meta.get("maxContextLength")
                               or m.get("max_context_length"))
                    quant = d_meta.get("quantization") or m.get("quantization")
                    if not isinstance(quant, dict):
                        quant = {"name": str(quant or "")}
                    item = {
                        "type": "llm",
                        "identifier": inst_id,
                        "modelKey": d_meta.get("modelKey") or mid,
                        "path": d_meta.get("path") or mid,
                        "displayName": d_meta.get("displayName") or m.get("display_name") or m.get("name") or mid,
                        "architecture": m.get("arch") or m.get("architecture") or d_meta.get("architecture") or "",
                        "quantization": quant,
                        "sizeBytes": d_meta.get("sizeBytes") or m.get("size_bytes") or 0,
                        "paramsString": d_meta.get("paramsString") or m.get("params_string") or "",
                        "contextLength": ctx_len,
                        "maxContextLength": d_meta.get("maxContextLength") or m.get("max_context_length"),
                        "status": "loaded",
                        "vision": mtype == "vlm" or bool(m.get("vision")) or bool(d_meta.get("vision")) or "vision" in str(mid).lower(),
                        "trainedForToolUse": ("tool_use" in (m.get("capabilities") or [])
                                              or bool(m.get("trained_for_tool_use"))
                                              or bool(d_meta.get("trainedForToolUse"))),
                    }
                    loaded_items.append(item)
                return loaded_items
        except Exception:
            pass

    return []


def _match_alias(val, selected):
    if not val or not selected:
        return False
    if val == selected:
        return True
    return str(val).replace("\\", "/") == str(selected).replace("\\", "/")


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        value = int(value)
    except ValueError:
        return None
    return value if value > 0 else None


def _chat_model(item):
    kind = str(item.get("type") or "").lower()
    if kind:
        return kind in ("llm", "chat", "vlm", "vision")
    # Older CLI versions may omit type; reject embedding names in any alias.
    return not any("embedding" in str(item.get(key) or "").lower()
                   for key in ("identifier", "modelKey", "path", "id"))


def readiness(cfg):
    """None means unverified or inapplicable, not a missing model."""
    result = {"warnings": [], "loaded": None, "context_length": None}
    if cfg.get("provider") != "lmstudio":
        return result
    try:
        items, err = loaded_models(cfg)
    except Exception as exc:
        # Diagnostics must not prevent startup when CLI verification fails.
        items, err = [], str(exc) or type(exc).__name__
    if not err and (not isinstance(items, list) or
                    any(not isinstance(item, dict) or not model_key(item) for item in items)):
        err = "неожиданный ответ lms ps"
    if err:
        result["warnings"].append("LM Studio: проверка готовности модели недоступна: %s" % err)
        return result

    selected = cfg.get("model")
    if selected:
        # Prefer an API identifier, but accept either exact disk-model alias or normalized path.
        keys = ("identifier", "modelKey", "path", "id", "displayName", "indexedModelIdentifier")
        model = next((item for key in keys
                      for item in items if _match_alias(item.get(key), selected)), None)
    else:
        model = next((item for item in items if _chat_model(item)), None)
    result["loaded"] = model is not None
    if model is None:
        result["warnings"].append(
            "LM Studio: выбранная модель «%s» не загружена." % selected if selected else
            "LM Studio: не загружена ни одна чат-модель (embedding-модели не подходят).")
        return result

    config = model.get("config")
    config = config if isinstance(config, dict) else {}
    actual = _positive_int(model.get("contextLength")) or _positive_int(config.get("contextLength"))
    result["context_length"] = actual
    expected = max(_positive_int(cfg.get("context_tokens")) or 0,
                   _positive_int(cfg.get("lms_context_length")) or 0)
    if actual is not None and actual < expected:
        result["warnings"].append(
            "LM Studio: контекст модели «%s» — %s токенов, меньше настроенного %s "
            "(максимум context_tokens и lms_context_length)." %
            (selected or model_key(model), actual, expected))
    return result


def invalidate():
    with _CACHE["lock"]:
        _CACHE.update(disk_at=0.0, ps_at=0.0)


def server_start(cfg=None, timeout=40):
    out, err = run_cli(["server", "start"], cfg, timeout)
    return (err == ""), err or (out or "")[:300]


IMPORT_MODES = ("copy", "hard-link", "symbolic-link", "move")


def import_model(path, mode="copy", cfg=None):
    """Импорт локального GGUF через официальный lms import; если CLI старый — копия в models."""
    path = os.path.expanduser(str(path or "").strip().strip('"'))
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "нет такого файла: %s" % path}
    if os.path.getsize(path) > 60 * 1073741824:
        return {"ok": False, "error": "файл больше 60 ГБ"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".gguf", ".safetensors", ".bin", ".pt", ".pth"):
        return {"ok": False, "error": "ожидается файл модели (.gguf и т.п.), а не %s" % ext}
    mode = mode if mode in IMPORT_MODES else "copy"
    cli = cli_path(cfg)
    if cli:
        out, err = run_cli(["import", "--help"], cfg, timeout=20)
        if not err and "import" in (out or "").lower():
            flag = {"copy": ["-c"], "hard-link": ["-L"], "symbolic-link": ["-l"], "move": []}[mode]
            return {"ok": True, "job": True, "args": ["import", path, "-y"] + flag}
    settings = os.path.join(os.path.expanduser("~"), ".lmstudio", "settings.json")
    models_dir = os.path.join(os.path.expanduser("~"), ".lmstudio", "models")
    try:
        with open(settings, "r", encoding="utf-8") as fh:
            models_dir = json.load(fh).get("downloadsFolder") or models_dir
    except Exception:
        pass
    target_dir = os.path.join(models_dir, "local-imports", os.path.splitext(os.path.basename(path))[0])
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(path))
    if os.path.abspath(target) == os.path.abspath(path):
        return {"ok": True, "path": target, "note": "файл уже в папке моделей"}
    try:
        if mode == "move":
            import shutil
            shutil.move(path, target)
        elif mode == "hard-link":
            os.link(path, target)
        elif mode == "symbolic-link":
            os.symlink(path, target)
        else:
            import shutil
            shutil.copy2(path, target)
    except OSError as exc:
        return {"ok": False, "error": "импорт не удался: %s" % exc}
    invalidate()
    return {"ok": True, "path": target, "note": "файл в папке моделей; LM Studio увидит его после пересканирования"}


# ---------------------------------------------------------------------------
# Vulkan / CUDA / ROCm / Metal / CPU Runtime Manager & Advanced Model Loader
# ---------------------------------------------------------------------------

def lmstudio_home():
    override = os.environ.get("LMSTUDIO_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".lmstudio")


def default_vulkan_runtime_name():
    if os.name == "nt":
        return "llama.cpp-win-x86_64-vulkan-avx2"
    import sys
    if sys.platform == "darwin":
        return "llama.cpp-mac-arm64-apple-metal-advsimd"
    return "llama.cpp-linux-x86_64-vulkan-avx2"


def _version_tuple(ver):
    import re
    parts = re.findall(r"\d+", str(ver or ""))
    return tuple(int(p) for p in parts[:4]) if parts else (0,)


def _classify_runtime(name, manifest=None):
    low = str(name or "").lower()
    gpu_meta = (manifest or {}).get("gpu") if isinstance(manifest, dict) else {}
    fw = str((gpu_meta or {}).get("framework") or "").lower()
    if "vulkan" in low or "vulkan" in fw:
        return "vulkan"
    if "cuda" in low or "cuda" in fw:
        return "cuda"
    if "rocm" in low or "hip" in low or "rocm" in fw:
        return "rocm"
    if "metal" in low or "mlx" in low or "metal" in fw:
        return "metal"
    return "cpu"


def _runtime_label(kind, name, display_name=""):
    low = str(name or "").lower()
    if kind == "vulkan":
        return "Vulkan llama.cpp"
    if kind == "cuda":
        if "cuda13" in low:
            return "NVIDIA CUDA 13 llama.cpp"
        if "cuda12" in low:
            return "NVIDIA CUDA 12 llama.cpp"
        return "NVIDIA CUDA llama.cpp"
    if kind == "rocm":
        return "AMD ROCm llama.cpp"
    if kind == "metal":
        return "Apple Metal"
    return display_name or "CPU AVX2 llama.cpp"


def _read_backend_preferences(home=None):
    home = home or lmstudio_home()
    pref_path = os.path.join(home, ".internal", "backend-preferences-v1.json")
    try:
        with open(pref_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_runtimes(cfg=None):
    """Возвращает установленные и доступные движки LM Studio (Vulkan, CUDA 12, ROCm, CPU)."""
    home = lmstudio_home()
    prefs = _read_backend_preferences(home)
    active_name = ""
    active_version = ""
    for pref in prefs:
        if isinstance(pref, dict) and str(pref.get("model_format") or "gguf").lower() == "gguf":
            active_name = str(pref.get("name") or "")
            active_version = str(pref.get("version") or "")
            break

    raw_entries = []
    idx_path = os.path.join(home, ".internal", "internal-engine-index.json")
    if os.path.isfile(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as fh:
                idx_data = json.load(fh)
            arr = idx_data.get("json") if isinstance(idx_data, dict) else idx_data
            if isinstance(arr, list):
                raw_entries.extend(x for x in arr if isinstance(x, dict))
        except Exception:
            pass

    # Дополнительно сканируем extensions/backends на случай только что скачанного движка
    backends_dir = os.path.join(home, "extensions", "backends")
    if os.path.isdir(backends_dir):
        known_dirs = {os.path.normcase(str(e.get("dirPath") or "")) for e in raw_entries if e.get("dirPath")}
        try:
            for entry in os.scandir(backends_dir):
                if not entry.is_dir() or os.path.normcase(entry.path) in known_dirs:
                    continue
                mf_path = os.path.join(entry.path, "manifest.json")
                if not os.path.isfile(mf_path):
                    continue
                try:
                    with open(mf_path, "r", encoding="utf-8") as fh:
                        mf = json.load(fh)
                    if isinstance(mf, dict) and mf.get("name"):
                        raw_entries.append({
                            "manifest": mf,
                            "dirPath": entry.path,
                            "backendCompatibility": {"status": "compatible"},
                            "memoryInfo": {},
                            "hardwareSurveyResult": {},
                        })
                except Exception:
                    continue
        except Exception:
            pass

    grouped = {}
    for item in raw_entries:
        mf = item.get("manifest") if isinstance(item.get("manifest"), dict) else {}
        formats = [str(f).lower() for f in (mf.get("supported_model_formats") or ["gguf"])]
        if formats and "gguf" not in formats:
            continue
        name = str(mf.get("name") or "").strip()
        ver = str(mf.get("version") or "").strip()
        if not name:
            continue
        kind = _classify_runtime(name, mf)
        disp = ""
        dd = item.get("displayData")
        if isinstance(dd, list) and dd:
            first = dd[0]
            if isinstance(first, list) and len(first) >= 2 and isinstance(first[1], dict):
                disp = str(first[1].get("displayName") or "")
        compat_obj = item.get("backendCompatibility") if isinstance(item.get("backendCompatibility"), dict) else {}
        status = str(compat_obj.get("status") or "unknown")
        status_msg = str(compat_obj.get("message") or "")
        mem_info = item.get("memoryInfo") if isinstance(item.get("memoryInfo"), dict) else {}
        vram_gb = round(int(mem_info.get("vramCapacity") or 0) / (1024 ** 3), 2)
        gpus = []
        gpu_survey = (((item.get("hardwareSurveyResult") or {}).get("gpuSurveyResult") or {}).get("gpuInfo") or [])
        for g in (gpu_survey if isinstance(gpu_survey, list) else []):
            if isinstance(g, dict):
                gname = str(g.get("name") or g.get("deviceName") or "").strip()
                if gname and gname not in gpus:
                    gpus.append(gname)

        rec = grouped.setdefault(name, {
            "name": name,
            "kind": kind,
            "label": _runtime_label(kind, name, disp),
            "display_name": disp or _runtime_label(kind, name),
            "installed": True,
            "version": ver,
            "versions": [],
            "compatible": False,
            "status": status,
            "status_message": status_msg,
            "vram_gb": vram_gb,
            "gpus": gpus,
            "selected": False,
            "selected_version": "",
            "install_query": name,
        })
        if ver and ver not in rec["versions"]:
            rec["versions"].append(ver)

        # Предпочитаем совместимую (compatible) и самую свежую версию для сводки
        cur_score = (1 if rec["compatible"] else 0, _version_tuple(rec["version"]))
        new_score = (1 if status == "compatible" else 0, _version_tuple(ver))
        if new_score >= cur_score:
            rec["version"] = ver or rec["version"]
            rec["compatible"] = (status == "compatible")
            rec["status"] = status
            rec["status_message"] = status_msg
            if vram_gb > 0:
                rec["vram_gb"] = vram_gb
            if gpus:
                rec["gpus"] = gpus

    vulkan_pack = default_vulkan_runtime_name()
    has_any_vulkan = any(r["kind"] == "vulkan" for r in grouped.values())
    if not has_any_vulkan:
        grouped[vulkan_pack] = {
            "name": vulkan_pack,
            "kind": "vulkan",
            "label": "Vulkan llama.cpp",
            "display_name": "Vulkan llama.cpp (универсальное GPU-ускорение NVIDIA / AMD / Intel)",
            "installed": False,
            "version": "",
            "versions": [],
            "compatible": True,
            "status": "not_installed",
            "status_message": "Движок Vulkan ещё не установлен — скачайте его в 1 клик.",
            "vram_gb": 0.0,
            "gpus": [],
            "selected": False,
            "selected_version": "",
            "install_query": vulkan_pack,
        }

    for rec in grouped.values():
        rec["versions"].sort(key=_version_tuple, reverse=True)
        if active_name and rec["name"] == active_name:
            rec["selected"] = True
            rec["selected_version"] = active_version or rec["version"]

    # Порядок сортировки: выбранный -> Vulkan -> совместимые GPU -> остальные -> CPU
    kind_rank = {"vulkan": 0, "cuda": 1, "rocm": 2, "metal": 3, "cpu": 4}
    runtimes = sorted(
        grouped.values(),
        key=lambda r: (
            0 if r["selected"] else 1,
            kind_rank.get(r["kind"], 9),
            0 if r["compatible"] else 1,
            r["name"],
        ),
    )

    vulkan_installed = any(r["kind"] == "vulkan" and r["installed"] for r in runtimes)
    vulkan_active = any(r["kind"] == "vulkan" and r["selected"] for r in runtimes)
    return {
        "ok": True,
        "active_name": active_name,
        "active_version": active_version,
        "vulkan_pack": vulkan_pack,
        "vulkan_installed": vulkan_installed,
        "vulkan_active": vulkan_active,
        "runtimes": runtimes,
    }


def select_runtime(name, version=None, cfg=None):
    """Переключает активный GGUF runtime движок LM Studio (например на Vulkan)."""
    target_name = str(name or "").strip()
    if not target_name:
        return {"ok": False, "error": "не указано имя runtime-движка"}
    if target_name.lower() == "vulkan":
        target_name = default_vulkan_runtime_name()

    info = list_runtimes(cfg)
    matched = next((r for r in info["runtimes"] if r["name"] == target_name), None)
    resolved_ver = str(version or "").strip()
    if not resolved_ver and matched:
        resolved_ver = str(matched.get("version") or "")
    if matched and not matched.get("installed") and not resolved_ver:
        return {
            "ok": False,
            "need_install": True,
            "install_query": matched.get("install_query") or target_name,
            "error": "Движок «%s» ещё не установлен. Сначала нажмите «Скачать Vulkan»." % target_name,
        }

    home = lmstudio_home()
    internal_dir = os.path.join(home, ".internal")
    os.makedirs(internal_dir, exist_ok=True)
    pref_path = os.path.join(internal_dir, "backend-preferences-v1.json")
    existing = _read_backend_preferences(home)
    updated = [p for p in existing if isinstance(p, dict) and str(p.get("model_format") or "").lower() != "gguf"]
    entry = {"model_format": "gguf", "name": target_name}
    if resolved_ver:
        entry["version"] = resolved_ver
    updated.insert(0, entry)

    tmp_path = pref_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(updated, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, pref_path)
    except OSError as exc:
        return {"ok": False, "error": "не удалось записать backend-preferences-v1.json: %s" % exc}

    # Дополнительно пробуем штатный CLI (в некоторых сборках LM Studio он синхронизирует демон)
    try:
        sel_arg = "%s@%s" % (target_name, resolved_ver) if resolved_ver else target_name
        run_cli(["runtime", "select", sel_arg], cfg, timeout=8)
    except Exception:
        pass

    invalidate()
    fresh = list_runtimes(cfg)
    fresh["selected"] = entry
    return fresh


def install_or_update_runtime_args(query=None):
    """Возвращает аргументы CLI для скачивания/обновления движка (по умолчанию Vulkan)."""
    q = str(query or "").strip()
    if not q or q.lower() == "vulkan":
        q = default_vulkan_runtime_name()
    if q.startswith("-"):
        q = default_vulkan_runtime_name()
    return ["runtime", "get", q, "-y"]


def normalize_gpu_offload(value):
    """Нормализует параметр выгрузки на GPU в пару (cli_flag, float_ratio 0..1)."""
    if value is None:
        return "max", 1.0
    if isinstance(value, bool):
        return ("max", 1.0) if value else ("off", 0.0)
    raw = str(value).strip().lower()
    if raw in ("", "max", "auto", "full", "100%"):
        return "max", 1.0
    if raw in ("off", "none", "cpu", "0%"):
        return "off", 0.0
    if raw.endswith("%"):
        raw = raw[:-1].strip()
        try:
            pct = max(0.0, min(100.0, float(raw)))
            if pct <= 0.0:
                return "off", 0.0
            if pct >= 100.0:
                return "max", 1.0
            ratio = round(pct / 100.0, 2)
            return str(ratio), ratio
        except ValueError:
            return "max", 1.0
    try:
        num = float(raw)
    except ValueError:
        return "max", 1.0
    if num <= 0.0:
        return "off", 0.0
    if num > 1.0:
        # Передано в процентах (например 75 -> 0.75)
        if num >= 100.0:
            return "max", 1.0
        ratio = round(num / 100.0, 2)
        return str(ratio), ratio
    if num >= 0.995:
        return "max", 1.0
    ratio = round(num, 2)
    return str(ratio), ratio


def _resolve_model_config_path(model_path_or_key, cfg=None):
    raw_key = str(model_path_or_key or "").strip()
    if not raw_key:
        return None
    rel = ""
    try:
        disk, _ = disk_models(cfg)
        for item in (disk or []):
            if any(_match_alias(item.get(k), raw_key)
                   for k in ("modelKey", "path", "identifier", "indexedModelIdentifier", "displayName")):
                rel = str(item.get("path") or item.get("indexedModelIdentifier") or item.get("modelKey") or "")
                break
    except Exception:
        pass
    if not rel:
        rel = raw_key
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    if not parts:
        return None
    base_dir = os.path.abspath(os.path.join(lmstudio_home(), ".internal", "user-concrete-model-default-config"))
    target = os.path.abspath(os.path.join(base_dir, *parts) + ".json")
    if not target.startswith(base_dir + os.sep):
        return None
    return target


def read_model_load_preset(model_path_or_key, cfg=None):
    """Читает сохранённые в LM Studio индивидуальные настройки загрузки модели."""
    cfg_file = _resolve_model_config_path(model_path_or_key, cfg)
    out = {}
    if not cfg_file or not os.path.isfile(cfg_file):
        return out
    try:
        with open(cfg_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        fields = ((data.get("load") or {}).get("fields") or []) if isinstance(data, dict) else []
        fmap = {f.get("key"): f.get("value") for f in fields if isinstance(f, dict) and f.get("key")}
        if "llm.load.contextLength" in fmap:
            out["context_length"] = _positive_int(fmap["llm.load.contextLength"])
        if "llm.load.llama.acceleration.offloadRatio" in fmap:
            cli_g, ratio = normalize_gpu_offload(fmap["llm.load.llama.acceleration.offloadRatio"])
            out["gpu_offload"] = cli_g
            out["gpu_ratio"] = ratio
        if "llm.load.llama.flashAttention" in fmap:
            out["flash_attention"] = bool(fmap["llm.load.llama.flashAttention"])
        kv = fmap.get("llm.load.llama.kCacheQuantizationType")
        if isinstance(kv, dict):
            out["kv_quant"] = str(kv.get("value") or "f16") if kv.get("checked") else "f16"
        if "llm.load.llama.evalBatchSize" in fmap:
            out["eval_batch_size"] = _positive_int(fmap["llm.load.llama.evalBatchSize"])
        if "llm.load.numParallelSessions" in fmap:
            out["parallel"] = _positive_int(fmap["llm.load.numParallelSessions"])
    except Exception:
        pass
    return out


def apply_model_load_preset(model_path_or_key, context_length=None, gpu_offload=None,
                            flash_attention=None, kv_quant=None, eval_batch_size=None,
                            parallel=None, cfg=None):
    """Сохраняет параметры загрузки (контекст, GPU offload, Flash Attention, KV-квантование)
    в per-model конфиг LM Studio перед вызовом `lms load`, чтобы движок (вкл. Vulkan) их применил."""
    cfg_file = _resolve_model_config_path(model_path_or_key, cfg)
    if not cfg_file:
        return False
    doc = {"preset": "", "operation": {"fields": []}, "load": {"fields": []}}
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                doc = loaded
                if not isinstance(doc.get("operation"), dict):
                    doc["operation"] = {"fields": []}
                if not isinstance(doc.get("load"), dict):
                    doc["load"] = {"fields": []}
                if not isinstance(doc["load"].get("fields"), list):
                    doc["load"]["fields"] = []
        except Exception:
            pass

    updates = {}
    if context_length is not None:
        cval = _positive_int(context_length)
        if cval:
            updates["llm.load.contextLength"] = cval
    if gpu_offload is not None:
        _, ratio = normalize_gpu_offload(gpu_offload)
        updates["llm.load.llama.acceleration.offloadRatio"] = 1 if ratio >= 0.995 else (0 if ratio <= 0.0 else ratio)
    if flash_attention is not None:
        updates["llm.load.llama.flashAttention"] = bool(flash_attention)
    if kv_quant is not None:
        kv_clean = str(kv_quant or "f16").strip().lower()
        if kv_clean not in ("f16", "q8_0", "q4_0"):
            kv_clean = "f16"
        kv_obj = {"checked": kv_clean != "f16", "value": kv_clean}
        updates["llm.load.llama.kCacheQuantizationType"] = dict(kv_obj)
        updates["llm.load.llama.vCacheQuantizationType"] = dict(kv_obj)
    if eval_batch_size is not None:
        eb = _positive_int(eval_batch_size)
        if eb:
            updates["llm.load.llama.evalBatchSize"] = eb
    if parallel is not None:
        par = _positive_int(parallel)
        if par:
            updates["llm.load.numParallelSessions"] = par

    if not updates:
        return True

    new_fields = []
    seen = set()
    for f in doc["load"]["fields"]:
        if not isinstance(f, dict) or not f.get("key"):
            continue
        k = f["key"]
        if k in updates:
            new_fields.append({"key": k, "value": updates[k]})
            seen.add(k)
        else:
            new_fields.append(f)
    for k, v in updates.items():
        if k not in seen:
            new_fields.append({"key": k, "value": v})
    doc["load"]["fields"] = new_fields

    try:
        os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
        tmp = cfg_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, cfg_file)
        return True
    except OSError:
        return False


def build_load_cli_args(model, context_length=8192, gpu="max", parallel=1, ttl=0, identifier=""):
    """Формирует точные аргументы `lms load` с окном контекста, GPU offload, слотами и TTL."""
    ctx = max(512, min(int(context_length or 8192), 262144))
    cli_gpu, _ = normalize_gpu_offload(gpu)
    args = ["load", str(model), "--context-length", str(ctx), "--gpu", cli_gpu, "-y"]
    try:
        par = int(parallel or 1)
        if par > 1:
            args += ["--parallel", str(min(16, par))]
    except (TypeError, ValueError):
        pass
    try:
        ttl_i = int(ttl or 0)
        if ttl_i >= 1:
            args += ["--ttl", str(ttl_i)]
    except (TypeError, ValueError):
        pass
    ident = str(identifier or "").strip()
    if ident and not ident.startswith("-"):
        args += ["--identifier", ident]
    return args


def parse_estimate_cli_output(text):
    """Парсит вывод `lms load <model> --estimate-only` в структурированный словарь."""
    import re
    raw = str(text or "")
    if not raw.strip():
        return None
    gpu_m = re.search(r"Estimated\s+GPU\s+Memory:\s*([0-9]+(?:\.[0-9]+)?)\s*GiB", raw, re.I)
    tot_m = re.search(r"Estimated\s+Total\s+Memory:\s*([0-9]+(?:\.[0-9]+)?)\s*GiB", raw, re.I)
    off_m = re.search(r"GPU\s+Offload:\s*([^\r\n]+)", raw, re.I)
    est_m = re.search(r"Estimate:\s*([^\r\n]+)", raw, re.I)
    if not gpu_m and not tot_m:
        return None
    return {
        "gpu_memory_gb": round(float(gpu_m.group(1)), 2) if gpu_m else 0.0,
        "total_memory_gb": round(float(tot_m.group(1)), 2) if tot_m else 0.0,
        "gpu_offload_text": off_m.group(1).strip() if off_m else "",
        "verdict": est_m.group(1).strip() if est_m else "",
    }


def estimate_load_cli(model, context_length=8192, gpu="max", cfg=None, timeout=15):
    """Запрашивает у самого LM Studio (`--estimate-only`) точный расчёт VRAM и общей памяти."""
    if not model:
        return None, "не указана модель"
    cli = cli_path(cfg)
    if not cli:
        return None, "lms.exe не найден"
    ctx = max(512, min(int(context_length or 8192), 262144))
    cli_gpu, _ = normalize_gpu_offload(gpu)
    try:
        proc = subprocess.run(
            [cli, "load", str(model), "--context-length", str(ctx), "--gpu", cli_gpu, "--estimate-only"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        combined = (out + "\n" + err).strip()
    except subprocess.TimeoutExpired:
        return None, "таймаут расчёта памяти lms"
    except Exception as exc:
        return None, str(exc)[:300]
    parsed = parse_estimate_cli_output(combined)
    if parsed:
        return parsed, ""
    return None, combined[:300] or "не удалось получить оценку памяти от lms"
