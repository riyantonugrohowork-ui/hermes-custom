# Memory

## Project Overview

Hermes Agent — bot otomasi personal berbasis Python. Wujud utamanya adalah
**recipe (alur)**: tiap alur punya jadwal cron sendiri, berjalan otonom tanpa
diminta, dan hanya menampilkan input kalau alurnya memang mendeklarasikannya.

Prinsip desain yang dipegang:

- **Alur adalah wujud utama.** Dashboard merender `recipes.RECIPES`; tidak ada
  bagian lain yang perlu tahu isi alur.
- **Tanpa input = tanpa tanya.** Alur dengan `inputs: []` cukup satu tombol
  "Jalankan", tanpa dialog apa pun.
- **Input muncul di kartu alur**, bukan di halaman chat terpisah.
- **Jadwal menempel pada alur** (cron), bukan pada task manual.

Dokumentasi alur E-Cuti yang lebih rinci ada di `README-e-cuti.md`.

---

## Struktur Modul

```
hermes/
  __main__.py         CLI (dispatch manual per subcommand, tanpa argparse)
  config.py           muat .env + Docker secrets → objek Config
  cron.py             parser cron 5 field (tanpa dependency)
  recipes.py          registry RECIPES + execute() + summarize() + notify
  scheduler.py        pemeriksa jadwal (recipe + task lama)
  autopilot.py        daemon otonom (startup + tick tiap menit)
  recipe_ecuti.py     alur rekap E-Cuti (export → impor kepegawaian)
  recipe_penomoran.py alur penomoran surat cuti
  recipe_magang.py    pemantau pengajuan magang (portal izin)
  recipe_duk.py       alur DUK (SISDM → JSON → impor)
  recipe_pegawai.py   alur data pegawai (SISDM → JSON → impor)
  duk_parse.py        parser HTML DUK (tabel → baris, murni)
  sisdm_pegawai.py    parser HTML SISDM (profil + riwayat, murni)
  recipe_bersih.py    pembersih berkas lama di output/
  recipe_ekin.py      alur aktivitas harian e-Kinerja (sapu → simpan → lapor)
  ekin_parse.py       parser HTML Aktivitas Harian e-kin (tabel → baris, murni)
  ekin_butir.py       daftar butir kegiatan e-kin + menitnya (data murni)
  webui.py            dashboard (http.server bawaan, tanpa dependency)
  db.py               skema SQLite (notes, records, logs, recipe_state, ekin_*)
  tasks.py            model Task lama (instruksi + jadwal + Plan)
  telegram_client.py  kirim pesan Telegram
  telegram_bot.py     bot interaktif
  kepeg.py            klien API Generator Surat Cuti (nomor + sisa cuti)
  sheets.py           akses Google Sheets
  brain.py            instruksi → Plan (rule/LLM) untuk alur bebas
  runner.py           eksekusi Plan
  builtin_tools.py    tool eksekusi generik
  web.py              Playwright generik
```

---

## Recipe yang Terpasang

Registry ada di `hermes/recipes.py` (`RECIPES`). Menambah alur = tambah satu
entri di situ + satu cabang di `recipes.execute()`.

### `ecuti_rekap` — Rekap E-Cuti
- **Jadwal:** `0 7 * * *` (harian 07:00)
- **Input:** tanggal mulai/selesai + item Bitwarden (opsional; kosong = sebulan terakhir)
- **Langkah:** login SIMPATIK (Bitwarden `2017skpd019`) → ambil token `/token?app=ecuti`
  → export `dashboard/export_range` → Excel ke `output/ecuti/` → unggah ke
  `POST /api/v1/impor-cuti` (multipart, `X-API-Key`, scope `impor`).
- **Impor lewat API, bukan browser.** Endpoint memakai service yang sama dengan
  halaman panel, jadi aturan upsert/pelewatan barisnya identik dan tidak ada
  lagi sesi Google/Playwright yang perlu dirawat.
