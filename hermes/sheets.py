"""Akses Google Sheet penomoran surat (tab bulan berjalan).

Nomor awal diambil dari Sheet, bukan dari API kepegawaian — API bisa
tertinggal dari catatan manual yang sudah ditulis di Sheet.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BULAN_TAB = [
    "JANUARI", "FEBRUARI", "MARET", "APRIL", "MEI", "JUNI",
    "JULI", "AGUSTUS", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DESEMBER",
]
BULAN_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def tab_bulan(d: date | None = None) -> str:
    """Nama tab bulan berjalan, mis. 'NOVEMBER'."""
    return BULAN_TAB[(d or date.today()).month - 1]


def tanggal_id(d: date | None = None) -> str:
    """Tanggal format Sheet: '10 November 2026'."""
    d = d or date.today()
    return f"{d.day} {BULAN_ID[d.month - 1]} {d.year}"


def _service(cfg: "Config"):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not cfg.google_service_account_file:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_FILE belum diisi — tidak bisa menulis ke Sheet."
        )
    creds = service_account.Credentials.from_service_account_file(
        cfg.google_service_account_file, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id(cfg: "Config") -> str:
    if not cfg.google_sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID belum diisi di .env.")
    return cfg.google_sheet_id


def read_rows(cfg: "Config", tab: str) -> list[list[str]]:
    svc = _service(cfg)
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(cfg), range=f"{tab}!A:E")
        .execute()
    )
    return res.get("values", [])


def next_number(cfg: "Config", tab: str | None = None) -> tuple[int, str]:
    """Nomor berikutnya = NOMOR tertinggi di tab bulan ini + 1.

    Kolom NOMOR boleh berisi angka saja ('1274') atau sudah lengkap
    ('1274/800/X/2026'); keduanya dibaca sebagai angka awal.
    """
    tab = tab or tab_bulan()
    highest = 0
    for row in read_rows(cfg, tab)[1:]:
        if not row:
            continue
        raw = str(row[0]).strip().split("/")[0]
        if raw.isdigit():
            highest = max(highest, int(raw))
    return highest + 1, tab


def append_rows(cfg: "Config", tab: str, rows: list[list[str]]) -> int:
    """Tambahkan baris ke bawah tabel; return jumlah baris terkirim."""
    if not rows:
        return 0
    svc = _service(cfg)
    svc.spreadsheets().values().append(
        spreadsheetId=_sheet_id(cfg),
        range=f"{tab}!A:E",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    return len(rows)
