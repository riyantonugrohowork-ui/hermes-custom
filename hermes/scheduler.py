"""Scheduler: jalankan task berulang & recipe sesuai jadwalnya.

Dua sumber jadwal:
  1. Recipe (utama) — jadwal dideklarasikan di `recipes.RECIPES` (cron).
  2. Task lama — kolom `tasks.schedule` ("every:<detik>" | "cron:<expr>").

Format cron: menit jam tanggal bulan hari (mis. `*/30 * * * *`).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from . import builtin_tools, recipes, runner, tasks

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("hermes.scheduler")

LAST_RUN_KEY = "scheduler_last_run"


def _parse_schedule(spec: str | None) -> tuple[str, int | None]:
    if not spec:
        return ("once", None)
    if spec.startswith("every:"):
        try:
            return ("every", int(spec.split(":", 1)[1]))
        except ValueError:
            return ("once", None)
    if spec.startswith("cron:"):
        return ("cron", None)
    return ("once", None)


def _next_interval_seconds(spec: str | None) -> int | None:
    kind, every = _parse_schedule(spec)
    if kind == "every" and every:
        return every
    return None


def _due_by_cron(spec: str | None, last: datetime, now: datetime) -> bool:
    from . import cron

    kind, _ = _parse_schedule(spec)
    if kind != "cron":
        return False
    return cron.is_due(spec.split(":", 1)[1], last, now)


def _recipe_last_run(conn: sqlite3.Connection, key: str) -> datetime | None:
    from . import db

    row = db.get_note(conn, f"recipe_last:{key}")
    if not row:
        return None
    try:
        return datetime.fromisoformat(row)
    except ValueError:
        return None


def mark_recipe_run(conn: sqlite3.Connection, key: str, when: datetime) -> None:
    from . import db

    db.set_note(conn, f"recipe_last:{key}", when.isoformat(timespec="seconds"))


def due_recipes(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Recipe yang jadwal terakhirnya sudah lewat dan belum dijalankan.

    Satu-satunya tempat yang menentukan "perlu jalan atau tidak", supaya
    pemanggil (daemon, startup, CLI) tidak pernah menjalankan dua kali.
    """
    from . import cron

    now = now or datetime.now()
    out: list[dict] = []
    for meta in recipes.RECIPES:
        key = meta["key"]
        # Satu recipe bisa punya beberapa jendela jadwal (mis. absen hadir pagi
        # dan pulang sore). Yang menentukan: kemunculan TERAKHIR dari gabungan
        # semuanya, dibandingkan dengan waktu jalan terakhir.
        jadwal = recipes.schedules_of(meta)
        if not jadwal:
            continue
        terjadwal = []
        for spec in jadwal:
            try:
                terjadwal.append(cron.prev_run(spec, now))
            except cron.CronError:
                log.warning("Jadwal recipe %s tidak valid: %r", key, spec)
        if not terjadwal:
            continue
        scheduled = max(terjadwal)
        last = _recipe_last_run(conn, key)
        if last is not None and last >= scheduled:
            continue  # kemunculan terakhir sudah dijalankan
        out.append({"key": key, "scheduled": scheduled, "last": last})
    return out


def run_due_recipes(
    cfg: "Config",
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> list[dict]:
    """Jalankan recipe yang jadwalnya sudah lewat dan belum dijalankan.

    Semantik catch-up: yang dibandingkan bukan "sudah waktunya?" tapi
    "kemunculan jadwal TERAKHIR sebelum sekarang sudah dijalankan?".
    Jadi kalau laptop mati/tidur saat jam 07:00 lalu dinyalakan jam 08:00,
    alur tetap jalan sekali — dan tidak diulang saat restart berikutnya.
    Tidak mengejar beruntun: yang terlewat hanya dikejar sekali.
    """
    now = now or datetime.now()
    due = due_recipes(conn, now)
    results: list[dict] = []
    for item in due:
        key = item["key"]
        last = item["last"]
        log.info(
            "Menjalankan recipe %s (jadwal %s, terakhir jalan %s)",
            key, item["scheduled"].strftime("%Y-%m-%d %H:%M"),
            last.strftime("%Y-%m-%d %H:%M") if last else "belum pernah",
        )
        mark_recipe_run(conn, key, now)
        try:
            res = recipes.run_recipe(cfg, conn, key)
        except Exception as e:  # noqa: BLE001
            log.exception("Recipe %s gagal", key)
            res = {"ok": False, "error": str(e)}
        results.append({"recipe": key, "result": res})
    return results


def run_due_tasks(cfg: "Config", conn: sqlite3.Connection) -> list[dict]:
    """Jalankan semua task berjadwal yang sudah jatuh tempo."""
    results = []
    registry = builtin_tools.build_registry(cfg, conn)
    for t in tasks.list_tasks(conn, enabled_only=True):
        interval = _next_interval_seconds(t.schedule)
        if interval is None:
            continue
        last = t.last_run_at
        if last:
            try:
                elapsed = time.time() - datetime.fromisoformat(last).timestamp()
            except ValueError:
                elapsed = interval  # paksa jalan jika format tak dikenal
            if elapsed < interval:
                continue
        log.info("Menjalankan task terjadwal %s (%s)", t.id, t.title)
        res = runner.run_task(cfg, conn, registry, t.id)
        results.append({"task_id": t.id, "result": res})
    return results


def tick(cfg: "Config", conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Satu putaran pemeriksaan jadwal (dipakai loop & bisa dipanggil manual)."""
    recipe_runs = run_due_recipes(cfg, conn, now)
    task_runs = run_due_tasks(cfg, conn)
    return {"recipes": recipe_runs, "tasks": task_runs}


def loop(cfg: "Config", conn: sqlite3.Connection, poll_seconds: int = 30) -> None:
    """Loop utama scheduler (blocking)."""
    log.info("Scheduler aktif, cek tiap %ss", poll_seconds)
    while True:
        try:
            tick(cfg, conn)
        except Exception:  # noqa: BLE001
            log.exception("Gagal menjalankan jadwal")
        time.sleep(poll_seconds)
