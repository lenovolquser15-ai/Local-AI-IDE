"""Windows Toast notifications (zero-dependency WinRT / BalloonTip fallback)."""
import os
import subprocess
import threading

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.Lock()
_LAST_NOTIFICATIONS = []


def _escape_ps_xml(text):
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _send_windows_toast(title, message):
    t = _escape_ps_xml(title[:100])
    m = _escape_ps_xml(message[:280])
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>%s</text><text>%s</text></binding></visual></toast>'); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LocalHarness'); "
        "$notifier.Show($toast)"
    ) % (t.replace("'", "''"), m.replace("'", "''"))
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=6,
            creationflags=NO_WINDOW,
        )
        return True
    except Exception:
        return False


def send(title, message, cfg=None, async_send=True):
    """Send a desktop toast notification if enabled in config."""
    if cfg is not None and not cfg.get("toast_notifications", True):
        return False
    entry = {"title": str(title or "LocalHarness"), "message": str(message or "")}
    with _LOCK:
        _LAST_NOTIFICATIONS.append(entry)
        del _LAST_NOTIFICATIONS[:-30]
    if os.name != "nt" or os.environ.get("LH_NO_TOAST") == "1":
        return True
    if async_send:
        threading.Thread(target=_send_windows_toast, args=(entry["title"], entry["message"]), daemon=True).start()
        return True
    return _send_windows_toast(entry["title"], entry["message"])


def recent():
    with _LOCK:
        return list(_LAST_NOTIFICATIONS)
