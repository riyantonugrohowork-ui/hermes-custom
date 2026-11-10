"""Recipe E-Cuti: login SIMPATIK → export Excel → impor ke kepegawaian.

Alur tetap (bukan ditebak LLM) karena melibatkan:
  - CSRF dinamis (harus diambil live tiap kali)
  - download file Excel
  - upload Livewire/FilePond (bukan form POST biasa)
  - OAuth Google dengan sesi tersimpan

Pemakaian (dipicu dari chat):
  "ambil rekap e-cuti"
  → otak memanggil run_ecuti(cfg) → jalankan seluruh alur.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# ---------- URL tetap alur ----------
SIMPATIK_LOGIN = "https://simpatik.semarangkota.go.id/login"
SIMPATIK_TOKEN = "http://simpatik.semarangkota.go.id/token?app=ecuti"
ECUTI_EXPORT_PAGE = "https://e-cuti.semarangkota.go.id/administrator/dashboard/export_range"
KEPEG_IMPORT = "https://kepegawaian.it-dpmptsp.my.id/kepegawaian/impor-cuti"

# Sesi Google kepegawaian (login manual sekali, dipakai ulang).
GOOGLE_STATE_FILE = "google_session.json"

# Format tanggal datepicker (sesuai form E-Cuti).
DATE_FMT = "%Y-%m-%d"  # SIMPATIK memakai format yyyy-mm-dd


def date_range(tgl_mulai: str = "", tgl_selesai: str = "") -> tuple[str, str]:
    """Rentang tanggal ekspor (yyyy-mm-dd).

    Default: hari ini - 1 bulan s/d hari ini. Nilai dari input UI menang.
    """
    today = date.today()
    prev_month = today.month - 1 or 12
    prev_year = today.year if today.month > 1 else today.year - 1
    last_day = calendar.monthrange(prev_year, prev_month)[1]
    default_start = date(prev_year, prev_month, min(today.day, last_day))
    start = tgl_mulai or default_start.strftime(DATE_FMT)
    end = tgl_selesai or today.strftime(DATE_FMT)
    return start, end


def storage_dir(cfg: "Config") -> Path:
    d = cfg.db_path.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def google_state_path(cfg: "Config") -> Path:
    return storage_dir(cfg) / GOOGLE_STATE_FILE


def bitwarden_login(cfg: "Config", item: str, username: str = "") -> tuple[str, str] | None:
    """Ambil username+password dari Bitwarden (login item).

    `username` dipakai kalau nama item dipakai beberapa akun — `bw get`
    menolak nama yang ambigu ("More than one result was found").
    Return (username, password) atau None jika tak tersedia.
    """
    import shutil
    import subprocess

    if shutil.which("bw") is None:
        return None
    if not os.environ.get("BW_SESSION"):
        return None

    if username:
        try:
            p = subprocess.run(
                ["bw", "list", "items", "--search", item],
                capture_output=True, text=True, timeout=60,
            )
            if p.returncode == 0:
                for it in json.loads(p.stdout or "[]"):
                    lg = it.get("login") or {}
                    if it.get("name") == item and lg.get("username") == username:
                        if lg.get("password"):
                            return (lg["username"], lg["password"])
        except (ValueError, subprocess.SubprocessError):
            pass
        return None

    def _get(field: str) -> str | None:
        try:
            p = subprocess.run(
                ["bw", "get", field, item],
                capture_output=True, text=True, timeout=20,
            )
            return p.stdout.strip() if p.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    user = _get("username")
    pwd = _get("password")
    if user and pwd:
        return (user, pwd)
    return None


# ---------- Browser helpers ----------

# WAF e-cuti ("http_bot_simple") memblokir headless default → pakai fingerprint realistis.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


def _new_context(browser, storage_state: str | None = None):
    """Context dengan fingerprint realistis agar lolos WAF."""
    ctx = browser.new_context(
        accept_downloads=True,
        user_agent=USER_AGENT,
        locale="id-ID",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={"Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
        storage_state=storage_state,
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return ctx


def _download_export(page, tgl_mulai: str, tgl_selesai: str, out_dir: Path) -> str:
    """Isi form export_range → klik Save → tangkap file Excel yang terunduh."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Buka dashboard dulu (akses deep-link langsung memicu WAF), lalu export.
    page.goto("https://e-cuti.semarangkota.go.id/administrator/dashboard",
              wait_until="domcontentloaded")
    page.goto(ECUTI_EXPORT_PAGE, wait_until="domcontentloaded")
    page.wait_for_selector("#form_rekap", timeout=20000)

    # Set nilai via JS (bootstrap-datepicker bisa menimpa nilai saat fill).
    page.evaluate(
        """([a,b]) => {
            const set = (id, v) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            };
            set('tanggal_mulai', a);
            set('tanggal_selesai', b);
        }""",
        [tgl_mulai, tgl_selesai],
    )
    page.wait_for_timeout(500)

    # Tombol Save adalah <input type="submit"> di dalam #form_rekap.
    with page.expect_download(timeout=90000) as dl:
        page.click("#form_rekap input[type=submit], #form_rekap button[type=submit]")
    download = dl.value
    fname = download.suggested_filename or f"rekap_cuti_{tgl_mulai}_{tgl_selesai}.xlsx"
    path = str(out_dir / fname)
    download.save_as(path)
    return path


