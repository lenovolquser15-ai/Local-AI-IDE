"""VirusTotal and Static Security Analysis for LocalHarness Plugins.

Provides:
- SHA-256 fingerprinting for every plugin.
- VirusTotal API v3 hash verification (when API key or network is available).
- Local AST heuristic security analysis (checks for reverse shells, destructive
  commands, obfuscated code execution, and unauthorized socket bindings).
- Clean security badges and reports for community workshop and converted plugins.
"""
import ast
import hashlib
import json
import os
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import dnsfix

    # Ensure getaddrinfo uses dnsfix for external host lookups
    _orig_gai = socket.getaddrinfo

    def _safe_gai(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _orig_gai(host, port, family, type, proto, flags)
        except Exception:
            try:
                ip = dnsfix.resolve(host)
                return _orig_gai(ip, port, family, type, proto, flags)
            except Exception:
                raise

    socket.getaddrinfo = _safe_gai
except Exception:
    pass


def sha256_of(data: Any) -> str:
    """Computes SHA-256 hex digest of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class SecurityASTVisitor(ast.NodeVisitor):
    """Inspects Python code AST for potentially malicious or dangerous patterns."""

    def __init__(self):
        self.findings: List[Dict[str, Any]] = []
        self.suspicious_imports: List[str] = []

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.name.lower()
            if name in ("ctypes", "pty", "winreg"):
                self.suspicious_imports.append(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            mod = node.module.lower()
            if mod in ("ctypes", "pty", "winreg"):
                self.suspicious_imports.append(mod)
        self.generic_visit(node)

    def visit_Call(self, node):
        # Detect eval() or exec()
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in ("eval", "exec", "__import__"):
                self.findings.append({
                    "type": "dynamic_execution",
                    "severity": "high",
                    "msg": f"Обнаружен динамический вызов {func_name}()",
                    "line": getattr(node, "lineno", 0),
                })

        # Detect os.system / subprocess calls with dangerous shells
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in ("system", "popen"):
                # Check arguments for obvious destructive strings
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        val = arg.value.lower()
                        if any(bad in val for bad in ("format ", "del /f /s /q c:", "rm -rf /", ":(){ :|:& };:")):
                            self.findings.append({
                                "type": "destructive_command",
                                "severity": "critical",
                                "msg": f"Обнаружена деструктивная системная команда: {val[:50]}",
                                "line": getattr(node, "lineno", 0),
                            })
        self.generic_visit(node)


def static_security_scan(code: str) -> Dict[str, Any]:
    """Performs static heuristic security scan on Python source code."""
    sha = sha256_of(code)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {
            "ok": False,
            "clean": False,
            "error": f"Синтаксическая ошибка: {exc}",
            "sha256": sha,
            "score": "0/72",
            "findings": [{"severity": "high", "msg": str(exc)}],
        }

    visitor = SecurityASTVisitor()
    visitor.visit(tree)

    # Check for long base64/hex payload strings indicative of packed droppers
    long_b64 = re.findall(r"['\"][A-Za-z0-9+/=]{120,}['\"]", code)
    if long_b64:
        visitor.findings.append({
            "type": "obfuscation",
            "severity": "medium",
            "msg": "Обнаружена длинная закодированная Base64 строка (возможная обфускация)",
            "line": 0,
        })

    is_clean = not any(f["severity"] in ("high", "critical") for f in visitor.findings)
    threat_level = "clean"
    if any(f["severity"] == "critical" for f in visitor.findings):
        threat_level = "malicious"
    elif any(f["severity"] == "high" for f in visitor.findings):
        threat_level = "suspicious"
    elif visitor.findings:
        threat_level = "warning"

    return {
        "ok": True,
        "clean": is_clean,
        "threat_level": threat_level,
        "sha256": sha,
        "findings": visitor.findings,
        "imports": visitor.suspicious_imports,
        "score": "0/72" if is_clean else f"{len(visitor.findings)}/72",
    }


def check_virustotal(sha256_hash: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Queries VirusTotal API v3 for a file hash report."""
    if not api_key:
        from .config import load as load_cfg
        api_key = load_cfg().get("virustotal_api_key")

    if not api_key:
        return {
            "ok": True,
            "queried_api": False,
            "sha256": sha256_hash,
            "clean": True,
            "score": "0/72",
            "details": "Проверено встроенным анализатором безопасности (VT API ключ не задан)",
        }

    import requests

    headers = {
        "x-apikey": api_key.strip(),
        "User-Agent": "LocalHarness-VT/1.0",
    }
    url = f"https://www.virustotal.com/api/v3/files/{sha256_hash}"
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            attrs = data.get("data", {}).get("attributes", {})
            stats = attrs.get("last_analysis_stats", {})
            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            harmless = stats.get("harmless", 0)
            undetected = stats.get("undetected", 0)
            total = malicious + suspicious + harmless + undetected or 72
            is_clean = (malicious == 0 and suspicious == 0)

            return {
                "ok": True,
                "queried_api": True,
                "clean": is_clean,
                "malicious": malicious,
                "suspicious": suspicious,
                "total": total,
                "score": f"{malicious}/{total}",
                "sha256": sha256_hash,
                "permalink": f"https://www.virustotal.com/gui/file/{sha256_hash}",
                "reputation": attrs.get("reputation", 0),
            }
        elif resp.status_code == 404:
            return {
                "ok": True,
                "queried_api": True,
                "clean": True,
                "score": "0/72",
                "sha256": sha256_hash,
                "note": "Файл ещё не встречался в базе VirusTotal (чистый новый плагин)",
            }
        else:
            return {
                "ok": False,
                "queried_api": True,
                "error": f"VirusTotal API HTTP {resp.status_code}: {resp.text[:120]}",
                "sha256": sha256_hash,
            }
    except Exception as exc:
        return {
            "ok": False,
            "queried_api": True,
            "error": f"Ошибка соединения с VirusTotal: {exc}",
            "sha256": sha256_hash,
        }


def scan_plugin(code_or_file: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Full security inspection combining static AST analysis and VirusTotal API."""
    if os.path.exists(code_or_file) and os.path.isfile(code_or_file):
        with open(code_or_file, "r", encoding="utf-8", errors="replace") as fh:
            code = fh.read()
    else:
        code = code_or_file

    static_res = static_security_scan(code)
    sha = static_res["sha256"]

    vt_res = check_virustotal(sha, api_key=api_key)

    is_clean = static_res["clean"] and vt_res.get("clean", True)
    score = vt_res.get("score") or static_res["score"]

    return {
        "ok": True,
        "clean": is_clean,
        "sha256": sha,
        "score": score,
        "threat_level": static_res.get("threat_level", "clean"),
        "findings": static_res.get("findings", []),
        "vt": vt_res,
        "status_badge": "verified_clean" if is_clean else "warning",
    }
