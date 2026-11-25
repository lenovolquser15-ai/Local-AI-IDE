"""Third-party tool packs: drop a .py into <root>/plugins and it is live without a restart.

A plugin file looks like this:

    NAME = "example"
    TOOLS = [{"name": "my_tool", "desc": "что делает",
              "params": {"arg": ("string", "зачем")}, "required": ["arg"],
              "fn": lambda cfg, args: {"ok": True, "value": args["arg"]}}]

Вкладка «Плагины» умеет: включать/выключать файлы (config.plugins_disabled),
удалять их, ставить из каталога plugins/catalog.json и скачивать по URL.
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time

from . import permissions
from .config import app_root, load as load_cfg, save as save_cfg

PLUGIN_DIR = os.path.join(app_root(), "plugins")
CATALOG_PATH = os.path.join(PLUGIN_DIR, "catalog.json")
WORKSHOP_PATH = os.path.join(PLUGIN_DIR, "workshop_catalog.json")
WORKSHOP_DIR = os.path.join(PLUGIN_DIR, "_workshop")

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
    tool_specs = []
    for item in raw_tools:
        if not isinstance(item, dict) or not item.get("name") or not callable(item.get("fn")):
            raise ValueError("инструмент плагина обязан иметь name и fn(cfg, args)")
        tool = item["name"]
        entry = _spec_for(module_name, item)
        REGISTRY[tool] = {"module": module_name, "fn": item["fn"], "spec": entry,
                          "dangerous": bool(item.get("dangerous"))}
        names.append(tool)
        params_meta = {}
        for pk, pv in (entry.get("params") or {}).items():
            if isinstance(pv, (list, tuple)) and len(pv) >= 2:
                params_meta[pk] = {"type": str(pv[0]), "desc": str(pv[1])}
            else:
                params_meta[pk] = {"type": "string", "desc": str(pv)}
        tool_specs.append({
            "name": tool,
            "desc": entry.get("desc", ""),
            "params": params_meta,
            "required": list(entry.get("required") or []),
            "dangerous": bool(item.get("dangerous")),
        })
        if item.get("dangerous"):
            permissions.PLUGIN_DANGEROUS.add(tool)
    raw_settings = getattr(module, "SETTINGS", None)
    settings_schema = raw_settings if isinstance(raw_settings, list) else []
    return {
        "name": getattr(module, "NAME", module_name),
        "file": os.path.basename(path),
        "desc": getattr(module, "DESC", "") or (ast.get_docstring(ast.parse(open(path, "r", encoding="utf-8", errors="replace").read())) or "")[:240],
        "version": str(getattr(module, "VERSION", "") or ""),
        "author": str(getattr(module, "AUTHOR", "") or ""),
        "tools": names,
        "tool_specs": tool_specs,
        "settings_schema": settings_schema,
        "error": None,
        "mtime": int(os.path.getmtime(path)),
        "enabled": True,
    }


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
        return {"ok": False, "error": info["error"], "file": fname, "tools": [], **status()}
    return {**status(), "ok": True, "file": fname, "name": info.get("name"),
            "tools": list(info.get("tools") or [])}


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


def workshop_catalog():
    """Каталог Мастерской с локальным аудитом кода каждого плагина."""
    from . import virustotal
    entries = []
    if os.path.exists(WORKSHOP_PATH):
        try:
            with open(WORKSHOP_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries") or []
        except Exception:
            entries = []

    scan()
    installed_names = {v.get("name") for v in PLUGINS.values()}
    installed_files = set(PLUGINS)
    vt_enabled = bool((load_cfg() or {}).get("virustotal_api_key"))

    for e in entries:
        e.setdefault("id", str(e.get("name") or len(entries)))
        fname = os.path.basename(e.get("file") or (e["id"] + ".py"))
        e["installed"] = (e.get("name") in installed_names) or (fname in installed_files)
        local_src = os.path.join(WORKSHOP_DIR, fname)
        if os.path.exists(local_src):
            try:
                with open(local_src, "r", encoding="utf-8") as f:
                    content = f.read()
                audit = virustotal.static_security_scan(content)
                e["sha256"] = audit["sha256"]
                e["audit"] = {
                    "clean": audit["clean"],
                    "level": audit["threat_level"],
                    "findings": len(audit["findings"]),
                }
            except Exception:
                pass

    return {
        "dir": PLUGIN_DIR,
        "workshop_path": WORKSHOP_PATH,
        "vt_api_enabled": vt_enabled,
        "entries": entries,
    }


def _workshop_entry(entry_id):
    entry_id = str(entry_id or "").strip()
    for e in workshop_catalog().get("entries", []):
        if str(e.get("id")) == entry_id or str(e.get("name")) == entry_id:
            return e
    return None


def get_workshop_code(entry_id):
    """Возвращает исходный код плагина из Мастерской для предварительного просмотра."""
    target_entry = _workshop_entry(entry_id)
    if not target_entry:
        return {"ok": False, "error": f"Плагин '{entry_id}' не найден"}

    fname = os.path.basename(target_entry.get("file") or f"{entry_id}.py")
    src_path = os.path.join(WORKSHOP_DIR, fname)
    if not os.path.exists(src_path):
        return {"ok": False, "error": f"Файл не найден: {fname}"}

    with open(src_path, "r", encoding="utf-8") as fh:
        return {"ok": True, "id": entry_id, "file": fname, "code": fh.read()}


def install_workshop(entry_id):
    """Установить плагин из Мастерской сообщества с проверкой безопасности."""
    from . import virustotal
    target_entry = _workshop_entry(entry_id)
    if not target_entry:
        return {"ok": False, "error": f"Плагин '{entry_id}' не найден в Мастерской"}

    fname = os.path.basename(target_entry.get("file") or f"{entry_id}.py")
    src_path = os.path.join(WORKSHOP_DIR, fname)
    if not os.path.exists(src_path):
        return {"ok": False, "error": f"Исходный код плагина не найден: {fname}"}

    with open(src_path, "r", encoding="utf-8") as fh:
        code = fh.read()

    # VirusTotal verification
    vt_report = virustotal.scan_plugin(code)
    if not vt_report.get("clean"):
        return {
            "ok": False,
            "error": "Установка заблокирована: плагин не прошёл проверку безопасности VirusTotal",
            "findings": vt_report.get("findings"),
        }

    res = install_code(code, fname)
    if res.get("ok"):
        res["security"] = vt_report
    return res


def convert_and_install_repo(repo_url, custom_name=None):
    """Конвертировать любой AI-репозиторий с GitHub в готовый плагин LocalHarness."""
    from . import repo_converter
    return repo_converter.convert_repo(repo_url, custom_name=custom_name, save=True)


def create_plugin(name, code, desc="", author="", publish=False, tags=None):
    """Создать новый плагин из веб-интерфейса LocalHarness, проверить безопасность и сохранить."""
    from . import virustotal
    clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", str(name or "")).strip("_").lower()
    if not clean_name:
        return {"ok": False, "error": "Укажите имя плагина (латинские буквы, цифры и _)"}

    code = str(code or "").strip()
    if not code:
        return {"ok": False, "error": "Код плагина не может быть пустым"}

    fname = f"{clean_name}.py"

    # VirusTotal and AST security scan
    vt_res = virustotal.scan_plugin(code)
    if not vt_res.get("clean"):
        return {
            "ok": False,
            "error": "Код не прошёл проверку безопасности VirusTotal: обнаружены потенциально опасные конструкции",
            "findings": vt_res.get("findings", []),
            "score": vt_res.get("score"),
            "threat_level": vt_res.get("threat_level"),
        }

    # Install code into plugins/
    res = install_code(code, fname)
    if not res.get("ok"):
        return res

    published_entry = None
    publish_error = ""
    if publish:
        pub_res = publish_to_workshop(
            fname_or_name=fname,
            author=author or "Пользователь LocalHarness",
            desc=desc,
            tags=tags,
            name_hint=clean_name,
        )
        if pub_res.get("ok"):
            published_entry = pub_res.get("entry")
        else:
            publish_error = pub_res.get("error") or "не удалось опубликовать в Мастерскую"

    installed_name = str(res.get("name") or "").strip()
    name_warning = ""
    if installed_name and installed_name != clean_name:
        name_warning = (f"В коде NAME = \"{installed_name}\", а файл сохранён как {fname}. "
                        f"Приведи NAME к \"{clean_name}\", чтобы имена совпадали.")

    return {
        "ok": True,
        "name": clean_name,
        "file": fname,
        "tools": res.get("tools", []),
        "published": bool(published_entry),
        "published_entry": published_entry,
        "publish_error": publish_error,
        "name_warning": name_warning,
        "security": vt_res,
        "sha256": vt_res.get("sha256"),
    }


def publish_to_workshop(fname_or_name, author="Пользователь LocalHarness", desc="", tags=None,
                        name_hint=None):
    """Публикация установленного плагина в Мастерскую сообщества (создано в LocalHarness)."""
    from . import virustotal
    src_path = _resolve_plugin_file(fname_or_name)
    if not src_path or not os.path.exists(src_path):
        base_name = os.path.basename(str(fname_or_name or "").strip())
        if not base_name.endswith(".py"):
            base_name += ".py"
        src_path = os.path.join(PLUGIN_DIR, base_name)

    if not os.path.exists(src_path):
        return {"ok": False, "error": f"Файл плагина '{fname_or_name}' не найден в папке плагинов"}

    with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
        code = fh.read()

    # Verify security before publishing to workshop
    vt_res = virustotal.scan_plugin(code)
    if not vt_res.get("clean"):
        return {
            "ok": False,
            "error": "Публикация отклонена: плагин не прошёл проверку безопасности VirusTotal",
            "findings": vt_res.get("findings", []),
        }

    # Parse metadata from AST
    tools = []
    docstring = ""
    parsed_name = os.path.splitext(os.path.basename(src_path))[0]
    try:
        tree = ast.parse(code)
        docstring = ast.get_docstring(tree) or ""
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id == "NAME" and isinstance(node.value, ast.Constant):
                            parsed_name = str(node.value.value)
                        elif target.id == "TOOLS" and isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Dict):
                                    for k, v in zip(elt.keys, elt.values):
                                        if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                            tools.append(str(v.value))
    except Exception:
        pass

    plugin_name = str(name_hint or "").strip() or parsed_name
    plugin_id = re.sub(r"[^a-zA-Z0-9_]", "_", plugin_name).strip("_").lower() or "plugin"
    display_name = re.sub(r"[\x00-\x1f<>/\\]", "", str(plugin_name)).strip() or plugin_id
    fname = f"{plugin_id}.py"
    os.makedirs(WORKSHOP_DIR, exist_ok=True)
    dst_path = os.path.join(WORKSHOP_DIR, fname)
    with open(dst_path, "w", encoding="utf-8") as fh:
        fh.write(code)

    # Process tags
    final_tags = ["localharness"]
    if isinstance(tags, list):
        final_tags.extend([str(t).strip() for t in tags if str(t).strip()])
    elif isinstance(tags, str) and tags.strip():
        final_tags.extend([t.strip() for t in tags.split(",") if t.strip()])
    else:
        final_tags.extend(["сообщество", "инструмент"])
    final_tags = list(dict.fromkeys(final_tags))

    # Update workshop catalog
    data = {"version": 1, "description": "Мастерская плагинов LocalHarness", "entries": []}
    if os.path.exists(WORKSHOP_PATH):
        try:
            with open(WORKSHOP_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            pass

    entries = data.get("entries") or []
    sha256 = virustotal.sha256_of(code)

    entry = {
        "id": plugin_id,
        "name": display_name,
        "version": "1.0",
        "author": author or "Пользователь LocalHarness",
        "desc": desc or docstring or f"Пользовательский плагин {display_name}, созданный в LocalHarness",
        "file": fname,
        "tools": tools,
        "vt_status": "verified_clean" if vt_res.get("vt", {}).get("queried_api") else "static_audit",
        "vt_score": vt_res.get("score") if vt_res.get("vt", {}).get("queried_api") else "—",
        "sha256": sha256,
        "tags": final_tags,
        "made_in_localharness": True,
    }

    idx = next((i for i, e in enumerate(entries)
                if str(e.get("id")) in (plugin_id, display_name, plugin_name)
                or str(e.get("name")) in (plugin_id, display_name, plugin_name)), -1)
    if idx >= 0:
        entries[idx] = entry
    else:
        entries.append(entry)

    data["entries"] = entries
    try:
        with open(WORKSHOP_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка сохранения каталога Мастерской: {exc}"}

    return {"ok": True, "entry": entry, "file": fname}


def unpublish_from_workshop(plugin_id_or_name):
    """Снять плагин с публикации в Мастерской сообщества."""
    target_id = str(plugin_id_or_name or "").strip()
    if not target_id:
        return {"ok": False, "error": "Не указан ID плагина для снятия с публикации"}

    data = {"version": 1, "entries": []}
    if os.path.exists(WORKSHOP_PATH):
        try:
            with open(WORKSHOP_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            pass

    entries = data.get("entries") or []
    removed_entry = None
    new_entries = []
    for e in entries:
        if str(e.get("id")) == target_id or str(e.get("name")) == target_id:
            removed_entry = e
        else:
            new_entries.append(e)

    if not removed_entry:
        return {"ok": False, "error": f"Плагин '{target_id}' не найден в каталоге Мастерской"}

    data["entries"] = new_entries
    try:
        with open(WORKSHOP_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка обновления каталога: {exc}"}

    if removed_entry.get("made_in_localharness"):
        fname = removed_entry.get("file") or f"{target_id}.py"
        w_path = os.path.join(WORKSHOP_DIR, fname)
        if os.path.exists(w_path):
            try:
                os.remove(w_path)
            except Exception:
                pass

    return {"ok": True, "id": target_id, "removed": removed_entry}


def get_plugin_code(fname):
    """Возвращает исходный код установленного плагина для редактирования."""
    src_path = _resolve_plugin_file(fname)
    if not src_path or not os.path.exists(src_path):
        base_name = os.path.basename(str(fname or "").strip())
        if not base_name.endswith(".py"):
            base_name += ".py"
        src_path = os.path.join(PLUGIN_DIR, base_name)
    if not os.path.exists(src_path):
        return {"ok": False, "error": f"Файл плагина '{fname}' не найден"}

    with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
        code = fh.read()

    tools = []
    docstring = ""
    plugin_name = os.path.splitext(os.path.basename(src_path))[0]
    try:
        tree = ast.parse(code)
        docstring = ast.get_docstring(tree) or ""
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id == "NAME" and isinstance(node.value, ast.Constant):
                            plugin_name = str(node.value.value)
                        elif target.id == "TOOLS" and isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Dict):
                                    for k, v in zip(elt.keys, elt.values):
                                        if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                            tools.append(str(v.value))
    except Exception:
        pass

    return {
        "ok": True,
        "name": plugin_name,
        "file": os.path.basename(src_path),
        "desc": docstring,
        "code": code,
        "tools": tools,
        "installed": os.path.dirname(os.path.abspath(src_path)) == os.path.abspath(PLUGIN_DIR),
    }


def update_plugin_code(fname, code):
    """Сохранить отредактированный исходный код плагина с AST-проверкой и горячей перезагрузкой."""
    from . import virustotal
    code = str(code or "")
    if not code.strip():
        return {"ok": False, "error": "Исходный код плагина пуст"}
    if len(code) > 1_000_000:
        return {"ok": False, "error": "Размер кода превышает 1 МБ"}

    safe_fname = _safe_name(fname)
    try:
        ast.parse(code, filename=safe_fname)
    except SyntaxError as exc:
        return {"ok": False, "error": f"Синтаксическая ошибка в строке {exc.lineno}: {exc.msg}"}

    sec = virustotal.scan_plugin(code)
    if sec.get("verdict") == "malicious":
        return {"ok": False, "error": "Код заблокирован проверкой безопасности", "security": sec}

    res = install_code(code, safe_fname)
    # Keep _workshop copy in sync if this is a workshop/author plugin
    w_path = os.path.join(WORKSHOP_DIR, safe_fname)
    if os.path.isfile(w_path) and res.get("ok"):
        try:
            with open(w_path, "w", encoding="utf-8") as fh:
                fh.write(code)
        except OSError:
            pass
    res["security"] = sec
    return res


def get_plugin_settings(fname):
    """Получить схему настроек плагина (SETTINGS) и текущие сохранённые значения."""
    scan()
    base = os.path.basename(str(fname or "").strip())
    if base and not base.endswith(".py"):
        base += ".py"
    info = PLUGINS.get(base)
    if not info:
        for k, v in PLUGINS.items():
            if v.get("name") == fname or k == fname:
                info = v
                base = k
                break
    if not info:
        return {"ok": False, "error": f"Плагин '{fname}' не найден"}
    key = info.get("name") or os.path.splitext(base)[0]
    cfg = load_cfg()
    all_settings = cfg.get("plugin_settings") or {}
    saved = dict(all_settings.get(key) or {})
    schema = list(info.get("settings_schema") or [])
    for field in schema:
        fk = field.get("key")
        if fk and fk not in saved and "default" in field:
            saved[fk] = field["default"]
    return {"ok": True, "plugin": key, "file": base, "schema": schema, "values": saved}


def save_plugin_settings(fname, values):
    """Сохранить пользовательские настройки плагина в config.json."""
    cur = get_plugin_settings(fname)
    if not cur.get("ok"):
        return cur
    key = cur["plugin"]
    if not isinstance(values, dict):
        return {"ok": False, "error": "values должен быть словарём"}
    with _lock:
        cfg = load_cfg()
        all_settings = dict(cfg.get("plugin_settings") or {})
        merged = dict(all_settings.get(key) or {})
        merged.update(values)
        all_settings[key] = merged
        cfg["plugin_settings"] = all_settings
        save_cfg(cfg)
    return {"ok": True, "plugin": key, "values": merged}


TOOL_SANDBOX_TIMEOUT = 30


def _tool_source(tool_name):
    """Файл плагина, в котором зарегистрирован инструмент (только включённые)."""
    scan()
    item = REGISTRY.get(tool_name)
    if not item:
        return None
    module = item.get("module")
    for info in PLUGINS.values():
        stem = os.path.splitext(info.get("file") or "")[0]
        if module not in (info.get("name"), stem):
            continue
        if info.get("error") or not info.get("enabled", True):
            return None
        path = os.path.join(PLUGIN_DIR, info.get("file") or "")
        return path if os.path.isfile(path) else None
    return None


def _sandbox_command():
    root = app_root()
    if getattr(sys, "frozen", False):
        return [sys.executable, "plugin-run"], root
    return [sys.executable, "-m", "harness.plugin_sandbox"], root


def _parse_sandbox(stdout):
    from .plugin_sandbox import MARKER
    payload = None
    logs = []
    for line in (stdout or "").splitlines():
        if line.startswith(MARKER):
            try:
                payload = json.loads(line[len(MARKER):])
            except ValueError:
                payload = None
        else:
            logs.append(line)
    return payload, "\n".join(logs).strip()


def test_plugin_tool(tool_name, args=None, cfg=None, timeout=TOOL_SANDBOX_TIMEOUT):
    """Вызвать инструмент плагина в отдельном процессе с ограничением по времени."""
    tool_name = str(tool_name or "").strip()
    if not tool_name:
        return {"ok": False, "error": "Не указано имя инструмента для тестирования"}
    if cfg is None:
        cfg = load_cfg()
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception as exc:
            return {"ok": False, "error": f"Ошибка JSON в параметрах: {exc}"}
    elif not isinstance(args, dict):
        args = {}

    src = _tool_source(tool_name)
    if not src:
        return {"ok": False, "error": f"Инструмент '{tool_name}' не найден среди включённых плагинов"}

    try:
        timeout = max(1.0, float(timeout))
    except (TypeError, ValueError):
        timeout = float(TOOL_SANDBOX_TIMEOUT)

    cmd, cwd = _sandbox_command()
    payload = json.dumps({"file": src, "tool": tool_name, "args": args, "cfg": cfg}, ensure_ascii=False)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "sandboxed": True,
                "error": f"Инструмент не уложился в {int(timeout)} с — процесс остановлен",
                "duration_ms": round((time.time() - t0) * 1000, 1)}
    except OSError as exc:
        return {"ok": False, "error": f"Не удалось запустить изолированный процесс: {exc}"}

    duration_ms = round((time.time() - t0) * 1000, 1)
    parsed, output = _parse_sandbox(proc.stdout)
    if not parsed:
        tail = (output or (proc.stderr or "").strip())[-600:]
        return {"ok": False, "sandboxed": True, "duration_ms": duration_ms,
                "error": "Плагин завершился без результата" + (": " + tail if tail else "")}
    if not parsed.get("ok"):
        return {"ok": False, "sandboxed": True, "duration_ms": duration_ms,
                "error": parsed.get("error") or "Инструмент завершился с ошибкой"}
    return {
        "ok": True,
        "tool": tool_name,
        "result": parsed.get("result"),
        "duration_ms": duration_ms,
        "sandboxed": True,
        "output": output[-4000:] if output else "",
    }


def generate_plugin_ai(prompt, cfg=None):
    """Генерация кода плагина по текстовому описанию задачи с помощью локальной LLM."""
    from . import virustotal
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "Опишите задачу для создания плагина"}

    if cfg is None:
        cfg = load_cfg()

    system_prompt = (
        "You are an expert Python engineer generating a complete plugin for the LocalHarness AI agent.\n"
        "A valid LocalHarness plugin MUST define:\n"
        "1. NAME = \"short_tool_name\" (lowercase, underscore)\n"
        "2. One or more functions: def tool_fn(cfg, args): ... returning a dict, e.g. {\"ok\": True, ...}\n"
        "3. TOOLS = [\n"
        "    {\n"
        "        \"name\": \"tool_fn\",\n"
        "        \"desc\": \"Description in Russian\",\n"
        "        \"params\": {\"param\": (\"string|number|boolean\", \"doc\")},\n"
        "        \"required\": [\"param\"],\n"
        "        \"fn\": tool_fn\n"
        "    }\n"
        "]\n"
        "Output ONLY raw Python code. Do NOT output dangerous code like eval, exec, or rm -rf."
    )

    code = None
    try:
        from .llm import LLM
        model_client = LLM(cfg)
        response = model_client.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Создай готовый рабочий плагин LocalHarness для следующей задачи:\n{prompt}"}
            ]
        )
        raw_text = response.get("content") or ""
        m = re.search(r"```(?:python)?\s*(.*?)\s*```", raw_text, re.DOTALL)
        if m:
            code = m.group(1).strip()
        elif "NAME =" in raw_text and "TOOLS =" in raw_text:
            code = raw_text.strip()
    except Exception:
        pass

    generated_by_ai = bool(code)

    if not code:
        safe_desc = " ".join(str(prompt).split())[:160]
        safe_desc = safe_desc.replace("\\", "\\\\").replace('"', '\\"')
        slug_src = re.sub(r"[^a-zA-Z0-9_]+", "_", str(prompt).lower()).strip("_")
        digest = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:6]
        clean_slug = (slug_src[:24].strip("_") or "custom_plugin") + "_" + digest
        code = f'''"""Плагин: {safe_desc}"""
import json
import re

NAME = "{clean_slug}"

def run_task(cfg, args):
    """Выполняет задачу: {safe_desc}."""
    input_val = args.get("input_val", "")
    return {{"ok": True, "result": f"Обработано: {{input_val}}", "task": "{safe_desc}"}}

TOOLS = [
    {{
        "name": "run_task",
        "desc": "{safe_desc}",
        "params": {{
            "input_val": ("string", "Входные данные для обработки")
        }},
        "required": [],
        "fn": run_task
    }}
]
'''

    tools = []
    docstring = ""
    plugin_name = "generated_plugin"
    try:
        tree = ast.parse(code)
        docstring = ast.get_docstring(tree) or ""
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id == "NAME" and isinstance(node.value, ast.Constant):
                            plugin_name = str(node.value.value)
                        elif target.id == "TOOLS" and isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Dict):
                                    for k, v in zip(elt.keys, elt.values):
                                        if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                            tools.append(str(v.value))
    except Exception as exc:
        return {"ok": False, "error": f"Сгенерированный код содержит синтаксическую ошибку: {exc}", "raw": code}

    sec = virustotal.scan_plugin(code)
    return {
        "ok": True,
        "name": plugin_name,
        "desc": docstring or prompt,
        "code": code,
        "tools": tools,
        "security": sec,
        "generated_by_ai": generated_by_ai,
        "warning": "" if generated_by_ai else
                   "Локальная модель недоступна — собран шаблон. Отредактируй код под задачу или повтори позже.",
    }


def _resolve_plugin_file(name):
    """Ищет файл плагина по имени в plugins/ и plugins/_workshop/."""
    base = os.path.basename(str(name or "").strip())
    if not base:
        return None
    candidates = [base] if base.endswith(".py") else [base, base + ".py"]
    for folder in (PLUGIN_DIR, WORKSHOP_DIR):
        for cand in candidates:
            path = os.path.join(folder, cand)
            if os.path.isfile(path):
                return path
    for fname, info in scan().items():
        if info.get("name") in candidates or fname in candidates:
            path = os.path.join(PLUGIN_DIR, fname)
            if os.path.isfile(path):
                return path
    return None


def scan_virustotal(fname_or_code):
    """Проверка плагина по имени файла из plugins/ или по исходному коду."""
    from . import virustotal
    path = _resolve_plugin_file(fname_or_code)
    res = virustotal.scan_plugin(path or fname_or_code)
    if path and res.get("ok"):
        res["file"] = os.path.basename(path)
    return res


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
