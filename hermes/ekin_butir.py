"""Butir kegiatan e-Kinerja yang biasa dipakai (data murni, tanpa I/O).

Dipisah dari resep dan dashboard supaya satu sumber: antrian rencana di web,
penomoran butir saat mengisi, dan tampilan semuanya membaca daftar ini.

`waktu` = menit per satuan, `satuan` = nama satuan di e-kin. Angka ini hanya
untuk perkiraan di layar; menit sebenarnya yang tercatat di e-kin dibaca dari
tabel aktivitas (e-kin kadang memotong, mis. 240×2 menjadi 390 menit).
"""
from __future__ import annotations

BUTIR: list[dict] = [
    {"nama": "Mengelola Dokumen Kepegawaian", "waktu": 120, "satuan": "Dokumen"},
    {"nama": "Memeriksa Kelengkapan Dokumen Kepegawaian Masing-Masing ASN", "waktu": 120, "satuan": "Dokumen"},
    {"nama": "Menyusun Administrasi Kepegawaian", "waktu": 90, "satuan": "Dokumen"},
    {"nama": "Menyusun dan mengarsip dokumen", "waktu": 60, "satuan": "Dokumen"},
    {"nama": "Mengikuti Seminar/pelatihan/diklat", "waktu": 180, "satuan": "Kegiatan"},
    {"nama": "Membuat usulan Kenaikan Gaji Berkala", "waktu": 120, "satuan": "Dokumen"},
    {"nama": "Membuat usulan Kenaikan Pangkat", "waktu": 90, "satuan": "Dokumen"},
    {"nama": "Membuat usulan Pensiun ASN", "waktu": 90, "satuan": "Dokumen"},
    {"nama": "Membuat pengajuan cuti bagi ASN", "waktu": 90, "satuan": "Dokumen"},
    {"nama": "Melaksanakan tugas kedinasan lain yang diberikan oleh pimpinan sesuai tugas dan fungsinya", "waktu": 60, "satuan": "Kegiatan"},
    {"nama": "Menyiapkan kegiatan pengelolaan Website dan Portal", "waktu": 45, "satuan": "Kegiatan"},
    {"nama": "Mengikuti Rapat Koordinasi internal", "waktu": 180, "satuan": "Kegiatan"},
    {"nama": "Mengikuti rapat eksternal", "waktu": 120, "satuan": "Kegiatan"},
    {"nama": "Membuat Program Aplikasi Sistem Informasi", "waktu": 240, "satuan": "Kegiatan"},
    {"nama": "Mengembangkan Program Aplikasi Sistem Informasi", "waktu": 240, "satuan": "Kegiatan"},
    {"nama": "Mengikuti apel pagi", "waktu": 30, "satuan": "Kegiatan"},
]

_BY_NAMA = {b["nama"]: b for b in BUTIR}


def cari(nama: str) -> dict | None:
    """Butir berdasarkan nama persis (case-insensitive)."""
    return _BY_NAMA.get(nama) or next(
        (b for b in BUTIR if b["nama"].lower() == (nama or "").strip().lower()), None
    )


def menit(nama: str, kuantitas: int = 1) -> int:
    """Perkiraan menit untuk satu butir × kuantitas (0 bila butir tak dikenal)."""
    b = cari(nama)
    return (b["waktu"] if b else 0) * max(1, int(kuantitas or 1))
