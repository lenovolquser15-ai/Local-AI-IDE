"""Fake OpenAI endpoint on loopback to exercise the agent loop without a GPU."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 12999
HITS = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _call(self, name, args, cid):
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send({"object": "list", "data": [{"id": "mock/tool-tester", "object": "model"}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        HITS["n"] += 1
        turn = HITS["n"]
        messages = req.get("messages") or []
        sys.stderr.write("mock turn %d tools=%s msgs=%d\n" % (turn, bool(req.get("tools")), len(messages)))
        if any("ping" in str(m.get("content")) for m in messages):
            if req.get("tools"):
                return self._send(self._call("ping", {}, "c0"))
            return self._send({"choices": [{"message": {"role": "assistant", "content": "TOOLS_UNDIALOG"}}]})
        observations = [m for m in messages if str(m.get("content") or "").startswith("[") or m.get("role") == "tool"]
        if turn == 1:
            return self._send(self._call("shell", {"cmd": "Get-Location"}, "c1"))
        if turn == 2 and observations:
            return self._send(self._call("python", {"code": "1+1"}, "c2"))
        if turn == 3 and observations:
            return self._send(self._call("shell", {"cmd": "Remove-Item -Recurse -Force C:\\Windows\\Temp"}, "c3"))
        if observations:
            return self._send({"choices": [{"message": {"role": "assistant",
                                                        "content": "Готово: проверил папку, посчитал 1+1 и отработал запрос на подтверждение."}}]})
        return self._send(self._call("shell", {"cmd": "Get-Location"}, "cX"))


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
