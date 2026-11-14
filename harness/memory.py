"""Cross-session persistent memory for LocalHarness.

Stores facts about user preferences, project structure, and environment in data/memory.json.
When enabled, injected into the agent system prompt so the model remembers facts across sessions.
"""
import json
import os
import re
import threading
import time
import uuid

from .config import DATA_DIR

MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
LOCK = threading.RLock()


def _load_raw():
    if not os.path.isfile(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw(facts):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)


def list_facts():
    with LOCK:
        return _load_raw()


def add_fact(text):
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "пустой текст факта"}
    with LOCK:
        facts = _load_raw()
        # check duplicate
        low = text.lower()
        for f in facts:
            if f.get("text", "").strip().lower() == low:
                return {"ok": True, "fact": f, "duplicate": True}
        fact = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created_at": int(time.time()),
        }
        facts.append(fact)
        _save_raw(facts)
        return {"ok": True, "fact": fact}


def delete_fact(fact_id):
    with LOCK:
        facts = _load_raw()
        before = len(facts)
        facts = [f for f in facts if f.get("id") != fact_id]
        if len(facts) < before:
            _save_raw(facts)
            return {"ok": True}
        return {"ok": False, "error": "факт не найден"}


def clear_facts():
    with LOCK:
        _save_raw([])
        return {"ok": True}


def search_facts(query, limit=10):
    query = str(query or "").strip().lower()
    words = [w for w in re.split(r"\s+", query) if w]
    with LOCK:
        facts = _load_raw()
    if not words:
        return facts[:limit]
    scored = []
    for f in facts:
        text = f.get("text", "").lower()
        score = sum(1 for w in words if w in text)
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:limit]]


def system_prompt_block(max_facts=15):
    facts = list_facts()
    if not facts:
        return ""
    lines = ["ДОЛГОВРЕМЕННАЯ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ И ПРОЕКТЕ (сохраняется сквозь сессии):"]
    for f in facts[-max_facts:]:
        lines.append("- %s" % f.get("text", ""))
    return "\n".join(lines)
