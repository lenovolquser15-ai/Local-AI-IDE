"""Client for any OpenAI-compatible endpoint (LM Studio / Ollama) with two tool protocols.

Local GGUF reasoning models often ignore or mangle native function calling, so the
same loop can drive them with a text JSON protocol instead of the tools parameter.
"""
import json
import os
import re
import threading
import time

import requests

from .config import DATA_DIR

PROTOCOL_CACHE = os.path.join(DATA_DIR, "protocol_cache.json")
CHARS_PER_TOKEN = 3.6


class SpeedMeter:
    """tok/s measured at the socket: the local server rarely reports usage, so estimate from
    the stream and let usage override the estimate when it does arrive."""

    def __init__(self, cb=None):
        self.cb = cb
        self.t0 = time.time()
        self.first = None
        self.chars = 0
        self.tokens = None
        self.reported = 0.0

    def piece(self, text):
        now = time.time()
        if self.first is None:
            self.first = now
        self.chars += len(text)
        if self.cb and now - self.reported >= 0.4:
            self.reported = now
            self.cb(self.snapshot(now))

    def usage(self, data):
        if isinstance(data, dict) and data.get("completion_tokens"):
            self.tokens = int(data["completion_tokens"])

    def snapshot(self, now=None):
        now = now or time.time()
        gen = max(0.0, now - (self.first or now))
        tokens = self.tokens if self.tokens is not None else max(1, int(round(self.chars / CHARS_PER_TOKEN)))
        return {"tokens": tokens, "chars": self.chars, "exact": self.tokens is not None,
                "seconds": round(now - self.t0, 2), "gen_seconds": round(gen, 2),
                "ttft": round(self.first - self.t0, 2) if self.first else None,
                "tps": round(tokens / gen, 1) if gen >= 0.5 else None}

    def finish(self):
        if self.cb:
            self.cb(self.snapshot())


class LLMError(Exception):
    pass


def _split_url(base_url):
    return base_url.rstrip("/")


def _as_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return [{"type": "text", "text": str(content)}]


def _parts_text(parts):
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text" and p.get("text"))


def _merge_content(a, b):
    parts = _as_parts(a) + _as_parts(b)
    if not parts:
        return ""
    if all(p.get("type") == "text" for p in parts):
        return "\n".join(p.get("text", "") for p in parts if p.get("text"))
    return parts


def normalize_messages(messages):
    """Strict jinja templates (Mistral/Ministral) demand one leading system message,
    user/assistant alternation and tool results bound to the preceding tool_calls.
    Repair broken histories here instead of letting the server answer HTTP 400."""
    systems = []
    rest = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            text = _parts_text(_as_parts(msg.get("content")))
            if text:
                systems.append(text)
        elif role in ("user", "assistant", "tool"):
            rest.append(msg)
    out = []
    if systems:
        out.append({"role": "system", "content": "\n\n".join(systems)})
    pending = {}

    def flush_pending():
        for cid, name in pending.items():
            item = {"role": "tool", "tool_call_id": cid, "content": "(прервано)"}
            if name:
                item["name"] = name
            out.append(item)
        pending.clear()

    def push_user(content):
        if not _parts_text(_as_parts(content)) and not _as_parts(content):
            content = "(пусто)"
        if out and out[-1]["role"] == "user":
            out[-1]["content"] = _merge_content(out[-1].get("content"), content)
        else:
            out.append({"role": "user", "content": content})

    for msg in rest:
        role = msg["role"]
        if role == "tool":
            cid = msg.get("tool_call_id")
            if cid and cid in pending:
                item = {"role": "tool", "tool_call_id": cid,
                        "content": msg.get("content") if msg.get("content") is not None else ""}
                if pending.get(cid):
                    item["name"] = pending[cid]
                out.append(item)
                pending.pop(cid, None)
            else:
                flush_pending()
                text = _parts_text(_as_parts(msg.get("content")))
                push_user("Наблюдение: %s" % (text or "(пусто)"))
        elif role == "user":
            flush_pending()
            push_user(msg.get("content") or "")
        elif role == "assistant":
            flush_pending()
            calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if calls:
                out.append({"role": "assistant", "content": content or "", "tool_calls": calls})
                for call in calls:
                    cid = call.get("id")
                    if cid:
                        pending[cid] = (call.get("function") or {}).get("name") or ""
            elif out and out[-1]["role"] == "assistant" and not out[-1].get("tool_calls"):
                out[-1]["content"] = _merge_content(out[-1].get("content"), content)
            elif _as_parts(content):
                out.append({"role": "assistant", "content": content})
    flush_pending()
    return _enforce_alternation(out, has_system=bool(systems))


