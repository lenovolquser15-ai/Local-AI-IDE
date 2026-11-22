"""Model Context Protocol (MCP) stdio JSON-RPC 2.0 client and server registry."""
import json
import os
import re
import shlex
import subprocess
import threading
import time

from . import config as config_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.RLock()
_CLIENTS = {}  # server_name -> McpStdioClient


class McpStdioClient:
    """Manages a single stdio MCP server process (JSON-RPC 2.0 over stdin/stdout)."""

    def __init__(self, name, command, args=None, env=None):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.proc = None
        self.tools = []
        self.error = ""
        self._req_id = 0
        self._io_lock = threading.Lock()
        self._connected = False

    def start(self, timeout=12.0):
        with self._io_lock:
            if self.proc and self.proc.poll() is None and self._connected:
                return True
            self.stop_locked()
            argv = [self.command] + self.args
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in self.env.items()})
            try:
                self.proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    creationflags=NO_WINDOW,
                )
            except Exception as exc:
                self.error = "Не удалось запустить MCP %s: %s" % (self.name, exc)
                return False

            try:
                init_res = self._rpc_locked(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "LocalHarness", "version": "1.0.8"},
                    },
                    timeout=timeout,
                )
                if "error" in init_res:
                    self.error = str(init_res["error"])
                    self.stop_locked()
                    return False
                self._notify_locked("notifications/initialized", {})
                tools_res = self._rpc_locked("tools/list", {}, timeout=timeout)
                raw_tools = (tools_res.get("result") or {}).get("tools") or []
                self.tools = [t for t in raw_tools if isinstance(t, dict) and t.get("name")]
                self._connected = True
                self.error = ""
                return True
            except Exception as exc:
                self.error = str(exc)[:300]
                self.stop_locked()
                return False

    def _notify_locked(self, method, params):
        if not self.proc or not self.proc.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}, ensure_ascii=False) + "\n"
        self.proc.stdin.write(msg.encode("utf-8"))
        self.proc.stdin.flush()

    def _rpc_locked(self, method, params, timeout=15.0):
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("MCP-процесс не запущен")
        self._req_id += 1
        rid = self._req_id
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, ensure_ascii=False) + "\n"
        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()

        result_box = {"data": None, "err": None}

        def _reader():
            try:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        result_box["err"] = "MCP закрыл stdout"
                        return
                    text = line.decode("utf-8", "replace").strip()
                    if not text or not text.startswith("{"):
                        continue
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        continue
                    if obj.get("id") == rid:
                        result_box["data"] = obj
                        return
            except Exception as exc:
                result_box["err"] = str(exc)

        th = threading.Thread(target=_reader, daemon=True)
        th.start()
        th.join(timeout=timeout)
        if th.is_alive():
            raise TimeoutError("MCP %s: таймаут %s с на %s" % (self.name, timeout, method))
        if result_box["err"]:
            raise RuntimeError(result_box["err"])
        return result_box["data"] or {}

    def call_tool(self, tool_name, arguments=None, timeout=30.0):
        if not self._connected or not self.proc or self.proc.poll() is not None:
            if not self.start():
                return {"ok": False, "error": self.error or "MCP-сервер недоступен"}
        with self._io_lock:
            try:
                resp = self._rpc_locked("tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout=timeout)
                if "error" in resp:
                    return {"ok": False, "error": str(resp["error"])}
                res = resp.get("result") or {}
                content = res.get("content") or []
                texts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text") or ""))
                    else:
                        texts.append(json.dumps(part, ensure_ascii=False))
                return {
                    "ok": not bool(res.get("isError")),
                    "output": "\n".join(texts)[:12000],
                    "raw": res,
                }
            except Exception as exc:
                self._connected = False
                return {"ok": False, "error": str(exc)}

    def stop_locked(self):
        self._connected = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def stop(self):
        with self._io_lock:
            self.stop_locked()


def _configured_servers(cfg=None):
    cfg = config_mod.load() if cfg is None else cfg
    raw = cfg.get("mcp_servers") or []
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("name") and item.get("command"):
            out.append(item)
    return out


def list_servers(cfg=None):
    servers = _configured_servers(cfg)
    rows = []
    with _LOCK:
        for s in servers:
            name = s["name"]
            client = _CLIENTS.get(name)
            connected = bool(client and client._connected and client.proc and client.proc.poll() is None)
            rows.append({
                "name": name,
                "command": s.get("command", ""),
                "args": s.get("args") or [],
                "enabled": s.get("enabled", True),
                "connected": connected,
                "error": (client.error if client else ""),
                "tools": [t.get("name") for t in (client.tools if client else [])],
            })
    return {"ok": True, "servers": rows}


def connect_server(name, cfg=None):
    servers = _configured_servers(cfg)
    spec = next((s for s in servers if s["name"] == name), None)
    if not spec:
        return {"ok": False, "error": "MCP-сервер не найден: %s" % name}
    with _LOCK:
        old = _CLIENTS.pop(name, None)
        if old:
            old.stop()
        client = McpStdioClient(name, spec["command"], spec.get("args") or [], spec.get("env") or {})
        _CLIENTS[name] = client
    ok = client.start()
    return {
        "ok": ok,
        "name": name,
        "connected": ok,
        "tools": [t.get("name") for t in client.tools],
        "error": client.error,
    }


def add_server(name, command, args=None, env=None, connect=True):
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name or "").strip())[:40]
    command = str(command or "").strip()
    if not name or not command:
        return {"ok": False, "error": "Нужны имя (name) и команда (command)"}
    if isinstance(args, str):
        args = shlex.split(args) if args.strip() else []
    with config_mod.LOCK:
        cfg = config_mod.load()
        servers = [s for s in (cfg.get("mcp_servers") or []) if isinstance(s, dict) and s.get("name") != name]
        servers.append({"name": name, "command": command, "args": list(args or []), "env": dict(env or {}), "enabled": True})
        cfg["mcp_servers"] = servers
        config_mod.save(cfg)
    res = {"ok": True, "name": name}
    if connect:
        conn = connect_server(name, cfg)
        res.update({"connected": conn["connected"], "tools": conn["tools"], "error": conn.get("error", "")})
    return res


