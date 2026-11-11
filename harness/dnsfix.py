"""Name resolution that bypasses the broken Winsock getaddrinfo on this PC.

Windows here returns WSAHOST_NOT_FOUND (11001) for every external name, while raw
DNS over UDP and Resolve-DnsName both work. So resolve to an address ourselves and
open the socket to that address, keeping the real hostname for SNI and validation.
"""
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time

from .config import DATA_DIR

IPS = {}
LOCK = threading.Lock()
CACHE_FILE = os.path.join(DATA_DIR, "dns_cache.json")
SERVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
TTL = 3600
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ResolveError(Exception):
    pass


def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        now = time.time()
        for host, item in blob.items():
            if item.get("expires", 0) > now and item.get("ip"):
                IPS[host] = item["ip"]
    except Exception:
        pass


def _save_cache():
    blob = {}
    now = time.time()
    for host, ip in list(IPS.items()):
        blob[host] = {"ip": ip, "expires": now + TTL}
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
    except Exception:
        pass


def _via_dnspython(host, timeout):
    try:
        import dns.resolver
    except ImportError:
        return None
    for server in SERVERS:
        try:
            res = dns.resolver.Resolver(configure=False)
            res.nameservers = [server]
            res.timeout = timeout
            res.lifetime = timeout
            answers = res.resolve(host, "A")
            ips = [a.to_text() for a in answers if re.match(r"^\d+(\.\d+){3}$", a.to_text())]
            if ips:
                return ips
        except Exception:
            continue
    return None


def _ips_from_text(out, host):
    found = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out)
    if not found:
        return []
    marker = re.search(r"(?:^|\n)\s*(?:Name|Имя)\s*:\s*[^\n]*", out)
    if marker:
        tail = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", marker.group(0) + out[marker.end():])
        if tail:
            return tail
    return found[1:] or found


def _via_nslookup(host, timeout):
    try:
        proc = subprocess.run(
            ["nslookup", host], capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
        )
        ips = _ips_from_text((proc.stdout or "") + (proc.stderr or ""), host)
        return ips or None
    except Exception:
        return None


def _via_powershell(host, timeout):
    script = (
        "$ProgressPreference='SilentlyContinue';"
        "$r=Resolve-DnsName -Name '%s' -Server 1.1.1.1 -DnsOnly -Type A -ErrorAction Stop;"
        "($r | Where-Object IPAddress | ForEach-Object IPAddress) -join ','" % host.replace("'", "")
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
        )
        ips = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", proc.stdout or "")
        return ips or None
    except Exception:
        return None


_load_cache()


def resolve(host, timeout=4.0):
    if re.match(r"^\d+(\.\d+){3}$", host) or host in ("localhost",):
        return host
    with LOCK:
        hit = IPS.get(host)
        if hit:
            return hit
    ips = None
    for step in (_via_dnspython, _via_powershell, _via_nslookup):
        ips = step(host, timeout)
        if ips:
            break
    if not ips:
        raise ResolveError("не смог разрешить имя %s (DNS сломан на уровне getaddrinfo)" % host)
    ip = ips[0]
    with LOCK:
        IPS[host] = ip
        _save_cache()
    return ip


def pinned_context():
    return ssl.create_default_context()


class PinHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        ip = resolve(self.host)
        self.sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)


class PinHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        ip = resolve(self.host)
        sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)
        self.sock = pinned_context().wrap_socket(sock, server_hostname=self.host)


def connection_for(url, timeout=30.0):
    scheme = url.split("://", 1)[0]
    rest = url.split("://", 1)[1]
    host = rest.split("/", 1)[0]
    path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    if ":" in host:
        hostname, port = host.rsplit(":", 1)
    else:
        hostname, port = host, 443 if scheme == "https" else 80
    if scheme == "https":
        return PinHTTPSConnection(hostname, int(port), timeout=timeout)
    return PinHTTPConnection(hostname, int(port), timeout=timeout)
