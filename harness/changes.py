"""Workspace & explicit file change tracker for post-run / post-goal diff summaries (`N files changed +X -Y`)."""
import difflib
import hashlib
import os
import time

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "$RECYCLE.BIN", "AppData", "target", ".gradle", "dist", "build",
    "_internal", ".pytest_cache", ".ruff_cache", "sessions", "shots", "jobs",
}
BINARY_EXTS = {
    ".exe", ".dll", ".pyd", ".so", ".dylib", ".bin", ".gguf", ".safetensors",
    ".pt", ".pth", ".onnx", ".zip", ".7z", ".rar", ".tar", ".gz", ".png",
    ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".pdf", ".mp3", ".wav",
    ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".otf", ".pyc", ".db", ".sqlite",
}

MAX_TRACK_FILE_BYTES = 512 * 1024
MAX_SCAN_FILES = 1600
MAX_SCAN_SECONDS = 0.18
MAX_DIFF_LINES = 140
MAX_SUMMARY_FILES = 40


def _is_ignored_file(name):
    low = name.lower()
    if low.endswith(".lh.tmp") or low.endswith(".tmp") or low.endswith(".log"):
        return True
    _, ext = os.path.splitext(low)
    return ext in BINARY_EXTS


def _read_text_safe(path, max_bytes=MAX_TRACK_FILE_BYTES):
    """Читает текстовый файл (до max_bytes); для бинарных или отсутствующих возвращает None."""
    try:
        st = os.stat(path)
        if st.st_size > max_bytes:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes or b"\x00" in raw[:4096]:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


