"""Third-party tool packs: drop a .py into <root>/plugins and it is live without a restart.

A plugin file looks like this:

    NAME = "example"
    TOOLS = [{"name": "my_tool", "desc": "что делает",
              "params": {"arg": ("string", "зачем")}, "required": ["arg"],
              "fn": lambda cfg, args: {"ok": True, "value": args["arg"]}}]

Вкладка «Плагины» умеет: включать/выключать файлы (config.plugins_disabled),
удалять их, ставить из каталога plugins/catalog.json и скачивать по URL.
"""
import importlib.util
import json
import os
import re
import shutil
import threading
import time

from . import permissions
from .config import app_root, load as load_cfg, save as save_cfg

PLUGIN_DIR = os.path.join(app_root(), "plugins")
CATALOG_PATH = os.path.join(PLUGIN_DIR, "catalog.json")

_lock = threading.Lock()
PLUGINS = {}
REGISTRY = {}
_scanned = False


def _spec_for(module_name, item):
    return {"profile": "plugin", "name": item["name"],
            "desc": item.get("desc") or "инструмент плагина %s" % module_name,
            "params": item.get("params") or {}, "required": item.get("required") or []}


def _disabled_files():
    cfg = load_cfg()
    raw = cfg.get("plugins_disabled")
    if isinstance(raw, list):
        return set(str(x) for x in raw)
    return set()


