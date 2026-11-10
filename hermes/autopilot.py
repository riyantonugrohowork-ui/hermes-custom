"""Daemon otonom: jalankan recipe sesuai jadwalnya tanpa campur tangan manusia.

Mode:
  - saat container start  : recipe yang punya `run_on_start` dijalankan sekali
  - setiap menit          : periksa jadwal cron tiap recipe (lihat scheduler.py)

Sesi Bitwarden di-refresh bila terkunci sebelum recipe yang membutuhkannya jalan.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("hermes.autopilot")

SECRET_FILE = "/run/secrets/bw_masterpass"
STARTUP_DELAY = int(os.getenv("HERMES_DAILY_DELAY", "25"))


def _read_masterpass() -> str:
    try:
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _bw_status() -> str:
    try:
        p = subprocess.run(["bw", "status", "--nointeraction"],
                           capture_output=True, text=True, timeout=15)
        out = p.stdout or ""
        if '"status":"unlocked"' in out:
            return "unlocked"
        if '"status":"locked"' in out:
            return "locked"
        if '"status":"unauthenticated"' in out:
            return "unauthenticated"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def refresh_bw_session() -> bool:
    """Pastikan BW_SESSION valid; unlock ulang dari master password bila perlu."""
    if _bw_status() == "unlocked":
        return True
    mp = _read_masterpass()
    if not mp:
        log.warning("BW_SESSION tidak valid & master password kosong — vault terkunci.")
        return False
    try:
        if _bw_status() == "unauthenticated":
            email = os.getenv("BW_EMAIL", "")
            if email:
                subprocess.run(
                    ["bw", "login", email, "--passwordenv", "BW_PASSWORD", "--raw"],
                    env={**os.environ, "BW_PASSWORD": mp},
                    capture_output=True, text=True, timeout=25,
                )
        p = subprocess.run(
            ["bw", "unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
            env={**os.environ, "BW_PASSWORD": mp},
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0 and p.stdout.strip():
            os.environ["BW_SESSION"] = p.stdout.strip()
            log.info("Vault Bitwarden dibuka ulang.")
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("Gagal refresh BW session: %s", e)
    return False


_BW_CHECKED_AT = 0.0
_BW_CHECK_EVERY = 300  # detik — jangan panggil `bw` di setiap tick


def refresh_bw_session_if_needed(force: bool = False) -> bool:
    """Buka vault hanya bila perlu & belum dicek dalam 5 menit terakhir.

    Tick berjalan tiap menit; memanggil `bw status` setiap menit itu boros
    dan memperlambat startup karena `bw` itu Node.
    """
    global _BW_CHECKED_AT
    now = time.time()
    if not force and (now - _BW_CHECKED_AT) < _BW_CHECK_EVERY:
        return bool(os.environ.get("BW_SESSION"))
    _BW_CHECKED_AT = now
    return refresh_bw_session()


def run_start_recipes(cfg: "Config") -> None:
    """Jalankan recipe yang ditandai berjalan saat container start.

    Hanya yang benar-benar belum dijalankan: kalau laptop reboot berkali-kali
    dalam sehari, alur tidak ikut berjalan berkali-kali.
    """
    from . import recipes, scheduler, tasks

    conn = tasks.init(cfg.db_path)
    due = {item["key"] for item in scheduler.due_recipes(conn)}
    for meta in recipes.RECIPES:
        key = meta["key"]
        if not recipes.runs_on_start(meta):
            continue
        if key not in due:
            log.info("Recipe %s sudah berjalan sesuai jadwalnya — lewati startup.", key)
            continue
        log.info("Menjalankan recipe saat startup: %s", key)
        try:
            res = recipes.run_recipe(cfg, conn, key)
            status = "ok" if res.get("ok") else "gagal"
            log.info("Recipe %s saat startup: %s", key, status)
        except Exception:  # noqa: BLE001
            log.exception("Recipe %s gagal saat startup", key)
        scheduler.mark_recipe_run(conn, key, datetime.now())


def enabled() -> bool:
    from . import recipes

    return any(recipes.schedule_of(r) or recipes.runs_on_start(r) for r in recipes.RECIPES)


def loop(cfg: "Config") -> None:
    """Daemon otonom: startup recipe + tick scheduler tiap menit."""
    from . import scheduler, tasks

    log.info("Autopilot aktif — recipe berjalan sesuai jadwalnya.")
    time.sleep(STARTUP_DELAY)  # beri waktu container & web siap
    run_start_recipes(cfg)

    conn = tasks.init(cfg.db_path)
    while True:
        try:
            scheduler.tick(cfg, conn)
        except Exception:  # noqa: BLE001
            log.exception("Tick scheduler gagal")
        time.sleep(60)
