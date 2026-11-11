"""Keep the console from killing the app: Windows shells here are cp1251 and rich raises on ✓ → ⚡."""
import sys


def force_utf8():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