def _urai_impor(teks: str) -> dict:
    """Urai teks hasil impor jadi angka.

    Contoh: 'Impor selesai. 0 data baru, 27 diperbarui. 1 baris dilewati
    (No. Surat kosong). 3 baris dilewati (status bukan "TTE Kepala Dinas").'
    """
    bersih = re.sub(r"\s+", " ", (teks or "")).strip()
    hasil: dict = {"teks": bersih}

    m = re.search(r"(\d+)\s+data\s+baru", bersih)
    if m:
        hasil["baru"] = int(m.group(1))
    m = re.search(r"(\d+)\s+diperbarui", bersih)
    if m:
        hasil["diperbarui"] = int(m.group(1))

    lewat = [
        {"jumlah": int(n), "alasan": re.sub(r"\s+", " ", ket).strip(" .")}
        for n, ket in re.findall(r"(\d+)\s+baris\s+dilewati\s*\(([^)]*)\)", bersih)
    ]
    if lewat:
        hasil["dilewati"] = lewat
        hasil["jumlah_dilewati"] = sum(x["jumlah"] for x in lewat)

    baru = hasil.get("baru")
    diperbarui = hasil.get("diperbarui")
    if baru is not None and diperbarui is not None:
        hasil["total"] = baru + diperbarui + hasil.get("jumlah_dilewati", 0)
    return hasil


def _import_kepegawaian(context, cfg: "Config", xlsx_path: str) -> dict:
    """Buka impor-cuti → upload Excel via FilePond → klik Mulai Impor."""
    out_dir = cfg.output_dir / "ecuti"
    page = context.new_page()
    page.set_default_timeout(30000)
    page.goto(KEPEG_IMPORT, wait_until="domcontentloaded")
    page.wait_for_selector("form.fi-form", timeout=30000)

    # FilePond dirender via JS — input file-nya tersembunyi, jadi tunggu "attached".
    try:
        page.wait_for_selector("input.filepond--browser", state="attached", timeout=30000)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "FilePond input tidak muncul (halaman impor tak termuat?)."}

    page.set_input_files("input.filepond--browser", xlsx_path)

    # Tunggu unggahan selesai: FilePond menandai item, atau indikator hilang.
    page.wait_for_timeout(5000)

    # Klik tombol "Mulai Impor".
    clicked = False
    for sel in ("button.fi-btn[type=submit]", "button:has-text('Mulai Impor')"):
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        return {"ok": False, "error": "Tombol 'Mulai Impor' tidak ditemukan."}

    # Tunggu toast hasil impor muncul (bukan sekadar timer).
    teks_impor = ""
    for _ in range(20):
        el = page.query_selector("[role=status]") or page.query_selector(".fi-no-notification")
        if el:
            teks_impor = (el.inner_text() or "").strip()
            if "Impor" in teks_impor:
                break
        page.wait_for_timeout(1000)

    try:
        page.screenshot(path=str(out_dir / "import_result.png"), full_page=True)
    except Exception:  # noqa: BLE001
        pass

    hasil = _urai_impor(teks_impor)
    if not teks_impor:
        return {"ok": True, "note": "Impor dikirim (hasil tidak terbaca)."}
    return {"ok": True, "note": "Impor selesai.", **hasil}


# ---------- Orkestrator ----------

