"""Session persistence: model messages and UI timeline in one JSONL per chat."""
import json
import os
import re
import tempfile
import threading
import time
import uuid

from . import config as config_mod
from .config import SESSION_DIR

LOCK = threading.RLock()


def _session_dir():
    if SESSION_DIR != os.path.join(config_mod.ROOT, "data", "sessions"):
        return SESSION_DIR
    return config_mod.session_dir()


def valid_id(sid):
    return isinstance(sid, str) and re.fullmatch(r"[0-9A-Za-z_-]{1,100}", sid) is not None


def _path(sid):
    if not valid_id(sid):
        raise ValueError("invalid session id")
    sdir = _session_dir()
    os.makedirs(sdir, exist_ok=True)
    return os.path.join(sdir, "%s.jsonl" % sid)


def normalize_ws(raw):
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(text)))


def workspace_label(ws):
    clean = str(ws or "").strip().rstrip("\\/")
    if not clean:
        return ""
    base = os.path.basename(clean)
    return base or clean


def create(title="", workspace=""):
    ws = normalize_ws(workspace)
    with LOCK:
        for _ in range(16):
            sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            try:
                with open(_path(sid), "x", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "t": "meta",
                        "id": sid,
                        "created": time.time(),
                        "title": title,
                        "workspace": ws,
                    }, ensure_ascii=False) + "\n")
                return sid
            except FileExistsError:
                continue
    raise RuntimeError("failed to allocate unique session id")


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
            with tempfile.NamedTemporaryFile(dir=_session_dir(), delete=False) as fh:
                tmp = fh.name
                fh.write((json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8"))
                fh.write(rest)
            os.replace(tmp, path)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return True


def set_workspace(sid, workspace=""):
    if not valid_id(sid):
        return False
    ws = normalize_ws(workspace)
    with LOCK:
        path = _path(sid)
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            meta = json.loads(fh.readline())
            rest = fh.read()
        meta["workspace"] = ws
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=_session_dir(), delete=False) as fh:
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


def truncate_from_user_turn(sid, user_turn_index=-1):
    """Truncate a session starting at the specified user turn (0-based among user turns, or -1 for last).
    Returns {'ok': True, 'removed_text': str, 'remaining_messages': list} or {'ok': False, ...}."""
    if not valid_id(sid):
        return {"ok": False, "error": "invalid session id"}
    with LOCK:
        path = _path(sid)
        if not os.path.isfile(path):
            return {"ok": False, "error": "no such session"}
        records = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue

        user_msg_positions = []
        user_ui_positions = []
        for idx, rec in enumerate(records):
            if rec.get("t") == "msg" and (rec.get("msg") or {}).get("role") == "user":
                user_msg_positions.append(idx)
            elif rec.get("t") == "ui" and (rec.get("event") or {}).get("type") == "user":
                user_ui_positions.append(idx)

        if not user_msg_positions:
            return {"ok": False, "error": "в сессии нет сообщений пользователя"}

        t_idx = int(user_turn_index if user_turn_index is not None else -1)
        if t_idx < 0:
            t_idx = len(user_msg_positions) + t_idx
        t_idx = max(0, min(len(user_msg_positions) - 1, t_idx))

        cut_msg_pos = user_msg_positions[t_idx]
        removed_msg = records[cut_msg_pos].get("msg") or {}
        removed_content = removed_msg.get("content") or ""
        if isinstance(removed_content, list):
            removed_text = "\n".join(p.get("text", "") for p in removed_content if isinstance(p, dict) and p.get("type") == "text")
        else:
            removed_text = str(removed_content)

        if t_idx < len(user_ui_positions):
            cut_pos = min(cut_msg_pos, user_ui_positions[t_idx])
        else:
            cut_pos = cut_msg_pos

        kept = [r for i, r in enumerate(records) if r.get("t") == "meta" or i < cut_pos]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

        remaining_msgs = [r["msg"] for r in kept if r.get("t") == "msg"]
        return {
            "ok": True,
            "turn_index": t_idx,
            "removed_text": removed_text,
            "remaining_messages": remaining_msgs,
        }


def list_sessions(limit=None, query=""):
    rows = []
    query = query.strip().casefold()
    with LOCK:
        sdir = _session_dir()
        if not os.path.isdir(sdir):
            return rows
        for name in os.listdir(sdir):
            if not name.endswith(".jsonl") or not valid_id(name[:-6]):
                continue
            sid = name[:-6]
            path = os.path.join(sdir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    first = json.loads(fh.readline())
                stat = os.stat(path)
            except (OSError, ValueError):
                continue
            title = first.get("title") or sid
            ws = normalize_ws(first.get("workspace") or "")
            ws_name = workspace_label(ws)
            if query and query not in (title + " " + sid + " " + ws + " " + ws_name).casefold():
                continue
            rows.append({
                "id": sid,
                "title": title,
                "workspace": ws,
                "workspace_name": ws_name,
                "created": first.get("created"),
                "mtime": stat.st_mtime,
                "bytes": stat.st_size,
            })
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
