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
        proc = subprocess.run([cli] + list(args), capture_output=True, timeout=timeout,
                              creationflags=NO_WINDOW)
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
    try:
        req = urllib.request.Request(root_url + "/api/v0/models", headers={"User-Agent": "LocalHarness"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("data") or []
            loaded_items = []
            all_served = []
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
                mid = m.get("id")
                if not mid or m.get("type") == "embeddings" or "embedding" in mid.lower():
                    continue
                is_loaded = m.get("state") == "loaded"
                d_meta = disk_map.get(mid) or {}
                item = {
                    "type": "llm",
                    "identifier": mid,
                    "modelKey": d_meta.get("modelKey") or mid,
                    "path": d_meta.get("path") or mid,
                    "displayName": d_meta.get("displayName") or m.get("name") or mid,
                    "architecture": m.get("arch") or d_meta.get("architecture") or "",
                    "quantization": d_meta.get("quantization") or {"name": m.get("quantization") or ""},
                    "sizeBytes": d_meta.get("sizeBytes") or 0,
                    "paramsString": d_meta.get("paramsString") or "",
                    "contextLength": m.get("loaded_context_length") or d_meta.get("maxContextLength") or m.get("max_context_length"),
                    "maxContextLength": d_meta.get("maxContextLength") or m.get("max_context_length"),
                    "status": "loaded" if is_loaded else "ready",
                    "vision": m.get("type") == "vlm" or d_meta.get("vision") or "vision" in mid.lower(),
                    "trainedForToolUse": "tool_use" in (m.get("capabilities") or []) or d_meta.get("trainedForToolUse"),
                }
                if is_loaded:
                    loaded_items.append(item)
                all_served.append(item)
            if loaded_items:
                return loaded_items
            sel = (cfg or {}).get("model")
            if sel:
                matched = [it for it in all_served if it["identifier"] == sel]
                if matched:
                    return matched
            return all_served
    except Exception:
        pass

    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"User-Agent": "LocalHarness"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("data") or []
            models = []
            for m in raw:
                mid = m.get("id")
                if not mid or "embedding" in mid.lower():
                    continue
                models.append({
                    "type": "llm",
                    "identifier": mid,
                    "modelKey": mid,
                    "path": mid,
                    "displayName": mid,
                    "contextLength": 8192,
                    "status": "loaded",
                })
            if models:
                return models
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