- **Respons API** `{inserted, updated, skipped, tanpa_surat, status_lain,
  jumlah_diproses}` dipetakan `recipe_ecuti` ke `baru` / `diperbarui` /
  `dilewati` / `total` supaya `summarize()` tetap bekerja.
- **`jumlah_diproses` tidak mencakup baris yang dilewati** karena No Surat
  kosong atau status bukan TTE Kepala Dinas — `total` dihitung manual dari
  `inserted + updated + jumlah_dilewati`.

### `penomoran_cuti` — Penomoran Surat Cuti
- **Jadwal:** `0 * * * *` (tiap 1 jam)
- **Langkah:** login SIMPATIK → baca tabel Pengajuan → periksa CKAP milik PPPK →
  isi No Surat untuk baris berstatus target yang masih kosong → tulis register
  ke Google Sheet.
- **Status target:** `Pemberian Nomor Surat OPD` dan `Verifikasi Atasan`
  (mencakup "Verifikasi Atasan Langsung" & "... Atasan Langsung").
- **Batas pindai:** `PENOMORAN_MAX_HALAMAN` (default `3`) — hanya halaman teratas,
  karena pengajuan baru selalu muncul di atas.
- **CKAP oleh PPPK tidak boleh diberi nomor.** "Cuti Karena Alasan Penting"
  hanya untuk PNS; PPPK/PPPK Paruh Waktu tidak berhak. Baris seperti ini
  dilewati dari penomoran dan dilaporkan ke Telegram agar statusnya diubah.
  - Status ASN diambil dari `GET /api/v1/pegawai/{nip}` (`jenis`), **bukan
    ditebak dari NIP** — hanya server yang tahu status terbaru.
  - Hanya pengajuan belum final (`Pengajuan`, `Verifikasi Atasan*`) yang
    dilaporkan; yang sudah `TTE Kepala Dinas` tidak perlu diubah lagi.
  - Profil gagal diambil → NIP itu dilewati, **tidak** dianggap PPPK.
  - Notifikasi mengirim dua pesan: ringkasan temuan, lalu teks siap-copas
    dalam `<pre>` (sekali tap untuk salin). Temuan saja sudah cukup memicu
    notifikasi walau tidak ada nomor yang diisi.
- **Aturan tetap:**
  - Nomor yang **sudah terisi di SIMPATIK tidak pernah ditimpa** (SIMPATIK sumber kebenaran, bukan Sheet).
  - Cuti **tahun lalu dilewati** (filter tahun berjalan).
  - Nomor disusun dari **tanggal terbit**, bukan tanggal cuti.
  - Nomor berikutnya diambil dari **Sheet**, bukan `nomor_awal` API (API tertinggal).
  - `hari` kiriman **diabaikan API** — durasi dihitung ulang sebagai hari kerja
    (lewati Sabtu/Minggu + `hari_libur`).
  - Register **tetap ditulis ke Sheet**; mirror Sheets dari API mengarah ke
    spreadsheet lain, jadi tidak bisa diandalkan.

### `magang_baru` — Cek Pengajuan Magang
- **Jadwal:** `0 8 * * *` (harian 08:00)
- **Input:** tidak ada — satu tombol saja.
- **Langkah:** login `izin.semarangkota.go.id` → buka `/magang/admin-index` →
  baca status → laporkan yang berstatus `Pengajuan Baru` dan belum pernah dilaporkan.
- **Kredensial:** Bitwarden item `izin.semarangkota.go.id`, user `riyantoSimonev`.
- **State "sudah dilihat":** `data/magang_seen.json` (lihat bagian State).

### `duk_mingguan` — Impor DUK
- **Jadwal:** `0 7 * * 1` (Senin 07:00, mingguan)
- **Input:** tidak ada — satu tombol saja.
- **Langkah:** login SIMPATIK → `token?app=sisdm` → ambil
  `https://sisdm.semarangkota.go.id/duk` lewat `ctx.request` → parse tabel
  jadi baris terstruktur (`duk_parse.parse_duk`) → `POST /api/v1/impor-duk-json`.
