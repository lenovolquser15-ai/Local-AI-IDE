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
        snap = {"tokens": tokens, "chars": self.chars, "exact": self.tokens is not None,
                "seconds": round(now - self.t0, 2), "gen_seconds": round(gen, 2),
                "ttft": round(self.first - self.t0, 2) if self.first else None,
                "tps": round(tokens / gen, 1) if gen >= 0.5 else None}
        try:
            from . import thermal
            th = thermal.get_cached_thermal()
            if th and th.get("temp_c") is not None:
                snap["gpu_temp"] = th.get("temp_c")
                snap["gpu_status"] = th.get("status")
                snap["gpu_throttled"] = th.get("throttle_active", False)
        except Exception:
            pass
        return snap

    def finish(self):
        snap = self.snapshot()
        try:
            from . import dashboard
            if snap.get("tokens"):
                dashboard.record_tokens(snap["tokens"], tps=snap.get("tps"))
        except Exception:
            pass
        if self.cb:
            self.cb(snap)


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
        try:
            from . import cluster as cluster_mod
            from . import thermal as thermal_mod
            th = thermal_mod.get_cached_thermal(self.cfg)
            remote_node = cluster_mod.pick_llm_node(self.cfg, th)
            if remote_node:
                url = remote_node["url"] + "/chat/completions"
                body = dict(body, model=remote_node["model"])
        except Exception:
            pass
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
        try:
            from . import thermal as thermal_mod
        except Exception:
            thermal_mod = None
        try:
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
                if thermal_mod:
                    t_delay = thermal_mod.get_stream_delay(self.cfg)
                    if t_delay > 0:
                        time.sleep(t_delay)
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
        except Exception as exc:
            resp.close()
            with self._active_lock:
                self._active_response = None
            raise LLMError("LM Studio оборвал поток на середине (%s). Обычно это переполнение контекста "
                           "модели или её выгрузка — перезагрузи модель с большим контекстом и повтори."
                           % (exc.__class__.__name__ if not str(exc) else str(exc)[:200]))
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
            if stop is not None and stop.is_set():
                raise LLMError("остановлено пользователем")
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
        # Fast-path: if LM Studio metadata explicitly marks model as NOT trained for tool use,
        # skip the slow network probe and use our universal text/JSON protocol immediately.
        if (self.cfg or {}).get("provider") == "lmstudio" and self.model:
            try:
                from . import lms as lms_mod
                disk, _ = lms_mod.disk_models(self.cfg, timeout=5)
                for item in (disk or []):
                    if any(lms_mod._match_alias(item.get(k), self.model)
                           for k in ("modelKey", "path", "identifier", "displayName", "indexedModelIdentifier")):
                        if item.get("trainedForToolUse") is False:
                            return "json"
                        break
            except Exception:
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


def _repair_json_candidate(raw):
    """Мягкая починка типичных ошибок локальных GGUF-моделей без tool_use:
    висячие запятые, неэкранированные переводы строк внутри кавычек, одинарные кавычки / Python-словари."""
    import ast
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    # 1. Убираем trailing commas перед } и ] и экранируем сырые переводы строк внутри JSON-строк
    repaired_chars = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                repaired_chars.append(ch)
                esc = False
            elif ch == "\\":
                repaired_chars.append(ch)
                esc = True
            elif ch == '"':
                repaired_chars.append(ch)
                in_str = False
            elif ch == "\n":
                repaired_chars.append("\\n")
            elif ch == "\r":
                repaired_chars.append("\\r")
            elif ch == "\t":
                repaired_chars.append("\\t")
            else:
                repaired_chars.append(ch)
        else:
            if ch == '"':
                in_str = True
            repaired_chars.append(ch)
    fixed = re.sub(r",\s*([}\]])", r"\1", "".join(repaired_chars))
    try:
        return json.loads(fixed)
    except ValueError:
        pass
    # 2. Fallback для Python-литералов ({'tool': 'shell', 'args': {'cmd': 'dir'}, 'all': True})
    py_cand = re.sub(r"\btrue\b", "True", fixed)
    py_cand = re.sub(r"\bfalse\b", "False", py_cand)
    py_cand = re.sub(r"\bnull\b", "None", py_cand)
    try:
        val = ast.literal_eval(py_cand)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass
    return None


def _balanced_object(text):
    """Извлекает первый сбалансированный JSON/Python объект {...} из текста."""
    objs = _all_balanced_structures(text, limit=1, allow_arrays=False)
    return objs[0][0] if objs else None


