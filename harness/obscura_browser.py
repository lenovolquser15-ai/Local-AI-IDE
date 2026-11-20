"""Obscura lightweight Rust headless stealth browser driver for LocalHarness.

Obscura (https://github.com/h4ckf0r0day/obscura) is a single-binary Rust headless browser
exposing Chrome DevTools Protocol (CDP) via `obscura serve --port <PORT> --stealth`.
Uses ~30-130 MB RAM with instant cold start and built-in fingerprint randomization.
"""
import atexit
import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_CDP_URL = "http://127.0.0.1:9223"


def find_executable(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Locate the Obscura binary on Windows/Linux/macOS."""
    cfg = cfg or {}
    explicit = str(cfg.get("obscura_path") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit

    exe_name = "obscura.exe" if os.name == "nt" else "obscura"
    in_path = shutil.which(exe_name) or shutil.which("obscura")
    if in_path:
        return in_path

    home = os.path.expanduser("~")
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(home, ".obscura", "bin", exe_name),
        os.path.join(home, ".cargo", "bin", exe_name),
        os.path.join(local_app, "obscura", exe_name) if local_app else "",
        os.path.join(local_app, "Programs", "obscura", exe_name) if local_app else "",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", exe_name),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _cdp_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    raw = str((cfg or {}).get("obscura_cdp") or DEFAULT_CDP_URL).strip().rstrip("/")
    if not raw:
        return DEFAULT_CDP_URL
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "http://" + raw
    return raw


def _parse_host_port(cdp_url: str):
    parsed = urllib.parse.urlparse(cdp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9223
    return host, port


def is_cdp_alive(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 1.0) -> bool:
    """Check if an Obscura CDP server responds on /json/version or /json/list."""
    base = cdp_url.rstrip("/")
    for endpoint in ("/json/version", "/json/list"):
        try:
            req = urllib.request.Request(base + endpoint, headers={"User-Agent": "LocalHarness-Obscura"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return availability & runtime status of Obscura."""
    exe = find_executable(cfg)
    cdp = _cdp_url(cfg)
    running = is_cdp_alive(cdp, timeout=0.6)
    return {
        "available": bool(exe or running),
        "installed": bool(exe),
        "running": running,
        "exe": exe or "",
        "cdp_url": cdp,
        "stealth": bool((cfg or {}).get("obscura_stealth", True)),
    }


class ObscuraSession:
    """Thread-safe persistent Obscura CDP session with automatic `obscura serve` launch."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._cdp_url: str = DEFAULT_CDP_URL
        self._tab_id: Optional[str] = None
        self._last_url: str = ""
        atexit.register(self.close)

    def ensure_started(self, cfg: Optional[Dict[str, Any]] = None) -> str:
        """Ensure Obscura CDP server is running and return its HTTP CDP base URL."""
        cfg = cfg or {}
        cdp = _cdp_url(cfg)
        with self._lock:
            self._cdp_url = cdp
            if is_cdp_alive(cdp, timeout=0.8):
                return cdp

            exe = find_executable(cfg)
            if not exe:
                raise RuntimeError(
                    "Obscura не найден: установите бинарник с https://github.com/h4ckf0r0day/obscura "
                    "и укажите путь в Настройках → Поиск → Obscura (или добавьте obscura в PATH), "
                    "либо запустите `obscura serve --port 9223 --stealth` вручную."
                )

            host, port = _parse_host_port(cdp)
            cmd = [exe, "serve", "--host", host, "--port", str(port)]
            if cfg.get("obscura_stealth", True):
                cmd.append("--stealth")
            proxy = str(cfg.get("obscura_proxy") or "").strip()
            if proxy:
                cmd += ["--proxy", proxy]

            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )

            deadline = time.time() + 6.0
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    raise RuntimeError("Obscura завершился сразу после запуска (код %s)" % self._proc.returncode)
                if is_cdp_alive(cdp, timeout=0.4):
                    return cdp
                time.sleep(0.15)
            raise RuntimeError("Obscura не поднял CDP-порт %s за 6 секунд" % cdp)

    def _ensure_tab(self, cdp: str) -> str:
        from . import browser as cdp_mod
        tabs = cdp_mod.tabs(cdp)
        if self._tab_id and any(t.get("id") == self._tab_id for t in tabs):
            return self._tab_id
        if tabs:
            self._tab_id = tabs[0].get("id")
            return self._tab_id
        # Open a blank tab via /json/new
        req = urllib.request.Request(cdp.rstrip("/") + "/json/new?about:blank", method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                self._tab_id = data.get("id")
                return self._tab_id
        except Exception:
            req2 = urllib.request.Request(cdp.rstrip("/") + "/json/new?about:blank")
            with urllib.request.urlopen(req2, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                self._tab_id = data.get("id")
                return self._tab_id

    def open(self, url: str, cfg: Optional[Dict[str, Any]] = None, wait_s: float = 1.2) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            tid = self._ensure_tab(cdp)
            cdp_mod.cdp_call(tid, "Page.enable", {}, cdp=cdp)
            cdp_mod.cdp_call(tid, "Page.navigate", {"url": url}, cdp=cdp)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last_url = url
            title = ""
            try:
                title = cdp_mod.evaluate("document.title || ''", tab_id=tid, cdp=cdp) or ""
            except Exception:
                pass
            return {"ok": True, "engine": "obscura", "url": url, "title": str(title), "tab_id": tid}

    def read(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            tid = self._ensure_tab(cdp)
            page = cdp_mod.read_page(tab_id=tid, cdp=cdp)
            return {"ok": True, "engine": "obscura", **page}

    def evaluate(self, script: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            tid = self._ensure_tab(cdp)
            val = cdp_mod.evaluate(script, tab_id=tid, cdp=cdp)
            return {"ok": True, "engine": "obscura", "result": val}

    def click(self, selector: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            tid = self._ensure_tab(cdp)
            res = cdp_mod.click(selector, tab_id=tid, cdp=cdp)
            return {"ok": True, "engine": "obscura", "result": res}

    def fill(self, selector: str, value: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            tid = self._ensure_tab(cdp)
            res = cdp_mod.fill(selector, value, tab_id=tid, cdp=cdp)
            return {"ok": True, "engine": "obscura", "result": res}

    def scroll(self, amount: int = 600, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.evaluate("window.scrollBy(0, %d); window.scrollY;" % int(amount), cfg=cfg)

    def screenshot(self, path: Optional[str] = None, full_page: bool = False,
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            cdp = self.ensure_started(cfg)
            from . import browser as cdp_mod
            from .config import DATA_DIR
            tid = self._ensure_tab(cdp)
            params = {"format": "png"}
            if full_page:
                params["captureBeyondViewport"] = True
            res = cdp_mod.cdp_call(tid, "Page.captureScreenshot", params, cdp=cdp) or {}
            b64 = res.get("data")
            if not b64:
                return {"ok": False, "engine": "obscura", "error": "Obscura не вернул данные скриншота"}
            target = path or os.path.join(DATA_DIR, "shots", "obscura_%d.png" % int(time.time() * 1000))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return {"ok": True, "engine": "obscura", "path": target, "screenshot": target}

    def close(self):
        with self._lock:
            proc = self._proc
            self._proc = None
            self._tab_id = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


_SESSION = ObscuraSession()


def get_session() -> ObscuraSession:
    return _SESSION