- **JSON hasil:** `output/duk/DUK-YYYY-MM-DD.json` (bind-mount, permanen di
  host). Tidak ada PDF lagi — kolom dibaca langsung dari `<td>`, jadi tidak
  ada penebakan koordinat di sisi server.
- **Kenapa JSON, bukan PDF:** impor PDF menebak kolom dari posisi teks
  (`pdftotext -bbox`) dan tebakan itu meleset untuk sebagian baris. Kolom
  `<td>` sudah pasti, jadi nilai dikirim apa adanya.
- **`jabatan` dan `eselon` dipisah di parser** pada garis miring TERAKHIR —
  nama jabatan sendiri bisa memuat "/" (`Pranata Komputer Ahli Muda/Muda / JF`
  → eselon `JF`).
- **`masa_kerja_bulan` bisa > 12** (mis. `24 bulan`) karena itu angka resmi
  dari dokumen. Jangan dinormalkan; normalkan justru membuatnya beda dari
  sumber.
- **NIP bisa ganda di sumber.** DUK-2026-11-11 berisi 33 baris dengan 31 NIP
  unik (baris 1–2 dan 13–14 identik per pasangan). Datanya memang begitu di
  SISDM, bukan bug parser — jangan pakai `nip` sebagai kunci baris.
- **`periode` selalu null**: halaman `/duk` mencetak `PERIODE :` kosong.
- **Halaman `/duk` tanpa form/filter** — murni dokumen cetak; yang terbaca
  adalah DUK berjalan saat resep dijalankan.
- **Isi respons harus dibaca sebelum `ctx.close()`** — `Response.text()`
  sesudah konteks ditutup gagal dengan "Event loop is closed".
- **Baris tanpa `nama` dibuang di parser** karena server menolaknya per baris.
  Bila semua baris gagal, API menjawab 422 dan tidak menyimpan batch.

### `pegawai_harian` — Data Pegawai Aktif
- **Jadwal:** `0 7 * * 5` (Jumat 07:00, mingguan)
- **Input:** `kd_skpd` (kode OPD, opsional; kosong = `019`)
- **Langkah:** login SIMPATIK → `token?app=sisdm` → `/pegawai` → pilih
  `#kd_skpd` → klik `#btn-filter` → `#pegawai-table_length` = `All` →
  ambil baris `Kedudukan = Aktif` → ekstrak profil + seluruh riwayat →
  `POST /api/v1/impor-pegawai`.
- **Hasil:** `output/pegawai/data_pegawai_aktif_YYYY-MM-DD.json` (bind-mount,
  permanen di host). 57 pegawai aktif ≈ 550 detik.
- **OPD dipilih lewat kode, bukan nama** — `kd_skpd` `019` =
  Dinas Penanaman Modal dan Pelayanan Terpadu Satu Pintu. Nama OPD bisa
  berubah penulisannya, kode tidak.
- **"Aktif" bukan satu-satunya isi tabel.** Data nyata: 80 pegawai, 57 Aktif,
  19 Pensiun, 2 Pemberhentian, 1 Tugas Belajar, 1 Meninggal. Menyaring
  status wajib; kalau tidak, 23 pegawai nonaktif ikut terkirim.
- **`PPPK` tidak muncul sebagai kata "PPPK" di SISDM.** Statusnya ditulis
  lengkap `PEGAWAI PEMERINTAH DENGAN PERJANJIAN KERJA` (dan `... PARUH WAKTU`).
  Deteksi harus mencocokkan `PERJANJIAN KERJA`, bukan `PPPK` — kalau salah,
  riwayat PPPK tidak terambil dan KGB/pangkat yang seharusnya kosong justru
  ikut terambil.
