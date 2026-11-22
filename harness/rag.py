"""Local RAG / Knowledge Base: indexes folders and documents (MD, TXT, code, PDF, DOCX)
with optional LM Studio/Ollama embeddings and automatic BM25 lexical fallback."""
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from . import config as config_mod

_LOCK = threading.RLock()
_SUPPORTED_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sql", ".sh", ".bat", ".ps1",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
    ".pdf", ".docx",
}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "_internal", ".pytest_cache", ".ruff_cache"}
_MAX_FILE_BYTES = 8 * 1024 * 1024
_CHUNK_CHARS = 900
_CHUNK_OVERLAP = 150
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{2,}")


def _index_path():
    return os.path.join(config_mod.data_dir(), "rag_index.json")


def _load_index():
    path = _index_path()
    if not os.path.isfile(path):
        return {"folders": [], "files": {}, "chunks": [], "updated_at": 0.0, "has_embeddings": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("folders", [])
            data.setdefault("files", {})
            data.setdefault("chunks", [])
            return data
    except Exception:
        pass
    return {"folders": [], "files": {}, "chunks": [], "updated_at": 0.0, "has_embeddings": False}


def _save_index(data):
    path = _index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _extract_docx(path):
    try:
        with zipfile.ZipFile(path, "r") as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        parts = [el.text for el in root.iter() if el.tag.endswith("}t") and el.text]
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_pdf(path):
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages[:200])
    except Exception:
        pass
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        return "\n".join(page.get_text() for page in doc)
    except Exception:
        pass
    # Lightweight fallback: extract printable ASCII/UTF-8 text runs from uncompressed streams
    try:
        with open(path, "rb") as fh:
            raw = fh.read(2 * 1024 * 1024)
        matches = re.findall(rb"\(([^()]{4,300})\)", raw)
        return " ".join(m.decode("utf-8", "ignore") for m in matches[:2000])
    except Exception:
        return ""


def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_MAX_FILE_BYTES)
    except Exception:
        return ""


