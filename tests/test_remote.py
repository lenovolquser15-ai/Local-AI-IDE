"""Remote access: signed tokens, cookie gate for tunneled traffic and the relay Worker."""
import hashlib
import hmac
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["LH_DATA"] = str(ROOT / "tests" / ".tmp")
os.environ.setdefault("LH_CONFIG", str(ROOT / "tests" / ".tmp" / "no-such-config.json"))
sys.path.insert(0, str(ROOT))

from harness import config as cfgmod
from harness import remote, server


class TokenTests(unittest.TestCase):
    def test_code_shape(self):
        for _ in range(50):
            code = remote.make_code()
            self.assertRegex(code, r"^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{10}$")
            self.assertEqual(remote.normalize_code(remote.format_code(code)), code)
            self.assertEqual(remote.normalize_code(code.lower()), code)

    def test_normalize_rejects_short(self):
        self.assertEqual(remote.normalize_code("abc"), "")
        self.assertEqual(remote.normalize_code(""), "")
        self.assertEqual(remote.normalize_code("O0I1L1UUUU"), "001111VVVV")
        self.assertEqual(remote.normalize_code("O0I1L1UUUUU"), "")

    def test_token_roundtrip(self):
        info = remote.verify_token(remote.mint_token())
        self.assertIsNotNone(info)
        self.assertGreater(info["exp"], time.time())
        self.assertEqual(info["epoch"], remote.epoch())

    def test_tampered_token(self):
        parts = remote.mint_token().split(".")
        parts[4] = "0" * 64
        self.assertIsNone(remote.verify_token(".".join(parts)))
        self.assertIsNone(remote.verify_token("v1.1.2.3"))
        self.assertIsNone(remote.verify_token(""))

    def test_expired_token(self):
        self.assertIsNone(remote.verify_token(remote.mint_token(exp=int(time.time()) - 5)))

    def test_epoch_revokes(self):
        token = remote.mint_token()
        old = remote.epoch()
        try:
            remote.save_cfg(epoch=old + 1)
            self.assertIsNone(remote.verify_token(token))
        finally:
            remote.save_cfg(epoch=old)
        self.assertIsNotNone(remote.verify_token(token))

    def test_cookie_and_tunnel_detection(self):
        token = remote.mint_token()
        self.assertTrue(remote.authorized({"Cookie": "a=1; lh_remote=%s" % token}))
        self.assertFalse(remote.authorized({"Cookie": "lh_remote=bad"}))
        self.assertFalse(remote.authorized({}))
        self.assertTrue(remote.is_tunnel({"CF-Ray": "8a1"}))
        self.assertTrue(remote.is_tunnel({"CF-Connecting-IP": "1.2.3.4"}))
        self.assertTrue(remote.is_tunnel({"X-LH-Relay": "1"}))
        self.assertFalse(remote.is_tunnel({}))