- **Ambil halaman via `ctx.request`, bukan `page.goto`.** 57 pegawai ×
  ~11 halaman = ribuan request; `page.goto` akan memuat CSS/JS tiap kali.
- **Kompetensi & CPNS/PPPK butuh form edit.** Tabel daftar tidak memuat
  tanggal pelaksanaan, jumlah jam, atau gaji — datanya hanya ada di
  `/riwayat_*/{id}/edit`.
- **Impor bertahap 100 record** (`BATCH_IMPOR`); API menolak > 2000.
- **Notifikasi diringkas** (jumlah + PNS/PPPK), log nama tidak ikut dikirim.

### `bersih_output` — Bersihkan Berkas Unduhan
- **Jadwal:** `0 23 * * 0` (Minggu 23:00, mingguan)
- **Input:** `retensi` (hari, opsional; kosong = `HERMES_OUTPUT_RETENSI_HARI`)
- **Langkah:** pindai `output/` rekursif → hapus berkas yang `mtime`-nya lebih
  tua dari batas → laporkan. Folder tidak dihapus, hanya berkas.
- **Retensi default 7 hari.** Berkas baru tidak pernah disentuh, jadi hasil
  hari itu aman.
- **Pengaman:** setiap berkas diverifikasi berada di bawah `output/`
  (`Path.resolve().relative_to()`), supaya symlink ke luar folder tidak ikut
  terhapus. Nilai retensi tak sah (`0`, negatif, bukan angka) kembali ke default.
- **Gate notifikasi:** hanya kirim kalau ada berkas yang benar-benar dihapus.

### `ekin_harian` — Aktivitas Harian e-Kinerja
- **Jadwal:** `0 7,16 * * *` (07:00 sebelum jam kerja, 16:00 sesudah 15:00)
- **Input:** tidak ada — satu tombol saja.
- **Akun:** `199303252025061002` (env `EKIN_BW_USERNAME`), kredensial dari
  Bitwarden item `simpatik.semarangkota.go.id`, **wajib** pakai `username=`
  karena item itu dipakai beberapa akun sekaligus.
- **Langkah:** login SIMPATIK → `token?app=ekinv3` → sapu
  `/administrator/aktivitas_harian` → simpan ke `ekin_aktivitas` → laporkan.
- **Tidak ada total per hari di e-kin.** Halaman hanya 10 baris per halaman;
  panel kalender (`/per_kalender*`) cuma memuat judul kegiatan; grafik
  Highcharts hanya akumulasi **bulanan**. Jadi total harian dijumlah sendiri
  dari baris tersimpan — itu alasan tabel SQL-nya ada.
- **Paginasi:** `/index/{offset}`, offset = (halaman−1)×10. Tautan "Last"
  memakai offset yang sama. Jangan pakai "Next" (di halaman akhir menunjuk
  dirinya sendiri). Sapuan berhenti saat satu halaman seluruhnya lebih tua
  dari batas yang diminta.
- **Kolom tabel:** checkbox id → tanggal → uraian → (satuan|kuantitas|waktu)
  → status → aksi. `waktu` **tidak selalu** satuan × kuantitas (240×2 bisa
  tercatat 390 menit karena e-kin memotong), jadi angkanya dibaca apa adanya.
- **Batas penilaian bergeser mengikuti jam** (`batas_periksa()`): sebelum
  15:00 hanya sampai kemarin, sesudahnya termasuk hari ini. Alasannya input
  e-kin memang hanya bisa sebelum 08:00 / sesudah 15:00.
- **Target:** 420 menit/hari dianggap cukup; kurang dari itu dilaporkan
  beserta selisihnya. Minimum bulanan 8.000 menit dilaporkan sebagai progres.
- **Hari kerja** = Senin–Jumat. Hari libur nasional tidak bisa ditebak dari
  e-kin (sama seperti hari kosong), jadi diisi manual lewat `EKIN_HARI_LIBUR`.
