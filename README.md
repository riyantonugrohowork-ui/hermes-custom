# Hermes Agent 🤖

Agen otomasi personal yang **berjalan otonom mengikuti alur (recipe)**.
Dashboard menampilkan daftar alur; tiap alur punya jadwal sendiri dan jalan tanpa diminta.
Chat bukan tampilan utama — ia hanya dipakai kalau sebuah alur memang butuh input.

---

## Arsitektur

```
        [ DASHBOARD ]  daftar alur (recipe)
              │  klik / jadwal cron / startup
              ▼
   [ RECIPE / recipes.py ]  ← sumber kebenaran: steps, inputs, schedule
              │  input hanya jika alur mendeklarasikannya
              ▼
   [ SCHEDULER / scheduler.py + cron.py ]  (daemon otonom)
              │
              ▼
   [ RECIPE RUNTIME ]
     ├─ recipe_ecuti  → SIMPATIK → export Excel → impor kepegawaian
     ├─ web_fill      → Playwright (isi portal web)
     ├─ make_docx     → dokumen Word
     ├─ append_excel  → Excel/CSV
     ├─ save_note     → SQLite
     └─ notify        → Telegram
```

Inti desainnya:

- **Alur adalah wujud utama.** Dashboard merender `recipes.RECIPES`; tidak ada yang perlu tahu isi alur.
- **Tanpa input = tanpa tanya.** Alur dengan `inputs: []` cukup satu tombol, tanpa dialog apa pun.
- **Input muncul di kartu alur**, bukan di halaman chat terpisah.
- **Jadwal menempel pada alur** (cron), bukan pada task manual.

---

## Setup

```bash
# 1. Virtual env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Konfigurasi
cp .env.example .env
nano .env           # isi kredensial yang dibutuhkan

# 3. (opsional) browser untuk portal web
playwright install chromium
```

### Menjalankan dengan Docker

```bash
docker compose up -d --build
# Dashboard: http://localhost:10000
```

---

## Pakai

```bash
python -m hermes recipes            # daftar alur + jadwal + status
python -m hermes recipe run <key>   # jalankan satu alur sekarang
python -m hermes recipe run ecuti_rekap tanggal_mulai=2026-11-01
python -m hermes autopilot          # daemon otonom (jadwal + startup)
python -m hermes tick               # periksa jadwal sekali (untuk cron sistem)
python -m hermes web                # dashboard di http://localhost:10000
```

Perintahnya lain yang masih ada untuk debugging: `status`, `run "instruksi"`, `plan`, `tasks`, `chat`, `telegram`.

---

## Menambah alur baru

Cukup tambahkan satu entri di `hermes/recipes.py`, lalu satu pelaksana:

```python
{
    "key": "cek_surat",
    "title": "Cek Surat Masuk",
    "description": "Ambil daftar surat baru dan beri tahu kalau ada.",
    "steps": ["Buka portal surat", "Baca daftar", "Bandingkan dengan yang sudah dilihat"],
    "inputs": [],                      # kosong = jalan tanpa tanya apa pun
    "schedule": "*/30 * * * *",        # tiap 30 menit
},
```

Lalu daftarkan pelaksananya di `recipes.execute()`:

```python
if key == "cek_surat":
    return recipe_surat.run(cfg, **inputs)
```

Dashboard otomatis menampilkannya — tanpa mengubah UI.

### Alur yang butuh input

```python
"inputs": [
    {"name": "tanggal_mulai", "label": "Tanggal mulai", "type": "date", "required": False},
    {"name": "kata_kunci",    "label": "Kata kunci",    "type": "text", "required": True},
],
```

Field akan muncul **di dalam kartu alur**. Selama input kosong dan tidak wajib,
alur tetap jalan memakai nilai default.

---

## Format jadwal

Ekspresi cron 5 field: `menit jam tanggal bulan hari`

| Contoh | Arti |
|---|---|
| `0 7 * * *` | tiap hari 07:00 |
| `*/30 * * * *` | tiap 30 menit |
| `0 8 * * 1-5` | Senin–Jumat 08:00 |
| `@daily` / `@hourly` | alias harian / per jam |

Scheduler **mengejar jadwal yang terlewat satu kali**. Yang dibandingkan bukan
"sudah jam 7?" tapi "jadwal terakhir sebelum sekarang sudah dijalankan?".

Artinya, kalau laptop mati atau tidur saat jam 07:00 lalu dinyalakan jam 08:00,
alur tetap berjalan sekali begitu container hidup. Tapi tidak diulang:

| Kejadian | Hasil |
|---|---|
| Laptop nyala 08:00, belum jalan hari ini | ✅ jalan sekali |
| Restart/reboot lagi jam 10:00 | ⏭️ dilewati (sudah jalan) |
| Mati 3 hari, nyala hari ke-4 | ✅ jalan sekali (bukan 3×) |
| Nyala 06:00, sebelum jadwal | ⏳ menunggu 07:00 |

Karena Docker memakai `restart: unless-stopped`, container hidup otomatis
begitu Docker Desktop menyala — tidak perlu menjalankan apa pun secara manual.

---

## Struktur
```
hermes/
  __main__.py     CLI
  config.py       muat .env
  cron.py         parser cron (tanpa dependency)
  recipes.py      daftar alur: steps, inputs, schedule, eksekusi
  scheduler.py    pemeriksa jadwal (recipe + task lama)
  autopilot.py    daemon otonom (startup + tick tiap menit)
  recipe_ecuti.py alur E-Cuti lengkap
  webui.py        dashboard (daftar alur + log)
  brain.py        instruksi -> Plan (rule/LLM), untuk alur bebas
  runner.py       eksekusi Plan
  builtin_tools.py tool eksekusi generik
  web.py          Playwright generik
  db.py, tasks.py penyimpanan
```

---

## Keamanan
- `.env` berisi kredensial — **jangan pernah** di-commit atau di-echo.
- Password dashboard dibaca dari Docker secret `secrets/web_pass` (bukan `.env`).
- Sesi login portal disimpan di `data/` dan dipakai ulang; perlakukan seperti kredensial.
- Aksi berisiko pada alur bebas memakai `needs_confirmation=true`.