def run_ecuti(
    cfg: "Config",
    bw_item: str = "2017skpd019",
    google_email: str = "",
    tgl_mulai: str = "",
    tgl_selesai: str = "",
) -> dict:
    """Jalankan seluruh alur E-Cuti. Return ringkasan hasil."""
    from playwright.sync_api import sync_playwright

    out_dir = cfg.output_dir / "ecuti"
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Bitwarden wajib untuk login SIMPATIK ---
    login = bitwarden_login(cfg, bw_item)
    if login is None:
        return {
            "ok": False,
            "step": "login",
            "error": (
                "Kredensial SIMPATIK tidak tersedia. Pastikan: "
                "1) Bitwarden CLI terpasang & vault terbuka (BW_SESSION aktif), "
                f"2) item '{bw_item}' berisi username+password."
            ),
        }

    tgl_mulai, tgl_selesai = date_range(tgl_mulai, tgl_selesai)
    user, pwd = login
    log: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)

            # ===== Tahap 1–3: SIMPATIK → token → e-cuti export =====
            ctx1 = _new_context(browser)
            pg = ctx1.new_page()
            pg.set_default_timeout(30000)
            log.append(f"Login SIMPATIK sebagai {user}")
            resp = pg.goto(SIMPATIK_LOGIN, wait_until="domcontentloaded")

            # Situs pemerintah sering memblokir IP luar Indonesia (geoblock).
            body_probe = ""
            try:
                body_probe = pg.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
            if "hanya dapat diakses melalui Alamat IP Indonesia" in body_probe or (
                resp is not None and resp.status == 403
            ):
                ctx1.close()
                return {
                    "ok": False,
                    "step": "jaringan",
                    "error": (
                        "SIMPATIK menolak koneksi ini karena IP bukan dari Indonesia "
                        f"(HTTP {resp.status if resp else '?'}). Jalankan Hermes dari jaringan "
                        "Indonesia, atau lewat koneksi/tunnel yang ber-IP Indonesia. "
                        "Ini bukan masalah kredensial."
                    ),
                    "log": log,
                }

            # Form login SIMPATIK (Laravel): email, password, submit = input[type=submit]
            u = pg.query_selector("input[name=email], input[name=username], #email, #username")
            pw = pg.query_selector("input[name=password], #password")
            if u is None or pw is None:
                ctx1.close()
                return {"ok": False, "step": "login",
                        "error": "Field login SIMPATIK tidak ditemukan (selector berubah?).",
                        "log": log}
            u.fill(user)
            pw.fill(pwd)
            # tombol submit adalah <input type="submit">, bukan <button>
            pg.click("form input[type=submit], input[type=submit]")
            pg.wait_for_load_state("domcontentloaded")
            pg.wait_for_timeout(1500)

            if "/login" in pg.url:
                ctx1.close()
                return {"ok": False, "step": "login",
                        "error": "Login SIMPATIK ditolak (masih di halaman login — cek kredensial).",
                        "log": log}
            log.append("Login berhasil")

            log.append("Ambil token e-cuti")
            pg.goto(SIMPATIK_TOKEN, wait_until="domcontentloaded")
            try:
                pg.wait_for_url("**/administrator/**", timeout=15000)
            except Exception:  # noqa: BLE001
                pg.wait_for_timeout(4000)
            log.append(f"Token → {pg.url[:60]}")

            log.append(f"Export rekap {tgl_mulai} s/d {tgl_selesai}")
            xlsx = _download_export(pg, tgl_mulai, tgl_selesai, out_dir)
            log.append(f"Excel terunduh: {Path(xlsx).name}")
            ctx1.close()

            # ===== Tahap 4–5: kepegawaian (sesi Google) → impor =====
            sp = google_state_path(cfg)
            if not sp.is_file():
                browser.close()
                return {
                    "ok": True,
                    "step": "export_only",
                    "xlsx": xlsx,
                    "range": f"{tgl_mulai} s/d {tgl_selesai}",
                    "log": log,
                    "warning": (
                        "Export berhasil, TAPI impor belum jalan: sesi Google kepegawaian "
                        f"belum ada. Jalankan `python -m hermes login-google` sekali untuk akun "
                        f"{google_email or 'kepegawaian'}, lalu ulangi untuk impor."
                    ),
                }
            ctx2 = _new_context(browser, storage_state=str(sp))
            imp = _import_kepegawaian(ctx2, cfg, xlsx)
            ctx2.close()
            if not imp.get("ok"):
                return {"ok": False, "step": "import", "error": imp.get("error"), "xlsx": xlsx, "log": log}
            log.append(imp.get("note") or "Impor dikirim ke kepegawaian")
            browser.close()

        hasil = {
            "ok": True,
            "step": "done",
            "xlsx": xlsx,
            "range": f"{tgl_mulai} s/d {tgl_selesai}",
            "log": log,
        }
        # Angka hasil impor (data baru/diperbarui/dilewati) untuk ringkasan.
        for k in ("baru", "diperbarui", "dilewati", "jumlah_dilewati", "total", "teks"):
            if imp.get(k) is not None:
                hasil[k] = imp[k]
        return hasil
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "unknown", "error": f"{type(e).__name__}: {e}", "log": log}
