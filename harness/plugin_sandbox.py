"""Изолированный запуск инструмента плагина.

Читает задание из stdin (JSON), вызывает fn(cfg, args) и печатает результат
одной строкой с маркером. Работает отдельным процессом, поэтому зависший или
упавший плагин не роняет сервер; родитель ограничивает его по времени.
"""
import importlib.util
import json
import os
import sys

MARKER = "@@LH_PLUGIN_RESULT@@"


def _emit(payload):
    sys.stdout.write("\n" + MARKER + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        _emit({"ok": False, "error": "не удалось прочитать задание: %s: %s" % (type(exc).__name__, exc)})
        return 2

    path = str(payload.get("file") or "")
    tool = str(payload.get("tool") or "")
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    cfg = payload.get("cfg") if isinstance(payload.get("cfg"), dict) else {}

    if not os.path.isfile(path):
        _emit({"ok": False, "error": "файл плагина не найден: %s" % path})
        return 2

    try:
        spec = importlib.util.spec_from_file_location("lh_plugin_sandbox", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        items = getattr(module, "TOOLS", None) or []
        item = next((t for t in items if isinstance(t, dict) and str(t.get("name")) == tool), None)
        if not item or not callable(item.get("fn")):
            _emit({"ok": False, "error": "инструмент '%s' не найден в файле плагина" % tool})
            return 2
        result = item["fn"](cfg, args)
        _emit({"ok": True, "result": result})
        return 0
    except Exception as exc:
        _emit({"ok": False, "error": "плагин упал: %s: %s" % (type(exc).__name__, exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