def _all_balanced_structures(text, limit=8, allow_arrays=True):
    """Находит до `limit` сбалансированных JSON-объектов {...} (и массивов [...]) с их позициями (obj, start, end)."""
    results = []
    if not text:
        return results
    openers = "{[" if allow_arrays else "{"
    idx = 0
    n = len(text)
    while idx < n and len(results) < limit:
        best_pos = -1
        for op in openers:
            p = text.find(op, idx)
            if p != -1 and (best_pos == -1 or p < best_pos):
                best_pos = p
        if best_pos == -1:
            break
        stack = []
        in_str = None
        esc = False
        parsed_end = -1
        for i in range(best_pos, n):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == in_str:
                    in_str = None
                continue
            if ch in ('"', "'"):
                in_str = ch
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or ch != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    cand = text[best_pos:i + 1]
                    obj = _repair_json_candidate(cand)
                    if isinstance(obj, (dict, list)):
                        results.append((obj, best_pos, i + 1))
                        parsed_end = i + 1
                    break
        idx = parsed_end if parsed_end != -1 else (best_pos + 1)
    return results


def _coerce_call_item(item, index=0):
    """Превращает словарь любого популярного формата (LocalHarness / OpenAI / Hermes / Qwen / ReAct)
    в стандартный OpenAI tool_call dict, либо возвращает None."""
    if not isinstance(item, dict):
        return None
    fn_block = item.get("function") if isinstance(item.get("function"), dict) else None
    name = (
        item.get("tool")
        or item.get("tool_name")
        or item.get("action")
        or (fn_block.get("name") if fn_block else None)
        or item.get("name")
    )
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name.lower() in ("final", "final_answer", "none", "null", "answer", "response"):
        return None
    if fn_block and "arguments" in fn_block:
        args = fn_block.get("arguments")
    else:
        for key in ("args", "arguments", "parameters", "params", "action_input", "input"):
            if key in item:
                args = item[key]
                break
        else:
            # Некоторые маленькие GGUF пишут плоский JSON: {"tool": "shell", "cmd": "dir"}
            reserved = {"tool", "tool_name", "name", "action", "id", "type", "note", "thought", "reasoning"}
            extra = {k: v for k, v in item.items() if k not in reserved}
            args = extra if extra else {}
    if isinstance(args, str):
        repaired = _repair_json_candidate(args)
        args = repaired if isinstance(repaired, dict) else ({"input": args} if args.strip() else {})
    elif not isinstance(args, dict):
        args = {}
    return {
        "id": str(item.get("id") or ("json_%d_%d" % (int(time.time() * 1000), index))),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


_XML_TOOL_BLOCK_RE = re.compile(
    r"(?:<(?:tool_call|Seed:tool_call|function_call)>|<\|tool_call_start\|>)\s*(.*?)\s*"
    r"(?:</(?:tool_call|Seed:tool_call|function_call)>|<\|tool_call_end\|>)",
    re.S | re.I,
)
_DEEPSEEK_TOOL_RE = re.compile(
    r"<[｜|]tool[▁_]call[▁_]begin[｜|]>(?:function<[｜|]tool[▁_]sep[｜|]>)?\s*([a-zA-Z0-9_.\-]+)\s*"
    r"(?:```(?:json)?\s*)?(\{.*?\})(?:\s*```)?\s*<[｜|]tool[▁_]call[▁_]end[｜|]>",
    re.S,
)
_FUNC_TAG_RE = re.compile(
    r"<function\s*=\s*([a-zA-Z0-9_.\-]+)\s*>(.*?)</function>|<function\s*=\s*([a-zA-Z0-9_.\-]+)\s*\((\{.*?\})\)\s*/?>",
    re.S | re.I,
)
_INVOKE_TAG_RE = re.compile(
    r"<invoke\s+name=[\"']([a-zA-Z0-9_.\-]+)[\"']\s*>(.*?)</invoke>",
    re.S | re.I,
)
_INVOKE_PARAM_RE = re.compile(
    r"<parameter\s+name=[\"']([a-zA-Z0-9_.\-]+)[\"']\s*>(.*?)</parameter>",
    re.S | re.I,
)
_REACT_RE = re.compile(
    r"(?:^|\n)\s*Action\s*:\s*`?([a-zA-Z0-9_.\-]+)`?\s*\n\s*Action\s+Input\s*:\s*(\{.*?\})",
    re.S | re.I,
)


def parse_json_protocol(text, known_tools=None, strict_for_native=False):
    """Универсальный парсер вызовов инструментов без native tool_use.
    Поддерживает чистый JSON, markdown ```json```, <tool_call>, Qwen/Hermes, DeepSeek,
    Mistral [TOOL_CALLS], Llama <function=...>, Claude <invoke>, ReAct Action/Action Input
    и мягкую починку синтаксиса для локальных GGUF-моделей."""
    raw_text = text or ""
    clean = strip_reasoning(raw_text)
    # Если модель открыла <think>, забыла закрыть </think>, но в конце явно написала <tool_call>:
    if not clean and ("<tool_call>" in raw_text or "<function=" in raw_text or '"tool"' in raw_text):
        for marker in ("<tool_call>", "<Seed:tool_call>", "<function=", "[TOOL_CALLS]"):
            pos = raw_text.find(marker)
            if pos != -1:
                clean = raw_text[pos:].strip()
                break

    allowed = {str(t).strip() for t in known_tools} if known_tools else None
    calls = []
    spans_to_strip = []

    def _accept_name(cname):
        if not allowed:
            return True
        if cname in allowed:
            return True
        # Разрешаем также стандартные алиасы инструментов (bash -> shell, fs_read -> read_file и т.д.)
        from . import tools as tools_mod
        canon = tools_mod.TOOL_NAME_ALIASES.get(cname.lower())
        return bool(canon and canon in allowed)

    # 1. Явные XML / специальные теги (<tool_call>, DeepSeek, <function=...>, <invoke ...>)
    for m in _XML_TOOL_BLOCK_RE.finditer(clean):
        spans_to_strip.append(m.span())
        inner = m.group(1).strip()
        parsed_inner = _repair_json_candidate(inner)
        if parsed_inner is None:
            sub = _balanced_object(inner)
            parsed_inner = sub
        items = parsed_inner if isinstance(parsed_inner, list) else ([parsed_inner] if isinstance(parsed_inner, dict) else [])
        for it in items:
            c = _coerce_call_item(it, len(calls))
            if c and _accept_name(c["function"]["name"]):
                calls.append(c)

    for m in _DEEPSEEK_TOOL_RE.finditer(clean):
        spans_to_strip.append(m.span())
        tname = m.group(1).strip()
        targs = _repair_json_candidate(m.group(2)) or {}
        c = _coerce_call_item({"name": tname, "arguments": targs}, len(calls))
        if c and _accept_name(c["function"]["name"]):
            calls.append(c)

    for m in _FUNC_TAG_RE.finditer(clean):
        spans_to_strip.append(m.span())
        tname = (m.group(1) or m.group(3) or "").strip()
        raw_arg = (m.group(2) if m.group(1) else m.group(4)) or "{}"
        targs = _repair_json_candidate(raw_arg.strip()) or _balanced_object(raw_arg) or {}
        c = _coerce_call_item({"name": tname, "arguments": targs}, len(calls))
        if c and _accept_name(c["function"]["name"]):
            calls.append(c)

    for m in _INVOKE_TAG_RE.finditer(clean):
        spans_to_strip.append(m.span())
        tname = m.group(1).strip()
        params = {}
        for pm in _INVOKE_PARAM_RE.finditer(m.group(2)):
            pkey = pm.group(1).strip()
            pval = pm.group(2).strip()
            conv = _repair_json_candidate(pval)
            params[pkey] = conv if conv is not None and pval.startswith(("{", "[", "true", "false")) else pval
        c = _coerce_call_item({"name": tname, "arguments": params}, len(calls))
        if c and _accept_name(c["function"]["name"]):
            calls.append(c)

    if calls:
        note = clean
        for s, e in sorted(spans_to_strip, reverse=True):
            note = note[:s] + " " + note[e:]
        note = re.sub(r"```(?:xml|json|tool_call|tool)?\s*```", "", note).strip()
        return {"kind": "tools", "content": note, "calls": calls}

    # 2. Mistral [TOOL_CALLS] [...]
    if "[TOOL_CALLS]" in clean:
        pos = clean.find("[TOOL_CALLS]")
        after = clean[pos + len("[TOOL_CALLS]"):].strip()
        structs = _all_balanced_structures(after, limit=4, allow_arrays=True)
        for obj, _s, _e in structs:
            items = obj if isinstance(obj, list) else [obj]
            for it in items:
                c = _coerce_call_item(it, len(calls))
                if c and _accept_name(c["function"]["name"]):
                    calls.append(c)
        if calls:
            return {"kind": "tools", "content": clean[:pos].strip(), "calls": calls}

    # 3. В native-режиме перехватываем неразмеченный JSON только если он явно содержит вызов известного инструмента
    if strict_for_native:
        fenced_tc = re.search(r"```(?:tool_call|tool|json)\s*(\{.*?\}|\[.*?\])\s*```", clean, re.S)
        cand_text = fenced_tc.group(1) if fenced_tc else (clean if clean.lstrip().startswith("{") else "")
        if not cand_text:
            return {"kind": "final", "content": clean, "calls": []}
        structs = _all_balanced_structures(cand_text, limit=4, allow_arrays=True)
        for obj, _s, _e in structs:
            items = obj if isinstance(obj, list) else ([obj] if isinstance(obj, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                if not any(k in it for k in ("tool", "tools", "tool_calls", "function")) and not (
                    "name" in it and ("arguments" in it or "args" in it or "parameters" in it)
                ):
                    continue
                c = _coerce_call_item(it, len(calls))
                if c and _accept_name(c["function"]["name"]):
                    calls.append(c)
        if calls:
            note = clean[:fenced_tc.start()].strip() if fenced_tc else ""
            return {"kind": "tools", "content": note, "calls": calls}
        return {"kind": "final", "content": clean, "calls": []}

    # 4. ReAct формат: Action: <tool> / Action Input: {...}
    rm = _REACT_RE.search(clean)
    if rm:
        tname = rm.group(1).strip()
        targs = _repair_json_candidate(rm.group(2)) or _balanced_object(rm.group(2)) or {}
        c = _coerce_call_item({"name": tname, "arguments": targs}, 0)
        if c and _accept_name(c["function"]["name"]):
            prefix = clean[:rm.start()].strip()
            prefix = re.sub(r"^\s*Thought\s*:\s*", "", prefix, flags=re.I).strip()
            return {"kind": "tools", "content": prefix, "calls": [c]}

    # 5. Fenced code blocks ```json / ```tool_call ИЛИ JSON-объекты/массивы прямо в тексте
    fenced_blocks = list(re.finditer(r"```(?:json|tool_call|tool)?\s*(\{.*?\}|\[.*?\])\s*```", clean, re.S))
    structs = []
    if fenced_blocks:
        for fm in fenced_blocks:
            obj = _repair_json_candidate(fm.group(1))
            if obj is None:
                sub = _all_balanced_structures(fm.group(1), limit=4, allow_arrays=True)
                for sobj, _, _ in sub:
                    structs.append((sobj, fm.start(), fm.end()))
            else:
                structs.append((obj, fm.start(), fm.end()))
    if not structs:
        structs = _all_balanced_structures(clean, limit=6, allow_arrays=True)

    if not structs:
        return {"kind": "final", "content": clean, "calls": []}

    notes = []
    used_spans = []
    for blob, s_pos, e_pos in structs:
        if isinstance(blob, list):
            for it in blob:
                c = _coerce_call_item(it, len(calls))
                if c and _accept_name(c["function"]["name"]):
                    calls.append(c)
                    used_spans.append((s_pos, e_pos))
            continue
        if not isinstance(blob, dict):
            continue
        # Финальный ответ {"final": "..."}
        if ("final" in blob or "final_answer" in blob) and not any(k in blob for k in ("tool", "tools", "calls", "tool_calls")):
            fin_val = blob.get("final") if "final" in blob else blob.get("final_answer")
            return {"kind": "final", "content": str(fin_val if fin_val is not None else clean), "calls": []}
        if "content" in blob and not any(k in blob for k in ("tool", "tools", "calls", "tool_calls", "name", "function", "action")):
            return {"kind": "final", "content": str(blob.get("content") or clean), "calls": []}

        raw_calls = blob.get("tools") or blob.get("calls") or blob.get("tool_calls") or []
        if isinstance(raw_calls, dict):
            raw_calls = [raw_calls]
        single = _coerce_call_item(blob, len(calls))
        candidates = ([single] if single else []) + [
            _coerce_call_item(it, len(calls) + idx + 1) for idx, it in enumerate(raw_calls)
        ]
        matched_here = False
        for c in candidates:
            if c and _accept_name(c["function"]["name"]):
                calls.append(c)
                matched_here = True
        if matched_here:
            used_spans.append((s_pos, e_pos))
            if blob.get("note"):
                notes.append(str(blob["note"]).strip())

    if not calls:
        first_blob = structs[0][0]
        if isinstance(first_blob, dict):
            fallback = first_blob.get("note") or first_blob.get("answer") or first_blob.get("response") or clean
            return {"kind": "final", "content": str(fallback), "calls": []}
        return {"kind": "final", "content": clean, "calls": []}

    if notes:
        note_text = " ".join(notes)
    else:
        rem = clean
        for s, e in sorted(set(used_spans), reverse=True):
            rem = rem[:s] + " " + rem[e:]
        note_text = rem.strip()
    return {"kind": "tools", "content": note_text, "calls": calls}
