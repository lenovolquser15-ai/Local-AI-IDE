"""Пример плагина для LocalHarness: паузы и ожидание открытого порта.

Положил файл в C:\\LocalHarness\\plugins — кнопка «плагины» вверху подхватывает его без перезапуска.
Инструменты отсюда появляются в том же списке, что и встроенные, и модель видит их описание.
"""
import socket
import time

NAME = "waitfor"


def sleep(cfg, args):
    seconds = min(float(args.get("seconds") or 1), 600)
    time.sleep(seconds)
    return {"ok": True, "slept": seconds, "now": time.strftime("%H:%M:%S")}


def wait_port(cfg, args):
    host = args.get("host") or "127.0.0.1"
    port = int(args.get("port") or 0)
    if not port:
        return {"ok": False, "error": "нужен port"}
    timeout = min(float(args.get("timeout") or 30), 600)
    deadline = time.time() + timeout
    tries = 0
    last = None
    while time.time() < deadline:
        tries += 1
        try:
            with socket.create_connection((host, port), timeout=2):
                return {"ok": True, "open": True, "host": host, "port": port, "tries": tries}
        except OSError as exc:
            last = str(exc)
            time.sleep(1.0)
    return {"ok": False, "open": False, "host": host, "port": port, "tries": tries,
            "error": "за %s с порт не открылся (последняя ошибка: %s)" % (timeout, last)}


TOOLS = [
    {"name": "sleep",
     "desc": "Пауза между шагами (ждать, пока поднялся сервер или дописался лог). Максимум 600 секунд.",
     "params": {"seconds": ("number", "сколько ждать")},
     "required": ["seconds"],
     "fn": sleep},
    {"name": "wait_port",
     "desc": "Ждать, пока TCP-порт начнёт принимать соединения (например 1234 у LM Studio). "
             "Возвращается сразу, как порт открылся, или по таймауту.",
     "params": {"port": ("number", "номер порта"), "host": ("string?", "хост, по умолчанию 127.0.0.1"),
                "timeout": ("number?", "секунды ожидания, до 600")},
     "required": ["port"],
     "fn": wait_port},
]
