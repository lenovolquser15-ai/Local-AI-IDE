"""WebSocket client: рукопожатие, маскировка кадров, обмен сообщениями."""
import base64
import hashlib
import os
import re
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["LH_DATA"] = str(ROOT / "tests" / ".tmp")
os.environ.setdefault("LH_CONFIG", str(ROOT / "tests" / ".tmp" / "no-such-config.json"))
sys.path.insert(0, str(ROOT))

from harness.wsclient import GUID, OP_TEXT, WSError, WebSocket


def server_frame(opcode, payload):
    head = bytearray([0x80 | opcode])
    if len(payload) < 126:
        head.append(len(payload))
    else:
        head.append(126)
        head += struct.pack("!H", len(payload))
    return bytes(head) + payload


def read_client_frame(sock):
    head = sock.recv(2)
    if len(head) < 2:
        return None
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    mask = sock.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload += chunk
    if masked:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return head[0] & 0x0F, payload


class EchoWS:
    def __init__(self, mode="echo"):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n")[0].decode("latin-1")
            if self.mode == "reject":
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            key = re.search(r"Sec-WebSocket-Key:\s*(.+)", head, re.I).group(1).strip()
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode()
            conn.sendall((
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept
            ).encode("ascii"))
            conn.sendall(server_frame(OP_TEXT, b"hello"))
            frame = read_client_frame(conn)
            if frame and frame[0] == OP_TEXT:
                conn.sendall(server_frame(OP_TEXT, b"echo:" + frame[1]))
            read_client_frame(conn)
        finally:
            try:
                conn.close()
            except OSError:
                pass


class WSClientTests(unittest.TestCase):
    def test_handshake_and_echo(self):
        srv = EchoWS()
        ws = WebSocket("ws://127.0.0.1:%d/agent" % srv.port, timeout=5).connect()
        self.assertEqual(ws.recv(), (OP_TEXT, b"hello"))
        ws.send_text("привет")
        self.assertEqual(ws.recv(), (OP_TEXT, b"echo:\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82"))
        ws.close()
        self.assertIsNone(ws.sock)

    def test_rejected_handshake(self):
        srv = EchoWS(mode="reject")
        with self.assertRaises(WSError):
            WebSocket("ws://127.0.0.1:%d/agent" % srv.port, timeout=5).connect()


if __name__ == "__main__":
    unittest.main()