- **Idempoten:** penyimpanan mengganti rentang tanggal yang disapu
  (`DELETE ... BETWEEN` lalu insert), jadi run beruntun tidak menggandakan.
  Riwayat bulan sebelumnya tetap tersimpan.
- **Gate notifikasi:** hanya kirim kalau ada hari kosong atau di bawah 420
  menit — alur ini jalan 2× sehari.
- **Tab Kinerja di dashboard** menampilkan hari kerja bulan ini yang belum
  memenuhi target, dengan form isian multi-baris per tanggal (butir kegiatan,
  catatan, kuantitas) — pengganti antrian riwayat di extension browser.
  Isian disimpan di `ekin_rencana`; tabel ini sengaja terpisah dari
  `ekin_aktivitas` karena isian belum tentu sudah masuk e-kin.
- **Isian tidak langsung dikirim.** Tab Kinerja hanya menaruh isian di antrian
  (`ekin_rencana.status = pending`); pengiriman ke SIMPATIK dilakukan alur
  `ekin_kirim` yang jalan jam 15.00, atau tombol "Kirim ke SIMPATIK" saat
  jendela input e-kin terbuka (sebelum 08.00 / sesudah 15.00). Di luar jendela
  itu tombol menolak — server e-kin yang menolak, bukan pilihan kita.
- **Status kirim per baris:** `pending` (menunggu) → `sent` (terbukti ada di
  tabel e-kin) atau `error` (ditolak/ditolak-tak-terlihat). Status ditentukan
  dengan menyapu ulang tabel e-kin setelah kirim, jadi run beruntun tidak
  menggandakan data. Menyimpan ulang tanggal mempertahankan status baris yang
  isinya tidak berubah.
- **Urutan isi form e-kin yang benar** (kalau tidak, `waktu` bisa 0): pilih
  tanggal → pilih butir → tunggu `#satuan_waktu` terisi (AJAX
  `get_detail_butir_kegiatan`) → ketik kuantitas → tunggu
  `#waktu == kuantitas × satuan_waktu` → klik `.btn_save[data-stype="stay"]`.

### `ekin_kirim` — Kirim Isian e-Kinerja ke SIMPATIK
- **Jadwal:** `0 15 * * *` (sesudah jam 15.00, saat e-kin menerima input).
- **Input:** tidak ada.
- **Langkah:** ambil `ekin_rencana` berstatus `pending` → login SIMPATIK →
  isi `/aktivitas_harian/add_new` per butir → sapu ulang tabel → tandai
  `sent` yang benar-benar muncul, `error` untuk sisanya.
- **Butir dicari lewat** `GET /administrator/aktivitas_harian/get_butir_kegiatan?search=<nama>`,
  dicocokkan **persis** (titik, huruf besar-kecil, spasi diabaikan) lalu
  diambil id pertama. Nama yang tidak ada di e-kin tidak dikirim.
- **Satuan menit diambil dari e-kin**, bukan dari katalog lokal: server
  mengisi `#satuan_waktu` lewat `get_detail_butir_kegiatan`.
- **Gate notifikasi:** diam kalau tidak ada yang dikirim dan tidak ada gagal.

### `absen_cek` — Cek Presensi Harian (absen.semarangkota.go.id)
- **Jadwal:** `50,55 7 * * 1-5` (hadir) dan `1,6 16 * * 1-5` (pulang) — lihat
  `schedule_extra`; satu ekspresi cron tidak bisa memuat dua jendela berbeda.
- **Input:** `sesi_shift` opsional (`hadir`/`pulang`); kosong = ditentukan jam.
- **BACA SAJA.** Tidak pernah mengisi atau menghapus presensi. Endpoint yang
  dipakai hanya `/home`, `/presensi/rekap-harian`,
  `/presensi/rekap-harian/asn/<kode>`, `/web-service/get-jam-kerja` (query),
  dan `/web-service/list-pegawai-asn-all-harian`. Jangan menambah panggilan ke
  tombol/endpoint KONFIRMASI, simpan, atau hapus.
