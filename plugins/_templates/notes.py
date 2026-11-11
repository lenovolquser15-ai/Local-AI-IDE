"""Каталожный плагин notes: долговременные заметки агента в data/notes.md.

Ставится из вкладки «Плагины → Каталог». Не зависит от GPU и интернета.
"""
import os
import time


def _notes_path():
    import harness.config as config
    return os.path.join(config.DATA_DIR, "notes.md")


def note_add(cfg, args):
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "нужен text"}
    path = _notes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n- %s — %s" % (time.strftime("%Y-%m-%d %H:%M"), text))
    return {"ok": True, "saved": True, "file": path}


def note_find(cfg, args):
    query = str(args.get("query") or "").lower().strip()
    limit = int(args.get("limit") or 20)
    path = _notes_path()
    if not os.path.exists(path):
        return {"ok": True, "hits": [], "total": 0, "file": path}
    hits = []
    total = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            if not query or query in line.lower():
                hits.append(line)
    return {"ok": True, "hits": hits[:limit], "total": total, "file": path,
            "shown": min(len(hits), limit)}


NAME = "notes"
TOOLS = [
    {"name": "note_add",
     "desc": "Записать заметку в долговременную память харнеса (data/notes.md). Коротко и по делу.",
     "params": {"text": ("string", "что запомнить")}, "required": ["text"], "fn": note_add},
    {"name": "note_find",
     "desc": "Найти заметки по подстроке. Без query — последние записи.",
     "params": {"query": ("string?", "что искать"), "limit": ("number?", "сколько строк, по умолчанию 20")},
     "required": [], "fn": note_find},
]
