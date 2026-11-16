"""Camoufox stealth browser driver for LocalHarness.

Provides anti-detect, fingerprint-spoofed web browsing with:
- Playwright-backed Firefox or native Camoufox engine
- Page reading, clicking, form filling, JS evaluation, scrolling
- Full screenshot support for vision/multimodal AI models
- DNS-fallback integration for Windows getaddrinfo stability
- Persistent session across agent turns
"""
import atexit
import base64
import glob
import logging
import os
import socket
import threading
import time
import warnings
from typing import Any, Dict, Optional

# Suppress known harmless Camoufox console warnings
warnings.filterwarnings("ignore")

logger = logging.getLogger("camoufox_browser")

# Ensure DNS resolution works reliably on Windows Winsock
try:
    from . import dnsfix

    _orig_getaddrinfo = socket.getaddrinfo

    def _safe_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)
        except Exception:
            try:
                ip = dnsfix.resolve(host)
                return _orig_getaddrinfo(ip, port, family, type, proto, flags)
            except Exception:
                raise

    socket.getaddrinfo = _safe_getaddrinfo
except Exception:
    pass


def _find_firefox_executable() -> Optional[str]:
    """Finds an installed Firefox or Camoufox executable on Windows/Linux/macOS."""
    # 1. Check native Camoufox cache
    try:
        from camoufox.pkgman import camoufox_path, LAUNCH_FILE, OS_NAME
        native = camoufox_path(download_if_missing=False)
        if native and os.path.exists(native):
            exe = os.path.join(str(native), LAUNCH_FILE.get(OS_NAME, "camoufox.exe"))
            if os.path.exists(exe):
                return exe
    except Exception:
        pass

    # 2. Check Playwright-installed Firefox in %LOCALAPPDATA%\ms-playwright\firefox-*\firefox\firefox.exe
    local_app_data = os.getenv("LOCALAPPDATA", "")
    if local_app_data:
        pattern = os.path.join(local_app_data, "ms-playwright", "firefox-*", "firefox", "firefox.exe")
        matches = glob.glob(pattern)
        if matches:
            # Ensure properties.json exists beside firefox.exe so Camoufox config validator succeeds
            ff_exe = matches[-1]
            ff_dir = os.path.dirname(ff_exe)
            prop_file = os.path.join(ff_dir, "properties.json")
            if not os.path.exists(prop_file):
                _write_fallback_properties(prop_file)
            return ff_exe

    # 3. Check standard Firefox installs
    candidates = [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _write_fallback_properties(path: str):
    """Writes minimal properties.json for Camoufox when running with stock/playwright Firefox."""
    try:
        import orjson
        minimal_props = [
            {"property": "general.useragent.override", "type": "string"},
            {"property": "privacy.resistFingerprinting", "type": "bool"},
            {"property": "webgl.disabled", "type": "bool"},
            {"property": "webgl.enable-webgl2", "type": "bool"},
            {"property": "webgl.force-enabled", "type": "bool"},
            {"property": "layout.css.devPixelsPerPx", "type": "string"},
            {"property": "intl.accept_languages", "type": "string"},
        ]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(orjson.dumps(minimal_props))
    except Exception:
        pass


class CamoufoxSession:
    """Singleton session holding an active Camoufox browser instance."""

    def __init__(self):
        # Reentrant: open()/read() hold the lock and _ensure_browser() may call close().
        self._lock = threading.RLock()
        self._cm = None
        self._browser = None
        self._context = None
        self._page = None
        self._headless = True
        self._last_active = 0.0

    def is_active(self) -> bool:
        return self._browser is not None and self._page is not None

    def _ensure_browser(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        headless = bool(cfg.get("camoufox_headless", True))
        humanize = bool(cfg.get("camoufox_humanize", True))

        if self._browser is not None and self._headless == headless:
            try:
                if self._page and not self._page.is_closed():
                    return
            except Exception:
                pass

        self.close()

        from camoufox.sync_api import Camoufox

        ff_exe = _find_firefox_executable()
        if not ff_exe:
            # Camoufox без браузера пытается скачивать сборку и висит; лучше честная ошибка.
            raise RuntimeError(
                "не найден Firefox для Camoufox: выполни `camoufox fetch` или `playwright install firefox`")
        launch_kw: Dict[str, Any] = {
            "headless": headless,
            "humanize": humanize,
            "exclude_addons": ["ubo"],
            "i_know_what_im_doing": True,
            "executable_path": ff_exe,
        }

        self._cm = Camoufox(**launch_kw)
        self._browser = self._cm.__enter__()
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        )
        self._page = self._context.new_page()
        self._headless = headless
        self._last_active = time.time()

    def open(self, url: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("file://"):
                url = "https://" + url
            self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._last_active = time.time()
            return {
                "ok": True,
                "title": self._page.title(),
                "url": self._page.url,
            }

    def read(self, tab_id: Optional[Any] = None, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            title = self._page.title()
            current_url = self._page.url
            try:
                meta = self._page.locator("meta[name='description']").first.get_attribute("content", timeout=1000)
            except Exception:
                meta = ""

            try:
                text = self._page.inner_text("body", timeout=5000)
            except Exception:
                text = ""

            self._last_active = time.time()
            return {
                "ok": True,
                "title": title,
                "url": current_url,
                "description": meta or "",
                "text": (text or "").strip()[:60000],
            }

    def screenshot(self, path: Optional[str] = None, full_page: bool = False,
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            if not path:
                from .config import DATA_DIR
                os.makedirs(DATA_DIR, exist_ok=True)
                path = os.path.join(DATA_DIR, "browser_screenshot.png")

            bytes_data = self._page.screenshot(path=path, full_page=full_page)
            b64_img = base64.b64encode(bytes_data).decode("ascii")
            self._last_active = time.time()
            return {
                "ok": True,
                "path": path,
                "title": self._page.title(),
                "url": self._page.url,
                "image": b64_img,
            }

    def click(self, selector: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            loc = self._page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=10000)
            self._last_active = time.time()
            return {"ok": True, "result": f"clicked '{selector}'"}

    def fill(self, selector: str, value: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            loc = self._page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.fill(value, timeout=10000)
            self._last_active = time.time()
            return {"ok": True, "result": f"filled '{selector}'"}

    def scroll(self, direction: str = "down", amount: int = 600,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            delta_y = amount if str(direction).lower() == "down" else -amount
            self._page.mouse.wheel(0, delta_y)
            time.sleep(0.3)
            self._last_active = time.time()
            return {"ok": True, "result": f"scrolled {direction} {amount}px"}

    def evaluate(self, expression: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_browser(cfg)
            res = self._page.evaluate(expression)
            self._last_active = time.time()
            return {"ok": True, "result": res}

    def tabs(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if not self.is_active():
                return {"ok": True, "tabs": []}
            tabs_list = []
            if self._context:
                for idx, p in enumerate(self._context.pages):
                    tabs_list.append({
                        "id": str(idx),
                        "title": p.title(),
                        "url": p.url,
                        "type": "page",
                    })
            return {"ok": True, "tabs": tabs_list}

    def close(self) -> Dict[str, Any]:
        with self._lock:
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            try:
                if self._cm and self._browser:
                    self._cm.__exit__(None, None, None)
            except Exception:
                pass
            self._cm = None
            self._browser = None
            self._context = None
            self._page = None
            return {"ok": True, "result": "browser closed"}

    def status(self) -> Dict[str, Any]:
        return {
            "active": self.is_active(),
            "headless": self._headless,
            "url": self._page.url if (self._page and not self._page.is_closed()) else None,
            "title": self._page.title() if (self._page and not self._page.is_closed()) else None,
            "installed": _find_firefox_executable() is not None,
        }


# Global session singleton
_SESSION = CamoufoxSession()


def get_session() -> CamoufoxSession:
    return _SESSION


@atexit.register
def _cleanup():
    try:
        _SESSION.close()
    except Exception:
        pass