def remove_server(name):
    with _LOCK:
        old = _CLIENTS.pop(name, None)
        if old:
            old.stop()
    with config_mod.LOCK:
        cfg = config_mod.load()
        cfg["mcp_servers"] = [s for s in (cfg.get("mcp_servers") or []) if isinstance(s, dict) and s.get("name") != name]
        config_mod.save(cfg)
    return {"ok": True, "removed": name}


def toggle_server(name, enabled):
    with config_mod.LOCK:
        cfg = config_mod.load()
        for s in (cfg.get("mcp_servers") or []):
            if isinstance(s, dict) and s.get("name") == name:
                s["enabled"] = bool(enabled)
        config_mod.save(cfg)
    if not enabled:
        with _LOCK:
            old = _CLIENTS.pop(name, None)
            if old:
                old.stop()
        return {"ok": True, "name": name, "enabled": False}
    return connect_server(name, cfg)


def tool_entries(cfg=None):
    """Return tool schemas in LocalHarness format for all connected MCP servers."""
    out = []
    with _LOCK:
        for sname, client in list(_CLIENTS.items()):
            if not client._connected:
                continue
            for t in client.tools:
                tname = "mcp__%s__%s" % (sname, t["name"])
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                props = schema.get("properties") or {}
                params = {}
                for pk, pv in props.items():
                    ptype = (pv.get("type") if isinstance(pv, dict) else "string") or "string"
                    pdesc = (pv.get("description") if isinstance(pv, dict) else "") or ""
                    params[pk] = (ptype, pdesc)
                out.append({
                    "name": tname,
                    "desc": "[MCP %s] %s" % (sname, t.get("description") or t["name"]),
                    "params": params,
                    "required": list(schema.get("required") or []),
                })
    return out


def call_tool(full_name, arguments=None):
    """Dispatch mcp__<server>__<tool> to the corresponding MCP client."""
    if not str(full_name).startswith("mcp__"):
        return None
    parts = str(full_name).split("__", 2)
    if len(parts) != 3:
        return {"ok": False, "error": "некорректное имя MCP-инструмента: %s" % full_name}
    _, sname, tname = parts
    with _LOCK:
        client = _CLIENTS.get(sname)
    if not client:
        return {"ok": False, "error": "MCP-сервер %s не подключён" % sname}
    return client.call_tool(tname, arguments or {})


def stop_all():
    with _LOCK:
        for client in _CLIENTS.values():
            client.stop()
        _CLIENTS.clear()
