"""Basis data lokal (SQLite) — penyimpanan terstruktur Hermes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_records_dataset ON records(dataset);

-- Log aktivitas Hermes (untuk dashboard).
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,        -- info | success | warn | error
    event TEXT NOT NULL,        -- nama kejadian, mis: task_run, tool_call
    message TEXT NOT NULL,
    meta TEXT,                  -- JSON konteks tambahan
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);

-- Status terakhir tiap alur (recipe).
CREATE TABLE IF NOT EXISTS recipe_state (
    key TEXT PRIMARY KEY,
    last_status TEXT,           -- ok | failed
    last_duration_ms INTEGER,
    runs INTEGER DEFAULT 0,
    last_reply TEXT,            -- ringkasan hasil terakhir untuk kartu dashboard
    last_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Tambah kolom bila belum ada (migrasi ringan untuk DB yang sudah jalan)."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in cols:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
        conn.commit()


def connect(db_path: Path, check_same_thread: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "recipe_state", "last_reply", "TEXT")
    return conn


def set_note(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO notes(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, value),
    )
    conn.commit()


def get_note(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM notes WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def list_notes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT key, value, updated_at FROM notes ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def add_record(conn: sqlite3.Connection, dataset: str, payload: str) -> int:
    cur = conn.execute(
        "INSERT INTO records(dataset, payload) VALUES(?, ?)", (dataset, payload)
    )
    conn.commit()
    return int(cur.lastrowid)


def list_records(conn: sqlite3.Connection, dataset: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, payload, created_at FROM records WHERE dataset=? "
        "ORDER BY id DESC LIMIT ?",
        (dataset, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Log aktivitas ----

def add_log(
    conn: sqlite3.Connection,
    level: str,
    event: str,
    message: str,
    meta: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO logs(level, event, message, meta) VALUES(?,?,?,?)",
        (
            level,
            event,
            message,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_logs(
    conn: sqlite3.Connection,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
) -> list[dict]:
    where, args = [], []
    if level:
        where.append("level=?")
        args.append(level)
    if event:
        where.append("event=?")
        args.append(event)
    sql = "SELECT id, level, event, message, meta, created_at FROM logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d["meta"]:
            try:
                d["meta"] = json.loads(d["meta"])
            except json.JSONDecodeError:
                pass
        result.append(d)
    return result


def log_stats(conn: sqlite3.Connection) -> dict:
    """Statistik aktivitas untuk dashboard."""
    total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    by_level = conn.execute(
        "SELECT level, COUNT(*) FROM logs GROUP BY level"
    ).fetchall()
    return {
        "total": total,
        "by_level": {r[0]: r[1] for r in by_level},
    }


def recipe_runs_by_day(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Jumlah eksekusi recipe per hari lokal (YYYY-MM-DD) dalam N hari terakhir."""
    rows = conn.execute(
        "SELECT DATE(created_at,'localtime') d, COUNT(*) c FROM logs "
        "WHERE event='recipe_done' AND DATE(created_at,'localtime') >= DATE('now','localtime', ?) "
        "GROUP BY d",
        (f"-{max(days - 1, 0)} day",),
    ).fetchall()
    return {r["d"]: r["c"] for r in rows}


def recipe_run_counts(conn: sqlite3.Connection) -> dict:
    """Jumlah eksekusi & waktu terakhir per recipe, dihitung dari log."""
    rows = conn.execute(
        "SELECT meta, datetime(created_at,'localtime') AS ts FROM logs "
        "WHERE event='recipe_done' ORDER BY id DESC"
    ).fetchall()
    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except json.JSONDecodeError:
            continue
        key = meta.get("recipe")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        last.setdefault(f"{key}:last", r["ts"])
    return {**counts, **last}


def recipe_stats(conn: sqlite3.Connection) -> dict:
    """Ringkasan eksekusi recipe dari riwayat log (bukan hanya state terakhir)."""
    rows = conn.execute(
        "SELECT meta FROM logs WHERE event='recipe_done'"
    ).fetchall()
    ok = failed = 0
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except json.JSONDecodeError:
            meta = {}
        if meta.get("status") == "ok":
            ok += 1
        else:
            failed += 1
    last = conn.execute(
        "SELECT datetime(MAX(last_run_at),'localtime') FROM recipe_state"
    ).fetchone()[0]
    return {"runs": ok + failed, "ok": ok, "failed": failed, "last_run": last}

