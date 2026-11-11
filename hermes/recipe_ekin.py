"""Recipe Aktivitas Harian e-Kinerja.

Alur:
  1. Login SIMPATIK dengan akun sendiri (bukan akun OPD)
  2. Ambil token `/token?app=ekinv3` → sesi e-kin
  3. Sapu tabel Aktivitas Harian (`/index/{offset}`, 10 baris per halaman)
  4. Simpan ke tabel `ekin_aktivitas` (cermin sumber, diganti per rentang)
  5. Laporkan hari kerja yang belum diisi & yang belum mencapai 420 menit

Kenapa perlu disimpan sendiri: halaman e-kin hanya menampilkan 10 baris per
halaman dan tidak menampilkan total per hari di mana pun — kalender cuma
memuat judul kegiatan, grafik akumulasi hanya total bulanan. Jadi total harian
dijumlah dari baris yang tersimpan di SQLite.

Aturan input e-kin yang dipegang:
  - pengisian hanya bisa di luar jam kerja (sebelum 08.00 / sesudah 15.00),
    jadi laporan pagi menilai sampai kemarin dan laporan sore sudah menilai
    hari ini — lihat `batas_periksa()`
  - 420 menit/hari dianggap cukup; kurang dari itu dilaporkan
  - minimal 8.000 menit per bulan
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

E_KIN = "https://e-kin.semarangkota.go.id/administrator/aktivitas_harian"
E_KIN_TOKEN = "http://simpatik.semarangkota.go.id/token?app=ekinv3"

# Item Bitwarden dipakai beberapa akun sekaligus, jadi `username` wajib
# disebut — `bw get` menolak nama yang ambigu ("More than one result found").
BW_ITEM = "simpatik.semarangkota.go.id"
BW_USER = os.getenv("EKIN_BW_USERNAME", "199303252025061002")

TARGET_HARIAN = int(os.getenv("EKIN_TARGET_HARIAN", "420"))
TARGET_BULANAN = int(os.getenv("EKIN_TARGET_BULANAN", "8000"))

# Rentang yang disapu: bulan berjalan + N bulan sebelumnya. Cukup untuk laporan
# dan riwayat dashboard tanpa membaca seluruh arsip (79 halaman).
BULAN_KE_BELAKANG = int(os.getenv("EKIN_BULAN_KE_BELAKANG", "1"))

# 10 baris per halaman. Batas ini jaring pengaman agar satu bug paginasi tidak
# berputar selamanya; halaman tanpa baris juga menghentikan sapuan.
BARIS_PER_HALAMAN = 10
MAX_HALAMAN = int(os.getenv("EKIN_MAX_HALAMAN", "40"))

# Hari libur nasional/cuti bersama tidak bisa ditebak dari e-kin: hari tanpa
# aktivitas terlihat sama dengan hari yang belum diisi. Isi manual lewat env,
# dipisah koma: EKIN_HARI_LIBUR=2026-11-25,2026-12-25
HARI_LIBUR = {x.strip() for x in os.getenv("EKIN_HARI_LIBUR", "").split(",") if x.strip()}

NAMA_HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
NAMA_BULAN = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def batas_periksa(now: datetime | None = None) -> date:
    """Hari terakhir yang adil untuk dinilai pada jam sekarang.

    e-kin baru bisa diisi sebelum 08.00 atau sesudah 15.00, jadi hari ini
    belum tentu "terlewat" saat laporan pagi berjalan. Sebelum 15.00 penilaian
    berhenti di kemarin; sesudahnya hari ini sudah boleh dinilai.
    """
    now = now or datetime.now()
    if now.hour >= 15:
        return now.date()
    return now.date() - timedelta(days=1)


def awal_bulan(d: date) -> date:
    return date(d.year, d.month, 1)


def jendela_input_buka(now: datetime | None = None) -> bool:
    """Apakah e-kin sedang menerima input: sebelum 08.00 atau sesudah 15.00.

    Ini aturan server, bukan pilihan kita: form `add_new` di luar jendela itu
    ditolak. Dashboard memakai fungsi ini untuk memutuskan tombol kirim boleh
    jalan atau isian cukup dititipkan ke antrian jam 15.00.
    """
    now = now or datetime.now()
    return now.hour < 8 or now.hour >= 15


def mundur_bulan(d: date, n: int) -> date:
    """Tanggal 1, n bulan sebelum bulan `d`."""
    tahun, bulan = d.year, d.month - n
    while bulan < 1:
        bulan += 12
        tahun -= 1
    return date(tahun, bulan, 1)


def hari_kerja(mulai: date, selesai: date, libur: set[str] | None = None) -> list[date]:
    """Tanggal Senin–Jumat dalam rentang (inklusif) yang bukan hari libur."""
    libur = HARI_LIBUR if libur is None else libur
    hasil: list[date] = []
    d = mulai
    while d <= selesai:
        if d.weekday() < 5 and d.isoformat() not in libur:
            hasil.append(d)
        d += timedelta(days=1)
    return hasil


def susun_laporan(
    per_hari: dict[str, int],
    mulai: date,
    sampai: date,
    target_harian: int = TARGET_HARIAN,
    libur: set[str] | None = None,
) -> dict:
    """Temuan satu bulan: hari kosong, hari di bawah 420 menit, capaian bulan.

    `per_hari` berisi semua tanggal yang punya aktivitas, termasuk Sabtu/Minggu
    — menit akhir pekan tetap dihitung e-kin untuk capaian bulanan, jadi ikut
    dijumlah. Yang dinilai sebagai kewajiban hanya hari kerja.
    """
    belum: list[dict] = []
    kurang: list[dict] = []
    lengkap = 0

    for d in hari_kerja(mulai, sampai, libur):
        menit = int(per_hari.get(d.isoformat()) or 0)
        if menit <= 0:
            belum.append({"tanggal": d.isoformat(), "hari": NAMA_HARI[d.weekday()]})
        elif menit < target_harian:
            kurang.append({
                "tanggal": d.isoformat(),
                "hari": NAMA_HARI[d.weekday()],
                "menit": menit,
                "kurang": target_harian - menit,
            })
        else:
            lengkap += 1

    total = sum(int(v or 0) for v in per_hari.values())
    return {
        "bulan": f"{NAMA_BULAN[mulai.month - 1]} {mulai.year}",
        "dinilai_sampai": sampai.isoformat(),
        "belum": belum,
        "kurang": kurang,
        "lengkap": lengkap,
        "jumlah_hari_kerja": len(hari_kerja(mulai, sampai, libur)),
        "total_menit": total,
        "target_bulanan": TARGET_BULANAN,
        "kurang_bulanan": max(0, TARGET_BULANAN - total),
    }


def _sapu(pg, sejak: date, log: list[str]) -> list[dict]:
    """Baca tabel Aktivitas Harian sampai melewati tanggal `sejak`.

    Paginasi memakai href sebenarnya (offset = (halaman−1)×10), bukan tautan
    "Next": di halaman terakhir "Next" menunjuk ke halaman itu sendiri.
    Baris terurut tanggal menurun, jadi sapuan berhenti setelah satu halaman
    seluruhnya lebih tua dari `sejak`.
    """
    from . import ekin_parse

    semua: list[dict] = []
    offset = 0
    halaman = 0

    while halaman < MAX_HALAMAN:
        url = E_KIN if offset == 0 else f"{E_KIN}/index/{offset}"
        resp = pg.goto(url, wait_until="domcontentloaded")
        if resp is not None and resp.status >= 400:
            log.append(f"Halaman offset {offset}: HTTP {resp.status}, sapuan berhenti")
            break

        baris = ekin_parse.parse_baris(pg.content())
        if not baris:
            break

        semua.extend(baris)
        halaman += 1
        offset += BARIS_PER_HALAMAN

        if min(b["tanggal"] for b in baris) < sejak.isoformat():
            break

    log.append(f"Tabel aktivitas: {len(semua)} baris dari {halaman} halaman")
    return semua


def _login(pg, user: str, pwd: str) -> None:
    """Login SIMPATIK lalu masuk ke e-kin lewat token ekinv3."""
    from . import recipe_ecuti

    pg.goto(recipe_ecuti.SIMPATIK_LOGIN, wait_until="domcontentloaded")
    pg.fill("input[name=email], input[name=username], #email, #username", user)
    pg.fill("input[name=password], #password", pwd)
    pg.click("form input[type=submit], input[type=submit]")
    pg.wait_for_load_state("domcontentloaded")
    pg.goto(E_KIN_TOKEN, wait_until="domcontentloaded")
    try:
        pg.wait_for_url("**/administrator/**", timeout=20000)
    except Exception:  # noqa: BLE001
        pg.wait_for_timeout(4000)


def _norm(teks: str) -> str:
    """Pembanding nama butir: abaikan titik, besar-kecil huruf, dan spasi ganda."""
    return " ".join((teks or "").replace(".", " ").split()).casefold()


def _id_butir(ctx, nama: str, cache: dict[str, str]) -> str | None:
    """Id butir e-kin untuk satu nama katalog.

    Daftar butir memuat beberapa entri dengan teks sama (berbeda periode SKP);
    yang diambil entri pertama yang teksnya persis sama — sama seperti memilih
    dari daftar di layar. Hasilnya di-cache per run.
    """
    if nama in cache:
        return cache[nama] or None
    import json
    import urllib.parse

    url = f"{E_KIN}/get_butir_kegiatan?search={urllib.parse.quote(nama)}"
    try:
        data = json.loads(ctx.request.get(url).text() or "[]")
    except Exception:  # noqa: BLE001
        data = []
    cocok = next((d for d in data if _norm(d.get("text")) == _norm(nama)), None)
    cache[nama] = (cocok or {}).get("id") or ""
    return cache[nama] or None


def _isi_satu(pg, ctx, tanggal: str, baris: dict, cache: dict[str, str]) -> dict:
    """Isi satu butir lewat form `/aktivitas_harian/add_new` lalu simpan."""
    nama = baris["uraian"]
    ident = _id_butir(ctx, nama, cache)
    if not ident:
        return {"ok": False, "error": f"Butir '{nama}' tidak ditemukan di e-kin"}

    pg.goto(f"{E_KIN}/add_new", wait_until="domcontentloaded")
    pg.fill("#tanggal", tanggal)

    # Pilih butir lewat select2 lalu jalankan handler `change` milik halaman:
    # handler itulah yang mengisi satuan waktu/kuantitas dari server.
    # `uraian` readonly, jadi diisi lewat DOM — bukan `fill` (akan ditolak).
    pg.evaluate(
        """([id, teks, catatan]) => {
            const s = document.querySelector('#id_butir_kegiatan');
            s.appendChild(new Option(teks, id, true, true));
            window.jQuery(s).trigger('change');
            const u = document.querySelector('#uraian');
            u.value = teks + ' ';
            document.querySelector('#catatan').value = catatan || '';
        }""",
        [ident, nama, baris.get("catatan") or ""],
    )

    # Urutan yang dipakai halaman ini, dan satu-satunya yang andal:
    #   1) pilih butir  -> satuan_waktu diisi lewat AJAX get_detail_butir_kegiatan
    #   2) tunggu satuan_waktu terisi
    #   3) ketik kuantitas -> handler keyup menghitung waktu = kuantitas x satuan
    #   4) tunggu waktu terisi, baru simpan
    # Mengisi `waktu` langsung lewat DOM pernah menghasilkan waktu 0 atau 30
    # untuk butir 60 menit, jadi kolom itu dibiarkan dihitung oleh halaman.
    try:
        pg.wait_for_function(
            """() => {
                const el = document.querySelector('#satuan_waktu');
                return el && String(el.value || '').trim() !== ''
                    && Number(el.value) > 0;
            }""",
            timeout=15000,
        )
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"Satuan waktu butir '{nama}' tidak terisi dari e-kin"}

    qty = max(1, int(baris["kuantitas"]))
    pg.fill("#kuantitas", "")
    pg.click("#kuantitas")
    pg.keyboard.type(str(qty), delay=60)

    try:
        pg.wait_for_function(
            """([qty]) => {
                const s = Number(document.querySelector('#satuan_waktu')?.value || 0);
                const w = Number(document.querySelector('#waktu')?.value || 0);
                return s > 0 && w === qty * s;
            }""",
            arg=[qty],
            timeout=10000,
        )
    except Exception:  # noqa: BLE001
        waktu = pg.input_value("#waktu")
        return {
            "ok": False,
            "error": (
                f"Waktu tidak terhitung: kuantitas {qty} x satuan "
                f"{pg.input_value('#satuan_waktu')} menit, kolom waktu = {waktu or '0'}"
            ),
        }

    waktu = pg.input_value("#waktu")

    try:
        with pg.expect_response(lambda r: "add_save" in r.url, timeout=45000) as info:
            pg.click('.btn_save[data-stype="stay"]')
        hasil = info.value.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Simpan gagal: {type(e).__name__}: {e}"}

    if hasil.get("success"):
        return {"ok": True, "waktu": int(waktu)}
    pesan = hasil.get("message") or "ditolak e-kin"
    galat = hasil.get("errors") or {}
    if isinstance(galat, dict) and galat:
        pesan += " — " + "; ".join(f"{k}: {v}" for k, v in galat.items())
    return {"ok": False, "error": str(pesan)[:300]}


def kirim_rencana(cfg: "Config", sampai: str | None = None) -> dict:
    """Kirim isian tertunda (tabel `ekin_rencana`) ke SIMPATIK.

    Isian boleh dibuat kapan saja dari dashboard; pengiriman menunggu jendela
    input e-kin (sesudah 15.00). Setelah mengirim, tabel aktivitas disapu ulang
    dan status tiap baris ditentukan dari sumber: baris yang benar-benar muncul
    di e-kin ditandai `sent`, sisanya `error`. Jadi run beruntun tidak
    menggandakan data.
    """
    from playwright.sync_api import sync_playwright

    from . import db, recipe_ecuti

    conn = db.connect(cfg.db_path)
    try:
        sampai = sampai or date.today().isoformat()
        tertunda = db.rencana_pending(conn, BW_USER, sampai)
    finally:
        conn.close()

    if not tertunda:
        return {"ok": True, "dikirim": 0, "gagal": 0, "log": ["Tidak ada isian tertunda"]}

    login = recipe_ecuti.bitwarden_login(cfg, BW_ITEM, username=BW_USER)
    if login is None:
        return {
            "ok": False,
            "step": "login",
            "error": f"Kredensial SIMPATIK untuk akun '{BW_USER}' tidak tersedia.",
        }
    user, pwd = login

    log: list[str] = []
    gagal: list[dict] = []
    dikirim: list[dict] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            ctx = recipe_ecuti._new_context(browser)
            pg = ctx.new_page()
            pg.set_default_timeout(45000)
            _login(pg, user, pwd)
            cache: dict[str, str] = {}

            for baris in tertunda:
                hasil = _isi_satu(pg, ctx, baris["tanggal"], baris, cache)
                if hasil["ok"]:
                    dikirim.append({**baris, "waktu": hasil["waktu"]})
                else:
                    gagal.append({**baris, "error": hasil["error"]})
                    log.append(f"❌ {baris['tanggal']} {baris['uraian'][:40]}: {hasil['error'][:120]}")

            # Sumber kebenaran tetap e-kin: sapu ulang rentang yang disentuh,
            # lalu tandai baris yang benar-benar sudah tercatat.
            if tertunda:
                sejak = min(b["tanggal"] for b in tertunda)
                semua = _sapu(pg, date.fromisoformat(sejak), log)
                tercatat: dict[tuple[str, str], bool] = {
                    (b["tanggal"], _norm(b["uraian"])): True for b in semua
                }
            else:
                tercatat = {}

            ctx.close()
            browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "browser", "error": f"{type(e).__name__}: {e}", "log": log}

    conn = db.connect(cfg.db_path)
    try:
        if tercatat:
            aktif = set(tercatat)
            for b in dikirim:
                if (b["tanggal"], _norm(b["uraian"])) in aktif:
                    db.tandai_rencana(conn, b["id"], "sent")
                else:
                    db.tandai_rencana(
                        conn, b["id"], "error",
                        "Diterima e-kin tetapi belum terlihat di tabel aktivitas",
                    )
        for b in gagal:
            db.tandai_rencana(conn, b["id"], "error", b["error"][:300])
        # Simpan hasil sapuan supaya dashboard langsung memakai angka terbaru.
        if tercatat:
            sejak = min(b["tanggal"] for b in tertunda)
            db.simpan_aktivitas_ekin(conn, user, semua, sejak, sampai)
        sisa = db.rencana_pending(conn, BW_USER, sampai)
    finally:
        conn.close()

    log.append(f"Terkirim {len(dikirim)} baris · gagal {len(gagal)} · sisa tertunda {len(sisa)}")
    return {
        "ok": not gagal,
        "akun": user,
        "dikirim": len(dikirim),
        "gagal": len(gagal),
        "sisa": len(sisa),
        "detail_gagal": [{"tanggal": b["tanggal"], "uraian": b["uraian"],
                          "error": b["error"]} for b in gagal[:20]],
        "log": log,
    }


def run_ekin(cfg: "Config") -> dict:
    """Jalankan resep: login → sapu tabel → simpan → susun laporan."""
    from playwright.sync_api import sync_playwright

    from . import db, recipe_ecuti

    login = recipe_ecuti.bitwarden_login(cfg, BW_ITEM, username=BW_USER)
    if login is None:
        return {
            "ok": False,
            "step": "login",
            "error": (
                f"Kredensial SIMPATIK untuk akun '{BW_USER}' tidak tersedia "
                f"(item Bitwarden '{BW_ITEM}' atau vault terkunci)."
            ),
        }
    user, pwd = login

    sampai = batas_periksa()
    sejak = mundur_bulan(awal_bulan(sampai), BULAN_KE_BELAKANG)
    log: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            ctx = recipe_ecuti._new_context(browser)
            pg = ctx.new_page()
            pg.set_default_timeout(45000)

            log.append(f"Login SIMPATIK sebagai {user}")
            resp = pg.goto(recipe_ecuti.SIMPATIK_LOGIN, wait_until="domcontentloaded")
            body = ""
            try:
                body = pg.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
            if "hanya dapat diakses melalui Alamat IP Indonesia" in body or (
                resp is not None and resp.status == 403
            ):
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "jaringan", "log": log,
                    "error": (
                        "SIMPATIK menolak koneksi ini karena IP bukan dari Indonesia. "
                        "Ini bukan masalah kredensial."
                    ),
                }

            pg.fill("input[name=email], input[name=username], #email, #username", user)
            pg.fill("input[name=password], #password", pwd)
            pg.click("form input[type=submit], input[type=submit]")
            pg.wait_for_load_state("domcontentloaded")

            log.append("Ambil token e-kin")
            pg.goto(E_KIN_TOKEN, wait_until="domcontentloaded")
            try:
                pg.wait_for_url("**/administrator/**", timeout=20000)
            except Exception:  # noqa: BLE001
                pg.wait_for_timeout(4000)

            baris = _sapu(pg, sejak, log)
            ctx.close()
            browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "browser", "error": f"{type(e).__name__}: {e}", "log": log}

    if not baris:
        return {
            "ok": False, "step": "baca", "log": log,
            "error": "Tabel Aktivitas Harian kosong — sesi e-kin mungkin tidak terbentuk.",
        }

    conn = db.connect(cfg.db_path)
    try:
        jumlah = db.simpan_aktivitas_ekin(
            conn, user, baris, sejak.isoformat(), sampai.isoformat()
        )
        mulai = awal_bulan(sampai)
        per_hari = db.total_harian_ekin(conn, mulai.isoformat(), sampai.isoformat())
        laporan = susun_laporan(per_hari, mulai, sampai)
        ringkasan = db.ringkasan_ekin(conn)
    finally:
        conn.close()

    log.append(
        f"Tersimpan {jumlah} baris ({sejak.isoformat()} s/d {sampai.isoformat()})"
        f" · dinilai sampai {sampai.isoformat()}"
    )

    return {
        "ok": True,
        "akun": user,
        "jumlah_baris": jumlah,
        "ringkasan_db": ringkasan,
        "log": log,
        **laporan,
    }
