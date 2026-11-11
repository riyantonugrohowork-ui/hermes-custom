"""Recipe data pegawai: SISDM → JSON → impor ke kepegawaian.

Alur:
  1. Login SIMPATIK (kredensial Bitwarden)
  2. Token SSO `/token?app=sisdm` → masuk portal SISDM
  3. /pegawai: pilih OPD + Cari, tampilkan **semua** baris, saring "Aktif"
  4. Untuk tiap pegawai aktif, ambil profil + seluruh sub-halaman riwayat
  5. Simpan JSON ke `output/pegawai/`, lalu kirim ke `POST /api/v1/impor-pegawai`

Pemilihan OPD memakai `kd_skpd` (kode, bukan nama) karena nama OPD bisa
berubah penulisannya. Nilainya bisa ditimpa lewat `SISDM_KD_SKPD`.

Pengambilan halaman memakai `ctx.request` (bukan navigasi `page.goto`) supaya
cookie sesi ikut terpakai tanpa memuat aset CSS/JS — puluhan pegawai ×
belasan halaman berarti ribuan request, dan memuat aset akan sangat lambat.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

SISDM_BASE = "https://sisdm.semarangkota.go.id"
SISDM_TOKEN = "http://simpatik.semarangkota.go.id/token?app=sisdm"
SISDM_PEGAWAI = f"{SISDM_BASE}/pegawai"

# kd_skpd Dinas Penanaman Modal dan Pelayanan Terpadu Satu Pintu.
KD_SKPD = "019"
STATUS_AKTIF = "Aktif"

# Kolom tabel /pegawai (0-based): 1=NIP, 2=Nama, 4=Pangkat, 11=Kedudukan.
COL_NIP, COL_NAMA, COL_PANGKAT, COL_KEDUDUKAN = 1, 2, 4, 11

# Batas aman satu request impor (API menolak > 2000 record per panggilan).
BATCH_IMPOR = 100


def _adalah_pppk(status_pegawai: str) -> bool:
    """True untuk PPPK (termasuk paruh waktu).

    SISDM menulis status ini lengkap — "PEGAWAI PEMERINTAH DENGAN PERJANJIAN
    KERJA" — bukan singkatan "PPPK", jadi keduanya harus dikenali. Salah
    mendeteksi berarti riwayat PPPK tidak terambil sekaligus riwayat
    KGB/pangkat yang tidak berlaku ikut terambil.
    """
    s = (status_pegawai or "").upper()
    return "PPPK" in s or "PERJANJIAN KERJA" in s


def _log(log: list[str], pesan: str) -> None:
    log.append(pesan)


def _baca_tabel(baris) -> list[dict]:
    """Ambil pegawai aktif dari baris tabel /pegawai (sudah di halaman browser)."""
    hasil = []
    for tr in baris:
        cells = tr.query_selector_all("td")
        if len(cells) <= COL_KEDUDUKAN:
            continue

        kedudukan = (cells[COL_KEDUDUKAN].inner_text() or "").strip()
        if kedudukan != STATUS_AKTIF:
            continue

        a = tr.query_selector('a[href*="/pegawai/"][title="Lihat"]')
        if a is None:
            continue

        hasil.append({
            "nip_tabel_utama": (cells[COL_NIP].inner_text() or "").strip(),
            "nama_tabel_utama": (cells[COL_NAMA].inner_text() or "").strip(),
            "pangkat_tabel_utama": (cells[COL_PANGKAT].inner_text() or "").strip(),
            "url": a.get_attribute("href") or "",
        })
    return hasil


def _ambil_teks(ctx, url: str, log: list[str]) -> str:
    """GET satu halaman lewat konteks browser (cookie sesi ikut).

    Mengembalikan "" bila gagal, supaya satu halaman rusak tidak
    menggagalkan seluruh pegawai.
    """
    try:
        r = ctx.request.get(url, timeout=60000)
        if r.status != 200:
            _log(log, f"  ⚠️ {url.rsplit('/', 1)[-1]}: HTTP {r.status}")
            return ""
        return r.text()
    except Exception as e:  # noqa: BLE001
        _log(log, f"  ⚠️ {url.rsplit('/', 1)[-1]}: {type(e).__name__}")
        return ""


def _kumpulkan_pegawai(ctx, meta: dict, log: list[str]) -> dict:
    """Ambil profil + seluruh riwayat satu pegawai → satu record impor."""
    from . import sisdm_pegawai as S

    url = meta["url"]
    base = url.rstrip("/")

    profil_html = _ambil_teks(ctx, url, log)
    if not profil_html:
        return {}
    profil = S.parse_profil(profil_html)

    alamat = {}
    if profil.get("address_edit_url"):
        html_alamat = _ambil_teks(ctx, profil["address_edit_url"], log)
        if html_alamat:
            alamat = S.parse_alamat(html_alamat)
    profil.pop("address_edit_url", None)

    anak = S.parse_anak(_ambil_teks(ctx, f"{base}/riwayat_anak", log))

    status = (profil.get("status_pegawai") or "").upper()
    pppk = _adalah_pppk(status)

    penghargaan = S.parse_penghargaan(
        _ambil_teks(ctx, f"{base}/riwayat_tanda_jasa", log)
    )
    jabatan = S.parse_jabatan(
        _ambil_teks(ctx, f"{base}/riwayat_jabatan", log)
    )

    # KGB & pangkat tidak berlaku untuk PPPK.
    kgb: dict = {}
    pangkat: list[dict] = []
    if not pppk:
        kgb = S.parse_kgb(_ambil_teks(ctx, f"{base}/riwayat_kgb", log))
        pangkat = S.parse_pangkat(_ambil_teks(ctx, f"{base}/riwayat_pangkat", log))

    # Kompetensi: daftar hanya memuat ringkasan, detailnya di form edit.
    html_komp = _ambil_teks(ctx, f"{base}/riwayat_kompetensi", log)
    kompetensi = [
        S.parse_kompetensi_form(_ambil_teks(ctx, href, log))
        for href in S.tautan_edit(html_komp, "riwayat_kompetensi")
    ]
    kompetensi = [k for k in kompetensi if k]

    # CPNS: satu baris per pegawai. Halaman riwayat_cpns kadang langsung
    # berisi form (tanpa tautan edit), jadi hasil halaman itu ikut dipakai.
    html_cpns = _ambil_teks(ctx, f"{base}/riwayat_cpns", log)
    tautan_cpns = S.tautan_edit(html_cpns, "riwayat_cpns")
    if tautan_cpns:
        cpns = [S.parse_cpns_form(_ambil_teks(ctx, h, log)) for h in tautan_cpns]
        cpns = [c for c in cpns if c]
    else:
        tertanam = S.parse_cpns_form(html_cpns)
        cpns = [tertanam] if tertanam and (tertanam.get("nomor_sk") or tertanam.get("tmt_sk")) else []

    # PPPK: daftar + detail form editnya.
    pppk_list: list[dict] = []
    pppk_detail: list[dict] = []
    if pppk:
        html_pppk = _ambil_teks(ctx, f"{base}/riwayat_pppk", log)
        pppk_list = S.parse_pppk(html_pppk)
        for href in S.tautan_edit(html_pppk, "riwayat_pppk"):
            detail = S.parse_pppk_form(_ambil_teks(ctx, href, log))
            if detail:
                pppk_detail.append(detail)

    return {
        "data_tabel_utama": {
            "nip_tabel_utama": meta["nip_tabel_utama"],
            "nama_tabel_utama": meta["nama_tabel_utama"],
            "pangkat_tabel_utama": meta["pangkat_tabel_utama"],
        },
        "detail_profil": profil,
        "data_alamat": alamat,
        "riwayat_anak": anak,
        "riwayat_penghargaan": penghargaan,
        "riwayat_kgb": kgb,
        "riwayat_kompetensi": kompetensi,
        "riwayat_cpns": cpns,
        "riwayat_jabatan": jabatan,
        "riwayat_pangkat": pangkat,
        "riwayat_pppk": pppk_list,
        "riwayat_pppk_detail": pppk_detail,
        "sumber_url": url,
    }


def _nama_berkas() -> str:
    return f"data_pegawai_aktif_{date.today():%Y-%m-%d}.json"


def run_pegawai(cfg: "Config", bw_item: str = "2017skpd019", kd_skpd: str = "") -> dict:
    """Jalankan resep ekstraksi data pegawai aktif lalu impor ke kepegawaian."""
    from playwright.sync_api import sync_playwright

    from . import kepeg, recipe_ecuti

    kode_opd = (kd_skpd or "").strip() or KD_SKPD

    login = recipe_ecuti.bitwarden_login(cfg, bw_item)
    if login is None:
        return {
            "ok": False,
            "step": "login",
            "error": (
                "Kredensial SIMPATIK tidak tersedia (item Bitwarden "
                f"'{bw_item}' atau vault terkunci)."
            ),
        }

    user, pwd = login
    log: list[str] = []
    mulai = datetime.now()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            ctx = recipe_ecuti._new_context(browser)
            pg = ctx.new_page()
            pg.set_default_timeout(60000)

            # --- login SIMPATIK → token SISDM ---
            _log(log, f"Login SIMPATIK sebagai {user}")
            resp = pg.goto(recipe_ecuti.SIMPATIK_LOGIN, wait_until="domcontentloaded")
            probe = ""
            try:
                probe = pg.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
            if "hanya dapat diakses melalui Alamat IP Indonesia" in probe or (
                resp is not None and resp.status == 403
            ):
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "jaringan", "log": log,
                    "error": "SIMPATIK menolak IP non-Indonesia. Ini bukan masalah kredensial.",
                }

            u = pg.query_selector("input[name=email], input[name=username], #email, #username")
            pw = pg.query_selector("input[name=password], #password")
            if u is None or pw is None:
                ctx.close()
                browser.close()
                return {"ok": False, "step": "login", "log": log,
                        "error": "Field login SIMPATIK tidak ditemukan."}
            u.fill(user)
            pw.fill(pwd)
            pg.click("form input[type=submit], input[type=submit]")
            pg.wait_for_load_state("domcontentloaded")
            pg.wait_for_timeout(1500)
            if "/login" in pg.url:
                ctx.close()
                browser.close()
                return {"ok": False, "step": "login", "log": log,
                        "error": "Login SIMPATIK ditolak (masih di halaman login)."}

            pg.goto(SISDM_TOKEN, wait_until="domcontentloaded")
            try:
                pg.wait_for_url("**sisdm.semarangkota.go.id/**", timeout=20000)
            except Exception:  # noqa: BLE001
                pg.wait_for_timeout(4000)
            if "sisdm.semarangkota.go.id" not in pg.url:
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "token", "log": log,
                    "error": f"Token SISDM tidak mengarah ke portal (url={pg.url[:80]}).",
                }
            _log(log, "Masuk portal SISDM")

            # --- filter OPD lalu tampilkan semua ---
            pg.goto(SISDM_PEGAWAI, wait_until="domcontentloaded")
            try:
                pg.wait_for_selector("#kd_skpd", timeout=30000)
            except Exception:  # noqa: BLE001
                ctx.close()
                browser.close()
                return {"ok": False, "step": "pegawai", "log": log,
                        "error": "Form filter /pegawai tidak muncul."}

            pg.select_option("#kd_skpd", kode_opd)
            pg.wait_for_timeout(1000)
            pg.click("#btn-filter")
            try:
                pg.wait_for_selector("#pegawai-table_info", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
            pg.wait_for_timeout(1500)
            _log(log, f"Filter OPD '{kode_opd}': {pg.inner_text('#pegawai-table_info') if pg.query_selector('#pegawai-table_info') else '?'}")

            # Tampilkan semua baris (DataTables: -1 = All), lalu tunggu penuh.
            try:
                pg.select_option("#pegawai-table_length select", "-1")
            except Exception:  # noqa: BLE001
                _log(log, "⚠️ Pilihan 'All' tidak tersedia — memakai halaman pertama")
            pg.wait_for_timeout(3000)

            baris = pg.query_selector_all("#pegawai-table tbody tr")
            semua = _baca_tabel(baris)
            _log(log, f"Pegawai berstatus '{STATUS_AKTIF}': {len(semua)}")

            if not semua:
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "pegawai", "log": log,
                    "error": "Tidak ada pegawai aktif terbaca (filter OPD kosong?).",
                }

            # --- ambil detail tiap pegawai ---
            records: list[dict] = []
            gagal: list[str] = []
            hitung = {"pns": 0, "pppk": 0}
            for i, meta in enumerate(semua, 1):
                try:
                    rec = _kumpulkan_pegawai(ctx, meta, log)
                except Exception as e:  # noqa: BLE001
                    rec = {}
                    _log(log, f"  ⚠️ {meta['nip_tabel_utama']}: {type(e).__name__}: {e}")
                if rec:
                    records.append(rec)
                    status = (rec.get("detail_profil") or {}).get("status_pegawai", "")
                    hitung["pppk" if _adalah_pppk(status) else "pns"] += 1
                    _log(log, f"  [{i}/{len(semua)}] {meta['nama_tabel_utama'][:45]}")
                else:
                    gagal.append(meta["nip_tabel_utama"])

            ctx.close()
            browser.close()

        if not records:
            return {
                "ok": False, "step": "ekstrak", "log": log,
                "error": "Tidak ada record pegawai yang berhasil diekstrak.",
            }

        # --- simpan JSON mentah ---
        out_dir = cfg.output_dir / "pegawai"
        out_dir.mkdir(parents=True, exist_ok=True)
        jalur = out_dir / _nama_berkas()
        jalur.write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        _log(log, f"JSON tersimpan: {jalur.name} ({len(records)} record)")

        # --- impor bertahap ---
        dibuat = diperbarui = 0
        galat_api: list = []
        for awal in range(0, len(records), BATCH_IMPOR):
            potong = records[awal:awal + BATCH_IMPOR]
            jawab = kepeg.impor_pegawai(cfg, potong)
            dibuat += int(jawab.get("created") or 0)
            diperbarui += int(jawab.get("updated") or 0)
            galat_api.extend(jawab.get("errors") or [])
            _log(
                log,
                f"Impor {awal + 1}-{awal + len(potong)}: "
                f"{jawab.get('created', 0)} baru, {jawab.get('updated', 0)} diperbarui",
            )

        durasi = int((datetime.now() - mulai).total_seconds())
        _log(log, f"Selesai dalam {durasi} detik")
        return {
            "ok": True,
            "step": "done",
            "jumlah": len(records),
            "pns": hitung["pns"],
            "pppk": hitung["pppk"],
            "created": dibuat,
            "updated": diperbarui,
            "gagal": len(gagal),
            "detail_gagal": gagal,
            "errors": galat_api[:10],
            "json": str(jalur),
            "durasi_detik": durasi,
            "log": log,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "step": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "log": log,
        }
