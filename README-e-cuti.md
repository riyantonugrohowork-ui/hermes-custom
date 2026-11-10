# Panduan Alur E-Cuti

Panduan menjalankan **template alur e-cuti** yang sudah terpasang di Hermes.

---

## Apa yang dilakukan alur ini

Satu perintah → lima tahap otomatis:

| # | Tahap | Detail |
|---|---|---|
| 1 | Login SIMPATIK | `simpatik.semarangkota.go.id/login` — kredensial dari Bitwarden item `2017skpd019` |
| 2 | Ambil token | `/token?app=ecuti` → redirect ke portal e-cuti |
| 3 | Export rekap | `dashboard/export_range` — isi tanggal (hari ini −1 bulan s/d hari ini) → Save → Excel terunduh |
| 4 | Buka kepegawaian | Sesi Google tersimpan (tanpa login ulang) |
| 5 | Impor cuti | `kepegawaian/impor-cuti` → upload Excel → Mulai Impor |

Hasil Excel: `output/ecuti/<nama-file>.xlsx`

---

## Prasyarat (sekali saja)

### 1. Vault Bitwarden terbuka
Item `2017skpd019` harus ada di vault dan berisi **username + password** SIMPATIK.

```bash
cd /root/docker/hermes
echo -n "MASTER-PASSWORD-VAULT-ANDA" > secrets/bw_masterpass
chmod 600 secrets/bw_masterpass
docker compose up -d --no-build --force-recreate
```

Cek berhasil:
```bash
docker compose logs --tail=5
# harus muncul: ✅ Vault terbuka (session baru dihasilkan).
```

### 2. Sesi Google kepegawaian
Google **memblokir** Chromium otomatis ("browser may not be secure").
Solusinya: login sekali lewat **Edge asli Windows** via CDP.

**a. Luncurkan Edge dengan debug port** (dari WSL, profil terpisah agar tak ganggu Edge biasa):
```bash
"/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
  --remote-debugging-port=9222 --user-data-dir="C:\\edge-debug" --no-first-run &
```

**b. Jalankan login via CDP:**
```bash
cd /root/docker/hermes
source .venv/bin/activate
python -m hermes login-google --cdp http://127.0.0.1:9222
```

Jendela Edge terbuka → login `riyantonugroho.work@gmail.com` → setelah masuk aplikasi,
sesi **tersimpan otomatis** ke `data/google_session.json`.

> Setelah sesi tersimpan, login berikutnya tidak perlu lagi — Google tidak dipanggil ulang;
> Hermes hanya memakai cookie sesi aplikasi.

---

## Menjalankan

### Lewat chat web
Buka `http://localhost:10000`, login, lalu ketik:

```
ambil rekap e-cuti
```

atau variasi yang dikenali: *"export rekap cuti"*, *"impor cuti"*, *"rekap e-cuti bulan ini"*.

### Cek prasyarat sebelum jalan
```bash
docker exec hermes-agent python -c "
from hermes import config, recipe_ecuti
c = config.get_config()
login = recipe_ecuti.bitwarden_login(c, '2017skpd019')
print('Bitwarden:', 'OK ' + login[0] if login else 'BELUM')
print('Sesi Google:', 'OK' if recipe_ecuti.google_state_path(c).is_file() else 'BELUM')
"
```

---

## Rentang tanggal

Otomatis: **hari ini − 1 bulan** s/d **hari ini**, format `DD-MM-YYYY`.
Contoh: hari ini 9 Nov 2026 → `09-10-2026` s/d `09-11-2026`.

---

## Kalau gagal — cek per tahap

Alur berhenti dengan pesan `step` yang jelas:

| Pesan | Arti | Solusi |
|---|---|---|
| `step: login` + "Kredensial SIMPATIK tidak tersedia" | Bitwarden terkunci / item tak ada | Buka vault (Langkah 1) |
| `step: login` + "Field login tidak ditemukan" | Selector halaman login berubah | Kirim screenshot halaman login |
| `step: google` | Sesi Google belum ada | Jalankan `/login-google` |
| `step: import` + "FilePond input tidak ditemukan" | Halaman impor berubah | Kirim screenshot halaman impor |

---

## Catatan teknis

- **CSRF token** diambil otomatis oleh browser saat halaman dimuat (tidak perlu diisi).
- **Aturan impor**: hanya baris ber-status **TTE Kepala Dinas** yang diimpor.
  `No Surat` yang sudah ada → diperbarui; belum ada → ditambahkan.
- **Kolom Excel** (urutan wajib, dari sistem): `No, No Surat, Nip, Nama,
  Tanggal Mulai Diajukan, Tanggal Selesai Diajukan, Opd, Unit Kerja,
  Lokasi Kerja, Status, Keperluan, Jenis`.
- Alur berjalan **headless** (tanpa ganggu layar), kecuali `/login-google`.
