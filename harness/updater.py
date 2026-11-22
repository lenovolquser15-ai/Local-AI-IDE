"""Self-updater: checks GitHub Releases for newer LocalHarness versions and downloads the installer."""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import LOCALHARNESS

GITHUB_REPO = "lenovolquser15-ai/Local-AI-IDE"
_CACHE = {"at": 0.0, "data": None, "lock": threading.Lock()}
_CACHE_TTL = 300.0
_DL_STATE = {"running": False, "progress": 0, "status": "idle", "error": "", "path": ""}
_DL_LOCK = threading.Lock()


def parse_version(ver):
    """Parse semver like '1.0.7', 'v1.1.0', '1.0.7-beta.1' into comparable tuple."""
    s = str(ver or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc)\.?(\d*))?", s, re.I)
    if not m:
        return (0, 0, 0, 0, 0)
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_kind = (m.group(4) or "").lower()
    pre_num = int(m.group(5) or 0)
    # Stable release (no pre-release suffix) sorts higher than beta/rc
    rank = {"": 4, "rc": 3, "beta": 2, "alpha": 1}.get(pre_kind, 0)
    return (major, minor, patch, rank, pre_num)


def is_newer(remote_ver, current_ver=None):
    cur = current_ver if current_ver is not None else LOCALHARNESS
    return parse_version(remote_ver) > parse_version(cur)


def check_update(force=False, timeout=8.0):
    """Query GitHub latest release and compare with current LOCALHARNESS version."""
    now = time.time()
    with _CACHE["lock"]:
        if not force and _CACHE["data"] and (now - _CACHE["at"]) < _CACHE_TTL:
            return dict(_CACHE["data"])

    url = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LocalHarness/%s" % LOCALHARNESS,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rel = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return {
            "ok": False,
            "current": LOCALHARNESS,
            "available": False,
            "error": "Не удалось проверить обновления: %s" % exc,
        }

    tag = str(rel.get("tag_name") or "").strip()
    latest = tag.lstrip("vV")
    assets = rel.get("assets") or []
    installer_url = ""
    installer_name = ""
    zip_url = ""
    for a in assets:
        name = str(a.get("name") or "")
        dl = str(a.get("browser_download_url") or "")
        if "Setup" in name and name.lower().endswith(".exe"):
            installer_url = dl
            installer_name = name
        elif name.lower().endswith(".zip"):
            zip_url = dl

    result = {
        "ok": True,
        "current": LOCALHARNESS,
        "latest": latest,
        "tag": tag,
        "name": rel.get("name") or tag,
        "available": is_newer(latest, LOCALHARNESS),
        "html_url": rel.get("html_url") or ("https://github.com/%s/releases" % GITHUB_REPO),
        "notes": str(rel.get("body") or "")[:3000],
        "installer_url": installer_url,
        "installer_name": installer_name,
        "zip_url": zip_url,
        "published_at": rel.get("published_at") or "",
    }
    with _CACHE["lock"]:
        _CACHE["data"] = dict(result)
        _CACHE["at"] = now
    return result


def download_status():
    with _DL_LOCK:
        return dict(_DL_STATE)


def download_and_launch(installer_url=None, installer_name=None, launch=True):
    """Download the Windows Setup .exe in a background thread and optionally launch it."""
    if not installer_url:
        info = check_update(force=True)
        if not info.get("ok") or not info.get("installer_url"):
            return {"ok": False, "error": info.get("error") or "Установщик не найден в релизе"}
        installer_url = info["installer_url"]
        installer_name = info.get("installer_name") or "LocalHarness-Setup.exe"

    if not installer_url.startswith("https://github.com/"):
        return {"ok": False, "error": "Разрешено скачивание только с github.com"}

    safe_name = re.sub(r"[^A-Za-z0-9._\-]", "_", os.path.basename(installer_name or "LocalHarness-Setup.exe"))
    if not safe_name.lower().endswith(".exe"):
        safe_name += ".exe"

    with _DL_LOCK:
        if _DL_STATE["running"]:
            return {"ok": True, "already_running": True, **dict(_DL_STATE)}
        _DL_STATE.update({"running": True, "progress": 0, "status": "downloading", "error": "", "path": ""})

    def _worker():
        target = os.path.join(tempfile.gettempdir(), safe_name)
        try:
            req = urllib.request.Request(installer_url, headers={"User-Agent": "LocalHarness/%s" % LOCALHARNESS})
            with urllib.request.urlopen(req, timeout=180) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(target, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = min(100, int(done * 100 / total))
                            with _DL_LOCK:
                                _DL_STATE["progress"] = pct
            with _DL_LOCK:
                _DL_STATE.update({"progress": 100, "path": target, "status": "launching" if launch else "ready"})
            if launch and os.name == "nt":
                subprocess.Popen([target], close_fds=True)
            with _DL_LOCK:
                _DL_STATE["status"] = "done"
                _DL_STATE["running"] = False
        except Exception as exc:
            with _DL_LOCK:
                _DL_STATE.update({"running": False, "status": "error", "error": str(exc)[:300]})

    threading.Thread(target=_worker, daemon=True, name="lh-updater").start()
    return {"ok": True, "started": True, **download_status()}
