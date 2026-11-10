"""Klien API Generator Surat Cuti (kepegawaian).

Dipakai untuk menghitung nomor surat + sisa cuti (N, N-1, N-2) yang
diisikan ke form SIMPATIK. Penting: POST juga menulis ke arsip
riwayat_generator_cuti, jadi setiap batch yang tidak terpakai dihapus
lagi lewat hapus_batch() supaya penomoran tidak melompat.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

BASE_DEFAULT = "https://kepegawaian.it-dpmptsp.my.id/api/v1"
SUMBER = "hermes-penomoran-v1"


def _base(cfg: "Config") -> str:
    return (getattr(cfg, "kepeg_base_url", "") or BASE_DEFAULT).rstrip("/")


def _key(cfg: "Config") -> str:
    key = getattr(cfg, "kepeg_api_key", "") or os.getenv("KEPEG_API_KEY", "")
    if not key:
        raise RuntimeError("KEPEG_API_KEY belum diisi.")
    return key


def _get(cfg: "Config", path: str, params: dict | None = None) -> dict:
    import requests

    r = requests.get(
        f"{_base(cfg)}/{path}",
        params=params or {},
        headers={"X-API-Key": _key(cfg)},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API {r.status_code}: {_pesan(r)}")
    return r.json()


def _pesan(r) -> str:
    try:
        d = r.json()
        return d.get("error") or d.get("message") or r.text[:200]
    except ValueError:
        return r.text[:200]


def berikutnya(cfg: "Config", kode: str = "800") -> dict:
    return _get(cfg, "generator-surat-cuti/berikutnya", {"kode": kode})


def cari_pegawai(cfg: "Config", q: str) -> list[dict]:
    return (_get(cfg, "generator-surat-cuti/pegawai", {"q": q}) or {}).get("data", [])


def riwayat(cfg: "Config", limit: int = 20, sumber: str = "") -> list[dict]:
    params: dict = {"limit": limit}
    if sumber:
        params["sumber"] = sumber
    return (_get(cfg, "generator-surat-cuti/riwayat", params) or {}).get("data", [])


def rincian(cfg: "Config", batch: str) -> dict:
    return _get(cfg, f"generator-surat-cuti/riwayat/{batch}")


def hapus_batch(cfg: "Config", batch: str | list[str]) -> dict:
    """Hapus arsip per batch (dipakai untuk membatalkan POST yang tak terpakai)."""
    import requests

    nilai = [batch] if isinstance(batch, str) else list(batch)
    r = requests.delete(
        f"{_base(cfg)}/generator-surat-cuti/hapus",
        headers={"Content-Type": "application/json", "X-API-Key": _key(cfg)},
        json={"batch": nilai},
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API hapus {r.status_code}: {_pesan(r)}")
    return r.json()


def hapus_id(cfg: "Config", ids: int | list[int]) -> dict:
    """Hapus arsip per baris (rincian yang nomornya tidak jadi dipakai)."""
    import requests

    nilai = [ids] if isinstance(ids, (int, str)) else list(ids)
    r = requests.delete(
        f"{_base(cfg)}/generator-surat-cuti/hapus",
        headers={"Content-Type": "application/json", "X-API-Key": _key(cfg)},
        json={"id": nilai},
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API hapus {r.status_code}: {_pesan(r)}")
    return r.json()


def generate(
    cfg: "Config",
    rows: list[dict],
    kode: str = "800",
    nomor_awal: int | None = None,
    cek_riwayat: bool = True,
    abaikan_gate: bool = False,
) -> dict:
    """Olah + simpan rincian cuti (maks 200 baris per panggilan).

    Catatan docs: `hari` diabaikan — durasi dihitung ulang sebagai hari
    kerja dari rentang tanggal. Duplikat tidak diberi nomor dan tidak
    memotong sisa cuti.
    """
    import requests

    if len(rows) > 200:
        raise RuntimeError("Maksimal 200 baris per POST (docs API).")

    body: dict = {
        "rows": rows,
        "kode": kode,
        "sumber_label": SUMBER,
        "cek_riwayat": cek_riwayat,
    }
    if nomor_awal:
        body["nomor_awal"] = nomor_awal
    if abaikan_gate:
        body["abaikan_gate"] = True

    r = requests.post(
        f"{_base(cfg)}/generator-surat-cuti",
        headers={"Content-Type": "application/json", "X-API-Key": _key(cfg)},
        json=body,
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API {r.status_code}: {_pesan(r)}")
    return r.json()


def sisa_urut(sisa: dict | None, tahun: int | None = None) -> tuple[int, int, int]:
    """(N, N-1, N-2) dari dict sisa cuti per tahun."""
    sisa = sisa or {}
    if tahun is None:
        angka = [int(k) for k in sisa if str(k).isdigit()]
        tahun = max(angka) if angka else None
    if tahun is None:
        return 0, 0, 0

    def ambil(t):
        v = sisa.get(str(t), sisa.get(t, 0))
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return ambil(tahun), ambil(tahun - 1), ambil(tahun - 2)