def _counted_role(msg):
    """Ministral jinja counts only user and assistant-without-tool_calls toward the
    alternation index; assistant tool_calls and tool results are transparent."""
    role = msg.get("role")
    if role == "user":
        return "user"
    if role == "assistant" and not (msg.get("tool_calls") or []):
        return "assistant"
    return None


def _enforce_alternation(out, has_system):
    """Гарантия против jinja-400 Ministral: после ведущего system counted-роли
    (user / текстовый assistant) строго чередуются, начиная с user. Прозрачные
    assistant(tool_calls)/tool остаются на местах. Любое нарушение (два user или
    два текстовых assistant подряд, в т.ч. разделённые tool-блоком после сжатия)
    чинится минимальным плейсхолдером нужной роли — смысл истории не теряется."""
    first = 1 if has_system else 0
    body = out[first:]
    if body and body[0].get("role") != "user":
        body.insert(0, {"role": "user", "content": "(продолжение разговора)"})
    fixed = []
    index = 0
    for msg in body:
        counted = _counted_role(msg)
        if counted is None:
            fixed.append(msg)
            continue
        expected = "user" if index % 2 == 0 else "assistant"
        if counted != expected:
            fixed.append({"role": expected,
                          "content": "(продолжение разговора)" if expected == "user" else "(продолжаю)"})
            index += 1
        fixed.append(msg)
        index += 1
    return out[:first] + fixed


