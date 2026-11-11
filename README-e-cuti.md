# Panduan Alur E-Cuti

Panduan menjalankan **template alur e-cuti** yang sudah terpasang di Hermes.

---

## Apa yang dilakukan alur ini

Satu perintah → empat tahap otomatis:

| # | Tahap | Detail |
|---|---|---|
| 1 | Login SIMPATIK | `simpatik.semarangkota.go.id/login` — kredensial dari Bitwarden item `2017skpd019` |
| 2 | Ambil token | `/token?app=ecuti` → redirect ke portal e-cuti |
| 3 | Export rekap | `dashboard/export_range` — isi tanggal (hari ini −1 bulan s/d hari ini) → Save → Excel terunduh |
| 4 | Impor cuti | `POST /api/v1/impor-cuti` → unggah Excel (multipart, `X-API-Key`) |

Hasil Excel: `output/ecuti/<nama-file>.xls`

> Tahap impor memakai **API**, bukan browser. Tidak ada lagi sesi Google,
> FilePond, atau screenshot hasil impor yang perlu dirawat.

---

## Prasyarat (sekali saja)

### 1. Vault Bitwarden terbuka
Item `2017skpd019` harus ada di vault dan berisi **username + password** SIMPATIK.

```bash
cd /home/dpmptsp/docker/hermes-custom
echo -n "MASTER-PASSWORD-VAULT-ANDA" > secrets/bw_masterpass
chmod 600 secrets/bw_masterpass
docker compose up -d --no-build --force-recreate
```

Login Bitwarden sekali bila container belum pernah login (config tersimpan di
`data/bw-config/`, jadi bertahan antar-restart):

```bash
docker exec -it hermes-agent bw login    # email → password → OTP
```

Cek berhasil:
```bash
docker compose logs --tail=5
# harus muncul: ✅ Vault terbuka (session baru dihasilkan).
```

### 2. Kunci API kepegawaian
Impor butuh kunci dengan scope **`impor`** (buat di panel Kepegawaian →
Master → API Key):

```
KEPEG_API_KEY=dpmk_...
```

Taruh di `.env`, lalu `docker compose up -d --force-recreate`.

Cek kunci:
```bash
curl -s -X POST https://kepegawaian.it-dpmptsp.my.id/api/v1/impor-cuti \
  -H "X-API-Key: $KEPEG_API_KEY"
# {"ok":false,"error":"Berkas Excel cuti ... wajib dikirim ... field \"file\"."}
# → kunci valid & scope benar (galatnya soal berkas, bukan soal akses)
```

---

## Menjalankan

### Lewat dashboard
Buka `https://hermes.it-dpmptsp.my.id`, tab **Alur**, kartu **Rekap E-Cuti**,
lalu klik **▶ Jalankan**. Isi tanggal bila perlu — kosong berarti sebulan terakhir.

### Lewat CLI
```bash
docker exec hermes-agent python -m hermes recipe run ecuti_rekap
docker exec hermes-agent python -m hermes recipe run ecuti_rekap tanggal_mulai=2026-11-01
```

---

## Rentang tanggal

Otomatis: **hari ini − 1 bulan** s/d **hari ini**, format `yyyy-mm-dd`.
Contoh: hari ini 11 Nov 2026 → `2026-10-11` s/d `2026-11-11`.

---

## Hasil impor

Respons API dipetakan ke ringkasan di dashboard:

| Kunci API | Arti |
|---|---|
| `inserted` | baris baru (`No Surat` belum ada) |
| `updated` | baris diperbarui (upsert per `No Surat`) |
| `skipped` | baris dilewati karena pernah diedit manual di panel |
| `tanpa_surat` | baris dilewati karena `No Surat` kosong |
| `status_lain` | baris dilewati karena status bukan **TTE Kepala Dinas** |

`jumlah_diproses` **tidak** mencakup `tanpa_surat` dan `status_lain`; total
baris di berkas dihitung `inserted + updated + jumlah_dilewati`.

Impor sukses membuka gerbang "impor cuti hari ini" untuk Generator Surat Cuti
(`import_hari_ini: true`).

---

## Kalau gagal — cek per tahap

Alur berhenti dengan pesan `step` yang jelas:

| Pesan | Arti | Solusi |
|---|---|---|
| `step: login` + "Kredensial SIMPATIK tidak tersedia" | Bitwarden terkunci / item tak ada | Buka vault (Prasyarat 1) |
| `step: login` + "Field login tidak ditemukan" | Selector halaman login berubah | Kirim screenshot halaman login |
| `step: jaringan` + "IP bukan dari Indonesia" | SIMPATIK melakukan geoblock | Jalankan dari jaringan Indonesia |
| `step: import` + `API impor 401/403` | Kunci salah / scope bukan `impor` | Periksa `KEPEG_API_KEY` |
| `step: import` + `API impor 422` | Header kolom Excel tidak cocok | Pastikan berkas dari e-cuti, bukan hasil edit manual |

---

## Catatan teknis

- **CSRF token** diambil otomatis oleh browser saat halaman dimuat (tidak perlu diisi).
- **Aturan impor**: hanya baris ber-status **TTE Kepala Dinas** yang diimpor.
  `No Surat` yang sudah ada → diperbarui; belum ada → ditambahkan; data lama yang
  tidak ada di berkas tetap tersimpan.
- **Kolom Excel** (urutan wajib, dari sistem): `No, No Surat, Nip, Nama,
  Tanggal Mulai Diajukan, Tanggal Selesai Diajukan, Opd, Unit Kerja,
  Lokasi Kerja, Status, Keperluan, Jenis`.
- Alur berjalan **headless** (tanpa ganggu layar).
