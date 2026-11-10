"""Model Task — unit kerja Hermes.

Task = instruksi bahasa manusia + jadwal + rencana aksi (plan) yang dibuat otak AI.
Tidak ada skema field kaku: otak yang memutuskan isian & urutan langkah.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    schedule TEXT,              -- cron | interval | NULL (sekali jalan)
    enabled INTEGER DEFAULT 1,
    last_plan TEXT,             -- JSON rencana aksi terakhir dari otak
    last_result TEXT,           -- JSON hasil eksekusi terakhir
    last_run_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,       -- ok | error | partial
    plan TEXT,
    result TEXT,
    error TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP
);

-- Rencana yang menunggu konfirmasi manusia sebelum dieksekusi.
CREATE TABLE IF NOT EXISTS pending_plans (
    task_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Riwayat percakapan per task (user & assistant bergantian).
CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,             -- user | assistant
    content TEXT NOT NULL,
    meta TEXT,                      -- JSON opsional (mis. tool yang dipakai)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_task_messages ON task_messages(task_id, id);
"""


@dataclass
class Task:
    id: str
    title: str
    instruction: str
    schedule: str | None = None
    enabled: bool = True
    last_plan: dict | None = None
    last_result: dict | None = None
    last_run_at: str | None = None


@dataclass
class Action:
    """Satu langkah dalam rencana. `tool` = nama tool, `args` = isian yang diputuskan otak."""
    tool: str
    args: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Plan:
    """Rencana aksi yang disusun otak AI dari instruksi."""
    task_id: str
    reasoning: str
    actions: list[Action]
    needs_confirmation: bool = False  # true jika ada aksi destruktif / submit final


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def create_task(conn: sqlite3.Connection, title: str, instruction: str, schedule: str | None = None) -> Task:
    task = Task(
        id=uuid.uuid4().hex[:12],
        title=title,
        instruction=instruction,
        schedule=schedule,
    )
    conn.execute(
        "INSERT INTO tasks(id, title, instruction, schedule) VALUES(?,?,?,?)",
        (task.id, task.title, task.instruction, task.schedule),
    )
    conn.commit()
    return task


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        instruction=row["instruction"],
        schedule=row["schedule"],
        enabled=bool(row["enabled"]),
        last_plan=json.loads(row["last_plan"]) if row["last_plan"] else None,
        last_result=json.loads(row["last_result"]) if row["last_result"] else None,
        last_run_at=row["last_run_at"],
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def list_tasks(conn: sqlite3.Connection, enabled_only: bool = False) -> list[Task]:
    q = "SELECT * FROM tasks"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY created_at DESC"
    return [_row_to_task(r) for r in conn.execute(q).fetchall()]


def save_plan(conn: sqlite3.Connection, task_id: str, plan: Plan) -> None:
    conn.execute(
        "UPDATE tasks SET last_plan=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(asdict(plan), ensure_ascii=False), task_id),
    )
    conn.commit()


def save_result(
    conn: sqlite3.Connection,
    task_id: str,
    result: dict,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE tasks SET last_result=?, last_run_at=CURRENT_TIMESTAMP, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(result, ensure_ascii=False), task_id),
    )
    conn.execute(
        "INSERT INTO task_runs(task_id, status, plan, result, error, finished_at) "
        "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            task_id,
            status,
            json.dumps(result.get("plan"), ensure_ascii=False) if result.get("plan") else None,
            json.dumps(result, ensure_ascii=False),
            error,
        ),
    )
    conn.commit()


def set_enabled(conn: sqlite3.Connection, task_id: str, enabled: bool) -> None:
    conn.execute(
        "UPDATE tasks SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if enabled else 0, task_id),
    )
    conn.commit()


def update_instruction(conn: sqlite3.Connection, task_id: str, instruction: str) -> None:
    """Perbarui instruksi task (pesan terbaru pada utas)."""
    conn.execute(
        "UPDATE tasks SET instruction=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (instruction, task_id),
    )
    conn.commit()


def delete_task(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM pending_plans WHERE task_id=?", (task_id,))
    conn.commit()


def list_history(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Riwayat eksekusi — gabungan task_runs + judul task."""
    rows = conn.execute(
        """SELECT r.id, r.task_id, r.status, r.plan, r.result, r.error,
                  r.started_at, r.finished_at, t.title, t.instruction
           FROM task_runs r
           LEFT JOIN tasks t ON t.id = r.task_id
           ORDER BY r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = json.loads(d["result"]) if d["result"] else None
        except json.JSONDecodeError:
            pass
        try:
            d["plan"] = json.loads(d["plan"]) if d["plan"] else None
        except json.JSONDecodeError:
            pass
        out.append(d)
    return out


# ---- Riwayat percakapan per task ----

def add_message(conn: sqlite3.Connection, task_id: str, role: str, content: str, meta: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO task_messages(task_id, role, content, meta) VALUES(?,?,?,?)",
        (task_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_messages(conn: sqlite3.Connection, task_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, meta, created_at FROM task_messages "
        "WHERE task_id=? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows[-limit:]:
        d = dict(r)
        if d["meta"]:
            try:
                d["meta"] = json.loads(d["meta"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


# ---- Pending plans (menunggu konfirmasi) ----

def set_pending(conn: sqlite3.Connection, task_id: str, plan_data: dict) -> None:
    conn.execute(
        "INSERT INTO pending_plans(task_id, plan) VALUES(?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET plan=excluded.plan, created_at=CURRENT_TIMESTAMP",
        (task_id, json.dumps(plan_data, ensure_ascii=False)),
    )
    conn.commit()


def get_pending(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute("SELECT plan FROM pending_plans WHERE task_id=?", (task_id,)).fetchone()
    return json.loads(row["plan"]) if row else None


def clear_pending(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM pending_plans WHERE task_id=?", (task_id,))
    conn.commit()


def init(db_path: Path) -> sqlite3.Connection:
    conn = db.connect(db_path, check_same_thread=False)
    ensure_schema(conn)
    return conn


def run_stats(conn: sqlite3.Connection) -> dict:
    """Statistik eksekusi task untuk dashboard."""
    total_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    total_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM task_runs WHERE status='ok'").fetchone()[0]
    error = conn.execute("SELECT COUNT(*) FROM task_runs WHERE status='error'").fetchone()[0]
    partial = conn.execute("SELECT COUNT(*) FROM task_runs WHERE status='partial'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM pending_plans").fetchone()[0]
    enabled = conn.execute("SELECT COUNT(*) FROM tasks WHERE enabled=1").fetchone()[0]

    # Eksekusi per hari (7 hari terakhir)
    per_day = conn.execute(
        "SELECT DATE(started_at) d, COUNT(*) c FROM task_runs "
        "WHERE started_at >= DATE('now','-6 day') GROUP BY d ORDER BY d"
    ).fetchall()
    by_day = {r["d"]: r["c"] for r in per_day}

    success_rate = (ok / total_runs * 100) if total_runs else 0.0
    return {
        "total_tasks": total_tasks,
        "enabled_tasks": enabled,
        "total_runs": total_runs,
        "ok": ok,
        "error": error,
        "partial": partial,
        "pending": pending,
        "success_rate": round(success_rate, 1),
        "by_day": by_day,
    }
