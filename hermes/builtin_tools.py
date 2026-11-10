"""Registry tools bawaan Hermes.

Setiap tool = satu kemampuan eksekusi yang bisa dipilih otak AI saat menyusun Plan.
Isian argumen ditentukan otak, bukan form manual.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .tools import ToolRegistry

if TYPE_CHECKING:
    from .config import Config

# Nama tool yang dipakai di brain._rule_plan: notify, save_note, web_fill


def build_registry(cfg: "Config", conn: sqlite3.Connection) -> ToolRegistry:
    reg = ToolRegistry()

    # ---- Data lokal ----
    @reg.register("save_note", "Simpan catatan/knowledge ke basis data lokal.", {"key": "kunci unik", "value": "isi catatan"})
    def save_note(key: str, value: str) -> dict:
        from . import db as _db

        _db.set_note(conn, key, value)
        return {"ok": True, "key": key}

    @reg.register("read_notes", "Baca semua catatan dari basis data lokal.", {})
    def read_notes() -> dict:
        from . import db as _db

        return {"ok": True, "notes": _db.list_notes(conn)}

    # ---- Notifikasi Telegram ----
    @reg.register("notify", "Kirim pesan notifikasi ke Telegram.", {"message": "teks pesan", "chat_id": "ID tujuan (opsional)"})
    def notify(message: str, chat_id: str | int | None = None) -> dict:
        from . import telegram_client

        ids = [int(chat_id)] if chat_id else cfg.telegram_allowed
        if not cfg.telegram_bot_token:
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN belum diisi"}
        results = [telegram_client.send_message(cfg, i, message) for i in ids]
        return {"ok": all(r.get("ok") for r in results) if results else False, "sent": results}

    # ---- Dokumen Word (notulen/laporan) ----
    @reg.register("make_docx", "Buat dokumen Word dari judul + daftar paragraf.", {"title": "judul", "paragraphs": "list paragraf", "filename": "nama file output"})
    def make_docx(title: str, paragraphs: list[str], filename: str = "output.docx") -> dict:
        from docx import Document

        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.output_dir / filename
        doc = Document()
        doc.add_heading(title, level=0)
        for p in paragraphs:
            doc.add_paragraph(str(p))
        doc.save(path)
        return {"ok": True, "path": str(path)}

    # ---- Excel / CSV ----
    @reg.register("append_excel", "Tambah satu baris ke file Excel.", {"path": "path file .xlsx", "sheet": "nama sheet", "row": "list nilai per kolom"})
    def append_excel(path: str, sheet: str | None = None, row: list = None) -> dict:  # type: ignore[assignment]
        from openpyxl import load_workbook

        if row is None:
            row = []
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        wb = load_workbook(p) if p.exists() else __import__("openpyxl").Workbook()
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        ws.append(row)
        wb.save(p)
        return {"ok": True, "path": str(p)}

    # ---- Portal Web (Playwright) ----
    @reg.register("web_fill", "Isi & proses portal web via browser headless sesuai instruksi.", {"instruction": "instruksi konkret apa yang diisi/diklik"})
    def web_fill(instruction: str) -> dict:
        from . import web

        if not cfg.portal_base_url:
            return {"ok": False, "error": "PORTAL_BASE_URL belum diisi"}
        return web.run_web_fill(cfg, instruction)

    @reg.register("web_read", "Baca data dari portal web (scrape) sesuai instruksi.", {"instruction": "data apa yang diambil"})
    def web_read(instruction: str) -> dict:
        from . import web

        if not cfg.portal_base_url:
            return {"ok": False, "error": "PORTAL_BASE_URL belum diisi"}
        return web.run_web_read(cfg, instruction)

    # ---- Recipe E-Cuti (alur tetap: export → impor) ----
    @reg.register(
        "ecuti_rekap",
        "Jalankan alur E-Cuti lengkap: login SIMPATIK (Bitwarden), export rekap cuti "
        "1 bulan terakhir dari e-cuti.semarangkota.go.id, lalu impor ke kepegawaian. "
        "Perlu vault Bitwarden terbuka & sesi Google kepegawaian tersimpan.",
        {"tanggal_mulai": "yyyy-mm-dd (opsional)", "tanggal_selesai": "yyyy-mm-dd (opsional)"},
    )
    def ecuti_rekap(tanggal_mulai: str = "", tanggal_selesai: str = "", bw_item: str = "") -> dict:
        from . import recipes

        inputs = {}
        if tanggal_mulai:
            inputs["tanggal_mulai"] = tanggal_mulai
        if tanggal_selesai:
            inputs["tanggal_selesai"] = tanggal_selesai
        if bw_item:
            inputs["bw_item"] = bw_item
        return recipes.run_recipe(cfg, conn, "ecuti_rekap", inputs)

    # ---- Balasan percakapan (tanpa aksi sistem) ----
    @reg.register(
        "reply",
        "Balas pengguna dengan teks biasa. Gunakan ini untuk sapaan, tanya balik, klarifikasi, "
        "obrolan, atau saat TIDAK ada tool yang cocok.",
        {"message": "teks balasan untuk pengguna"},
    )
    def reply(message: str) -> dict:
        return {"ok": True, "reply": message}

    # ---- Ringkasan (butuh LLM; di mode rule cukup lewatkan teks) ----
    @reg.register("summarize", "Rangkum teks panjang menjadi ringkas.", {"text": "teks sumber"})
    def summarize(text: str) -> dict:
        if cfg.brain_provider == "rule":
            return {"ok": True, "summary": text[:280], "note": "mode rule: potong 280 karakter"}
        return {"ok": True, "summary": text[:280]}

    # ---- Bitwarden (kredensial dari vault) ----
    @reg.register(
        "bw_get_password",
        "Ambil password dari vault Bitwarden CLI untuk item berikut.",
        {"item": "nama atau ID item login di Bitwarden"},
    )
    def bw_get_password(item: str) -> dict:
        import subprocess
        import shutil

        if shutil.which("bw") is None:
            return {"ok": False, "error": "`bw` CLI tidak ditemukan di PATH. Install dulu."}
        try:
            p = subprocess.run(
                ["bw", "get", "password", item],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "bw timeout."}
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip()
            if "not logged in" in err.lower() or "session" in err.lower():
                return {"ok": False, "error": "Bitwarden terkunci. Jalankan `bw unlock` lalu set BW_SESSION."}
            return {"ok": False, "error": f"bw: {err[:200]}"}
        password = p.stdout.strip()
        # Jangan pernah mengembalikan password mentah ke log / output chat.
        return {"ok": True, "item": item, "length": len(password), "masked": "•" * min(len(password), 12)}

    @reg.register(
        "bw_get_username",
        "Ambil username dari vault Bitwarden CLI.",
        {"item": "nama atau ID item login di Bitwarden"},
    )
    def bw_get_username(item: str) -> dict:
        import subprocess
        import shutil

        if shutil.which("bw") is None:
            return {"ok": False, "error": "`bw` CLI tidak ditemukan di PATH."}
        try:
            p = subprocess.run(
                ["bw", "get", "username", item],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "bw timeout."}
        if p.returncode != 0:
            return {"ok": False, "error": f"bw: {(p.stderr or p.stdout or '').strip()[:200]}"}
        return {"ok": True, "item": item, "username": p.stdout.strip()}

    return reg


def describe_tools(reg: ToolRegistry) -> str:
    return json.dumps([t.schema() for t in reg.all()], ensure_ascii=False, indent=2)
