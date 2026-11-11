"""Recipe DUK: login SIMPATIK → token SISDM → cetak /duk jadi PDF → impor API.

Alur tetap (bukan ditebak LLM) karena melibatkan:
  - login SIMPATIK via kredensial Bitwarden
  - token SSO `/token?app=sisdm` yang mengarahkan ke portal SISDM
  - halaman `/duk` adalah dokumen cetak (tanpa form), jadi PDF diambil
    dengan `page.pdf()` bukan tombol unduh
  - unggah berkas ke `POST /api/v1/impor-duk` (multipart, X-API-Key)

Halaman /duk tidak punya filter periode; yang tercetak adalah DUK berjalan
saat resep dijalankan. Impor menyimpan batch baru dan tidak menghapus batch
lama, jadi riwayat tetap utuh.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

SISDM_DUK = "https://sisdm.semarangkota.go.id/duk"
SISDM_TOKEN = "http://simpatik.semarangkota.go.id/token?app=sisdm"

# Batas waktu halaman DUK memuat seluruh baris sebelum dicetak.
DUK_READY_TIMEOUT = 30000


def _nama_berkas() -> str:
    return f"DUK-{date.today():%Y-%m-%d}.pdf"


def _cetak_pdf(page, path: Path) -> None:
    """Cetak halaman DUK ke PDF ber-text-layer (bukan hasil scan).

    `print_background` agar garis tabel ikut; ukuran A4 lanskap karena kolom
    DUK lebar (NO, NAMA, NIP, PANGKAT, JABATAN, MASA KERJA, PENDIDIKAN).
    """
    page.pdf(
        path=str(path),
        format="A4",
        landscape=True,
        print_background=True,
        margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
    )


def run_duk(cfg: "Config", bw_item: str = "2017skpd019") -> dict:
    """Jalankan resep DUK: login → cetak PDF → impor ke kepegawaian."""
    from playwright.sync_api import sync_playwright

    from . import kepeg, recipe_ecuti

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
    out_dir = cfg.output_dir / "duk"
    out_dir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            ctx = recipe_ecuti._new_context(browser)
            pg = ctx.new_page()
            pg.set_default_timeout(30000)

            # --- login SIMPATIK ---
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
                        "error": "Login SIMPATIK ditolak (masih di halaman login)."}
            log.append("Login berhasil")

            # --- token SISDM ---
            log.append("Ambil token SISDM")
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
            log.append(f"Token → {pg.url[:60]}")

            # --- halaman DUK ---
            pg.goto(SISDM_DUK, wait_until="domcontentloaded")
            try:
                pg.wait_for_selector("table tr", timeout=DUK_READY_TIMEOUT)
            except Exception:  # noqa: BLE001
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "duk", "log": log,
                    "error": "Tabel DUK tidak muncul (halaman berubah / sesi ditolak).",
                }
            pg.wait_for_timeout(2000)  # beri waktu baris terakhir ter-render

            baris = len(pg.query_selector_all("table tr"))
            log.append(f"Halaman DUK terbuka: {baris} baris tabel")
            if baris < 2:
                ctx.close()
                browser.close()
                return {
                    "ok": False, "step": "duk", "log": log,
                    "error": "Tabel DUK kosong — PDF tidak dicetak.",
                }

            pdf_path = out_dir / _nama_berkas()
            _cetak_pdf(pg, pdf_path)
            ctx.close()
            browser.close()

        ukuran = pdf_path.stat().st_size
        log.append(f"PDF tercetak: {pdf_path.name} ({ukuran // 1024} KB)")
        if ukuran < 1024:
            return {"ok": False, "step": "pdf", "pdf": str(pdf_path), "log": log,
                    "error": f"PDF terlalu kecil ({ukuran} byte) — kemungkinan kosong."}

        # --- impor lewat API ---
        imp = kepeg.impor_duk(cfg, str(pdf_path))
        log.append(
            f"Impor batch {imp.get('batch_id')}: {imp.get('jumlah', 0)} baris"
        )

        return {
            "ok": True,
            "step": "done",
            "pdf": str(pdf_path),
            "batch_id": imp.get("batch_id"),
            "jumlah": imp.get("jumlah", 0),
            "periode": imp.get("periode") or "",
            "opd": imp.get("opd") or "",
            "berkas": imp.get("berkas") or pdf_path.name,
            "log": log,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "step": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "log": log,
        }
