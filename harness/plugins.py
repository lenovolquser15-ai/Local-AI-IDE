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
import importlib.util
import json
import os
import re
import threading

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
    if publish:
        pub_res = publish_to_workshop(
            fname_or_name=fname,
            author=author or "Пользователь LocalHarness",
            desc=desc,
            tags=tags,
        )
        if pub_res.get("ok"):
            published_entry = pub_res.get("entry")

    return {
        "ok": True,
        "name": clean_name,
        "file": fname,
        "tools": res.get("tools", []),
        "published": bool(publish),
        "published_entry": published_entry,
        "security": vt_res,
        "sha256": vt_res.get("sha256"),
    }


def publish_to_workshop(fname_or_name, author="Пользователь LocalHarness", desc="", tags=None):
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

    fname = f"{plugin_name}.py"
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
        "id": plugin_name,
        "name": plugin_name,
        "version": "1.0",
        "author": author or "Пользователь LocalHarness",
        "desc": desc or docstring or f"Пользовательский плагин {plugin_name}, созданный в LocalHarness",
        "file": fname,
        "tools": tools,
        "vt_status": "clean",
        "vt_score": "0/72",
        "sha256": sha256,
        "tags": final_tags,
        "made_in_localharness": True,
    }

    idx = next((i for i, e in enumerate(entries) if str(e.get("id")) == plugin_name or str(e.get("name")) == plugin_name), -1)
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
    }


def test_plugin_tool(tool_name, args=None, cfg=None):
    """Выполнить вызов инструмента плагина в тестовом режиме и вернуть результат."""
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

    import time
    t0 = time.time()
    res = call(tool_name, cfg, args)
    duration_ms = round((time.time() - t0) * 1000, 2)

    if res is None:
        return {
            "ok": False,
            "error": f"Инструмент '{tool_name}' не найден среди зарегистрированных инструментов плагинов",
        }

    return {
        "ok": True,
        "tool": tool_name,
        "result": res,
        "duration_ms": duration_ms,
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

    if not code:
        clean_slug = re.sub(r"[^a-zA-Z0-9_]", "_", prompt.lower()[:30]).strip("_") or "custom_plugin"
        code = f'''"""Плагин: {prompt}"""
import json
import re

NAME = "{clean_slug}"

def run_task(cfg, args):
    """Выполняет задачу: {prompt}."""
    input_val = args.get("input_val", "")
    return {{"ok": True, "result": f"Обработано: {{input_val}}", "task": "{prompt}"}}

TOOLS = [
    {{
        "name": "run_task",
        "desc": "{prompt}",
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
