"""Resep: bersihkan berkas hasil unduhan di folder output.

Rekap e-cuti dan DUK menghasilkan berkas baru setiap kali dijalankan
(`output/ecuti/*.xls`, `output/duk/*.json`). Tanpa pembersihan folder itu
terus membesar. Resep ini menghapus berkas yang umurnya melewati batas
retensi; folder dan berkas baru dibiarkan.

Batas retensi dibaca dari `HERMES_OUTPUT_RETENSI_HARI` (default 7 hari),
jadi umur berkas tidak perlu diubah di kode saat kebijakan berubah.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

RETENSI_DEFAULT = 7


def retensi_hari() -> int:
    """Batas umur berkas (hari) dari env; nilai tak sah kembali ke default."""
    try:
        nilai = int(os.getenv("HERMES_OUTPUT_RETENSI_HARI", str(RETENSI_DEFAULT)))
    except ValueError:
        return RETENSI_DEFAULT
    return nilai if nilai > 0 else RETENSI_DEFAULT


def _di_dalam(path: Path, induk: Path) -> bool:
    """True bila `path` benar-benar berada di bawah `induk`.

    Dijaga agar symlink/berkas di luar folder output tidak ikut terhapus.
    """
    try:
        path.resolve().relative_to(induk.resolve())
        return True
    except ValueError:
        return False


def run_bersih(cfg: "Config", retensi: str = "") -> dict:
    """Hapus berkas lama di folder output; kembalikan ringkasannya."""
    try:
        batas_hari = int(retensi) if str(retensi).strip() else retensi_hari()
    except ValueError:
        batas_hari = retensi_hari()
    if batas_hari <= 0:
        batas_hari = retensi_hari()

    induk = cfg.output_dir
    if not induk.is_dir():
        return {
            "ok": True,
            "step": "done",
            "dihapus": 0,
            "retensi_hari": batas_hari,
            "log": [f"Folder output belum ada: {induk}"],
        }

    batas = time.time() - batas_hari * 86400
    dihapus: list[dict] = []
    gagal: list[str] = []

    for berkas in sorted(induk.rglob("*")):
        if not berkas.is_file() or not _di_dalam(berkas, induk):
            continue
        try:
            info = berkas.stat()
        except OSError:
            continue
        if info.st_mtime > batas:
            continue
        try:
            berkas.unlink()
            dihapus.append({
                "nama": str(berkas.relative_to(induk)),
                "kb": max(info.st_size // 1024, 1),
            })
        except OSError as e:  # noqa: PERF203
            gagal.append(f"{berkas.name}: {e}")

    total_kb = sum(x["kb"] for x in dihapus)
    log = [
        f"Retensi {batas_hari} hari — memindai {induk}",
        f"Berkas dihapus: {len(dihapus)} ({total_kb} KB)",
    ]
    if gagal:
        log.append("Gagal menghapus: " + " · ".join(gagal))

    return {
        "ok": True,
        "step": "done",
        "dihapus": len(dihapus),
        "kb": total_kb,
        "retensi_hari": batas_hari,
        "detail_dihapus": dihapus,
        "log": log,
    }
