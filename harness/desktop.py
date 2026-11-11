"""Windows tray lifecycle and explicit, per-user autostart registration."""
import logging
import os
import subprocess
import sys
import threading

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "LocalHarness"
_AUTOSTART_LOCK = threading.RLock()
_STATE_LOCK = threading.Lock()
_runtime = None


def should_use_tray(argv):
    """Headless and --no-tray always override the frozen Windows default."""
    return (sys.platform == "win32" and os.environ.get("LH_HEADLESS") != "1"
            and "--no-tray" not in argv
            and ("--tray" in argv or bool(getattr(sys, "frozen", False))))


def _autostart_command():
    args = [os.path.abspath(sys.executable)]
    if not getattr(sys, "frozen", False):
        # Unlike argv[0], this also works with python -m harness and test runners.
        args.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lh_entry.py")))
    return subprocess.list2cmdline(args + ["serve", "--tray", "--background"])


def _query_value(winreg, key):
    try:
        return winreg.QueryValueEx(key, _RUN_NAME)
    except FileNotFoundError:
        return None


def _owned(value, winreg):
    return value == (_autostart_command(), winreg.REG_SZ)


def status():
    """Return {supported, enabled, tray}; registry access errors propagate."""
    supported = sys.platform == "win32"
    enabled = False
    if supported:
        import winreg
        with _AUTOSTART_LOCK:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
                    enabled = _owned(_query_value(winreg, key), winreg)
            except FileNotFoundError:
                pass
    with _STATE_LOCK:
        tray = _runtime is not None and _runtime.ready.is_set()
    return {"supported": supported, "enabled": enabled, "tray": tray}


def set_autostart(enabled):
    """Change only this installation's HKCU Run command, never a foreign one."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    if sys.platform != "win32":
        raise OSError("Autostart is only supported on Windows")
    import winreg
    with _AUTOSTART_LOCK:
        access = winreg.KEY_READ | winreg.KEY_SET_VALUE
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access) as key:
                value = _query_value(winreg, key)
                if value is not None and not _owned(value, winreg):
                    raise FileExistsError("LocalHarness autostart belongs to another command")
                winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access)
            except FileNotFoundError:
                key = None
            if key is not None:
                with key:
                    if _owned(_query_value(winreg, key), winreg):
                        winreg.DeleteValue(key, _RUN_NAME)
        return status()


def _hide_owned_console():
    """Conservatively hide only a frozen process's unshared, visible console."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    import ctypes
    from ctypes import wintypes
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel32.GetConsoleProcessList.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    hwnd = kernel32.GetConsoleWindow()
    processes = (wintypes.DWORD * 2)()
    count = kernel32.GetConsoleProcessList(processes, len(processes))
    if (hwnd and count == 1 and processes[0] == os.getpid()
            and user32.IsWindowVisible(hwnd)):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW(hwnd, name, len(name))
        if name.value == "PseudoConsoleWindow":
            kernel32.FreeConsole()
            return None
        user32.ShowWindow(hwnd, 0)
        return hwnd
    return None


def _restore_console(hwnd):
    if hwnd:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 5)


def _icon_image():
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=12, fill="#173746")
    # Pixel-independent block lettering, without requiring an installed font.
    draw.line([(15, 17), (15, 46), (28, 46)], fill="white", width=5)
    draw.line([(36, 17), (36, 46)], fill="white", width=5)
    draw.line([(50, 17), (50, 46)], fill="white", width=5)
    draw.line([(36, 31), (50, 31)], fill="white", width=5)
    return image


class _Runtime:
    def __init__(self, cfg, pystray, open_browser):
        from . import server
        self.cfg = cfg
        self.server_module = server
        self.open_browser = open_browser
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self._exit_lock = threading.Lock()
        self._chat_lock = threading.Lock()
        self._shutdown_thread = None
        self._http_thread = None
        self._browser_timer = None
        self._hidden_console = None
        self._startup_error = None
        runtime = self

        class Handler(server.Handler):
            def handle_chat(self, body):
                # Close the gap between the stop snapshot and in-flight requests
                # registering new bridges. Never call bridge callbacks under RUNS_LOCK.
                with runtime._chat_lock:
                    if runtime.stopping.is_set():
                        server._json_response(self, {"ok": False, "error": "Server is stopping"}, 503)
                        return
                    super().handle_chat(body)

        self.icon = pystray.Icon("LocalHarness", _icon_image(), "LocalHarness", menu=pystray.Menu(
            pystray.MenuItem("Open", self._open, default=True),
            pystray.MenuItem("Exit", self.request_exit)))
        self.httpd = server.ThreadingHTTPServer(
            (cfg.get("host", "127.0.0.1"), int(cfg.get("port", 7788))), Handler)

    def _open(self, icon=None, item=None):
        if not self.stopping.is_set():
            from .__main__ import open_ui
            open_ui(self.cfg)

    def _setup(self, icon):
        try:
            with self._exit_lock:
                if self.stopping.is_set():
                    return
                icon.visible = True
                self._hidden_console = _hide_owned_console()
                self.ready.set()
                if self.open_browser:
                    self._browser_timer = threading.Timer(2.0, self._open)
                    self._browser_timer.daemon = True
                    self._browser_timer.start()
        except Exception as exc:
            # pystray runs setup in its own thread: surface errors on the main
            # thread rather than leaving an inaccessible server running.
            self._startup_error = exc
            self.request_exit()

    def request_exit(self, icon=None, item=None):
        """Menu callbacks must never join the HTTP loop or the tray setup thread."""
        with self._exit_lock:
            if self._shutdown_thread is not None:
                return
            self.stopping.set()
            self._shutdown_thread = threading.Thread(target=self._shutdown, name="LH-shutdown")
            self._shutdown_thread.start()

    def _shutdown(self):
        try:
            with self._exit_lock:
                if self._browser_timer is not None:
                    self._browser_timer.cancel()
            with self._chat_lock:
                with self.server_module.RUNS_LOCK:
                    bridges = [run.bridge for run in self.server_module.RUNS.values()
                               if not run.finished and run.bridge is not None]
            for bridge in bridges:
                try:
                    bridge.request_stop()
                except Exception:
                    logging.exception("Could not signal an active bridge")
        finally:
            try:
                if self._http_thread is not None:
                    if self._http_thread.is_alive():
                        self.httpd.shutdown()
                    self._http_thread.join()
            finally:
                try:
                    self.httpd.server_close()
                finally:
                    try:
                        _restore_console(self._hidden_console)
                    finally:
                        self.ready.clear()
                        self.icon.stop()

    def run(self):
        try:
            thread = threading.Thread(target=self.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.2}, name="LH-http", daemon=True)
            thread.start()
            self._http_thread = thread
            self.icon.run(setup=self._setup)
            if self._startup_error is not None:
                raise RuntimeError("Could not start the desktop tray") from self._startup_error
        except KeyboardInterrupt:
            pass
        finally:
            self.request_exit()
            self._shutdown_thread.join()


def run(cfg, *, open_browser=True):
    """Own one HTTP server and run the tray loop on the calling (main) thread."""
    if sys.platform != "win32":
        raise OSError("The desktop tray is only supported on Windows")
    import pystray
    global _runtime
    with _STATE_LOCK:
        if _runtime is not None:
            raise RuntimeError("The desktop tray is already running")
        runtime = _Runtime(cfg, pystray, open_browser)
        _runtime = runtime
    try:
        runtime.run()
    finally:
        with _STATE_LOCK:
            _runtime = None
