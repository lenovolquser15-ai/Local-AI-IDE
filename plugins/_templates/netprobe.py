"""Каталожный плагин netprobe: DNS и HTTP через обход сломанного разрешения имён.

Ставится из вкладки «Плагины → Каталог». Инструменты видны модели вместе с встроенными.
"""
def _netio():
    import harness.netio as netio
    return netio


def dns_lookup(cfg, args):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "нужен name, например html.duckduckgo.com"}
    import harness.dnsfix as dnsfix
    try:
        ip = dnsfix.resolve(name, timeout=float(args.get("timeout") or 8))
        return {"ok": True, "name": name, "ip": ip}
    except Exception as exc:
        return {"ok": False, "name": name, "error": "%s: %s" % (type(exc).__name__, exc)}


def http_ping(cfg, args):
    url = str(args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "нужен url"}
    netio = _netio()
    t0 = __import__("time").time()
    try:
        res = netio.request("GET", url, timeout=float(args.get("timeout") or 20), max_bytes=65536)
        ms = int((__import__("time").time() - t0) * 1000)
        return {"ok": res.get("status", 0) < 400, "url": res.get("url", url), "status": res.get("status"),
                "ms": ms, "bytes": len(res.get("body") or b"")}
    except Exception as exc:
        return {"ok": False, "url": url, "error": "%s: %s" % (type(exc).__name__), "exc": str(exc)[:200]}


NAME = "netprobe"
TOOLS = [
    {"name": "dns_lookup",
     "desc": "Резолв имени через обход сломанного DNS (dnspython/Resolve-DnsName). Возвращает IP.",
     "params": {"name": ("string", "домен, например html.duckduckgo.com"),
                "timeout": ("number?", "секунды, по умолчанию 8")},
     "required": ["name"], "fn": dns_lookup},
    {"name": "http_ping",
     "desc": "GET по URL через обход сломанного DNS. Возвращает статус, время и размер. Не для больших файлов.",
     "params": {"url": ("string", "https://…"), "timeout": ("number?", "секунды, по умолчанию 20")},
     "required": ["url"], "fn": http_ping},
]