def compute_line_diff(before_text, after_text, rel_path="file", max_lines=MAX_DIFF_LINES):
    """Считает точное число добавленных (+added) и удалённых (-removed) строк и unified diff."""
    b_lines = (before_text or "").splitlines()
    a_lines = (after_text or "").splitlines()
    if before_text is None and after_text is not None:
        added = len(a_lines)
        removed = 0
    elif before_text is not None and after_text is None:
        added = 0
        removed = len(b_lines)
    else:
        added = 0
        removed = 0
        matcher = difflib.SequenceMatcher(None, b_lines, a_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "insert":
                added += (j2 - j1)
            elif tag == "delete":
                removed += (i2 - i1)
            elif tag == "replace":
                removed += (i2 - i1)
                added += (j2 - j1)

    diff_lines = list(difflib.unified_diff(
        b_lines,
        a_lines,
        fromfile="a/" + rel_path.replace("\\", "/"),
        tofile="b/" + rel_path.replace("\\", "/"),
        n=2,
        lineterm="",
    ))
    truncated = False
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + ["… [дифф сокращён, ещё %d строк]" % (len(diff_lines) - max_lines)]
        truncated = True
    return added, removed, "\n".join(diff_lines), truncated


class WorkspaceTracker:
    """Отслеживает все изменения файлов за прогон агента / цели (как через write_file/edit_file, так и через shell/python)."""

    def __init__(self, workspace):
        self.workspace = os.path.abspath(workspace) if workspace else ""
        self._baseline = {}       # norm_path -> {"mtime_ns": int, "size": int, "sha1": str, "text": str|None}
        self._explicit_init = {}  # norm_path -> str|None (состояние ДО первого явного изменения)
        self._touched = set()     # norm_path всех явно затронутых файлов (даже вне workspace)
        self._path_display = {}   # norm_path -> original abs path
        if self.workspace and os.path.isdir(self.workspace):
            self._baseline = self._scan_workspace()

    def _norm(self, path):
        abs_p = os.path.abspath(path)
        norm_p = os.path.normcase(abs_p)
        self._path_display.setdefault(norm_p, abs_p)
        return norm_p

    def _rel(self, abs_path):
        if self.workspace:
            try:
                rel = os.path.relpath(abs_path, self.workspace)
                if not rel.startswith(".."):
                    return rel.replace("\\", "/")
            except ValueError:
                pass
        return os.path.basename(abs_path) or abs_path.replace("\\", "/")

    def _scan_workspace(self):
        snap = {}
        if not self.workspace or not os.path.isdir(self.workspace):
            return snap
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        count = 0
        for root, dirs, files in os.walk(self.workspace):
            if time.monotonic() > deadline or count >= MAX_SCAN_FILES:
                break
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            for fname in files:
                if count >= MAX_SCAN_FILES or time.monotonic() > deadline:
                    break
                if _is_ignored_file(fname):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                if st.st_size > MAX_TRACK_FILE_BYTES:
                    continue
                count += 1
                norm_p = self._norm(fpath)
                text = _read_text_safe(fpath)
                if text is None:
                    continue
                sha1 = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
                snap[norm_p] = {
                    "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
                    "size": st.st_size,
                    "sha1": sha1,
                    "text": text,
                }
        return snap

    def before_file_op(self, path):
        """Фиксирует состояние файла перед вызовом write_file / edit_file."""
        if not path:
            return None
        norm_p = self._norm(path)
        abs_p = self._path_display[norm_p]
        before = _read_text_safe(abs_p) if os.path.isfile(abs_p) else None
        if norm_p not in self._explicit_init and norm_p not in self._baseline:
            self._explicit_init[norm_p] = before
        self._touched.add(norm_p)
        return before

    def after_file_op(self, path, before_text=None):
        """Возвращает компактную сводку конкретного шага (для плашки `Edit · file.py 2KB +16 -2` в раздумьях)."""
        if not path:
            return None
        norm_p = self._norm(path)
        abs_p = self._path_display[norm_p]
        self._touched.add(norm_p)
        if not os.path.isfile(abs_p):
            return None
        try:
            sz = os.path.getsize(abs_p)
        except OSError:
            sz = 0
        after_text = _read_text_safe(abs_p)
        rel = self._rel(abs_p)
        if after_text is None:
            return {
                "path": abs_p,
                "rel_path": rel,
                "name": os.path.basename(abs_p),
                "size_bytes": sz,
                "status": "added" if before_text is None else "modified",
                "added": 0,
                "removed": 0,
                "diff": "",
            }
        added, removed, diff_txt, _ = compute_line_diff(before_text, after_text, rel_path=rel, max_lines=80)
        return {
            "path": abs_p,
            "rel_path": rel,
            "name": os.path.basename(abs_p),
            "size_bytes": sz,
            "status": "added" if before_text is None else "modified",
            "added": added,
            "removed": removed,
            "diff": diff_txt,
        }

    def finalize(self):
        """Собирает итоговую сводку всех изменённых файлов за весь прогон / цель (`N files changed +X -Y`)."""
        after_ws = self._scan_workspace()
        candidate_norms = set(self._baseline.keys()) | set(after_ws.keys()) | set(self._touched)
        changed_files = []
        total_add = 0
        total_rem = 0

        for norm_p in sorted(candidate_norms):
            abs_p = self._path_display.get(norm_p, norm_p)
            if norm_p in self._baseline:
                init_text = self._baseline[norm_p]["text"]
            else:
                init_text = self._explicit_init.get(norm_p)

            exists_now = os.path.isfile(abs_p)
            if not exists_now:
                if init_text is None:
                    continue
                now_text = None
                sz = 0
                status = "deleted"
            else:
                if norm_p in after_ws:
                    now_rec = after_ws[norm_p]
                    if norm_p in self._baseline and now_rec["sha1"] == self._baseline[norm_p]["sha1"]:
                        continue
                    now_text = now_rec["text"]
                    sz = now_rec["size"]
                else:
                    try:
                        sz = os.path.getsize(abs_p)
                    except OSError:
                        sz = 0
                    now_text = _read_text_safe(abs_p)
                    if init_text == now_text:
                        continue
                status = "added" if init_text is None else "modified"

            if init_text == now_text:
                continue

            rel = self._rel(abs_p)
            if init_text is None and now_text is None:
                add_n, rem_n, diff_str = 0, 0, "(бинарный файл изменён)"
            else:
                add_n, rem_n, diff_str, _ = compute_line_diff(init_text, now_text, rel_path=rel)
            total_add += add_n
            total_rem += rem_n
            if len(changed_files) < MAX_SUMMARY_FILES:
                changed_files.append({
                    "path": abs_p,
                    "rel_path": rel,
                    "name": os.path.basename(abs_p),
                    "status": status,
                    "size_bytes": sz,
                    "added": add_n,
                    "removed": rem_n,
                    "diff": diff_str,
                })

        total_count = len(changed_files)
        return {
            "files_changed": total_count,
            "insertions": total_add,
            "deletions": total_rem,
            "files": changed_files,
        }
