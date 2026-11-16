"""Минимальный клиент WebSocket (RFC 6455) без внешних зависимостей.

Нужен исходящий wss на 443: getaddrinfo на этой машине сломан, поэтому адрес
берём у dnsfix, а TLS проверяем по имени хоста.
"""
import base64
import hashlib
import os
import socket
import struct
from urllib.parse import urlsplit

from . import dnsfix

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 1, 2, 8, 9, 10
MAX_FRAME = 8 * 1024 * 1024


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url, headers=None, timeout=30.0):
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WSError("поддерживаются только ws:// и wss://")
        self.secure = parts.scheme == "wss"
        self.host = parts.hostname or ""
        self.port = int(parts.port or (443 if self.secure else 80))
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    def connect(self):
        if not self.host:
            raise WSError("в адресе нет хоста")
        ip = dnsfix.resolve(self.host)
        raw = socket.create_connection((ip, self.port), timeout=self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.secure:
            raw = dnsfix.pinned_context().wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            "GET %s HTTP/1.1" % self.path,
            "Host: %s" % self.host,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "User-Agent: LocalHarness",
        ]
        for name, value in self.headers.items():
            lines.append("%s: %s" % (name, value))
        self._raw_send(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        text = self._read_head().decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if " 101 " not in (status + " "):
            raise WSError("рукопожатие отклонено: %s" % status.strip())
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if ("sec-websocket-accept: " + expect).lower() not in text.lower():
            raise WSError("сервер ответил неверным ключом websocket")
        return self

    def _raw_send(self, data):
        self.sock.sendall(data)

    def _read_head(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("соединение закрыто на рукопожатии")
            data += chunk
            if len(data) > 65536:
                raise WSError("слишком большой ответ на рукопожатие")
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest
        return head

    def _recv_exact(self, size):
        while len(self._buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("соединение закрыто")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def _read_frame(self):
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_FRAME:
            raise WSError("слишком большой кадр")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        parts = []
        kind = OP_TEXT
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WSError("сервер закрыл соединение")
            if opcode in (OP_TEXT, OP_BINARY):
                kind, parts = opcode, [payload]
            else:
                parts.append(payload)
            if fin:
                return kind, b"".join(parts)

    def send_frame(self, opcode, payload):
        if self.sock is None:
            raise WSError("соединение не открыто")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._raw_send(bytes(header) + masked)

    def send_text(self, text):
        self.send_frame(OP_TEXT, str(text).encode("utf-8"))

    def settimeout(self, seconds):
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def close(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            self.send_frame_to(sock, OP_CLOSE, b"\x03\xe8")
        except Exception:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def send_frame_to(sock, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        sock.sendall(bytes(header) + masked)
