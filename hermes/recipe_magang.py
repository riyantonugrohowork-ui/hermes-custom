"""Resep: pantau pengajuan magang baru di portal izin.semarangkota.go.id.

Login pakai kredensial Bitwarden, buka /magang/admin-index, lalu laporkan
permohonan yang statusnya "Pengajuan Baru" dan belum pernah dilaporkan.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

BASE = "https://izin.semarangkota.go.id"
LOGIN_URL = f"{BASE}/login"
MAGANG_URL = f"{BASE}/magang/admin-index"
BW_ITEM = "izin.semarangkota.go.id"
BW_USER = "riyantoSimonev"
STATUS_BARU = "Pengajuan Baru"

MAX_HALAMAN = 10


def _state_path(cfg: "Config") -> Path:
    return cfg.db_path.parent / "magang_seen.json"


def _muat_state(cfg: "Config") -> set[str]:
    p = _state_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("dilihat") or [])
    except Exception:  # noqa: BLE001
        return set()


def _simpan_state(cfg: "Config", dilihat: set[str]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"dilihat": sorted(dilihat)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _baca_baris(page) -> list[dict]:
    """Ambil semua baris tabel permohonan."""
    hasil: list[dict] = []
    for tr in page.query_selector_all("table tbody tr"):
        tds = tr.query_selector_all("td")
        if len(tds) < 6:
            continue

        teks = [_clean(td.inner_text()) for td in tds]
        kode = teks[1]
        if not kode.upper().startswith("MGNG"):
            continue

        link = ""
        for a in tds[-1].query_selector_all("a"):
            href = a.get_attribute("href") or ""
            # /magang/11 → detail; /magang/update/11 → form edit.
            if re.fullmatch(r"/magang/\d+", href):
                link = BASE + href
                break

        hasil.append({
            "kode": kode,
            "pj": teks[2].split("\n")[0],
            "kampus": teks[3],
            "periode": teks[4],
            "status": teks[5],
            "link": link,
        })
    return hasil


def _detail_peserta(page, url: str) -> list[str]:
    """Nama + jurusan peserta dari halaman detail."""
    if not url:
        return []
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        return []

    nama: list[str] = []
    for tr in page.query_selector_all("table tr"):
        sel = tr.query_selector_all("td")
        if len(sel) < 2:
            continue
        no = _clean(sel[0].inner_text())
        isi = _clean(sel[1].inner_text())
        if no.isdigit() and isi:
            nama.append(isi)
    return nama


def run_magang(cfg: "Config", bw_item: str = BW_ITEM, bw_user: str = BW_USER) -> dict:
    """Cek pengajuan magang baru; laporkan yang belum pernah dilihat."""
    from playwright.sync_api import sync_playwright

    from . import recipe_ecuti

    log: list[str] = []
    login = recipe_ecuti.bitwarden_login(cfg, bw_item, username=bw_user)
    if login is None:
        return {
            "ok": False,
            "step": "login",
            "error": (
                f"Kredensial Bitwarden '{bw_item}' (user {bw_user}) tidak tersedia "
                "atau vault terkunci."
            ),
        }
    user, pwd = login

    dilihat = _muat_state(cfg)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=recipe_ecuti.LAUNCH_ARGS)
            page = recipe_ecuti._new_context(browser).new_page()
            page.set_default_timeout(30000)

            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.fill("#loginform-username", user)
            page.fill("#loginform-password", pwd)
            page.click("button[name=login-button]")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2500)
            if "/login" in page.url:
                browser.close()
                return {
                    "ok": False, "step": "login",
                    "error": f"Login gagal untuk {user} (masih di halaman login).",
                    "log": log,
                }
            log.append(f"Login {user} berhasil")

            semua: list[dict] = []
            terlihat: set[str] = set()
            for hal in range(1, MAX_HALAMAN + 1):
                url = MAGANG_URL if hal == 1 else f"{MAGANG_URL}?page={hal}"
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector("table tbody tr", timeout=15000)
                except Exception:  # noqa: BLE001
                    break
                baru = [r for r in _baca_baris(page) if r["kode"] not in terlihat]
                if not baru:
                    break
                for r in baru:
                    terlihat.add(r["kode"])
                semua.extend(baru)
            log.append(f"Permohonan terbaca: {len(semua)}")

            pengajuan = [r for r in semua if r["status"] == STATUS_BARU]
            segar = [r for r in pengajuan if r["kode"] not in dilihat]
            log.append(
                f"Status '{STATUS_BARU}': {len(pengajuan)} "
                f"({len(segar)} belum pernah dilaporkan)"
            )

            # Detail peserta hanya untuk yang benar-benar baru (hemat waktu).
            for r in segar[:10]:
                r["peserta"] = _detail_peserta(page, r["link"])

            browser.close()

        if segar:
            _simpan_state(cfg, dilihat | {r["kode"] for r in segar})

        return {
            "ok": True,
            "step": "done",
            "baru": len(segar),
            "total_pengajuan": len(pengajuan),
            "total": len(semua),
            "detail_baru": segar,
            "detail_lama": [r for r in pengajuan if r["kode"] in dilihat],
            "log": log,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "step": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "log": log,
        }
