"""Entry point: python -m harness <serve|cli|wake|open|check|tools|build>"""
import os
import subprocess
import sys
import threading
import time
import webbrowser

from .config import load, save
from .stdout_fix import force_utf8

# Constants
DEFAULT_PORT = 7788
CHROME_CANDIDATES = [
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
]

def chrome_path():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None

def url(cfg):
    return "http://%s:%d" % (cfg.get("host", "127.0.0.1"), int(cfg.get("port", DEFAULT_PORT)))

def window_size():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(78)) or 1360
        height = int(user32.GetSystemMetrics(79)) or 900
    except Exception:
        return "1360,900"
    return "%d,%d" % (max(900, min(1440, width)), max(600, height - 44))

def open_ui(cfg):
    target = url(cfg)
    exe = chrome_path()
    if exe:
        flags = 0x00000008 if os.name == "nt" else 0
        subprocess.Popen([exe, "--app=%s" % target, "--window-size=%s" % window_size(),
                          "--window-position=0,0"], close_fds=True, creationflags=flags)
        return "chrome app window"
    webbrowser.open(target)
    return "обычный браузер"

def wake(cfg, wait=45.0):
    from .server import ensure_provider
    status = ensure_provider(cfg, wait=wait)
    if status["ok"]:
        print("сервер живой: %s · моделей %d" % (status["base"], len(status["models"])))
        for name in status["models"][:12]:
            print("  -", name)
    else:
        print("не поднялся: %s" % (status.get("error", "unknown")))
    return status
def port_open(cfg):
    import socket
    try:
        with socket.create_connection((cfg.get("host", "127.0.0.1"), int(cfg.get("port", DEFAULT_PORT))), timeout=1.5):
            return True
    except OSError:
        return False

def main(argv=None):
    force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "serve"
    if command in ("--tray", "--no-tray", "--background", "--open"):
        command = "serve"
    cfg = load()
    if command == "serve":
        from . import desktop
        open_browser = ("--background" not in argv
                        and ("--open" in argv or os.environ.get("LH_HEADLESS") != "1"))
        if port_open(cfg):
            print("харнес уже крутится на %s%s" % (url(cfg), " — открываю окно" if open_browser else ""))
            if open_browser:
                open_ui(cfg)
            return 0
        if desktop.should_use_tray(argv):
            desktop.run(cfg, open_browser=open_browser)
        else:
            from .server import serve
            if open_browser:
                threading.Thread(target=lambda: (time.sleep(2.0), open_ui(cfg)), daemon=True).start()
            serve(cfg)
        return 0
    if command in ("cli", "chat"):
        from .cli import main as cli_main
        return cli_main(argv[1:])
    if command == "open":
        print(open_ui(cfg))
        return 0
    if command == "wake":
        return 0 if wake(cfg)["ok"] else 1
    if command == "check":
        from .cli import preflight
        from . import schemas
        from . import dnsfix
        models = preflight(cfg)
        try:
            print("dns-обход: %s" % dnsfix.resolve("html.duckduckgo.com"))
        except Exception as exc:
            print("dns-обход сломан: %s" % exc)
        print("инструменты (%s): %s" % (cfg.get("tool_profile"), ", ".join(s["name"] for s in schemas.entries(cfg.get("tool_profile", "core")))))
        print("chrome cdp: %s" % (chrome_path() or "не найден"))
        return 0 if models else 1
    if command == "tools":
        from .cli import main as cli_main
        return cli_main(["tools"] + argv[1:])
    if command == "config":
        if len(argv) > 2:
            key, value = argv[1], argv[2]
            if key == "model":
                value = None if value == "auto" else value
            try:
                value = int(value) if value.lstrip("-").isdigit() else value
            except AttributeError:
                pass
            cfg[key] = value
            save(cfg)
        print("конфиг: %s" % cfg)
        return 0
    print("употребление: lh [serve|cli <вопрос>|check|wake|open|tools|config <ключ> <значение>]")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())

