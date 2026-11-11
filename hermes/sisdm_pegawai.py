"""Parser halaman SISDM: profil pegawai + sub-halaman riwayat.

Fungsi di sini murni (teks HTML → dict/list) supaya bisa diuji dari berkas
contoh tanpa menyentuh jaringan. Pengambilan halaman ada di
`recipe_pegawai`.

Bentuk keluaran mengikuti JSON yang diterima `POST /api/v1/impor-pegawai`,
yang dibaca `PegawaiImportService` di sisi kepegawaian — nama kunci di sini
adalah kontrak, jangan diubah tanpa mengubah importer juga.
"""
from __future__ import annotations

import html as htmllib
import re

from lxml import html as LH


def _bersih(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _teks(el) -> str:
    return _bersih(el.text_content() if el is not None else "")


def _pecah_br(el) -> list[str]:
    """Teks sel yang memakai <br> sebagai pemisah baris.

    `text_content()` menempelkan antar-baris tanpa pemisah, jadi <br> diganti
    newline lebih dulu. Dipakai untuk sel "Nomor/Tanggal SK".
    """
    if el is None:
        return []
    mentah = LH.tostring(el, encoding="unicode")
    mentah = re.sub(r"<br\s*/?>", "\n", mentah, flags=re.I)
    mentah = htmllib.unescape(re.sub(r"<[^>]+>", "", mentah))
    return [_bersih(x) for x in mentah.split("\n") if _bersih(x)]


def _doc(teks_html: str):
    return LH.fromstring(teks_html or "<html></html>")


def _tabel(doc) -> tuple[list[str], list]:
    """Tabel data pertama yang punya baris tbody."""
    for t in doc.xpath("//table[.//tbody]"):
        baris = t.xpath(".//tbody/tr")
        if baris:
            heads = [_bersih(th.text_content()).lower() for th in t.xpath(".//thead//th")]
            return heads, baris
    return [], []


def _kol(heads: list[str], kata: str) -> int | None:
    """Indeks kolom pertama yang judulnya memuat `kata`."""
    for i, h in enumerate(heads):
        if kata in h:
            return i
    return None


def _sel(sel: list, i: int | None) -> str:
    if i is None or i >= len(sel):
        return ""
    return _teks(sel[i])


def _tautan_file(el) -> str | None:
    if el is None:
        return None
    href = el.xpath(".//a/@href")
    return href[0] if href else None


def _verifikasi(el) -> str:
    """Ikon centang = terverifikasi, tanda tanya = belum."""
    if el is None:
        return ""
    ikon = el.xpath(".//i")
    if not ikon:
        return _teks(el)
    cls = ikon[0].get("class") or ""
    if "fa-check" in cls:
        return "Terverifikasi"
    if "fa-question" in cls:
        return "Belum"
    return _teks(ikon[0]) or _teks(el)


# ---------------------------------------------------------------- profil


def parse_profil(teks_html: str) -> dict:
    """Profil utama: /pegawai/{id}.

    Dua sumber: kotak profil (jabatan/kelas, usia, KGB, BUP) dan tabel
    pasangan th/td (NIP, agama, golongan, dst).
    """
    doc = _doc(teks_html)
    data: dict = {}

    for p in doc.xpath(
        "//div[contains(@class,'box-profile')]//p"
        " | //div[contains(@class,'box-profile')]//h3"
    ):
        txt = _teks(p)
        if "Kelas Jabatan :" in txt:
            kiri, _, kanan = txt.partition("Kelas Jabatan :")
            data.setdefault("jabatan_kotak", kiri.strip())
            data["kelas_jabatan"] = kanan.strip()
        elif "Usia :" in txt:
            data["usia"] = txt.split("Usia :", 1)[1].strip()
        elif "KGB Selanjutnya :" in txt:
            data["kgb_selanjutnya"] = txt.split("KGB Selanjutnya :", 1)[1].strip()
        elif "BUP/BUT :" in txt:
            data["bup_but"] = txt.split("BUP/BUT :", 1)[1].strip()
        elif "TMT BUP/BUT:" in txt:
            nilai = txt.split("TMT BUP/BUT:", 1)[1].strip()
            data["tmt_bup_but"] = nilai.split("(")[0].strip()

    for h3 in doc.xpath("//h3[contains(.,'Capaian Bangkom')]"):
        txt = _teks(h3)
        if ":" in txt:
            data["capaian_bangkom"] = txt.split(":", 1)[1].strip()

    for tr in doc.xpath("//table//tr[th and td]"):
        label = _teks(tr.xpath("./th")[0])
        nilai = _teks(tr.xpath("./td")[0])

        if label == "NIP":
            data["nip"] = nilai
        elif label == "NIP Lama":
            data["nip_lama"] = "" if nilai == "-" else nilai
        elif label == "Nama Lengkap":
            data["nama"] = nilai
        elif label == "Agama":
            data["agama"] = nilai
        elif label == "Gender":
            data["gender"] = nilai
        elif label == "Status Perkawinan":
            data["status_perkawinan"] = nilai
        elif label == "Status Pegawai":
            data["status_pegawai"] = nilai
        elif label == "Pendidikan":
            data["pendidikan"] = nilai
        elif label == "Jabatan":
            data["jabatan"] = nilai
        elif label == "Tempat, Tanggal Lahir":
            tempat, _, tanggal = nilai.partition(",")
            data["tempat_lahir"] = tempat.strip()
            data["tanggal_lahir"] = tanggal.strip()
        elif label == "Golongan":
            m = re.match(r"^([^(]+)\(TMT\.?\s*([^)]+)\)", nilai)
            if m:
                data["golongan"] = m.group(1).strip()
                data["tmt_golongan"] = m.group(2).strip()
            else:
                data["golongan"] = nilai
                data["tmt_golongan"] = ""

    kotak = data.pop("jabatan_kotak", "")
    data.setdefault("jabatan", kotak)

    tautan = doc.xpath("//a[contains(@href,'pegawai_alamat')]/@href")
    data["address_edit_url"] = tautan[0] if tautan else None
    return data


def parse_alamat(teks_html: str) -> dict:
    """Form edit alamat (domisili + KTP).

    Nilai select diambil dari opsi yang terpilih, bukan atribut `value`, karena
    halaman ini menandai pilihan lewat `selected`.
    """
    doc = _doc(teks_html)

    def isian(id_el: str) -> str:
        el = doc.get_element_by_id(id_el, None)
        if el is None:
            return ""
        return _bersih(el.get("value") or "")

    def pilihan(id_el: str) -> str:
        el = doc.get_element_by_id(id_el, None)
        if el is None:
            return ""
        opsi = el.xpath(".//option[@selected]")
        if opsi:
            return _teks(opsi[0])
        return ""

    return {
        "jenis_domisili": pilihan("jns_rmh"),
        "alamat_domisili": isian("almt_pgw"),
        "rt_domisili": isian("rt_pgw"),
        "rw_domisili": isian("rw_pgw"),
        "kode_pos_domisili": isian("kode_pos"),
        "provinsi_domisili": pilihan("Kd_Prop"),
        "kota_domisili": pilihan("Kd_Kota"),
        "kecamatan_domisili": pilihan("Kd_Kec"),
        "kelurahan_domisili": pilihan("Kd_Kel"),
        "alamat_ktp": isian("almt_pgwktp"),
        "rt_ktp": isian("rt_pgwktp"),
        "rw_ktp": isian("rw_pgwktp"),
        "kode_pos_ktp": isian("kode_posktp"),
        "provinsi_ktp": pilihan("Kd_Propktp"),
        "kota_ktp": pilihan("Kd_Kotaktp"),
        "kecamatan_ktp": pilihan("Kd_Kecktp"),
        "kelurahan_ktp": pilihan("Kd_Kelktp"),
    }


# ------------------------------------------------------- riwayat berbasis tabel


def parse_anak(teks_html: str) -> list[dict]:
    doc = _doc(teks_html)
    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_nama = _kol(heads, "nama anak")
    i_ttl = _kol(heads, "tempat")
    i_usia = _kol(heads, "usia")
    i_pend = _kol(heads, "tingkat pendidikan")
    i_hub = _kol(heads, "hubungan")
    i_gender = _kol(heads, "gender")
    i_tunj = _kol(heads, "tunjangan")

    hasil = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 8:
            continue
        tempat, _, tanggal = _sel(sel, i_ttl).partition(",")
        hasil.append({
            "no": _sel(sel, i_no),
            "nama_anak": _sel(sel, i_nama),
            "tempat_lahir_anak": tempat.strip(),
            "tanggal_lahir_anak": tanggal.strip(),
            "usia_anak": _sel(sel, i_usia),
            "tingkat_pendidikan_anak": _sel(sel, i_pend),
            "hubungan_keluarga_anak": _sel(sel, i_hub),
            "gender_anak": _sel(sel, i_gender),
            "tunjangan_anak": _sel(sel, i_tunj),
        })
    return hasil


def parse_penghargaan(teks_html: str) -> list[dict]:
    doc = _doc(teks_html)
    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_sk = _kol(heads, "nomor")
    i_jenis = _kol(heads, "jenis")
    i_nama = _kol(heads, "nama penghargaan")
    i_peringkat = _kol(heads, "peringkat")
    i_asal = _kol(heads, "asal")
    i_file = _kol(heads, "file")

    hasil = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 7:
            continue
        sk = _pecah_br(sel[i_sk]) if i_sk is not None else []
        hasil.append({
            "no": _sel(sel, i_no),
            "nomor_sk_penghargaan": sk[0] if sk else "",
            "tanggal_sk_penghargaan": sk[1] if len(sk) > 1 else "",
            "jenis_penghargaan": _sel(sel, i_jenis),
            "nama_penghargaan": _sel(sel, i_nama),
            "peringkat_penghargaan": _sel(sel, i_peringkat),
            "asal_perolehan_penghargaan": _sel(sel, i_asal),
            "file_penghargaan_url": _tautan_file(sel[i_file]) if i_file is not None and i_file < len(sel) else None,
        })
    return hasil


def parse_kgb(teks_html: str) -> dict:
    """Ringkasan KGB (kotak peringatan) + daftar kenaikan.

    Ada dua kotak `alert-warning` dengan isi berbeda; keduanya dibaca, bukan
    hanya yang pertama.
    """
    doc = _doc(teks_html)
    data: dict = {}

    for h4 in doc.xpath("//div[contains(@class,'alert-warning')]//h4"):
        gabung = _bersih(" ".join(_pecah_br(h4)))
        if not gabung:
            continue

        if re.search(r"TMT Golongan Terakhir", gabung, re.I):
            m = re.search(r"TMT Golongan Terakhir\s*:?\s*(\d{2}-\d{2}-\d{4})", gabung, re.I)
            if m:
                data["tmt_golongan_terakhir"] = m.group(1)
            m = re.search(r"Masa Kerja\s*:?\s*(\d+\s*Tahun\s*\d+\s*Bulan)", gabung, re.I)
            if m:
                data["masa_kerja_terakhir"] = _bersih(m.group(1))

        if re.search(r"Kenaikan Gaji Berkala selanjutnya", gabung, re.I):
            m = re.search(r"(\d{2}-\d{2}-\d{4})", gabung)
            if m:
                data["kgb_selanjutnya"] = m.group(1)
            m = re.search(r"Masa Kerja\s*:?\s*(\d+\s*tahun\s*\d+\s*bulan)", gabung, re.I)
            if m:
                data["masa_kerja_saat_kgb"] = _bersih(m.group(1))
            m = re.search(r"Gaji Pokok sebesar\s*:?\s*(Rp\.?\s*[\d.,]+)", gabung, re.I)
            if m:
                data["gaji_pokok_saat_kgb"] = _bersih(m.group(1))

    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_sk = _kol(heads, "nomor")
    i_tmt = _kol(heads, "tmt")
    i_gol = _kol(heads, "golongan")
    i_unit = _kol(heads, "unit kerja")
    i_masa = _kol(heads, "masa kerja")
    i_lama = _kol(heads, "gaji lama")
    i_baru = _kol(heads, "gaji baru")
    i_file = _kol(heads, "file")
    i_verif = _kol(heads, "verifikasi")

    rincian = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 10:
            continue
        sk = _pecah_br(sel[i_sk]) if i_sk is not None else []
        rincian.append({
            "no": _sel(sel, i_no),
            "nomor_sk_kgb": sk[0] if sk else "",
            "tanggal_sk_kgb": sk[1] if len(sk) > 1 else "",
            "tmt_kgb": _sel(sel, i_tmt),
            "golongan_kgb": _sel(sel, i_gol),
            "unit_kerja_kgb": _sel(sel, i_unit),
            "masa_kerja_kgb": _sel(sel, i_masa),
            "gaji_lama": _sel(sel, i_lama),
            "gaji_baru": _sel(sel, i_baru),
            "file_kgb_url": _tautan_file(sel[i_file]) if i_file is not None and i_file < len(sel) else None,
            "verifikasi": _verifikasi(sel[i_verif]) if i_verif is not None and i_verif < len(sel) else "",
        })
    data["riwayat_kgb_detail"] = rincian
    return data


def parse_pangkat(teks_html: str) -> list[dict]:
    doc = _doc(teks_html)
    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_pangkat = _kol(heads, "pangkat")
    i_gol = _kol(heads, "golongan")
    i_sk = _kol(heads, "nomor")
    i_tmt = _kol(heads, "tmt")
    i_masa = _kol(heads, "masa kerja")
    i_jenis = _kol(heads, "jenis kenaikan")
    i_siasn = _kol(heads, "siasn")
    i_file = _kol(heads, "file")
    i_verif = _kol(heads, "verifikasi")

    hasil = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 11:
            continue
        sk = _pecah_br(sel[i_sk]) if i_sk is not None else []
        masa = _sel(sel, i_masa)
        m_tahun = re.search(r"(\d+)\s*tahun", masa, re.I)
        m_bulan = re.search(r"(\d+)\s*bulan", masa, re.I)
        hasil.append({
            "no": _sel(sel, i_no),
            "pangkat": _sel(sel, i_pangkat),
            "golongan": _sel(sel, i_gol),
            "nomor_sk_pangkat": sk[0] if sk else "",
            "tanggal_sk_pangkat": sk[1] if len(sk) > 1 else "",
            "tmt_sk_pangkat": _sel(sel, i_tmt),
            "masa_kerja_tahun": m_tahun.group(1) if m_tahun else "",
            "masa_kerja_bulan": m_bulan.group(1) if m_bulan else "",
            "jenis_kenaikan_pangkat": _sel(sel, i_jenis),
            "siasn": _sel(sel, i_siasn),
            "file_pangkat_url": _tautan_file(sel[i_file]) if i_file is not None and i_file < len(sel) else None,
            "verifikasi": _verifikasi(sel[i_verif]) if i_verif is not None and i_verif < len(sel) else "",
        })
    return hasil


def parse_jabatan(teks_html: str) -> list[dict]:
    doc = _doc(teks_html)
    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_jenis = _kol(heads, "jenis")
    i_baru = _kol(heads, "jabatan baru")
    i_unit = _kol(heads, "unit kerja")
    i_opd = _kol(heads, "opd")
    i_sk = _kol(heads, "nomor")
    i_tmt = _kol(heads, "tmt")
    i_file = _kol(heads, "file")
    i_verif = _kol(heads, "verifikasi")

    hasil = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 8:
            continue
        sk = _pecah_br(sel[i_sk]) if i_sk is not None else []
        hasil.append({
            "no": _sel(sel, i_no),
            "jenis_jabatan": _sel(sel, i_jenis),
            "jabatan_baru": _sel(sel, i_baru),
            "unit_kerja": _sel(sel, i_unit),
            "opd": _sel(sel, i_opd),
            "nomor_sk_jabatan": sk[0] if sk else "",
            "tanggal_sk_jabatan": sk[1] if len(sk) > 1 else "",
            "tmt_sk_jabatan": _sel(sel, i_tmt),
            "file_jabatan_url": _tautan_file(sel[i_file]) if i_file is not None and i_file < len(sel) else None,
            "verifikasi": _verifikasi(sel[i_verif]) if i_verif is not None and i_verif < len(sel) else "",
        })
    return hasil


def parse_pppk(teks_html: str) -> list[dict]:
    doc = _doc(teks_html)
    heads, baris = _tabel(doc)
    i_no = _kol(heads, "#")
    i_sk = _kol(heads, "nomor")
    i_tmt = _kol(heads, "tmt")
    i_gol = _kol(heads, "golongan")
    i_jab = _kol(heads, "jabatan")
    i_unit = _kol(heads, "unit kerja")
    i_file = _kol(heads, "file")
    i_verif = _kol(heads, "verifikasi")

    hasil = []
    for tr in baris:
        sel = tr.xpath("./td")
        if len(sel) < 7:
            continue
        sk = _pecah_br(sel[i_sk]) if i_sk is not None else []
        hasil.append({
            "no": _sel(sel, i_no),
            "nomor_sk_pppk": sk[0] if sk else "",
            "tanggal_sk_pppk": sk[1] if len(sk) > 1 else "",
            "tmt_sk_pppk": _sel(sel, i_tmt),
            "golongan_pppk": _sel(sel, i_gol),
            "jabatan_pppk": _sel(sel, i_jab),
            "unit_kerja_pppk": _sel(sel, i_unit),
            "file_pppk_url": _tautan_file(sel[i_file]) if i_file is not None and i_file < len(sel) else None,
            "verifikasi": _verifikasi(sel[i_verif]) if i_verif is not None and i_verif < len(sel) else "",
        })
    return hasil


# ------------------------------------------------------------ form edit


def _form_edit(doc) -> list | None:
    """Form edit pertama (bukan form logout / catatan verifikasi)."""
    for f in doc.xpath("//form"):
        aksi = (f.get("action") or "").lower()
        if "logout" in aksi or "verifikasi" in aksi:
            continue
        return f
    return None


def parse_kompetensi_form(teks_html: str) -> dict:
    """Detail kompetensi hanya lengkap di form edit (tabel daftar tidak
    memuat tanggal pelaksanaan & jumlah jam)."""
    doc = _doc(teks_html)
    form = _form_edit(doc)
    if form is None:
        return {}

    def isian(nama: str) -> str:
        el = form.xpath(f".//input[@name='{nama}']")
        if el:
            return _bersih(el[0].get("value") or "")
        el = form.xpath(f".//textarea[@name='{nama}']")
        return _bersih(el[0].text_content() or "") if el else ""

    def pilihan(nama: str) -> str:
        el = form.xpath(f".//select[@name='{nama}']")
        if not el:
            return ""
        opsi = el[0].xpath(".//option[@selected]")
        return _teks(opsi[0]) if opsi else ""

    return {
        "jenis": pilihan("jenis"),
        "nomor_sertifikat": isian("nomor_sertifikat"),
        "tanggal_sertifikat": isian("tanggal_sertifikat"),
        "nama_kompetensi": isian("nama"),
        "penyelenggara": isian("penyelenggara"),
        "tanggal_mulai": isian("tanggal_mulai"),
        "tanggal_selesai": isian("tanggal_selesai"),
        "jumlah_jam": isian("jumlah_jam"),
    }


def parse_cpns_form(teks_html: str) -> dict:
    doc = _doc(teks_html)
    form = _form_edit(doc)
    if form is None:
        return {}

    def isian(nama: str) -> str:
        el = form.xpath(f".//input[@name='{nama}'] | .//textarea[@name='{nama}']")
        if el:
            return _bersih(el[0].get("value") or el[0].text_content() or "")
        return ""

    def pilihan(nama: str) -> str:
        el = form.xpath(f".//select[@name='{nama}']")
        if not el:
            return ""
        opsi = el[0].xpath(".//option[@selected]")
        return _teks(opsi[0]) if opsi else ""

    berkas = form.xpath(".//input[@type='file'][starts-with(@name,'files[')]")
    tautan = form.xpath(".//a[contains(@href,'/preview?filename=')]/@href")

    return {
        "nomor_sk": isian("nomor_sk"),
        "tanggal_sk": isian("tanggal_sk"),
        "tmt_sk": isian("tmt_sk"),
        "kd_golongan": pilihan("kd_golongan"),
        "golongan": pilihan("kd_golongan"),
        "jabatan": isian("jabatan"),
        "unit_kerja": isian("unit_kerja"),
        "masa_kerja_tahun": isian("masa_kerja_tahun"),
        "masa_kerja_bulan": isian("masa_kerja_bulan"),
        "gaji": isian("gaji"),
        "file_field_name": berkas[0].get("name") if berkas else "",
        "file_url": tautan[0] if tautan else None,
    }


def parse_pppk_form(teks_html: str) -> dict:
    """Detail PPPK dari form edit (tabel daftar tidak memuat gaji)."""
    doc = _doc(teks_html)
    form = _form_edit(doc)
    if form is None:
        return {}

    def isian(nama: str) -> str:
        el = form.xpath(f".//input[@name='{nama}'] | .//textarea[@name='{nama}']")
        if el:
            return _bersih(el[0].get("value") or el[0].text_content() or "")
        return ""

    def pilihan(nama: str) -> str:
        el = form.xpath(f".//select[@name='{nama}']")
        if not el:
            return ""
        opsi = el[0].xpath(".//option[@selected]")
        return _teks(opsi[0]) if opsi else ""

    return {
        "nomor_sk": isian("nomor_sk"),
        "tanggal_sk": isian("tanggal_sk"),
        "tmt_sk_awal": isian("tmt_sk_awal"),
        "tmt_sk_akhir": isian("tmt_sk_akhir"),
        "kd_golongan": pilihan("kd_golongan"),
        "golongan": pilihan("kd_golongan"),
        "jabatan": isian("jabatan"),
        "unit_kerja": isian("unit_kerja"),
        "nomor_perjanjian_kerja": isian("nomor_perjanjian_kerja"),
        "tanggal_perjanjian_kerja": isian("tanggal_perjanjian_kerja"),
        "gaji": isian("gaji"),
    }


def tautan_edit(teks_html: str, bagian: str) -> list[str]:
    """URL form edit riwayat `bagian` (kompetensi/cpns/...), unik & berurutan."""
    doc = _doc(teks_html)
    pola = f"/{bagian}/"
    hasil = [
        href for href in doc.xpath("//a[contains(@href,'/edit')]/@href")
        if pola in href
    ]
    if not hasil:
        hasil = [
            href for href in doc.xpath("//a/@href")
            if pola in href and "/edit" in href
        ]
    unik: list[str] = []
    for href in hasil:
        if href not in unik:
            unik.append(href)
    return unik
