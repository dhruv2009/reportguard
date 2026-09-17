"""Read-only SQL execution.

- db opened with mode=ro
- authorizer only allows SELECT/READ/FUNCTION/RECURSIVE
- single statement, VM step budget, row cap
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class SqlRejected(ValueError):
    pass


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, check_same_thread=False)
    return conn


def _authorizer(action, arg1, arg2, dbname, source):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def run_readonly_sql(db_path: str | Path, query: str, max_rows: int = 50, max_vm_steps: int = 5_000_000) -> dict:
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        raise SqlRejected("Empty query")
    if ";" in q:
        raise SqlRejected("Only a single statement is allowed (remove ';').")
    if not q.lower().startswith(("select", "with")):
        raise SqlRejected("Only SELECT (or WITH ... SELECT) queries are allowed.")

    conn = connect_readonly(db_path)
    steps = {"n": 0}

    def _budget():
        steps["n"] += 1
        return 1 if steps["n"] > max_vm_steps // 1000 else 0

    try:
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(_budget, 1000)
        try:
            cur = conn.execute(q)
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            if "not authorized" in msg:
                raise SqlRejected(f"Blocked by read-only policy: {msg}") from exc
            if "interrupted" in msg:
                raise SqlRejected("Query exceeded the compute budget; add filters or aggregate.") from exc
            raise SqlRejected(f"SQL error: {msg}") from exc
        columns = [d[0] for d in cur.description or []]
        max_rows = max(1, min(int(max_rows), 200))
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = [list(r) for r in rows[:max_rows]]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
    finally:
        conn.close()


def describe_schema(db_path: str | Path) -> dict:
    conn = connect_readonly(db_path)
    try:
        tables = {}
        for (name, sql) in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            tables[name] = {"ddl": sql, "row_count": count}
        return {"dialect": "sqlite", "timestamps": "TEXT 'YYYY-MM-DD HH:MM:SS' in UTC", "tables": tables}
    finally:
        conn.close()