- **Target tetap satu orang:** NIP `199303252025061002` (Riyanto Nugroho) lewat
  env `ABSEN_NIP`. Kalau NIP tidak ada di rekap, alur **gagal** — tidak pernah
  jatuh ke "laporkan semua pegawai".
- **Captcha dijawab manusia lewat Telegram.** Tanpa OCR, tanpa browser. Bot
  mengirim gambar, user membalas kodenya. Sesi disimpan di
  `data/absen_session.json`; selama sesi hidup tidak ada captcha sama sekali.
- **Bot Telegram satu-satunya pembaca `getUpdates`.** Resep menunggu jawaban
  lewat tabel `notes` (`absen_captcha_jawaban`). Kalau resep ikut polling,
  keduanya berebut pesan yang sama dan balasan captcha bisa hilang.
- **Kredensial dicari lewat NAMA item persis** (`bw get item
  absen.semarangkota.go.id`), bukan username. Username
  `dpmptsp@semarangkota.go.id` dipakai lima item berbeda; mencari lewat
  username pernah memilih `sisdmk.kemkes.go.id` dan login selalu ditolak
  dengan pesan "Username, Password atau Captcha Tidak Sesuai" — pesan itu
  menyembunyikan penyebab sebenarnya.
- **Situs ini Laravel**, bukan CodeIgniter: token CSRF ada di `var token = "..."`
  dalam halaman; POST tanpa token dijawab `419 CSRF token mismatch`. Captcha
  terikat `laravel_session`, jadi gambar dan POST wajib satu sesi.
- **Gate notifikasi:** diam kalau target sudah absen. Jadwalnya dua kali
  beruntun (07:50 & 07:55), jadi tanpa gate ini selalu dobel.
- **Cara pakai manual:** kirim `/absen` atau `/absen pulang` ke bot.

---

## Notifikasi Telegram

Satu pintu: `recipes.notify_recipe()`, dipanggil di `run_recipe()` — termasuk
jalur exception, agar kegagalan tidak senyap.

**Gating (jangan spam):**

| Kondisi | Kirim? |
|---|---|
| Recipe gagal | ✅ selalu |
| Penomoran mengisi nomor | ✅ |
| Penomoran menemukan CKAP PPPK | ✅ (+ teks siap-copas) |
| Penomoran 0 terisi & tanpa temuan | ❌ diam |
| Rekap menghasilkan Excel | ✅ |
| Magang ada pengajuan baru | ✅ |
| Magang 0 baru | ❌ diam |
| DUK tersimpan (batch baru) | ✅ |
| Pegawai terimpor | ✅ (resume: jumlah + PNS/PPPK) |
| Pembersihan 0 berkas | ❌ diam |
| e-Kinerja ada hari kosong/kurang | ✅ |
| e-Kinerja semua hari memenuhi target | ❌ diam |

Penomoran jalan 12× sehari — tanpa gate ini akan jadi noise.

**Pencatatan untuk dashboard:** `simpan_notifikasi()` menulis ke tabel `logs`
dengan `event='telegram_sent'` dan `meta = {recipe, text, chat}`. Teks yang
disimpan adalah versi **asli (belum di-escape)** supaya enak dibaca.

**Escape HTML wajib:** pesan dikirim dengan `parse_mode=HTML`, jadi teks harus
di-`html.escape()` lebih dulu. Nama seperti `S.T`, `S.Kom`, `A.Md.` membuat
Telegram menolak pesan (`can't parse entities: Unsupported start tag "s.t"`).

Kegagalan kirim/pencatatan **tidak boleh** menggagalkan recipe.

---

## Dashboard (`python -m hermes web`)

