"""Реле: исходящий WebSocket до Worker и обратный HTTP-прокси к локальному серверу.

ПК сам открывает соединение на 443, поэтому NAT и проброс портов не мешают.
Worker присылает сжатые запросы, мы ходим в 127.0.0.1 и отдаём ответ кусками.
"""
import base64
import http.client
import json
import threading
import time

from . import remote
from .wsclient import WebSocket

READ_TIMEOUT = 90
PING_EVERY = 25
LOCAL_TIMEOUT = 300
READ_CHUNK = 64 * 1024
SKIP_HEADERS = ("connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer")


class Relay:
    def __init__(self, port):
        self.port = int(port)
        self.stop = threading.Event()
        self.ws = None
        self.send_lock = threading.Lock()
        self.bodies = {}
        self.requests = {}
        self.cancels = set()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="LH-relay", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self._drop()

    def _drop(self):
        with self.send_lock:
            ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    @property
    def connected(self):
        return self.ws is not None

    def _send(self, payload):
        text = json.dumps(payload, separators=(",", ":"))
        with self.send_lock:
            ws = self.ws
            if ws is None:
                return False
            try:
                ws.send_text(text)
                return True
            except Exception:
                self.ws = None
        try:
            ws.close()
        except Exception:
            pass
        return False

    def _run(self):
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:
                message = str(exc)[:200] or exc.__class__.__name__
                remote._log("реле: " + message)
                if not self.stop.is_set():
                    remote._set(connected=False, phase="starting",
                                error="реле переподключается: " + message)
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 1.7, 8.0)

    def _session(self):
        worker = remote.worker_url()
        if not worker:
            raise RuntimeError("не задан адрес реле — укажи его в настройках")
        remote._set(url=worker, phase="starting", error="", connected=False)
        code, exp = remote.ensure_code()
        if not remote.register(code, worker, exp, relay=True):
            raise RuntimeError("реле не приняло код: %s" %
                               (remote.STATE.get("worker_error") or "нет ответа"))
        url = worker.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/agent"
        ts = str(int(time.time()))
        ws = WebSocket(url, timeout=20, headers={
            "X-LH-Code": code,
            "X-LH-Ts": ts,
            "X-LH-Sign": remote.sign(ts + "." + code),
        })
        ws.connect()
        ws.settimeout(READ_TIMEOUT)
        with self.send_lock:
            self.ws = ws
        remote._set(connected=True, phase="running", error="", room=True, worker_error="")
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()
        try:
            while not self.stop.is_set():
                kind, data = ws.recv()
                if kind != 1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                self._handle(msg)
        finally:
            with self.send_lock:
                if self.ws is ws:
                    self.ws = None
            remote._set(connected=False)
            try:
                ws.close()
            except Exception:
                pass

    def _ping(self, ws):
        while not self.stop.is_set():
            if self.stop.wait(PING_EVERY):
                return
            with self.send_lock:
                if self.ws is not ws:
                    return
                try:
                    ws.send_text('{"t":"ping"}')
                except Exception:
                    return
            try:
                remote.keep_alive()
            except Exception as exc:
                remote._log("продление кода: " + str(exc)[:160])

    def _handle(self, msg):
        kind = msg.get("t")
        rid = str(msg.get("id") or "")
        if not rid:
            return
        if kind == "req":
            body = msg.get("body")
            if body is not None:
                self._dispatch(msg, base64.b64decode(body))
            elif int(msg.get("size") or 0) > 0:
                self.bodies[rid] = bytearray()
                self.requests[rid] = msg
            else:
                self._dispatch(msg, b"")
        elif kind == "body":
            buf = self.bodies.get(rid)
            if buf is None:
                return
            buf += base64.b64decode(msg.get("data") or "")
            self._send({"t": "ack", "id": rid, "n": int(msg.get("n") or 0)})
        elif kind == "bodyend":
            raw = bytes(self.bodies.pop(rid, b""))
            original = self.requests.pop(rid, msg)
            self._dispatch(original, raw)
        elif kind == "cancel":
            self.cancels.add(rid)

    def _dispatch(self, msg, body):
        threading.Thread(target=self._serve, args=(msg, body),
                         name="LH-relay-req", daemon=True).start()

    def _serve(self, msg, body):
        rid = str(msg.get("id") or "")
        conn = None
        live = self.ws is not None
        try:
            if rid in self.cancels:
                self.cancels.discard(rid)
                return
            headers = dict(msg.get("headers") or {})
            headers["X-LH-Relay"] = "1"
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=LOCAL_TIMEOUT)
            conn.putrequest(str(msg.get("method") or "GET"), str(msg.get("path") or "/"),
                            skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                if str(name).lower() in SKIP_HEADERS:
                    continue
                conn.putheader(str(name), str(value))
            conn.endheaders(body or None)
            resp = conn.getresponse()
            head = [[str(k), str(v)] for k, v in resp.getheaders()]
            self._send({"t": "head", "id": rid, "status": resp.status, "headers": head})
            if live and not self.connected:
                return
            stopped = False
            while not self.stop.is_set() and (not live or self.connected):
                if rid in self.cancels:
                    self.cancels.discard(rid)
                    stopped = True
                    break
                chunk = resp.read1(READ_CHUNK)
                if not chunk:
                    break
                self._send({"t": "chunk", "id": rid,
                            "data": base64.b64encode(chunk).decode("ascii")})
                if live and not self.connected:
                    stopped = True
                    break
            if not stopped:
                self._send({"t": "end", "id": rid})
        except Exception as exc:
            self.cancels.discard(rid)
            self._send({"t": "err", "id": rid, "error": str(exc)[:200]})
        finally:
            self.bodies.pop(rid, None)
            self.requests.pop(rid, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
