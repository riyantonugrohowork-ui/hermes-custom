"""Recipe Penomoran Surat Cuti.

Alur:
  1. Login SIMPATIK → token e-cuti
  2. Baca tabel Pengajuan, ambil baris berstatus "Pemberian Nomor Surat OPD"
  3. Lewati baris yang No Surat-nya SUDAH terisi di SIMPATIK
  4. Untuk yang belum: hitung nomor dari Sheet, isi form, simpan
  5. Tulis balik baris ke Sheet (NOMOR, KODE 800, TANGGAL, KETERANGAN)

Langkah 3 adalah aturan penting: nomor yang sudah ada tidak pernah ditimpa.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

ECUTI_PENGAJUAN = "https://e-cuti.semarangkota.go.id/administrator/pengajuan"
KODE_CUTI = "800"

# Hanya halaman teratas yang dipindai (10 baris per halaman). Pengajuan baru
# selalu muncul di atas, jadi halaman dalam tidak perlu ikut disapu.
MAX_HALAMAN = int(os.getenv("PENOMORAN_MAX_HALAMAN", "3"))

# Status yang form-nya punya isian No Surat dan memang perlu dinomori.
# "Verifikasi Atasan" mencakup "Verifikasi Atasan Langsung" dan
# "Verifikasi Atasan Atasan Langsung".
STATUS_TARGETS = ("Pemberian Nomor Surat OPD", "Verifikasi Atasan")

# Jenis cuti yang aturannya bergantung status ASN: "Cuti Karena Alasan
# Penting" hanya untuk PNS. PPPK/PPPK Paruh Waktu tidak berhak.
JENIS_CKAP = "Cuti Karena Alasan Penting"

# Pengajuan CKAP yang masih bisa diubah statusnya. Yang sudah lewat
# (TTE Kepala Dinas, selesai, ditolak) tidak perlu diperingatkan lagi.
STATUS_MASIH_BISA_DIUBAH = ("Pengajuan", "Verifikasi Atasan")

# Jenis ASN yang TIDAK boleh mengajukan CKAP.
ASN_TIDAK_BERHAK_CKAP = ("PPPK", "PPPK Paruh Waktu")

# Jenis cuti pengganti yang dimintakan untuk CKAP milik PPPK.
JENIS_TUJUAN_CKAP_PPPK = "Cuti Tahunan"

# Baris tabel Pengajuan (index kolom sesuai <thead> di halaman).
COL_NIP = 1          # Nama | NIP | Lokasi Kerja
COL_TANGGAL = 2      # Tanggal | Jenis | Keperluan
COL_STATUS = 5       # Diajukan Pada | Status
COL_CREATED = 6      # Created At
COL_AKSI = 7


def _kunci_nama(s: str) -> str:
    return _clean(str(s or "")).upper()


def _norm_tanggal(s: str) -> str:
    """Samakan tanggal ke YYYY-MM-DD agar bisa dibandingkan."""
    s = _clean(str(s or ""))
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


def _kunci_baris(nama: str, pengajuan: str) -> str:
    """Kunci duplikat = nama + tanggal pengajuan (aturan API)."""
    return f"{_kunci_nama(nama)}|{_norm_tanggal(pengajuan)}"


def _perlu_dinomori(status: str) -> bool:
    s = _clean(status)
    return any(s.startswith(p) for p in STATUS_TARGETS)


def _status_masih_bisa_diubah(status: str) -> bool:
    """Pengajuan yang belum final — masih mungkin diubah jenisnya."""
    s = _clean(status)
    return any(s.startswith(p) for p in STATUS_MASIH_BISA_DIUBAH)


def _adalah_ckap(jenis: str) -> bool:
    return _clean(jenis).lower() == JENIS_CKAP.lower()


def format_teks_ckap(temuan: list[dict]) -> str:
    """Teks siap-copas permintaan ubah status, satu blok per pengajuan.

    Bentuknya mengikuti extension "Permintaan Ubah Status Cuti" yang biasa
    dipakai, supaya bisa langsung ditempel ke WhatsApp tanpa disunting:
    identitas pegawai, jenis cuti asal → tujuan, lalu bagian "Ket" bernomor.
    """
    blok: list[str] = []

    for t in temuan:
        nip = t.get("nip") or "-"
        tanggal = rentang_teks(t.get("tanggal_teks", "")) or "-"
        blok.append("\n".join([
            f"Status ASN: {t.get('jenis_asn') or '-'}",
            f"Nama : {t.get('nama') or '-'}",
            f"NIP : {nip}",
            f"Jenis Cuti : {JENIS_TUJUAN_CKAP_PPPK}",
            f"Alasan Cuti : {t.get('keperluan') or '-'}",
            f"Tanggal Cuti : {tanggal}",
            "",
            "Ket :",
            f"1. STATUS ASN : {t.get('jenis_asn') or '-'}",
            f"2. Jenis Cuti : {JENIS_CKAP} menjadi {JENIS_TUJUAN_CKAP_PPPK.lower()}",
            f"3. {nip}",
        ]))

    if not blok:
        return ""

    return ("\n" + "-" * 40 + "\n").join(blok) + "\n\nTerima Kasih🙏🏻"


def periksa_ckap_pppk(cfg: "Config", semua: list[dict], log: list[str] | None = None) -> list[dict]:
    """Cari pengajuan "Cuti Karena Alasan Penting" milik PPPK.

    Aturan kepegawaian: CKAP hanya untuk PNS — PPPK/PPPK Paruh Waktu tidak
    berhak. Pengajuan seperti ini tidak boleh diberi nomor; yang dibutuhkan
    adalah perubahan status, jadi temuannya dilaporkan ke Telegram.

    Status ASN diambil dari `GET /api/v1/pegawai/{nip}`, bukan ditebak dari
    NIP: hanya server yang tahu status kepegawaian terbaru. Hanya pengajuan
    yang belum final (`Pengajuan` / `Verifikasi Atasan`) yang dilaporkan —
    yang sudah TTE Kepala Dinas tidak perlu diubah lagi.

    Kegagalan cek satu NIP tidak menghentikan sisanya: NIP itu dilewati dan
    dicatat di log, karena status yang tidak diketahui tidak boleh dianggap
    PPPK (maupun PNS).
    """
    from . import kepeg

    log = log if log is not None else []
    temuan: list[dict] = []
    sudah_dicek: dict[str, str] = {}

    for r in semua:
        if not _adalah_ckap(r.get("jenis", "")):
            continue
        if not _status_masih_bisa_diubah(r.get("status", "")):
            continue

        nip = r.get("nip", "")
        if not nip:
            log.append(f"CKAP {r.get('nama', '?')}: NIP kosong, status ASN tak bisa dicek")
            continue

        if nip not in sudah_dicek:
            try:
                sudah_dicek[nip] = kepeg.jenis_asn(cfg, nip)
            except Exception as e:  # noqa: BLE001
                log.append(f"CKAP {r.get('nama', '?')}: profil gagal ({type(e).__name__})")
                sudah_dicek[nip] = ""

        jenis_asn = sudah_dicek[nip]
        if jenis_asn not in ASN_TIDAK_BERHAK_CKAP:
            continue

        temuan.append({**r, "jenis_asn": jenis_asn})
        log.append(
            f"CKAP oleh {jenis_asn}: {r.get('nama', '?')} "
            f"(status {r.get('status', '?')}) — perlu ubah status"
        )

    return temuan


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_baris_teks(teks: str) -> dict:
    """Pecah teks satu baris tabel jadi nama/nip/jenis/rentang/keperluan.

    Format sumber (dipisah newline di dalam satu sel):
      SUZAN MAHARANI, S.T
      198304242014032002
      Dinas Penanaman Modal ...
    """
    lines = [_clean(x) for x in (teks or "").split("\n") if _clean(x)]
    nama = lines[0] if lines else ""
    nip = ""
    for x in lines[1:]:
        if x.isdigit():
            nip = x
            break
    return {"nama": nama, "nip": nip}


def hitung_hari(teks: str) -> int:
    """Jumlah hari kalender inklusif (dipakai hanya untuk tampilan lokal).

    Catatan: API menghitung ulang durasi sebagai HARI KERJA (Sabtu/Minggu
    dan hari libur dilewati), jadi angka itu yang dipakai di Sheet.
    """
    m = re.findall(r"(\d{4}-\d{2}-\d{2})", teks or "")
    if not m:
        return 1
    try:
        a = datetime.strptime(m[0], "%Y-%m-%d").date()
        b = datetime.strptime(m[1], "%Y-%m-%d").date() if len(m) > 1 else a
    except ValueError:
        return 1
    return max(1, (b - a).days + 1)


def _parse_rentang(teks: str) -> tuple[str, str]:
    """'2026-11-23 SD 2026-11-23' → ('2026-11-23','2026-11-23')."""
    m = re.findall(r"(\d{4}-\d{2}-\d{2})", teks or "")
    if not m:
        return "", ""
    return m[0], (m[1] if len(m) > 1 else m[0])


def rentang_teks(mulai: str) -> str:
    """'2026-11-23 SD 2026-11-23' → '23 November 2026'."""
    m = re.findall(r"(\d{4}-\d{2}-\d{2})", mulai or "")
    if not m:
        return ""
    a = datetime.strptime(m[0], "%Y-%m-%d").date()
    b = datetime.strptime(m[1], "%Y-%m-%d").date() if len(m) > 1 else a
    bulan = [
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    ]
    if a == b:
        return f"{a.day} {bulan[a.month - 1]} {a.year}"
    return f"{a.day:02d} - {b.day:02d} {bulan[b.month - 1]} {b.year}"


def _baca_tabel(page) -> list[dict]:
    """Ambil semua baris tabel Pengajuan yang bisa dibaca."""
    rows = []
    trs = page.query_selector_all("#tbody_pengajuan tr")
    for tr in trs:
        tds = tr.query_selector_all("td")
        if len(tds) <= COL_AKSI:
            continue
        teks_nama = tds[COL_NIP].inner_text()
        teks_tgl = tds[COL_TANGGAL].inner_text()
        status = _clean(tds[COL_STATUS].inner_text())

        ident = ""
        cb = tr.query_selector("input[name='id[]']")
        if cb:
            ident = cb.get_attribute("value") or ""

        link = ""
        a = tr.query_selector(f"td:nth-child({COL_AKSI + 1}) a[href*='pengajuan_tentukan_tanggal']")
        if a:
            link = a.get_attribute("href") or ""

        info = parse_baris_teks(teks_nama)
        tg = [_clean(x) for x in teks_tgl.split("\n") if _clean(x)]
        baris_created = tds[COL_CREATED].inner_text()
        rows.append({
            "id": ident,
            "status": status,
            "link": link,
            **info,
            "tanggal_teks": tg[0] if tg else "",
            "jenis": tg[1] if len(tg) > 1 else "",
            "keperluan": tg[2] if len(tg) > 2 else "",
            "diajukan": _normal_diajukan(baris_created),
        })
    return rows


def _normal_diajukan(teks: str) -> str:
    """'10-11-2026 09:45:36' → '2026-11-10 09:45:36' (format API)."""
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2}:\d{2})", teks or "")
    if not m:
        return ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}"


ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")


def _nomor_surat(urut: int) -> str:
    """1274 → '1274/800/XI/2026' memakai bulan & tahun TERBIT (hari ini).

    API menyusun sufiks dari tanggal CUTI, jadi cuti tahun lalu menghasilkan
    '1274/800/IX/2025'. Nomor surat diterbitkan hari ini, jadi sufiksnya
    disusun ulang di sini.
    """
    hari_ini = date.today()
    return f"{urut}/{KODE_CUTI}/{ROMAN[hari_ini.month - 1]}/{hari_ini.year}"


def _tahun_cuti(teks: str) -> int | None:
    """Tahun mulai cuti dari '2025-11-19 SD 2025-11-20'. None bila tak terbaca."""
    mulai, _ = _parse_rentang(teks)
    if not mulai:
        return None
    try:
        return datetime.strptime(mulai, "%Y-%m-%d").year
    except ValueError:
        return None


def _buka_form(page, url: str):
    """Buka form penomoran satu baris. None bila form tidak terbuka."""
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#no_surat", timeout=20000)
    except Exception:  # noqa: BLE001
        return None
    return page


def _nomor_sekarang(page) -> str:
    try:
        return (page.input_value("#no_surat") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _simpan_nomor(page, url: str, no_surat: str, sisa: dict) -> tuple[bool, str]:
    """Buka form baris ini, isi No Surat + sisa cuti, lalu simpan.

    Wajib navigasi ke `url` tiap kali: mengisi tanpa navigasi membuat nomor
    tertulis ke baris yang sedang terbuka (pernah terjadi, nomor nyasar).
    """
    if _buka_form(page, url) is None:
        return False, "form tidak terbuka"

    # Pastikan form yang terbuka memang milik baris ini (id di action form).
    ident = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    action = ""
    el = page.query_selector("#form_pengajuan")
    if el:
        action = el.get_attribute("action") or ""
    if ident and ident not in action:
        return False, f"form bukan milik {ident} (action={action})"

    if _nomor_sekarang(page):
        return False, "sudah bernomor"

    page.fill("#no_surat", no_surat)
    for field, key in (
        ("#sisa_cuti", "now"),
        ("#sisa_cuti_n1", "n1"),
        ("#sisa_cuti_n2", "n2"),
    ):
        try:
            page.fill(field, str(sisa.get(key, 0)))
        except Exception:  # noqa: BLE001
            pass

    # Pastikan nilai benar-benar masuk sebelum menyimpan.
    if _nomor_sekarang(page) != no_surat:
        return False, "nilai no_surat tidak terpasang"

    try:
        page.click(".btn_save_back")
    except Exception:  # noqa: BLE001
        page.click("#btn_save")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)
    return True, ""


def run_penomoran(
    cfg: "Config",
    bw_item: str = "2017skpd019",
    dry_run: str = "",
    abaikan_gate: bool = False,
) -> dict:
    """Jalankan resep penomoran surat cuti."""
    from playwright.sync_api import sync_playwright

    from . import kepeg, recipe_ecuti, sheets

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
    mode_dry = str(dry_run).lower() in ("1", "true", "yes", "ya")
    log: list[str] = []
    terisi: list[dict] = []
    dilewati: list[dict] = []
    ditulis_sheet: list[list[str]] = []

    try:
        tab = sheets.tab_bulan()
        nomor_awal, tab = sheets.next_number(cfg, tab)
        log.append(f"Nomor awal dari Sheet '{tab}': {nomor_awal}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            ctx = recipe_ecuti._new_context(browser)
            pg = ctx.new_page()
            pg.set_default_timeout(30000)

            # --- login SIMPATIK → token ---
            log.append(f"Login SIMPATIK sebagai {user}")
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
                    "error": (
                        "SIMPATIK menolak IP non-Indonesia. Ini bukan masalah kredensial."
                    ),
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
                        "error": "Login SIMPATIK ditolak."}

            pg.goto(recipe_ecuti.SIMPATIK_TOKEN, wait_until="domcontentloaded")
            try:
                pg.wait_for_url("**/administrator/**", timeout=15000)
            except Exception:  # noqa: BLE001
                pg.wait_for_timeout(4000)
            log.append("Masuk e-cuti")

            # --- baca tabel pengajuan (hanya halaman teratas) ---
            # e-cuti memakai offset: /pengajuan/index/10, /index/20, ...
            # Dibaca terbatas MAX_HALAMAN supaya tidak menyapu seluruh riwayat.
            semua: list[dict] = []
            terlihat: set[str] = set()
            url = ECUTI_PENGAJUAN
            for hal in range(1, MAX_HALAMAN + 1):
                pg.goto(url, wait_until="domcontentloaded")
                try:
                    pg.wait_for_selector("#tbody_pengajuan tr", timeout=15000)
                except Exception:  # noqa: BLE001
                    break

                halaman = _baca_tabel(pg)
                baru = [r for r in halaman if r["id"] not in terlihat]
                for r in baru:
                    terlihat.add(r["id"])
                semua.extend(baru)
                if not baru or hal == MAX_HALAMAN:
                    break  # halaman berulang / batas halaman tercapai

                nx = pg.query_selector("a[rel='next']")
                nxt = (nx.get_attribute("href") or "").strip() if nx else ""
                if not nxt or nxt in ("#", "") or nxt == url:
                    break
                url = nxt
            log.append(f"Baris pengajuan terbaca: {len(semua)} (maks {MAX_HALAMAN} halaman)")

            # CKAP oleh PPPK tidak boleh diberi nomor — yang dibutuhkan
            # perubahan status. Diperiksa lebih dulu supaya barisnya tidak
            # ikut masuk daftar nomor.
            ckap_pppk = periksa_ckap_pppk(cfg, semua, log)
            nip_ckap = {r.get("nip") for r in ckap_pppk}
            teks_ckap = format_teks_ckap(ckap_pppk)

            target = [
                r for r in semua
                if _perlu_dinomori(r["status"]) and r["link"]
                and r.get("nip") not in nip_ckap
            ]
            log.append(f"Baris perlu dinomori ({', '.join(STATUS_TARGETS)}): {len(target)}")

            # Lewati yang sudah bernomor SEBELUM meminta nomor baru, supaya
            # catatan yang sudah ada tidak pernah ditimpa.
            perlu: list[dict] = []
            for r in target:
                H = _buka_form(pg, r["link"])
                if H is None:
                    dilewati.append({**r, "nomor": "", "alasan": "form tidak terbuka"})
                    log.append(f"Lewati {r['nama']} — form tidak terbuka")
                    continue
                nomor_lama = _nomor_sekarang(pg)
                if nomor_lama:
                    dilewati.append({**r, "nomor": nomor_lama})
                    log.append(f"Lewati {r['nama']} — sudah bernomor {nomor_lama}")
                    continue

                tahun = _tahun_cuti(r["tanggal_teks"])
                if tahun is not None and tahun != date.today().year:
                    dilewati.append({**r, "nomor": "", "alasan": f"cuti tahun {tahun}"})
                    log.append(
                        f"Lewati {r['nama']} — cuti tahun {tahun}, bukan "
                        f"{date.today().year}"
                    )
                    continue

                perlu.append(r)

            if not perlu:
                ctx.close()
                browser.close()
                return {
                    "ok": True, "step": "done", "terisi": 0,
                    "dilewati": len(dilewati), "nomor_berikut": nomor_awal,
                    "detail_terisi": [], "detail_dilewati": dilewati,
                    "ckap_pppk": ckap_pppk,
                    "teks_ckap": teks_ckap,
                    "log": log + ["Tidak ada yang perlu diberi nomor."],
                }

            # Simulasi: jangan sentuh API/SIMPATIK/Sheet sama sekali.
            # (POST API juga menulis arsip, jadi mode ini benar-benar read-only.)
            if mode_dry:
                for i, r in enumerate(perlu):
                    terisi.append({**r, "nomor": f"(simulasi) {nomor_awal + i}"})
                ctx.close()
                browser.close()
                return {
                    "ok": True, "step": "dry_run", "terisi": len(terisi),
                    "dilewati": len(dilewati), "nomor_berikut": nomor_awal + len(perlu),
                    "detail_terisi": terisi, "detail_dilewati": dilewati,
                    "ckap_pppk": ckap_pppk,
                    "teks_ckap": teks_ckap,
                    "log": log + ["Mode simulasi: tidak ada yang diubah."],
                }

            # Satu panggilan API untuk semua baris: nomor + sisa cuti.
            # Aturan docs: `hari` diabaikan (dihitung ulang sebagai hari kerja),
            # duplikat tidak diberi nomor & tidak memotong sisa cuti.
            batch = ""
            peta: dict[str, dict] = {}
            try:
                jawab = kepeg.generate(
                    cfg,
                    [{
                        "nama": r["nama"],
                        "jenis": r["jenis"],
                        "tanggal_mulai": _parse_rentang(r["tanggal_teks"])[0],
                        "tanggal_selesai": _parse_rentang(r["tanggal_teks"])[1],
                        "pengajuan_at": r.get("diajukan", ""),
                    } for r in perlu],
                    kode=KODE_CUTI,
                    nomor_awal=nomor_awal,
                    abaikan_gate=abaikan_gate,
                )
                batch = jawab.get("batch") or ""
                for h in jawab.get("hasil") or []:
                    peta[_kunci_baris(h.get("nama", ""), h.get("pengajuan_at", ""))] = h
                log.append(
                    f"API batch {batch}: {jawab.get('jumlah_bernomor', 0)} bernomor, "
                    f"{jawab.get('jumlah_duplikat', 0)} duplikat"
                )
            except Exception as e:  # noqa: BLE001
                ctx.close()
                browser.close()
                return {"ok": False, "step": "api", "log": log,
                        "error": f"API kepegawaian gagal: {e}"}

            nomor = nomor_awal
            terpakai: list[dict] = []
            for r in perlu:
                h = peta.get(_kunci_baris(r["nama"], r.get("diajukan", "")))
                if h is None:
                    h = peta.get(_kunci_baris(r["nama"], "")) or {}

                if h.get("duplikat"):
                    dilewati.append({**r, "nomor": "DUPLIKAT", "alasan": "duplikat"})
                    log.append(f"Lewati {r['nama']} — duplikat (sudah pernah dinomori)")
                    continue

                no_surat = _nomor_surat(nomor)

                n, n1, n2 = kepeg.sisa_urut(h.get("sisa"))
                sisa = {"now": n, "n1": n1, "n2": n2}

                # Satu baris gagal tidak boleh menghentikan sisanya.
                try:
                    ok_simpan, alasan = _simpan_nomor(pg, r["link"], no_surat, sisa)
                except Exception as e:  # noqa: BLE001
                    ok_simpan, alasan = False, f"{type(e).__name__}: {e}"

                if not ok_simpan:
                    dilewati.append({**r, "nomor": no_surat, "alasan": alasan})
                    log.append(f"GAGAL isi {r['nama']} → {no_surat}: {alasan}")
                    continue

                terisi.append({**r, "nomor": no_surat, "sisa": sisa})
                terpakai.append(h)
                log.append(f"Isi {r['nama']} → {no_surat} (sisa {n}/{n1}/{n2})")

                # Pakai hari & rentang versi API (hari kerja).
                hari = h.get("hari")
                rentang = h.get("rentang") or rentang_teks(r["tanggal_teks"])
                ditulis_sheet.append([
                    no_surat.split("/")[0],
                    KODE_CUTI,
                    sheets.tanggal_id(),
                    f"{r['jenis']} {r['nama']} {hari} hari ({rentang})",
                    "",
                ])
                nomor += 1
            ctx.close()
            browser.close()

        # Hapus hanya rincian arsip yang nomornya TIDAK jadi dipakai, supaya
        # penomoran berikutnya tidak melompat.
        if batch and terpakai:
            dipakai = {_kunci_baris(h.get("nama", ""), h.get("pengajuan_at", ""))
                       for h in terpakai}
            sisa_id = [h["id"] for h in peta.values()
                       if h.get("id") is not None
                       and _kunci_baris(h.get("nama", ""), h.get("pengajuan_at", "")) not in dipakai]
            if sisa_id:
                try:
                    kepeg.hapus_id(cfg, sisa_id)
                    log.append(f"Arsip tak terpakai dihapus: {sisa_id}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"Gagal hapus arsip {sisa_id}: {e}")
        elif batch and not terpakai:
            try:
                kepeg.hapus_batch(cfg, batch)
                log.append(f"Batch {batch} dihapus — tidak ada nomor yang terpakai.")
            except Exception as e:  # noqa: BLE001
                log.append(f"Batch {batch} GAGAL dihapus ({e}) — hapus manual.")

        # Sheet penomoran tetap ditulis dari sini: mirror milik API berada di
        # spreadsheet terpisah, sedangkan register nomor manual ada di Sheet ini.
        if ditulis_sheet:
            sheets.append_rows(cfg, tab, ditulis_sheet)
            log.append(f"Sheet '{tab}': {len(ditulis_sheet)} baris ditambahkan")

        return {
            "ok": True,
            "step": "done",
            "terisi": len(terisi),
            "dilewati": len(dilewati),
            "nomor_berikut": nomor,
            "batch": batch,
            "detail_terisi": terisi,
            "detail_dilewati": dilewati,
            "ckap_pppk": ckap_pppk,
            "teks_ckap": teks_ckap,
            "log": log,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "step": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "log": log,
        }