class Relay(BaseHTTPRequestHandler):
    rooms = {}
    broken = False

    def log_message(self, *args):
        pass

    def _send(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        ts = self.headers.get("X-LH-Ts") or ""
        expect = hmac.new(remote.secret().encode(), (ts + "." + body).encode(),
                          hashlib.sha256).hexdigest()
        good = hmac.compare_digest(expect, self.headers.get("X-LH-Sign") or "")
        if self.path == "/api/rooms" and good and not Relay.broken:
            data = json.loads(body)
            Relay.rooms[data["code"]] = data
            self._send({"ok": True})
        else:
            self._send({"ok": False, "error": "bad"}, 401)


class RegisterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.old = dict(remote.remote_cfg())
        remote.save_cfg(worker_url="http://127.0.0.1:%d" % cls.port)

    @classmethod
    def tearDownClass(cls):
        remote.save_cfg(worker_url=cls.old.get("worker_url", ""))
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_register_and_failure(self):
        code = remote.make_code()
        exp = int(time.time()) + 600
        self.assertTrue(remote.register(code, "https://abc.trycloudflare.com", exp))
        self.assertEqual(Relay.rooms[code]["exp"], exp)
        Relay.broken = True
        try:
            self.assertFalse(remote.register(remote.make_code(), "https://abc.trycloudflare.com", exp))
        finally:
            Relay.broken = False

    def test_keep_alive_extends_code(self):
        saved = dict(remote.STATE)
        code = remote.make_code()
        exp = int(time.time()) + 120
        try:
            remote.STATE.update(code=code, exp=exp, url="http://127.0.0.1:%d" % self.port,
                                mode="relay", connected=True)
            self.assertTrue(remote.keep_alive())
            self.assertIn(code, Relay.rooms)
            self.assertGreater(Relay.rooms[code]["exp"], exp)
            self.assertEqual(Relay.rooms[code]["exp"], remote.STATE["exp"])
            self.assertFalse(remote.keep_alive())
            Relay.broken = True
            try:
                short = int(time.time()) + 60
                remote.STATE["exp"] = short
                self.assertFalse(remote.keep_alive())
            finally:
                Relay.broken = False
            self.assertEqual(remote.STATE["exp"], short)
        finally:
            remote.STATE.clear()
            remote.STATE.update(saved)


class HttpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def request(self, path, method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        status, extra = res.status, dict(res.getheaders())
        conn.close()
        return status, data, extra

    def test_loopback_stays_open(self):
        status, data, _ = self.request("/api/memory")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_tunnel_without_code_is_closed(self):
        status, data, _ = self.request("/api/memory", headers={"CF-Ray": "8a1"})
        self.assertEqual(status, 401)
        self.assertIn("Нужен код доступа".encode("utf-8"), data)

    def test_claim_sets_secure_cookie(self):
        token = remote.mint_token()
        status, _, headers = self.request(
            "/api/remote/claim?rt=" + token,
            headers={"CF-Ray": "8a1", "X-Forwarded-Proto": "https"})
        self.assertEqual(status, 302)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("lh_remote=" + token, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(headers.get("Location"), "/")

    def test_claim_cookie_opens_access(self):
        token = remote.mint_token()
        status, data, _ = self.request(
            "/api/memory", headers={"CF-Ray": "8a1", "Cookie": "lh_remote=" + token})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_bad_claim(self):
        status, _, _ = self.request("/api/remote/claim?rt=nonsense", headers={"CF-Ray": "8a1"})
        self.assertEqual(status, 403)

    def test_lan_claim_has_no_secure_flag(self):
        token = remote.mint_token()
        status, _, headers = self.request("/api/remote/claim?rt=" + token)
        self.assertEqual(status, 302)
        self.assertNotIn("Secure", headers.get("Set-Cookie", ""))

    def test_remote_post_rejects_foreign_origin(self):
        token = remote.mint_token()
        before = remote.epoch()
        status, _, _ = self.request(
            "/api/remote/rotate", method="POST",
            headers={"CF-Ray": "8a1", "Cookie": "lh_remote=" + token,
                     "Origin": "https://evil.example", "Content-Type": "application/json"},
            body="{}")
        self.assertEqual(status, 403)
        self.assertEqual(remote.epoch(), before)

    def test_remote_post_requires_cookie(self):
        status, _, _ = self.request(
            "/api/remote/rotate", method="POST",
            headers={"CF-Ray": "8a1", "Content-Type": "application/json"}, body="{}")
        self.assertEqual(status, 401)


class WorkerTests(unittest.TestCase):
    def node(self):
        candidates = [os.environ.get("LH_NODE") or "",
                      str(ROOT / "data" / "bin" / "nodejs" / "node.exe"),
                      shutil.which("node") or ""]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return ""

    def test_worker_relay(self):
        node = self.node()
        if not node:
            self.skipTest("Node.js не найден")
        script = ROOT / "tests" / "test_worker.mjs"
        proc = subprocess.run([node, str(script), remote.secret()],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, (proc.stdout or "") + (proc.stderr or ""))
        lines = [line for line in (proc.stdout or "").splitlines() if line.startswith("{")]
        self.assertTrue(lines, proc.stdout)
        data = json.loads(lines[-1])
        self.assertTrue(data["ok"])
        info = remote.verify_token(data["token"])
        self.assertIsNotNone(info)
        self.assertEqual(info["exp"], data["exp"])
        self.assertEqual(info["epoch"], data["epoch"])


class DefaultsTests(unittest.TestCase):
    def test_remote_defaults(self):
        cfg = cfgmod.load()
        self.assertIn("remote", cfg)
        self.assertIn("worker_url", cfg["remote"])
        self.assertFalse(cfg["remote"]["auto_start"])


if __name__ == "__main__":
    unittest.main()
