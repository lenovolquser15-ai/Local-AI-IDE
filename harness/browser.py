"""Minimal Chrome DevTools Protocol client over a hand-rolled WebSocket.

Used when the model must read a page that only works inside his real, logged-in
Chrome session (FunPay and friends). Loopback only, so the broken DNS is not in play.
"""
import base64
import json
import os
import socket
import struct
import urllib.parse

import requests

DEFAULT_CDP = "http://127.0.0.1:9222"


class BrowserError(Exception):
    pass


def _ws_url(cdp):
    try:
        tabs = requests.get(cdp.rstrip("/") + "/json/version", timeout=4).json()
        return tabs.get("webSocketDebuggerUrl")
    except Exception as exc:
        raise BrowserError("Chrome не отвечает на %s. Запусти его с --remote-debugging-port=9222." % cdp)


class _Frame:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_frame(self, timeout=15.0):
        self.sock.settimeout(timeout)
        while True:
            need = 2
            while len(self.buf) < need:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise BrowserError("соединение с Chrome закрылось")
                self.buf += chunk
            b1, b2 = self.buf[0], self.buf[1]
            length = b2 & 0x7F
            offset = 2
            if length == 126:
                while len(self.buf) < offset + 2:
                    self.buf += self.sock.recv(65536)
                length = struct.unpack(">H", self.buf[offset:offset + 2])[0]
                offset += 2
            elif length == 127:
                while len(self.buf) < offset + 8:
                    self.buf += self.sock.recv(65536)
                length = struct.unpack(">Q", self.buf[offset:offset + 8])[0]
                offset += 8
            while len(self.buf) < offset + length:
                self.buf += self.sock.recv(65536)
            payload = self.buf[offset:offset + length]
            self.buf = self.buf[offset + length:]
            if not (b1 & 0x0F) == 0x8:
                return payload
            return b""


def _connect(ws_url):
    parsed = urllib.parse.urlsplit(ws_url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=10)
    sock.sendall((
        "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
    ).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise BrowserError("Chrome отклонил upgrade")
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise BrowserError("рукопожатие WebSocket не прошло: %s" % resp.split(b"\r\n")[0])
    frame = _Frame(sock)
    frame.buf = resp.split(b"\r\n\r\n", 1)[1]
    return sock, frame


def _send(sock, payload):
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def tabs(cdp=DEFAULT_CDP):
    try:
        items = requests.get(cdp.rstrip("/") + "/json/list", timeout=5).json()
    except Exception as exc:
        raise BrowserError("Chrome не отвечает на %s. Нужен запуск с --remote-debugging-port=9222." % cdp)
    return [{"id": t.get("id"), "title": t.get("title"), "url": t.get("url"), "type": t.get("type")}
            for t in items if t.get("type") == "page"]


def evaluate(expression, tab_id=None, cdp=DEFAULT_CDP, timeout=20.0):
    items = requests.get(cdp.rstrip("/") + "/json/list", timeout=5).json()
    pages = [t for t in items if t.get("type") == "page"]
    if not pages:
        raise BrowserError("в Chrome нет ни одной вкладки")
    tab = pages[0]
    if tab_id:
        tab = next((t for t in pages if t.get("id") == tab_id), tab)
    sock, frame = _connect(tab["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expression, "returnByValue": True,
                                           "awaitPromise": True}}))
        deadline = timeout
        while True:
            raw = frame.read_frame(deadline)
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if msg.get("id") == 1:
                if msg.get("error"):
                    raise BrowserError(msg["error"].get("message", "CDP error"))
                result = msg.get("result", {}).get("result", {})
                return result.get("value")
    finally:
        try:
            sock.close()
        except Exception:
            pass


READ_JS = (
    "(() => { const t=document.title;"
    " const m=document.querySelector('meta[name=description]');"
    " return JSON.stringify({title:t, url:location.href,"
    " description:m?m.content:'', text:document.body.innerText.slice(0,60000)}); })()"
)


def read_page(url=None, tab_id=None, cdp=DEFAULT_CDP):
    if url:
        target = tab_id or None
        if not target:
            requests.get(cdp.rstrip("/") + "/json/new?" + urllib.parse.quote(url, safe=""), timeout=6)
        import time
        time.sleep(2.5)
        items = requests.get(cdp.rstrip("/") + "/json/list", timeout=5).json()
        pages = [t for t in items if t.get("type") == "page"]
        match = next((t for t in pages if t.get("url", "").startswith(url.split("?")[0])), None)
        target = match["id"] if match else (pages[0]["id"] if pages else None)
        if not target:
            raise BrowserError("не удалось открыть вкладку с %s" % url)
        tab_id = target
    raw = evaluate(READ_JS, tab_id=tab_id, cdp=cdp)
    try:
        return json.loads(raw)
    except Exception:
        return {"title": "", "url": tab_id or "", "text": str(raw)[:12000]}


def click(selector, tab_id=None, cdp=DEFAULT_CDP):
    js = (
        "(() => { const el=document.querySelector(%r); if(!el) return 'no-element';"
        " el.scrollIntoView(); el.click(); return 'clicked:'+(el.innerText||el.tagName); })()" % selector
    )
    return evaluate(js, tab_id=tab_id, cdp=cdp)


def fill(selector, value, tab_id=None, cdp=DEFAULT_CDP):
    js = (
        "(() => { const el=document.querySelector(%r); if(!el) return 'no-element';"
        " el.focus(); el.value=%r; el.dispatchEvent(new Event('input',{bubbles:true}));"
        " return 'filled'; })()" % (selector, value)
    )
    return evaluate(js, tab_id=tab_id, cdp=cdp)
