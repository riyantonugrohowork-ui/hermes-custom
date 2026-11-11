"""Bot Telegram Hermes: chat -> buat task -> susun plan -> eksekusi."""
from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from . import builtin_tools, runner, tasks

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("hermes.telegram")

HELP = """🤖 **Hermes Agent**

Kirim instruksi apa saja dalam bahasa biasa. Contoh:
  • `catat key: deadline, value: 30 Nov`
  • `kirim notif "rapat pukul 10"`
  • `notulen: [paste teks]` → buat dokumen Word
  • `isi portal: isi form cuti karyawan Budi tgl 1-5`

Perintah:
  /start — mulai
  /help — bantuan
  /tasks — daftar task
  /status — status provider & config
  /lanjut <task_id> — setujui rencana yang menunggu konfirmasi
  /absen [hadir|pulang] — cek presensi harian (baca saja)
"""


def _authed(cfg: "Config", chat_id: int) -> bool:
    return not cfg.telegram_allowed or chat_id in cfg.telegram_allowed


def handle_update(cfg: "Config", conn: sqlite3.Connection, update: dict) -> None:
    from . import telegram_client as tg

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not _authed(cfg, chat_id):
        tg.send_message(cfg, chat_id, "⛔ Chat ID tidak diizinkan.")
        return

    if not text:
        return

    # Jawaban captcha absen: saat resep absen sedang menunggu, teks bebas
    # diperlakukan sebagai kode captcha — user cukup membalas karakternya, tidak
    # perlu perintah. Ini harus diperiksa SEBELUM routing teks bebas, karena
    # kalau tidak balasannya malah dibuatkan task baru.
    if not text.startswith("/"):
        from . import recipe_absen

        if recipe_absen.pending_aktif(conn):
            recipe_absen.tulis_jawaban(conn, text.strip())
            tg.send_message(cfg, chat_id, f"🔑 Jawaban captcha diterima: <b>{text.strip()}</b>")
            return

    if text.startswith("/start") or text.startswith("/help"):
        tg.send_message(cfg, chat_id, HELP)
        return

    if text.startswith("/status"):
        tg.send_message(cfg, chat_id, f"🧠 Otak: `{cfg.brain_provider}`\nPortal: {'✅' if cfg.portal_base_url else '—'}\nTelegram: {'✅' if cfg.telegram_bot_token else '—'}")
        return

    if text.startswith("/tasks"):
        all_tasks = tasks.list_tasks(conn)
        if not all_tasks:
            tg.send_message(cfg, chat_id, "Belum ada task. Kirim instruksi saja.")
            return
        lines = ["📋 **Task:**"]
        for t in all_tasks[:20]:
            flag = "🟢" if t.enabled else "⚪"
            lines.append(f"{flag} `{t.id}` {t.title}")
        tg.send_message(cfg, chat_id, "\n".join(lines))
        return

    if text.startswith("/lanjut"):
        parts = text.split()
        if len(parts) < 2:
            tg.send_message(cfg, chat_id, "Pakai: `/lanjut <task_id>`")
            return
        tid = parts[1]
        registry = builtin_tools.build_registry(cfg, conn)
        result = runner.execute_pending(conn, registry, tid)
        if result is None:
            tg.send_message(cfg, chat_id, "Tidak ada rencana yang menunggu konfirmasi untuk task itu.")
            return
        tg.send_message(cfg, chat_id, runner.summarize_result(result))
        return

    if text.startswith("/absen"):
        # Cek presensi manual. Kalau sesi absen sudah mati, captcha dikirim ke
        # chat ini dan jawabannya dibaca dari sini juga.
        from . import recipes

        sesi = ""
        bagian = text.split()
        if len(bagian) > 1 and bagian[1].lower() in ("hadir", "pulang"):
            sesi = bagian[1].lower()
        tg.send_message(cfg, chat_id, "🔎 Memeriksa presensi…")
        result = recipes.run_recipe(cfg, conn, "absen_cek",
                                   {"sesi_shift": sesi} if sesi else {})
        tg.send_message(cfg, chat_id, recipes.summarize("absen_cek", result))
        return

    # ---- instruksi bebas -> buat task & jalankan ----
    title = text[:40] + ("…" if len(text) > 40 else "")
    task = tasks.create_task(conn, title=title, instruction=text)
    tg.send_message(cfg, chat_id, f"🧠 Menyusun rencana untuk task `{task.id}`…")

    registry = builtin_tools.build_registry(cfg, conn)
    result = runner.run_task(cfg, conn, registry, task.id)

    reply = f"🆔 `{task.id}`\n" + runner.summarize_result(result)
    tg.send_message(cfg, chat_id, reply)


def poll_loop(cfg: "Config", conn: sqlite3.Connection) -> None:
    """Long-polling Telegram (blocking). Jalankan via CLI: hermes telegram."""
    import requests

    if not cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN kosong. Isi di .env")

    base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    offset = 0
    log.info("Telegram long-polling dimulai")
    while True:
        try:
            r = requests.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            r.raise_for_status()
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(cfg, conn, upd)
                except Exception:  # noqa: BLE001
                    log.exception("Gagal memproses update")
        except requests.RequestException:
            log.exception("Koneksi Telegram terganggu")
