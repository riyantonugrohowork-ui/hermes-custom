"""Klien API Generator Surat Cuti (kepegawaian).

Dipakai untuk menghitung nomor surat + sisa cuti (N, N-1, N-2) yang
diisikan ke form SIMPATIK. Penting: POST juga menulis ke arsip
riwayat_generator_cuti, jadi setiap batch yang tidak terpakai dihapus
lagi lewat hapus_batch() supaya penomoran tidak melompat.
"""
from __future__ import annotations

import os
import re
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


def profil(cfg: "Config", nip: str) -> dict:
    """Profil satu pegawai via `GET /api/v1/pegawai/{nip}`.

    Dipakai untuk memastikan status ASN (PNS/PPPK) sebelum menyimpulkan aturan
    cuti. `nip` dinormalkan ke digit murni oleh server; NIP tak dikenal → 404
    yang dilempar sebagai RuntimeError oleh `_get`.
    """
    return (_get(cfg, f"pegawai/{re.sub(r'\\D+', '', str(nip or ''))}") or {}).get("data") or {}


def jenis_asn(cfg: "Config", nip: str) -> str:
    """`jenis` ringkas dari profil: PNS / PPPK / PPPK Paruh Waktu / dst.

    Kosong bila NIP tidak ada di data pegawai — pemanggil memperlakukan itu
    sebagai "tidak diketahui", bukan sebagai PNS.
    """
    return str(profil(cfg, nip).get("jenis") or "").strip()


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


def impor_cuti(cfg: "Config", path: str) -> dict:
    """Unggah rekap Excel ke tabel cuti (upsert per no_surat).

    Memakai service impor yang sama dengan halaman panel, jadi aturan
    upsert/pelewatan barisnya identik. Impor sukses juga menandai
    "impor cuti hari ini" sehingga gerbang Generator Surat Cuti terbuka.
    """
    import requests

    with open(path, "rb") as fh:
        r = requests.post(
            f"{_base(cfg)}/impor-cuti",
            headers={"X-API-Key": _key(cfg)},
            files={"file": (os.path.basename(path), fh, "application/vnd.ms-excel")},
            timeout=180,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"API impor {r.status_code}: {_pesan(r)}")
    return r.json()


def impor_duk_json(
    cfg: "Config",
    rows: list[dict],
    opd: str | None = None,
    periode: str | None = None,
    sumber_label: str = "hermes-duk-json-v1",
    original_filename: str = "hermes-duk-json",
) -> dict:
    """Kirim baris DUK terstruktur ke `POST /api/v1/impor-duk-json`.

    Berbeda dari `impor_duk`, tidak ada PDF dan tidak ada penebakan kolom:
    nilai dikirim apa adanya. Server tetap membuat batch baru dan tidak
    menghapus batch lama. Baris tanpa `nama` dilewati server dan dilaporkan
    di `errors`; bila semua baris gagal, server menjawab 422 tanpa menyimpan
    batch.
    """
    import requests

    r = requests.post(
        f"{_base(cfg)}/impor-duk-json",
        headers={"X-API-Key": _key(cfg), "Content-Type": "application/json"},
        json={
            "opd": opd,
            "periode": periode,
            "sumber_label": sumber_label,
            "original_filename": original_filename,
            "rows": rows,
        },
        timeout=300,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API impor-duk-json {r.status_code}: {_pesan(r)}")
    return r.json()


def impor_pegawai(cfg: "Config", records: list[dict]) -> dict:
    """Kirim data pegawai (hasil scrape SISDM) ke `POST /api/v1/impor-pegawai`.

    Tiap record adalah snapshot penuh: riwayat anak, kompetensi, jabatan,
    pangkat, dan KGB untuk NIP itu diganti seluruhnya di sisi kepegawaian.
    """
    import requests

    r = requests.post(
        f"{_base(cfg)}/impor-pegawai",
        headers={"X-API-Key": _key(cfg), "Content-Type": "application/json"},
        json={"records": records},
        timeout=300,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API impor-pegawai {r.status_code}: {_pesan(r)}")
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
