"""Recipe DUK: login SIMPATIK → token SISDM → baca /duk → impor JSON.

Alur tetap (bukan ditebak LLM) karena melibatkan:
  - login SIMPATIK via kredensial Bitwarden
  - token SSO `/token?app=sisdm` yang mengarahkan ke portal SISDM
  - halaman `/duk` adalah dokumen cetak tanpa form, jadi tabelnya dibaca
    langsung dari HTML (bukan dicetak PDF lalu di-parse ulang)
  - kirim baris terstruktur ke `POST /api/v1/impor-duk-json`

Kenapa JSON, bukan PDF: impor PDF harus menebak kolom dari koordinat teks
(`pdftotext -bbox`), dan tebakan itu meleset untuk baris tertentu. Di sini
kolom sudah pasti karena dibaca dari `<td>` aslinya, jadi tidak ada lagi
penebakan di sisi server.

Halaman /duk tidak punya filter periode; yang terbaca adalah DUK berjalan
saat resep dijalankan. Impor menyimpan batch baru dan tidak menghapus batch
lama, jadi riwayat tetap utuh.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

SISDM_DUK = "https://sisdm.semarangkota.go.id/duk"
SISDM_TOKEN = "http://simpatik.semarangkota.go.id/token?app=sisdm"

# Batas waktu halaman DUK memuat seluruh baris.
DUK_READY_TIMEOUT = 30000


def _nama_berkas() -> str:
    return f"DUK-{date.today():%Y-%m-%d}.json"


def run_duk(cfg: "Config", bw_item: str = "2017skpd019") -> dict:
    """Jalankan resep DUK: login → baca tabel → impor JSON."""
    import json

    from playwright.sync_api import sync_playwright

    from . import duk_parse, kepeg, recipe_ecuti

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
            # Dibaca lewat ctx.request (cookie sesi ikut) supaya HTML-nya utuh
            # tanpa perlu menunggu aset halaman. Isi respons diambil SEBELUM
            # konteks ditutup — `Response.text()` sesudah `ctx.close()` gagal
            # dengan "Event loop is closed".
            r = ctx.request.get(SISDM_DUK, timeout=60000)
            html_duk = r.text() if r.status == 200 else ""
            status_duk = r.status
            ctx.close()
            browser.close()

        if status_duk != 200:
            return {
                "ok": False, "step": "duk", "log": log,
                "error": f"Halaman DUK menolak akses (HTTP {status_duk}).",
            }

        hasil = duk_parse.parse_duk(html_duk)
        log.append(f"Halaman DUK terbaca: {hasil['jumlah']} baris pegawai")

        if hasil["jumlah"] == 0:
            return {
                "ok": False, "step": "duk", "log": log,
                "error": "Tabel DUK tidak berisi baris pegawai (halaman berubah / sesi ditolak).",
            }

        json_path = out_dir / _nama_berkas()
        json_path.write_text(
            json.dumps(hasil, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        log.append(f"JSON tersimpan: {json_path.name}")

        # --- impor lewat API JSON ---
        imp = kepeg.impor_duk_json(
            cfg,
            rows=hasil["rows"],
            opd=hasil["opd"],
            periode=hasil["periode"],
        )
        log.append(
            f"Impor batch {imp.get('batch_id')}: {imp.get('jumlah', 0)} baris"
        )

        dilewati = imp.get("dilewati") or 0
        if dilewati:
            log.append(f"Dilewati server: {dilewati} baris")

        return {
            "ok": True,
            "step": "done",
            "json": str(json_path),
            "batch_id": imp.get("batch_id"),
            "jumlah": imp.get("jumlah", 0),
            "dilewati": dilewati,
            "errors": imp.get("errors") or [],
            "periode": imp.get("periode") or "",
            "opd": imp.get("opd") or "",
            "berkas": imp.get("original_filename") or json_path.name,
            "log": log,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "step": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "log": log,
        }
