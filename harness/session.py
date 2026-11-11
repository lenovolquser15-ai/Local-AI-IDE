"""Session persistence: model messages and UI timeline in one JSONL per chat."""
import json
import os
import re
import tempfile
import threading
import time
import uuid

from .config import SESSION_DIR

LOCK = threading.RLock()


def valid_id(sid):
    return isinstance(sid, str) and re.fullmatch(r"[0-9A-Za-z_-]{1,100}", sid) is not None


def _path(sid):
    if not valid_id(sid):
        raise ValueError("invalid session id")
    os.makedirs(SESSION_DIR, exist_ok=True)
    return os.path.join(SESSION_DIR, "%s.jsonl" % sid)


def create(title=""):
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    with LOCK, open(_path(sid), "x", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "meta", "id": sid, "created": time.time(), "title": title}, ensure_ascii=False) + "\n")
    return sid


def append(sid, record):
    record = dict(record)
    record.setdefault("ts", time.time())
    with LOCK, open(_path(sid), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def message(sid, role, content=None, tool_calls=None):
    msg = {"role": role}
    if content is not None:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    append(sid, {"t": "msg", "msg": msg})
    return msg


def event(sid, payload):
    append(sid, {"t": "ui", "event": payload})


def load(sid):
    if not valid_id(sid):
        return None
    with LOCK:
        path = _path(sid)
        if not os.path.isfile(path):
            return None
        meta = {"id": sid, "title": ""}
        messages = []
        events = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("t") == "meta":
                    meta = item
                elif item.get("t") == "msg":
                    messages.append(item["msg"])
                elif item.get("t") == "ui":
                    events.append(item["event"])
        return {"meta": meta, "messages": messages, "events": events}


def set_title(sid, title):
    if not valid_id(sid):
        return False
    with LOCK:
        path = _path(sid)
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            meta = json.loads(fh.readline())
            rest = fh.read()
        meta["title"] = title
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=SESSION_DIR, delete=False) as fh:
                tmp = fh.name
                fh.write((json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8"))
                fh.write(rest)
            os.replace(tmp, path)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return True


def set_title_if_empty(sid, text):
    with LOCK:
        data = load(sid)
        if not data or data["meta"].get("title"):
            return
        title = (text or "").strip().split("\n")[0][:70] or sid
        set_title(sid, title)


def delete(sid):
    if not valid_id(sid):
        return False
    with LOCK:
        path = _path(sid)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True


def list_sessions(limit=None, query=""):
    rows = []
    query = query.strip().casefold()
    with LOCK:
        if not os.path.isdir(SESSION_DIR):
            return rows
        for name in os.listdir(SESSION_DIR):
            if not name.endswith(".jsonl") or not valid_id(name[:-6]):
                continue
            sid = name[:-6]
            path = os.path.join(SESSION_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    first = json.loads(fh.readline())
                stat = os.stat(path)
            except (OSError, ValueError):
                continue
            title = first.get("title") or sid
            if query and query not in (title + " " + sid).casefold():
                continue
            rows.append({"id": sid, "title": title, "created": first.get("created"),
                         "mtime": stat.st_mtime, "bytes": stat.st_size})
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit] if limit is not None else rows


def export_markdown(sid):
    data = load(sid)
    if data is None:
        return None
    title = data["meta"].get("title") or sid
    lines = ["# " + title.replace("\n", " "), "", "Сессия: " + sid, ""]
    labels = {"user": "Пользователь", "assistant": "Ассистент", "tool": "Инструмент"}
    for msg in data["messages"]:
        role = msg.get("role")
        if role not in labels:
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") if part.get("type") == "text"
                                else "[Изображение не включено в экспорт]" for part in content)
        lines.extend(["## " + labels[role], "", str(content), ""])
        if msg.get("tool_calls"):
            text = json.dumps(msg["tool_calls"], ensure_ascii=False, indent=2)
            fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))
            lines.extend([fence + "json", text, fence, ""])
    return "\n".join(lines)
