"""Реле: запросы из Worker проксируются в локальный сервер с авторизацией."""
import base64
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["LH_DATA"] = str(ROOT / "tests" / ".tmp")
os.environ.setdefault("LH_CONFIG", str(ROOT / "tests" / ".tmp" / "no-such-config.json"))
sys.path.insert(0, str(ROOT))

from harness import relay, remote, server


class RelayProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_request(self, msg, body=b""):
        node = relay.Relay(self.port)
        sent = []
        node._send = sent.append
        node._serve(msg, body)
        return sent

    @staticmethod
    def payload(messages):
        head = [m for m in messages if m.get("t") == "head"][0]
        chunks = b"".join(base64.b64decode(m["data"]) for m in messages if m.get("t") == "chunk")
        return head, chunks, messages[-1].get("t")

    def test_tunneled_get_without_cookie_is_denied(self):
        head, body, last = self.payload(self.run_request(
            {"id": "1", "method": "GET", "path": "/api/memory", "headers": {"host": "relay.test"}}))
        self.assertEqual(head["status"], 401)
        self.assertEqual(last, "end")

    def test_tunneled_get_with_cookie(self):
        token = remote.mint_token()
        head, body, last = self.payload(self.run_request({
            "id": "2", "method": "GET", "path": "/api/memory",
            "headers": {"host": "relay.test", "cookie": "lh_remote=" + token}}))
        self.assertEqual(head["status"], 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(last, "end")

    def test_tunneled_post_with_origin(self):
        token = remote.mint_token()
        raw = json.dumps({"text": "проверка реле"}).encode("utf-8")
        head, body, _ = self.payload(self.run_request({
            "id": "3", "method": "POST", "path": "/api/memory/add",
            "headers": {"host": "relay.test", "origin": "https://relay.test",
                        "cookie": "lh_remote=" + token, "content-type": "application/json",
                        "content-length": str(len(raw))}}, raw))
        self.assertEqual(head["status"], 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_foreign_origin_post_is_rejected(self):
        token = remote.mint_token()
        raw = b"{}"
        head, _, _ = self.payload(self.run_request({
            "id": "4", "method": "POST", "path": "/api/memory/add",
            "headers": {"host": "relay.test", "origin": "https://evil.test",
                        "cookie": "lh_remote=" + token, "content-type": "application/json",
                        "content-length": str(len(raw))}}, raw))
        self.assertEqual(head["status"], 403)

    def test_streamed_events_are_chunked(self):
        token = remote.mint_token()
        head, body, last = self.payload(self.run_request({
            "id": "5", "method": "GET", "path": "/api/events?session=missing&since=0",
            "headers": {"host": "relay.test", "cookie": "lh_remote=" + token}}))
        self.assertEqual(head["status"], 200)
        self.assertIn(b"session not found", body)
        self.assertEqual(last, "end")


if __name__ == "__main__":
    unittest.main()