Port default 10000, Basic Auth dari Docker secret `secrets/web_pass`
(bukan `.env`). Lima tab:

| Tab | Isi |
|---|---|
| **Dashboard** | kartu statistik, "Notifikasi terakhir" (3 pesan), grafik 7 hari, kesehatan |
| **Alur** | daftar recipe: jadwal, status, tombol Jalankan, hasil terakhir, "lihat log" |
| **Kinerja** | hari kerja bulan ini yang belum terisi + form isian butir per tanggal |
| **Notifikasi** | semua pesan Telegram yang benar-benar terkirim + waktunya |
| **Log** | log aktivitas, filter level, export `.txt` |

Endpoint:

```
GET  /                    halaman SPA
GET  /api/dashboard       stats + recipes + notifications (30 terakhir)
GET  /api/kinerja         ringkasan e-kin + hari belum terisi + butir + isian
GET  /api/notifications   pesan Telegram (200 terakhir)
GET  /api/logs[?level=]   log; ?format=txt untuk unduh
POST /api/recipes/<key>/run        jalankan alur (body: {"inputs": {...}})
POST /api/kinerja/isian            ganti isian satu tanggal (body: {tanggal, baris})
POST /api/kinerja/isian/<tgl>/hapus
POST /api/kinerja/kirim            kirim antrian ke SIMPATIK (body: {paksa?})
```

Auto-refresh 15 detik hanya saat tab Dashboard aktif.

---

## Kredensial & State

**Bitwarden** (`recipe_ecuti.bitwarden_login`):

- Butuh `BW_SESSION` di environment; `run_recipe()` membukanya otomatis bila
  recipe punya `needs_vault: True`.
- **Nama item bisa ambigu.** `bw get` menolak dengan *"More than one result was
  found"* kalau beberapa item berawalan sama (contoh: `izin.semarangkota.go.id`,
  `... ppid`, `...:7777`). Untuk kasus itu pakai parameter `username=` —
  fungsinya akan `bw list items --search` lalu memilih yang namanya persis sama.

**File state:**

| Path | Isi |
|---|---|
| `data/hermes.sqlite` | DB utama: `notes`, `records`, `logs`, `recipe_state`, `ekin_aktivitas`, `ekin_rencana` |
| `data/magang_seen.json` | kode `MGNG-*` yang sudah dilaporkan |
| `data/bw-config/` | config & login Bitwarden antar-restart |
| `output/ecuti/` | Excel hasil export |
| `output/duk/` | JSON DUK hasil baca tabel |
| `output/pegawai/` | JSON data pegawai aktif |

**Jangan pernah** commit atau echo isi `.env` dan `secrets/`.

---

## Environment Variable Penting

```
HERMES_WEB_HOST / HERMES_WEB_PORT      default 127.0.0.1:10000
HERMES_WEB_PASS                        (di Docker: secret web_pass)
HERMES_DB_PATH                         default data/hermes.sqlite
TELEGRAM_BOT_TOKEN                     token bot
TELEGRAM_ALLOWED_CHAT_IDS              daftar chat id (diparse jadi int!)
TELEGRAM_NOTIFY                        kill switch (0 = mati)
PENOMORAN_MAX_HALAMAN                  default 3
HERMES_OUTPUT_RETENSI_HARI             default 7 (resep bersih_output)
EKIN_BW_USERNAME                       akun e-kin (default 199303252025061002)
EKIN_TARGET_HARIAN / EKIN_TARGET_BULANAN   default 420 / 8000 menit
EKIN_HARI_LIBUR                        tanggal libur, dipisah koma
EKIN_BULAN_KE_BELAKANG / EKIN_MAX_HALAMAN  batas sapuan tabel e-kin
KEPEG_API_KEY / GOOGLE_SHEET_ID        akses API & register
BW_EMAIL                               untuk `bw login` di entrypoint
HERMES_DAILY_DELAY                     jeda startup autopilot (detik)
```

