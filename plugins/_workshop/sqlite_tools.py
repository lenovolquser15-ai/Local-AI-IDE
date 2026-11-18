"""SQLite Database Exploration Plugin for LocalHarness.

Provides safe SQL query execution and schema inspection for local database files.
"""
import os
import sqlite3

NAME = "sqlite_tools"


def _sql_query(cfg, args):
    db_path = args.get("path")
    if not db_path or not os.path.exists(db_path):
        return {"ok": False, "error": f"Файл базы данных не найден: {db_path}"}

    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "пустой SQL запрос"}

    limit = min(max(int(args.get("limit") or 50), 1), 500)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query)
        if cur.description:
            cols = [col[0] for col in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchmany(limit)]
            conn.close()
            return {"ok": True, "columns": cols, "count": len(rows), "rows": rows}
        else:
            conn.commit()
            affected = cur.rowcount
            conn.close()
            return {"ok": True, "rows_affected": affected}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка SQL: {exc}"}


def _sql_schema(cfg, args):
    db_path = args.get("path")
    if not db_path or not os.path.exists(db_path):
        return {"ok": False, "error": f"Файл базы данных не найден: {db_path}"}

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'")
        tables = [{"type": row[0], "name": row[1], "schema": row[2]} for row in cur.fetchall()]
        conn.close()
        return {"ok": True, "tables_count": len(tables), "tables": tables}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка чтения схемы: {exc}"}


TOOLS = [
    {
        "name": "sql_query",
        "desc": "Выполнить SQL-запрос к локальной базе данных SQLite",
        "params": {
            "path": ("string", "путь к .db или .sqlite файлу"),
            "query": ("string", "текст SQL-запроса (SELECT, INSERT, UPDATE, etc.)"),
            "limit": ("number?", "максимальное число строк в ответе (по умолчанию 50)"),
        },
        "required": ["path", "query"],
        "fn": _sql_query,
    },
    {
        "name": "sql_schema",
        "desc": "Показать список таблиц и DDL схему базы данных SQLite",
        "params": {
            "path": ("string", "путь к .db или .sqlite файлу"),
        },
        "required": ["path"],
        "fn": _sql_schema,
    },
]
