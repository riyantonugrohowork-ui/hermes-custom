"""Parser halaman /duk SISDM → baris DUK terstruktur (murni, tanpa I/O).

Halaman /duk adalah dokumen cetak: satu tabel 10 kolom (NO, NAMA, NIP, GOL,
TMT, GOL CPNS, TMT CPNS, JABATAN / Eselon, MASA KERJA, PENDIDIKAN) plus kop
"NAMA OPD" / "PERIODE" di tabel terpisah di atasnya.

Hasil parser sengaja berbentuk sama dengan `DukImportService::normalisasiBaris`
di sisi kepegawaian — `jabatan` dan `eselon` sudah dipisah, masa kerja sudah
jadi integer — supaya dikirim apa adanya ke `POST /api/v1/impor-duk-json`.
Dengan begitu tidak ada lagi penebakan kolom dari PDF di sisi server.
"""
from __future__ import annotations

import re

from lxml import html as LH

# Kolom tabel DUK, berurutan seperti di halaman.
KOL_NO = 0
KOL_NAMA = 1
KOL_NIP = 2
KOL_GOL = 3
KOL_TMT = 4
KOL_GOL_CPNS = 5
KOL_TMT_CPNS = 6
KOL_JABATAN = 7
KOL_MASA_KERJA = 8
KOL_PENDIDIKAN = 9
JUMLAH_KOLOM = 10


def _bersih(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _teks(el) -> str:
    return _bersih(el.text_content() if el is not None else "")


def _sel(sel: list, i: int) -> str:
    return _teks(sel[i]) if i < len(sel) else ""


def _kosong_jadi_none(nilai: str | None) -> str | None:
    nilai = _bersih(nilai)
    return nilai or None


def _tanpa_spasi(nilai: str | None) -> str | None:
    return re.sub(r"\s+", "", nilai or "") or None


def _pecah_jabatan_eselon(nilai: str) -> tuple[str | None, str | None]:
    """Pisahkan "Jabatan / Eselon" pada garis miring TERAKHIR.

    Dipakai garis miring terakhir karena nama jabatan sendiri bisa memuat "/"
    (mis. "Pranata Komputer Ahli Muda/Muda / JF" → eselon "JF").
    """
    nilai = _bersih(nilai)
    if not nilai:
        return None, None
    if "/" not in nilai:
        return nilai, None
    pos = nilai.rfind("/")
    return _kosong_jadi_none(nilai[:pos]), _kosong_jadi_none(nilai[pos + 1:])


def _pecah_masa_kerja(nilai: str) -> tuple[int | None, int | None]:
    """"29 tahun 14 bulan" → (29, 14).

    Angka bulan > 12 dibiarkan apa adanya: DUK membawa perhitungan resmi
    kepegawaian, jadi menormalkannya di sini justru membuatnya beda dari
    dokumen sumber.
    """
    nilai = _bersih(nilai)
    tahun = re.search(r"(\d+)\s*tahun", nilai, re.I)
    bulan = re.search(r"(\d+)\s*bulan", nilai, re.I)
    return (
        int(tahun.group(1)) if tahun else None,
        int(bulan.group(1)) if bulan else None,
    )


def _tabel_data(doc):
    """Tabel yang barisnya punya sel lengkap (bukan tabel kop 1 kolom)."""
    for t in doc.xpath("//table[.//tbody]"):
        for tr in t.xpath(".//tbody/tr"):
            if len(tr.xpath("./td")) >= JUMLAH_KOLOM:
                return t
    return None


def _kop(doc) -> tuple[str | None, str | None]:
    """Ambil "NAMA OPD" dan "PERIODE" dari kop halaman."""
    opd = None
    periode = None
    for baris in doc.xpath("//tr"):
        teks = _teks(baris)
        if opd is None and re.search(r"NAMA\s+OPD", teks, re.I):
            opd = _kosong_jadi_none(re.sub(r"^.*?NAMA\s+OPD\s*:\s*", "", teks, flags=re.I))
        elif periode is None and re.search(r"PERIODE", teks, re.I):
            periode = _kosong_jadi_none(re.sub(r"^.*?PERIODE\s*:\s*", "", teks, flags=re.I))
    return opd, periode


def parse_duk(teks_html: str) -> dict:
    """Kembalikan {"opd", "periode", "jumlah", "rows"} siap kirim ke API.

    `rows` hanya berisi baris yang punya nama; baris tanpa nama tidak mungkin
    disimpan server (ditolak per baris), jadi dibuang di sini sekalian.
    """
    doc = LH.fromstring(teks_html or "<html></html>")
    tabel = _tabel_data(doc)
    if tabel is None:
        return {"opd": None, "periode": None, "jumlah": 0, "rows": []}

    opd, periode = _kop(doc)
    rows: list[dict] = []

    for tr in tabel.xpath(".//tbody/tr"):
        sel = tr.xpath("./td")
        if len(sel) < JUMLAH_KOLOM:
            continue

        nama = _kosong_jadi_none(_sel(sel, KOL_NAMA))
        if nama is None:
            continue

        no = _sel(sel, KOL_NO).rstrip(".")
        jabatan, eselon = _pecah_jabatan_eselon(_sel(sel, KOL_JABATAN))
        tahun, bulan = _pecah_masa_kerja(_sel(sel, KOL_MASA_KERJA))

        rows.append({
            "urutan_duk": int(no) if no.isdigit() else None,
            "nama": nama,
            "nip": re.sub(r"\D+", "", _sel(sel, KOL_NIP)) or None,
            "gol": _tanpa_spasi(_sel(sel, KOL_GOL)),
            "tmt": _tanpa_spasi(_sel(sel, KOL_TMT)),
            "gol_cpns": _tanpa_spasi(_sel(sel, KOL_GOL_CPNS)),
            "tmt_cpns": _tanpa_spasi(_sel(sel, KOL_TMT_CPNS)),
            "jabatan": jabatan,
            "eselon": eselon,
            "masa_kerja_tahun": tahun,
            "masa_kerja_bulan": bulan,
            "pendidikan": _kosong_jadi_none(_sel(sel, KOL_PENDIDIKAN)),
        })

    return {"opd": opd, "periode": periode, "jumlah": len(rows), "rows": rows}