def _load_file(path):
    module_name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(
        "lh_plugin_%s_%d" % (module_name, int(os.path.getmtime(path))), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw_tools = getattr(module, "TOOLS", None)
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError("в файле нет списка TOOLS = [{name, desc, params, fn}]")
    names = []
    for item in raw_tools:
        if not isinstance(item, dict) or not item.get("name") or not callable(item.get("fn")):
            raise ValueError("инструмент плагина обязан иметь name и fn(cfg, args)")
        tool = item["name"]
        entry = _spec_for(module_name, item)
        REGISTRY[tool] = {"module": module_name, "fn": item["fn"], "spec": entry,
                          "dangerous": bool(item.get("dangerous"))}
        names.append(tool)
        if item.get("dangerous"):
            permissions.PLUGIN_DANGEROUS.add(tool)
    return {"name": getattr(module, "NAME", module_name), "file": os.path.basename(path),
            "tools": names, "error": None, "mtime": int(os.path.getmtime(path)),
            "enabled": True}


def scan(force=False):
    with _lock:
        if _scanned and not force:
            return PLUGINS
        disabled = _disabled_files()
        if force:
            REGISTRY.clear()
            permissions.PLUGIN_DANGEROUS.clear()
            PLUGINS.clear()
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        for fname in sorted(os.listdir(PLUGIN_DIR)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(PLUGIN_DIR, fname)
            if fname in disabled:
                PLUGINS[fname] = {"name": fname[:-3], "file": fname, "tools": [],
                                  "error": None, "enabled": False,
                                  "mtime": int(os.path.getmtime(path))}
                continue
            try:
                PLUGINS[fname] = _load_file(path)
            except Exception as exc:
                PLUGINS[fname] = {"name": fname[:-3], "file": fname, "tools": [],
                                  "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                                  "enabled": True, "mtime": int(os.path.getmtime(path))}
        globals()["_scanned"] = True
        return PLUGINS


def set_enabled(fname, enabled):
    """Переключить плагин: выключенный остаётся на диске, но не грузится."""
    fname = os.path.basename(str(fname or ""))
    if not fname.endswith(".py"):
        return {"ok": False, "error": "нужен файл .py"}
    with _lock:
        cfg = load_cfg()
        disabled = set(str(x) for x in (cfg.get("plugins_disabled") or []))
        if enabled:
            disabled.discard(fname)
        else:
            disabled.add(fname)
        cfg["plugins_disabled"] = sorted(disabled)
        save_cfg(cfg)
    scan(force=True)
    return {"ok": True, "file": fname, "enabled": bool(enabled), **status()}


def remove_file(fname):
    fname = os.path.basename(str(fname or ""))
    path = os.path.join(PLUGIN_DIR, fname)
    if not os.path.exists(path):
        return {"ok": False, "error": "нет файла %s" % fname}
    os.remove(path)
    with _lock:
        cfg = load_cfg()
        disabled = set(str(x) for x in (cfg.get("plugins_disabled") or []))
        if fname in disabled:
            disabled.discard(fname)
            cfg["plugins_disabled"] = sorted(disabled)
            save_cfg(cfg)
    scan(force=True)
    return {"ok": True, "file": fname, **status()}


def _safe_name(url_or_name):
    base = os.path.basename(str(url_or_name or "").split("?")[0].split("#")[0])
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    if not base.endswith(".py"):
        base = (base or "plugin") + ".py"
    if base.startswith("_") or base in (".py", "..py"):
        base = "plugin_" + base
    return base


def install_code(code, fname):
    """Проверить исходник и положить его в папку плагинов."""
    fname = _safe_name(fname)
    if len(code) > 1_000_000:
        return {"ok": False, "error": "файл больше 1 МБ — слишком жирный для плагина"}
    try:
        compile(code, fname, "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": "синтаксическая ошибка: %s" % exc}
    path = os.path.join(PLUGIN_DIR, fname)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(code)
    scan(force=True)
    info = PLUGINS.get(fname) or {}
    if info.get("error"):
        return {"ok": False, "error": info["error"], "file": fname, **status()}
    return {"ok": True, "file": fname, "name": info.get("name"), "tools": info.get("tools") or [], **status()}


def install_url(url):
    from . import netio
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "нужен http(s) URL на .py файл"}
    try:
        res = netio.request("GET", url, timeout=60.0, max_bytes=1_200_000)
    except Exception as exc:
        return {"ok": False, "error": "скачивание не удалось: %s: %s" % (type(exc).__name__, exc)}
    if res.get("status", 0) >= 400:
        return {"ok": False, "error": "HTTP %s при скачивании %s" % (res.get("status"), url)}
    body = res.get("body") or b""
    try:
        text = (body or b"").decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "файл не текстовый .py"}
    if "TOOLS" not in text:
        return {"ok": False, "error": "в файле нет TOOLS — это не похоже на плагин LocalHarness"}
    return install_code(text, _safe_name(url))


def _read_catalog_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    return data.get("entries") or data.get("plugins") or []


def catalog():
    entries = []
    if os.path.exists(CATALOG_PATH):
        try:
            entries = _read_catalog_file(CATALOG_PATH)
        except Exception as exc:
            entries = [{"id": "local", "name": "локальный catalog.json",
                        "desc": "не прочитался: %s" % exc, "url": ""}]
    cfg = load_cfg()
    remote_url = str(cfg.get("plugin_catalog_url") or "").strip()
    remote_err = None
    if remote_url:
        try:
            from . import netio
            res = netio.request("GET", remote_url, timeout=30.0, max_bytes=1_200_000)
            if res.get("status", 0) >= 400:
                remote_err = "HTTP %s" % res.get("status")
            else:
                text = res.get("text") or ""
                extra = json.loads(text)
                if isinstance(extra, dict):
                    extra = extra.get("entries") or extra.get("plugins") or []
                if isinstance(extra, list):
                    known = {str(e.get("id") or e.get("name")) for e in entries}
                    for item in extra:
                        key = str(item.get("id") or item.get("name"))
                        if key not in known:
                            entries.append(item)
                            known.add(key)
        except Exception as exc:
            remote_err = "%s: %s" % (type(exc).__name__, exc)
    scan()
    installed_names = {v.get("name") for v in PLUGINS.values()}
    installed_files = set(PLUGINS)
    for e in entries:
        e = e if isinstance(e, dict) else {"name": str(e)}
        e.setdefault("id", str(e.get("name") or len(entries)))
        e["installed"] = e.get("name") in installed_names or e.get("file") in installed_files
    return {"dir": PLUGIN_DIR, "catalog_path": CATALOG_PATH,
            "remote_url": remote_url, "remote_error": remote_err,
            "entries": [e for e in entries if isinstance(e, dict)]}


def install_catalog(entry_id, url=""):
    entry_id = str(entry_id or "")
    url = str(url or "").strip()
    if not url:
        for e in catalog().get("entries", []):
            if str(e.get("id")) == entry_id or str(e.get("name")) == entry_id:
                url = str(e.get("url") or "")
                fname = str(e.get("file") or "")
                if url:
                    break
                # template: id указывает на готовый пример в папке plugins/_templates
                tpl = os.path.join(PLUGIN_DIR, "_templates", fname or (entry_id + ".py"))
                if os.path.exists(tpl):
                    with open(tpl, "r", encoding="utf-8") as fh:
                        return install_code(fh.read(), os.path.basename(tpl))
                break
    if not url:
        return {"ok": False, "error": "в записи каталога нет url"}
    return install_url(url)


def entries():
    return [item["spec"] for info in scan().values() if info.get("enabled", True)
            for name in info["tools"]
            for item in [REGISTRY.get(name)] if item]


def call(name, cfg, args):
    scan()
    item = REGISTRY.get(name)
    if not item:
        return None
    try:
        result = item["fn"](cfg, args or {})
    except Exception as exc:
        return {"ok": False, "error": "плагин %s упал: %s: %s" % (item["module"], type(exc).__name__, exc)}
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def status():
    info = scan()
    files = sorted(info.values(), key=lambda v: v["file"])
    return {"dir": PLUGIN_DIR,
            "loaded": sum(1 for v in files if not v["error"] and v.get("enabled", True)),
            "tools": sorted(REGISTRY), "plugins": files}