def _chunk_text(text, chunk_size=_CHUNK_CHARS, overlap=_CHUNK_OVERLAP):
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_size)
        if end < n:
            nl = text.rfind("\n", start + chunk_size // 2, end)
            if nl != -1:
                end = nl
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _tokenize(text):
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _try_embed(texts, cfg=None):
    """Attempt to get embeddings from LM Studio/Ollama /v1/embeddings; returns list of vectors or None."""
    if not texts:
        return None
    cfg = cfg or config_mod.load()
    prov = (cfg.get("providers") or {}).get(cfg.get("provider", "lmstudio")) or {}
    base = (prov.get("base_url") or "http://127.0.0.1:1234/v1").rstrip("/")
    emb_model = cfg.get("rag_embedding_model") or ""
    if not emb_model:
        try:
            req = urllib.request.Request(base + "/models", headers={"User-Agent": "LocalHarness-RAG"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                models = [m.get("id", "") for m in (json.loads(resp.read().decode("utf-8")).get("data") or [])]
                emb_model = next((m for m in models if "embed" in m.lower()), "")
        except Exception:
            return None
    if not emb_model:
        return None
    try:
        body = json.dumps({"model": emb_model, "input": texts[:32]}).encode("utf-8")
        req = urllib.request.Request(
            base + "/embeddings",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "LocalHarness-RAG"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rows = data.get("data") or []
            if len(rows) == len(texts[:32]):
                return [r.get("embedding") for r in rows]
    except Exception:
        return None
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def status():
    with _LOCK:
        idx = _load_index()
        return {
            "ok": True,
            "folders": list(idx.get("folders") or []),
            "files_count": len(idx.get("files") or {}),
            "chunks_count": len(idx.get("chunks") or []),
            "has_embeddings": bool(idx.get("has_embeddings")),
            "updated_at": idx.get("updated_at") or 0.0,
        }


def index_path(target_path, cfg=None, use_embeddings=False):
    """Index a single file or a directory recursively into the local knowledge base."""
    path = os.path.abspath(os.path.expanduser(str(target_path or "").strip().strip('"')))
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "Путь не найден: %s" % target_path}

    files_to_scan = []
    if os.path.isfile(path):
        files_to_scan.append(path)
    else:
        for root, dirs, fnames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in fnames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in _SUPPORTED_EXTS:
                    full = os.path.join(root, fn)
                    try:
                        if os.path.getsize(full) <= _MAX_FILE_BYTES:
                            files_to_scan.append(full)
                    except OSError:
                        pass
            if len(files_to_scan) >= 500:
                break

    with _LOCK:
        idx = _load_index()
        if path not in idx["folders"]:
            idx["folders"].append(path)
        existing_chunks = [c for c in idx["chunks"] if c.get("source") != path and not str(c.get("file", "")).startswith(path + os.sep)]
        new_chunks = []
        indexed_files = 0

        for fpath in files_to_scan:
            text = extract_text(fpath)
            if not text.strip():
                continue
            pieces = _chunk_text(text)
            if not pieces:
                continue
            mtime = 0.0
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                pass
            idx["files"][fpath] = {"mtime": mtime, "chunks": len(pieces), "source": path}
            indexed_files += 1
            for i, piece in enumerate(pieces):
                new_chunks.append({
                    "id": hashlib.md5(("%s:%d" % (fpath, i)).encode("utf-8", "replace")).hexdigest()[:12],
                    "file": fpath,
                    "source": path,
                    "index": i,
                    "text": piece,
                })

        has_emb = bool(idx.get("has_embeddings"))
        if use_embeddings and new_chunks:
            for i in range(0, len(new_chunks), 32):
                batch = new_chunks[i:i + 32]
                vecs = _try_embed([c["text"] for c in batch], cfg=cfg)
                if vecs:
                    has_emb = True
                    for c, v in zip(batch, vecs):
                        c["emb"] = v
                else:
                    break

        idx["chunks"] = existing_chunks + new_chunks
        idx["has_embeddings"] = has_emb
        idx["updated_at"] = time.time()
        _save_index(idx)

    return {
        "ok": True,
        "path": path,
        "indexed_files": indexed_files,
        "added_chunks": len(new_chunks),
        **status(),
    }


def remove_folder(target_path):
    path = os.path.abspath(os.path.expanduser(str(target_path or "").strip().strip('"')))
    with _LOCK:
        idx = _load_index()
        idx["folders"] = [f for f in idx["folders"] if f != path]
        idx["files"] = {k: v for k, v in idx["files"].items() if k != path and v.get("source") != path}
        idx["chunks"] = [c for c in idx["chunks"] if c.get("source") != path and c.get("file") != path]
        idx["updated_at"] = time.time()
        _save_index(idx)
    return status()


def clear():
    with _LOCK:
        _save_index({"folders": [], "files": {}, "chunks": [], "updated_at": time.time(), "has_embeddings": False})
    return status()


def search(query, top_k=5, cfg=None):
    """Search indexed documents using embeddings (if present) + BM25 lexical scoring."""
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "пустой запрос", "results": []}
    top_k = max(1, min(20, int(top_k or 5)))

    with _LOCK:
        idx = _load_index()
        chunks = idx.get("chunks") or []
    if not chunks:
        return {"ok": True, "query": q, "results": [], "note": "База знаний пуста — добавь папку в Настройках → База знаний (RAG)."}

    q_tokens = _tokenize(q)
    q_vec = None
    if idx.get("has_embeddings"):
        vecs = _try_embed([q], cfg=cfg)
        if vecs and vecs[0]:
            q_vec = vecs[0]

    # BM25 statistics
    N = len(chunks)
    doc_freqs = {}
    chunk_tokens = []
    total_len = 0
    for c in chunks:
        toks = _tokenize(c.get("text", "") + " " + os.path.basename(c.get("file", "")))
        chunk_tokens.append(toks)
        total_len += len(toks)
        for t in set(toks):
            doc_freqs[t] = doc_freqs.get(t, 0) + 1
    avg_dl = max(1.0, total_len / max(1, N))

    scored = []
    for c, toks in zip(chunks, chunk_tokens):
        bm25 = 0.0
        dl = len(toks)
        if q_tokens and dl > 0:
            tf_map = {}
            for t in toks:
                tf_map[t] = tf_map.get(t, 0) + 1
            for qt in q_tokens:
                tf = tf_map.get(qt, 0)
                if tf > 0:
                    df = doc_freqs.get(qt, 1)
                    idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
                    bm25 += idf * ((tf * 2.2) / (tf + 1.2 * (1.0 - 0.75 + 0.75 * dl / avg_dl)))
        sem = _cosine(q_vec, c.get("emb")) if (q_vec and c.get("emb")) else 0.0
        score = bm25 + (sem * 5.0)
        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for score, c in scored[:top_k]:
        results.append({
            "file": c.get("file"),
            "chunk_index": c.get("index", 0),
            "score": round(score, 3),
            "text": c.get("text", "")[:1200],
        })
    return {"ok": True, "query": q, "count": len(results), "results": results}