---

## Common Workflows

```bash
python -m hermes recipes            # daftar alur + jadwal + status
python -m hermes recipe run <key>   # jalankan satu alur
python -m hermes tick               # periksa jadwal sekali
python -m hermes autopilot          # daemon otonom
python -m hermes web                # dashboard
```

**Deploy perubahan:**

```bash
cd /root/docker/hermes
.venv/bin/python -m compileall -q hermes/     # cek sintaks dulu
docker compose up -d --build hermes
```

`--build` itu **wajib**. `docker cp` ke container akan hilang begitu container
dibuat ulang, dan `up` tanpa `--build` memakai image lama.

**Debug di dalam container:**

```bash
docker exec -it hermes-agent python -c "from hermes import config, tasks; ..."
```

Sesi Bitwarden hanya hidup per-proses: `refresh_bw_session_if_needed()` dan
pemakaian `bw` harus berada dalam satu proses Python yang sama.

---

## Aturan Maintenance (belajar dari bug nyata)

1. **Paginasi jangan pakai `a[rel='next']`** — di e-cuti selalu ada, akibatnya
   590 baris terbaca padahal ~100 unik. Pakai href sebenarnya
   (`/index/{offset}`, offset = (halaman−1)×10) + dedup per ID.
2. **Isi form harus didahului navigasi.** `page.fill()` pada halaman yang salah
   pernah menulis nomor milik satu pegawai ke baris pegawai lain (kerusakan data
   nyata). Pola benar: navigasi ke URL baris → verifikasi `action` form berakhiran
   id baris itu → isi → cek nilai terpasang → baru simpan. Satu baris gagal tidak
   boleh menghentikan sisanya.
3. **Jangan andalkan `sleep` tetap** untuk menunggu hasil. Polling elemen hasil
   (mis. `[role=status]`) dengan batas waktu.
4. **`.xls` dari e-cuti adalah OLE2**, bukan xlsx — `openpyxl` menolaknya. Angka
   impor sudah tersedia di halaman; tidak perlu membaca ulang file.
5. **API tidak bisa mengosongkan nomor total** — backend menolak
   `"Kolom Sisa Cuti Wajib terisi."`; `sisa_cuti` harus tetap diisi.
6. **Key antar-recipe bisa bentrok** di `summarize()`. Magang dan rekap sama-sama
   memakai `baru`; blok impor digerbangi `"diperbarui" in result`.
7. **`cfg.telegram_allowed` berisi integer**, bukan string — `",".join()` langsung
   akan `TypeError`. Pakai `map(str, ...)`.
8. **Jangan telan error pencatatan dengan `log.debug`** — pakai `log.warning`
   agar kegagalan senyap (seperti bug join di atas) cepat ketahuan.
9. **Resep harus idempoten.** Run beruntun tidak boleh menggandakan hasil.
10. **Pollusi state dari uji coba.** Menjalankan recipe dengan `execute` palsu
    tetap mencatat `recipe_state`; reset state sebelum verifikasi akhir.
11. **Total harian e-kin dijumlah sendiri.** Halaman, kalender, dan grafik
    tidak ada yang memberi total per hari — grafik hanya akumulasi bulanan,
    kalender hanya judul kegiatan. Jangan coba menebak dari kalender.
12. **`waktu` e-kin dibaca apa adanya.** Satuan × kuantitas sering tidak sama
    dengan menit tercatat (e-kin memotong), jadi jangan dihitung ulang.

---

## Code Style Guidelines

- Nama variabel deskriptif; komentar hanya untuk logika yang tidak self-evident.
- Ikuti pola yang sudah ada; jangan menambah dependency baru kalau bisa
  memakai stdlib (cron, web server, parser semuanya buatan sendiri).
- Ekstrak kondisi kompleks menjadi variabel boolean bernama.
- Komunikasi ke user dalam Bahasa Indonesia.
