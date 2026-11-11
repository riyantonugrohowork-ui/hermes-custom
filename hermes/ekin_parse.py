"""Parser tabel Aktivitas Harian e-Kinerja → baris terstruktur (murni, tanpa I/O).

Tabel `/administrator/aktivitas_harian` punya 6 kolom:

  [0] checkbox `input[name='id[]']`  → id aktivitas (kunci baris)
  [1] Tanggal                        → `2026-10-28`
  [2] Uraian / Rencana Kinerja       → nama butir kegiatan
  [3] Satuan Waktu | Kuantitas | Waktu dihitung
  [4] Status                         → `verifikasi` / `Belum Diverifikasi`
  [5] Action                         → tautan edit/hapus

Kolom [3] berisi tiga `span.label` berurutan (satuan, kuantitas, waktu) dan
**waktu tidak selalu sama dengan satuan × kuantitas** — mis. 240×2 tercatat
390 menit karena e-kin memotongnya. Karena itu angka `waktu` dibaca apa
adanya, bukan dihitung ulang.

Halaman ini tidak punya total per hari; angkanya dijumlahkan dari baris.
"""
from __future__ import annotations

import re

from lxml import html as LH

KOL_ID = 0
KOL_TANGGAL = 1
KOL_URAIAN = 2
KOL_WAKTU = 3
KOL_STATUS = 4
JUMLAH_KOLOM = 6

_RE_TANGGAL = re.compile(r"\d{4}-\d{2}-\d{2}")
_RE_ID_LINK = re.compile(r"/(?:edit|delete)/(\d+)")


def _bersih(s: str | None) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _angka(teks: str) -> int | None:
    m = re.search(r"\d+", teks or "")
    return int(m.group()) if m else None


def _id_baris(tr) -> int | None:
    """Id aktivitas: checkbox `id[]` dulu, lalu tautan edit/hapus."""
    cb = tr.xpath("./td[1]//input[@name='id[]']/@value")
    if cb and cb[0].strip().isdigit():
        return int(cb[0].strip())
    for href in tr.xpath("./td[last()]//@href"):
        m = _RE_ID_LINK.search(href or "")
        if m:
            return int(m.group(1))
    return None


def _tanggal(tr) -> str | None:
    teks = _bersih(tr.xpath("./td[2]")[0].text_content() if tr.xpath("./td[2]") else "")
    m = _RE_TANGGAL.search(teks)
    return m.group() if m else None


def _uraian(tr) -> str:
    sel = tr.xpath("./td[3]")
    return _bersih(sel[0].text_content()) if sel else ""


def _waktu(tr) -> tuple[int | None, int | None, int | None]:
    """(satuan, kuantitas, waktu) dari kolom "Satuan Waktu | Kuantitas | Waktu"."""
    sel = tr.xpath("./td[4]")
    if not sel:
        return None, None, None
    label = [_bersih(x) for x in sel[0].xpath(".//span") if _bersih(x)]
    angka = [_angka(x) for x in label]
    if len(angka) >= 3:
        return angka[0], angka[1], angka[2]
    # Cadangan: teks sel apa adanya (label hilang / markup berubah).
    angka = [_angka(x) for x in _bersih(sel[0].text_content()).split()]
    angka = [a for a in angka if a is not None]
    if len(angka) >= 3:
        return angka[0], angka[1], angka[2]
    return None, None, None


def _status(tr) -> str:
    sel = tr.xpath("./td[5]")
    return _bersih(sel[0].text_content()) if sel else ""


def parse_baris(teks_html: str) -> list[dict]:
    """Kembalikan baris aktivitas dari HTML satu halaman.

    Baris tanpa id atau tanpa tanggal dilewati: keduanya kunci yang dipakai
    untuk menyimpan (id unik) dan mengelompokkan per hari.
    """
    doc = LH.fromstring(teks_html or "<html></html>")
    hasil: list[dict] = []

    for tr in doc.xpath("//tr[td]"):
        sel = tr.xpath("./td")
        if len(sel) < JUMLAH_KOLOM:
            continue
        tanggal = _tanggal(tr)
        ident = _id_baris(tr)
        if not tanggal or ident is None:
            continue
        satuan, kuantitas, waktu = _waktu(tr)
        hasil.append({
            "id_ekin": ident,
            "tanggal": tanggal,
            "uraian": _uraian(tr),
            "satuan": satuan,
            "kuantitas": kuantitas,
            "waktu": waktu or 0,
            "status": _status(tr),
        })

    return hasil
