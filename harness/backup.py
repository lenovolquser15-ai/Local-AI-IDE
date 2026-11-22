"""Profile Export / Import (.zip backup): config, memory, sessions, plugins, schedules."""
import base64
import io
import json
import os
import re
import time
import zipfile

from . import LOCALHARNESS
from . import config as config_mod
from . import plugins as plugins_mod

MAX_BACKUP_BYTES = 50 * 1024 * 1024  # 50 MB safety cap
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _is_safe_member(name):
    if not name or name.startswith("/") or name.startswith("\\") or ".." in name.split("/"):
        return False
    if "\\" in name or ":" in name:
        return False
    return True


def export_profile(include_sessions=True, include_plugins=True, include_memory=True, include_schedules=True):
    """Build a .zip archive (bytes) containing LocalHarness settings and user data."""
    buf = io.BytesIO()
    counts = {"config": 0, "memory": 0, "sessions": 0, "plugins": 0, "schedules": 0}

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Config
        cfg = config_mod.load()
        zf.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        counts["config"] = 1

        d_dir = config_mod.data_dir()

        # 2. Memory
        mem_path = os.path.join(d_dir, "memory.json")
        if include_memory and os.path.isfile(mem_path):
            zf.write(mem_path, "data/memory.json")
            counts["memory"] = 1

        # 3. Schedules
        sched_path = os.path.join(d_dir, "schedules.json")
        if include_schedules and os.path.isfile(sched_path):
            zf.write(sched_path, "data/schedules.json")
            counts["schedules"] = 1

        # 4. Sessions (.jsonl and legacy .json)
        sess_dir = os.path.join(d_dir, "sessions")
        if include_sessions and os.path.isdir(sess_dir):
            for fname in sorted(os.listdir(sess_dir)):
                if not fname.endswith((".jsonl", ".json")) or not _SAFE_NAME.match(fname):
                    continue
                full = os.path.join(sess_dir, fname)
                if os.path.isfile(full):
                    zf.write(full, "data/sessions/" + fname)
                    counts["sessions"] += 1

        # 5. User plugins (*.py directly inside plugins/)
        pdir = plugins_mod.PLUGIN_DIR
        if include_plugins and os.path.isdir(pdir):
            for fname in sorted(os.listdir(pdir)):
                if not fname.endswith(".py") or fname.startswith("_") or not _SAFE_NAME.match(fname):
                    continue
                full = os.path.join(pdir, fname)
                if os.path.isfile(full):
                    zf.write(full, "plugins/" + fname)
                    counts["plugins"] += 1

        manifest = {
            "app": "LocalHarness",
            "version": LOCALHARNESS,
            "exported_at": int(time.time()),
            "counts": counts,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return buf.getvalue()


def inspect_backup(raw_bytes):
    """Validate a zip backup and summarize its contents without writing to disk."""
    if not raw_bytes or len(raw_bytes) > MAX_BACKUP_BYTES:
        return {"ok": False, "error": "архив пуст или превышает 50 МБ"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes), "r")
    except zipfile.BadZipFile:
        return {"ok": False, "error": "файл не является корректным .zip архивом"}

    with zf:
        names = zf.namelist()
        for n in names:
            if not _is_safe_member(n):
                return {"ok": False, "error": "небезопасный путь внутри архива: %s" % n}
        manifest = {}
        if "manifest.json" in names:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8", "replace"))
            except Exception:
                manifest = {}
        summary = {
            "has_config": "config.json" in names,
            "has_memory": "data/memory.json" in names,
            "has_schedules": "data/schedules.json" in names,
            "sessions": sum(1 for n in names if n.startswith("data/sessions/") and n.endswith((".jsonl", ".json"))),
            "plugins": sum(1 for n in names if n.startswith("plugins/") and n.endswith(".py")),
            "manifest": manifest,
        }
        return {"ok": True, **summary}


def import_profile(raw_bytes, restore_config=True, restore_memory=True,
                   restore_sessions=True, restore_plugins=True, restore_schedules=True):
    """Restore LocalHarness profile from a validated .zip archive."""
    check = inspect_backup(raw_bytes)
    if not check.get("ok"):
        return check

    restored = {"config": False, "memory": False, "schedules": False, "sessions": 0, "plugins": 0}
    warnings = []

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        names = set(zf.namelist())

        if restore_config and "config.json" in names:
            try:
                incoming = json.loads(zf.read("config.json").decode("utf-8", "replace"))
                if isinstance(incoming, dict):
                    with config_mod.LOCK:
                        merged = config_mod._merge(config_mod.load(), incoming)
                        config_mod.save(merged)
                    restored["config"] = True
            except Exception as exc:
                warnings.append("config.json: %s" % exc)

        d_dir = config_mod.data_dir()

        if restore_memory and "data/memory.json" in names:
            try:
                os.makedirs(d_dir, exist_ok=True)
                data = zf.read("data/memory.json")
                json.loads(data.decode("utf-8", "replace"))
                with open(os.path.join(d_dir, "memory.json"), "wb") as fh:
                    fh.write(data)
                restored["memory"] = True
            except Exception as exc:
                warnings.append("memory.json: %s" % exc)

        if restore_schedules and "data/schedules.json" in names:
            try:
                os.makedirs(d_dir, exist_ok=True)
                data = zf.read("data/schedules.json")
                json.loads(data.decode("utf-8", "replace"))
                with open(os.path.join(d_dir, "schedules.json"), "wb") as fh:
                    fh.write(data)
                restored["schedules"] = True
            except Exception as exc:
                warnings.append("schedules.json: %s" % exc)

        if restore_sessions:
            sess_dir = os.path.join(d_dir, "sessions")
            os.makedirs(sess_dir, exist_ok=True)
            for n in sorted(names):
                if not n.startswith("data/sessions/") or not n.endswith((".jsonl", ".json")):
                    continue
                fname = n[len("data/sessions/"):]
                if not _SAFE_NAME.match(fname):
                    continue
                try:
                    data = zf.read(n)
                    text = data.decode("utf-8", "replace")
                    if fname.endswith(".jsonl"):
                        for ln in text.splitlines():
                            if ln.strip():
                                json.loads(ln)
                    else:
                        json.loads(text)
                    with open(os.path.join(sess_dir, fname), "wb") as fh:
                        fh.write(data)
                    restored["sessions"] += 1
                except Exception as exc:
                    warnings.append("%s: %s" % (fname, exc))

        if restore_plugins:
            pdir = plugins_mod.PLUGIN_DIR
            os.makedirs(pdir, exist_ok=True)
            for n in sorted(names):
                if not n.startswith("plugins/") or not n.endswith(".py"):
                    continue
                fname = n[len("plugins/"):]
                if fname.startswith("_") or not _SAFE_NAME.match(fname):
                    continue
                try:
                    code = zf.read(n)
                    with open(os.path.join(pdir, fname), "wb") as fh:
                        fh.write(code)
                    restored["plugins"] += 1
                except Exception as exc:
                    warnings.append("%s: %s" % (fname, exc))

    if restored["plugins"] > 0:
        try:
            plugins_mod.scan(force=True)
        except Exception:
            pass

    return {"ok": True, "restored": restored, "warnings": warnings}


def import_profile_b64(data_b64, **kw):
    try:
        raw = base64.b64decode(str(data_b64 or "").encode("ascii"))
    except Exception as exc:
        return {"ok": False, "error": "некорректный base64: %s" % exc}
    return import_profile(raw, **kw)