class LLM:
    def __init__(self, cfg):
        self.cfg = cfg
        prov = cfg["providers"][cfg["provider"]]
        self.base = _split_url(prov["base_url"])
        self.key = prov.get("api_key") or "none"
        self.model = cfg.get("model")
        self.label = prov.get("label", cfg["provider"])

    def headers(self):
        return {"Authorization": "Bearer %s" % self.key, "Content-Type": "application/json"}

    def models(self):
        try:
            resp = requests.get(self.base + "/models", headers=self.headers(), timeout=15)
            resp.raise_for_status()
            items = resp.json().get("data", [])
            return [m.get("id") for m in items if m.get("id")]
        except requests.RequestException as exc:
            raise LLMError("%s недоступен (%s). Запусти локальный сервер." % (self.label, exc.__class__.__name__))

    def alive(self):
        try:
            requests.get(self.base + "/models", timeout=4)
            return True
        except Exception:
            return False

    def detect_context_length(self, model=None):
        """Автоопределение размера контекста из LM Studio (GET /v1/models или CLI)."""
        target = model or self.model
        try:
            resp = requests.get(self.base + "/models", headers=self.headers(), timeout=4)
            if resp.ok:
                items = resp.json().get("data", [])
                for m in items:
                    if target and m.get("id") != target:
                        continue
                    ctx = (m.get("context_length") or m.get("contextLength") or
                           m.get("max_model_len") or m.get("context_window"))
                    if isinstance(ctx, int) and ctx > 512:
                        return ctx
        except Exception:
            pass
        try:
            from . import lms
            readiness = lms.readiness(self.cfg)
            if readiness.get("context_length"):
                return readiness["context_length"]
        except Exception:
            pass
        return None

    def _ensure_model(self):
        if not self.model:
            available = [m for m in self.models() if "embedding" not in m.lower()]
            if not available:
                raise LLMError("на %s нет ни одной модели — скачай что-нибудь в LM Studio" % self.base)
            self.model = available[0]
        return self.model

    def _payload(self, messages, tools, stream, protocol):
        body = {
            "model": self._ensure_model(),
            "messages": normalize_messages(messages),
            "stream": stream,
            "temperature": self.cfg.get("temperature", 0.4),
            "max_tokens": self.cfg.get("max_tokens", 1500),
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if protocol == "native" and tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def _model_hint(self, text):
        if "model" not in (text or "").lower():
            return ""
        try:
            available = [m for m in self.models() if "embedding" not in m.lower()]
        except LLMError:
            available = []
        if available:
            return " | доступные модели: %s" % ", ".join(available[:8])
        return ""

    def _post(self, body, stream):
        url = self.base + "/chat/completions"
        meter = self.meter
        if not stream:
            resp = requests.post(url, headers=self.headers(), json=body, timeout=(15, 900))
            if resp.status_code >= 400:
                raise LLMError("HTTP %s: %s%s" % (resp.status_code, resp.text[:600], self._model_hint(resp.text)))
            resp.encoding = "utf-8"
            payload = resp.json()
            meter.usage((payload.get("usage") or {}))
            message = payload["choices"][0]["message"]
            message["finish_reason"] = payload["choices"][0].get("finish_reason") or ""
            meter.piece(message.get("content") or "")
            meter.finish()
            return message
        result = {"role": "assistant", "content": "", "tool_calls": [], "reasoning": "", "finish_reason": ""}
        buffers = {}
        resp = requests.post(url, headers=self.headers(), json=body, stream=True, timeout=(15, 900))
        with self._active_lock:
            self._active_response = resp
        if resp.status_code >= 400:
            text = resp.text[:600]
            resp.close()
            with self._active_lock:
                self._active_response = None
            raise LLMError("HTTP %s: %s%s" % (resp.status_code, text, self._model_hint(text)))
        resp.encoding = "utf-8"
        ctype = (resp.headers.get("content-type") or "").lower()
        if "event-stream" not in ctype:
            payload = resp.json()
            message = payload["choices"][0]["message"]
            meter.usage((payload.get("usage") or {}))
            piece = message.get("content") or ""
            result["finish_reason"] = payload["choices"][0].get("finish_reason") or ""
            meter.piece(piece)
            meter.finish()
            extra = _reason_of(message)
            if extra:
                result["reasoning"] = extra
                message["reasoning"] = extra
            if piece and self.stream_cb:
                self.stream_cb(piece)
            message.setdefault("tool_calls", [])
            return message
        splitter = ThinkSplitter(self.reason_cb)
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            meter.usage(chunk.get("usage") or {})
            choices = chunk.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                result["finish_reason"] = choices[0]["finish_reason"]
            delta = choices[0].get("delta") or {}
            extra = _reason_of(delta)
            if extra:
                result["reasoning"] += extra
                if self.reason_cb:
                    self.reason_cb(extra)
                meter.piece(extra)
            piece = delta.get("content") or ""
            if piece:
                visible = splitter.feed(piece)
                meter.piece(piece)
                if visible and self.stream_cb:
                    self.stream_cb(visible)
            for call in delta.get("tool_calls") or []:
                idx = call.get("index", 0)
                slot = buffers.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
                    meter.piece(fn["arguments"])
        tail = splitter.flush()
        if tail:
            result["content"] += tail
            if self.stream_cb:
                self.stream_cb(tail)
        result["content"] = splitter.answer.strip()
        result["reasoning"] += splitter.reason
        for idx in sorted(buffers):
            slot = buffers[idx]
            if slot["name"]:
                result["tool_calls"].append({
                    "id": slot["id"] or ("call_%d" % idx),
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                })
        if not result["content"]:
            result["content"] = None
        if not result["reasoning"]:
            result.pop("reasoning")
        if not result["finish_reason"]:
            result.pop("finish_reason")
        meter.finish()
        return result

    stream_cb = None
    stats_cb = None
    reason_cb = None

    def complete(self, messages, tools=None, protocol="native", stream_cb=None, stats_cb=None,
                 reason_cb=None, stop=None):
        self.stream_cb = stream_cb
        self.stats_cb = stats_cb
        self.reason_cb = reason_cb
        self.meter = SpeedMeter(stats_cb)
        self._active_response = None
        self._active_lock = threading.Lock()
        body = self._payload(messages, tools, True, protocol)
        try:
            return self._post(body, True)
        except LLMError as exc:
            if "stream_options" in str(exc) and body.pop("stream_options", None):
                self.meter = SpeedMeter(stats_cb)
                return self._post(body, True)
            raise
        except requests.RequestException as exc:
            if stop is not None and stop.is_set():
                raise LLMError("остановлено пользователем")
            raise LLMError("сбой локального инференса: %s" % exc)
        finally:
            with self._active_lock:
                self._active_response = None

    def stop(self):
        with getattr(self, "_active_lock", threading.Lock()):
            response = getattr(self, "_active_response", None)
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def probe_protocol(self, tools, model=None):
        """Try native function calling once; fall back to the text JSON protocol."""
        if model:
            self.model = model
        try:
            self._ensure_model()
        except LLMError:
            pass
        try:
            with open(PROTOCOL_CACHE, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
            key = "%s|%s" % (self.base, self.model or "auto")
            known = cache.get(key)
            if known:
                return known
        except Exception:
            cache = {}
        probe = [{"role": "user", "content": "Call the tool ping once. Do not answer in prose."}]
        protocol = "json"
        try:
            msg = self.complete(probe, tools=tools, protocol="native")
            if msg.get("tool_calls"):
                protocol = "native"
        except Exception:
            protocol = "json"
        cache["%s|%s" % (self.base, self.model or "auto")] = protocol
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(PROTOCOL_CACHE, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2)
        except Exception:
            pass
        return protocol


REASON_TAGS = (
    (r"<think>", r"</think>"),
    (r"<thinking>", r"</thinking>"),
    (r"<reasoning>", r"</reasoning>"),
    (r"\[THINK\]", r"\[/THINK\]"),
)

_OPEN_TAGS = ("<think>", "<thinking>", "<reasoning>", "[THINK]", "[think]")
_REASON_KEYS = ("reasoning_content", "reasoning", "thinking", "reflection")


class ThinkSplitter:
    """Splits an inline  stream into answer and reasoning channels."""

    def __init__(self, reason_cb=None):
        self.reason_cb = reason_cb
        self.in_reason = False
        self.answer = ""
        self.reason = ""
        self._pending = ""

    def feed(self, piece):
        buf = self._pending + piece
        answer_bits = []
        while buf:
            if self.in_reason:
                pos = _find_first(buf, _CLOSE_TAGS)
                if pos is None:
                    keep = _tail_overlap(buf, _CLOSE_TAGS)
                    emit_len = len(buf) - len(keep)
                    if emit_len > 0:
                        chunk = buf[:emit_len]
                        self.reason += chunk
                        if self.reason_cb:
                            self.reason_cb(chunk)
                        buf = buf[emit_len:]
                    self._pending = buf
                    break
                chunk, buf = buf[:pos[0]], buf[pos[0] + len(pos[1]):]
                self.reason += chunk
                if self.reason_cb and chunk:
                    self.reason_cb(chunk)
                self.in_reason = False
            else:
                pos = _find_first(buf, _OPEN_TAGS)
                if pos is None:
                    keep = _tail_overlap(buf, _OPEN_TAGS)
                    answer_bits.append(buf[:len(buf) - len(keep)])
                    self.answer += buf[:len(buf) - len(keep)]
                    self._pending = buf[len(buf) - len(keep):]
                    buf = ""
                    break
                answer_bits.append(buf[:pos[0]])
                self.answer += buf[:pos[0]]
                buf = buf[pos[0] + len(pos[1]):]
                self.in_reason = True
        return "".join(answer_bits)

    def flush(self):
        if not self._pending:
            return ""
        text, self._pending = self._pending, ""
        if self.in_reason:
            self.reason += text
            if self.reason_cb:
                self.reason_cb(text)
            return ""
        self.answer += text
        return text


def _find_first(buf, tags):
    best = None
    for tag in tags:
        pos = buf.find(tag)
        if pos != -1 and (best is None or pos < best[0]):
            best = (pos, tag)
    return best


def _tail_overlap(buf, tags):
    longest = 0
    for tag in tags:
        for size in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:size]):
                longest = max(longest, size)
                break
    return buf[len(buf) - longest:] if longest else ""


_CLOSE_TAGS = ("</think>", "</thinking>", "</reasoning>", "[/THINK]", "[/think]")


def _reason_of(part):
    """Servers disagree on the native reasoning channel's name, so take the first one carrying text."""
    if not isinstance(part, dict):
        return ""
    for key in _REASON_KEYS:
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def strip_reasoning(text):
    if not text:
        return text
    for opener, closer in REASON_TAGS:
        text = re.sub(opener + r".*?" + closer, "", text, flags=re.S | re.I)
        text = re.sub(opener + r".*$", "", text, flags=re.S | re.I)
    text = re.sub(r"^[ \t]*(Thought|Думаю|Рассуждение)\s*:[^\n]*\n", "", text)
    return text.strip()


def _balanced_object(text):
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_json_protocol(text):
    """Extract tool calls from a model that answered in plain text."""
    clean = strip_reasoning(text or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.S)
    blob = _balanced_object(fenced.group(1)) if fenced else _balanced_object(clean)
    if not isinstance(blob, dict):
        return {"kind": "final", "content": clean, "calls": []}
    if "final" in blob or ("content" in blob and "tool" not in blob and "tools" not in blob):
        return {"kind": "final", "content": str(blob.get("final") or blob.get("content") or clean), "calls": []}
    raw_calls = blob.get("tools") or blob.get("calls") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if blob.get("tool"):
        raw_calls = [blob] + list(raw_calls)
    calls = []
    for i, item in enumerate(raw_calls):
        if not isinstance(item, dict) or not item.get("tool"):
            continue
        args = item.get("args", item.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"input": args}
        calls.append({
            "id": item.get("id") or ("json_%d_%d" % (int(time.time() * 1000), i)),
            "type": "function",
            "function": {"name": item["tool"], "arguments": json.dumps(args, ensure_ascii=False)},
        })
    if not calls:
        note = blob.get("note") or blob.get("answer") or clean
        return {"kind": "final", "content": note, "calls": []}
    return {"kind": "tools", "content": blob.get("note") or "", "calls": calls}
