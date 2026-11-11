"""Resep: cek presensi harian pegawai di absen.semarangkota.go.id.

Alur:
  1. Login (captcha dijawab manusia lewat Telegram; tanpa OCR, tanpa browser)
  2. Ambil token OPD dari /presensi/rekap-harian
  3. POST /web-service/get-jam-kerja -> id_jam_kerja untuk tanggal itu
  4. GET /web-service/list-pegawai-asn-all-harian -> presensi semua pegawai
  5. Laporkan pegawai yang masih "KONFIRMASI"

**BACA SAJA.** Modul ini tidak boleh mengisi atau menghapus presensi apa pun.
Endpoint yang dipakai hanya:
  GET  /home                                     cek sesi masih hidup
  GET  /presensi/rekap-harian                    token CSRF + daftar OPD
  GET  /presensi/rekap-harian/asn/<kode>         nama OPD untuk parameter
  POST /web-service/get-jam-kerja                query jam kerja (baca)
  GET  /web-service/list-pegawai-asn-all-harian  rekap presensi (baca)
`get-jam-kerja` memakai POST hanya karena bentuk API-nya begitu; isinya query,
bukan perubahan data. Jangan menambah panggilan ke tombol/endpoint KONFIRMASI,
simpan, atau hapus, meski terlihat memungkinkan.

Situs ini Laravel: setiap POST butuh `_token` dari halaman, dan captcha terikat
pada `laravel_session` — gambar dan POST wajib satu sesi.

Catatan penting soal kredensial: username `dpmptsp@semarangkota.go.id` dipakai
lima item Bitwarden berbeda (absen, bkpm, sisdmk, silintas, next-gen). Mencari
lewat username saja pernah memilih item yang salah dan membuat login selalu
ditolak dengan pesan "Username, Password atau Captcha Tidak Sesuai" — pesan itu
menyembunyikan penyebabnya. Karena itu item dicari lewat NAMA PERSIS.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .config import Config

BASE = "https://absen.semarangkota.go.id"
LOGIN = f"{BASE}/login"
REKAP_HARIAN = f"{BASE}/presensi/rekap-harian"

# Nama item Bitwarden. Jangan ganti dengan pencarian username (lihat docstring).
BW_ITEM = "absen.semarangkota.go.id"

# NIP yang dilaporkan. Bisa ditimpa lewat env ABSEN_NIP.
NIP_DEFAULT = "199303252025061002"

# Batas percobaan captcha per sesi login. Lebih dari ini berisiko rate-limit.
MAKS_CAPTCHA = 3
# Jawaban captcha yang lebih tua dari ini diabaikan (sudah tidak relevan).
BATAS_JAWABAN_DETIK = 600

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

STATE_KEY = "absen_session"
PENDING_KEY = "absen_captcha_pending"
# Jawaban captcha yang ditulis bot Telegram. Resep membacanya dari sini, bukan
# dari getUpdates — dua pembaca getUpdates akan berebut pesan yang sama.
JAWABAN_KEY = "absen_captcha_jawaban"


def _log(log: list[str], pesan: str) -> None:
    log.append(pesan)


# ---------- Kredensial ----------

def kredensial(item: str = BW_ITEM) -> tuple[str, str] | None:
    """Login absen dari Bitwarden, dicari lewat NAMA item persis."""
    import os

    if not os.environ.get("BW_SESSION"):
        from . import autopilot

        if not autopilot.refresh_bw_session_if_needed(force=True):
            return None
    try:
        p = subprocess.run(
            ["bw", "get", "item", item],
            capture_output=True, text=True, timeout=90,
        )
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout or "{}")
    except (ValueError, subprocess.SubprocessError):
        return None
    lg = data.get("login") or {}
    if lg.get("username") and lg.get("password"):
        return lg["username"], lg["password"]
    return None


# ---------- Sesi ----------

def _sesi_baru() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9"})
    return s


def _token(html: str) -> str:
    m = re.search(r'var\s+token\s*=\s*"([^"]+)"', html or "")
    return m.group(1) if m else ""


def _simpan_sesi(cfg: "Config", sesi: requests.Session, user: str) -> None:
    jalur = cfg.db_path.parent / "absen_session.json"
    jalur.parent.mkdir(parents=True, exist_ok=True)
    jalur.write_text(json.dumps({
        "cookies": sesi.cookies.get_dict(),
        "user": user,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }), encoding="utf-8")
    try:
        jalur.chmod(0o600)
    except OSError:
        pass


def _muat_sesi(cfg: "Config") -> requests.Session | None:
    jalur = cfg.db_path.parent / "absen_session.json"
    if not jalur.exists():
        return None
    try:
        data = json.loads(jalur.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    s = _sesi_baru()
    for k, v in (data.get("cookies") or {}).items():
        s.cookies.set(k, v, domain="absen.semarangkota.go.id")
    return s


def sesi_aktif(cfg: "Config") -> requests.Session | None:
    """Sesi tersimpan yang masih hidup, atau None kalau sudah mati."""
    s = _muat_sesi(cfg)
    if s is None:
        return None
    try:
        r = s.get(f"{BASE}/home", timeout=45)
    except requests.RequestException:
        return None
    if "/login" in r.url or r.status_code != 200:
        return None
    return s


# ---------- Login dengan captcha lewat Telegram ----------

def _kirim_foto(cfg: "Config", data: bytes, caption: str) -> bool:
    from . import telegram_client

    if not cfg.telegram_bot_token or not cfg.telegram_allowed:
        return False
    return bool(telegram_client.send_photo(
        cfg, cfg.telegram_allowed[0], data, caption=caption,
    ).get("ok"))


def _kirim_teks(cfg: "Config", teks: str) -> None:
    from . import telegram_client

    for chat in cfg.telegram_allowed or []:
        try:
            telegram_client.send_message(cfg, chat, teks)
        except Exception:  # noqa: BLE001
            pass


def _tulis_pending(conn, aktif: bool) -> None:
    from . import db

    db.set_note(conn, PENDING_KEY,
                json.dumps({"aktif": aktif,
                            "mulai": datetime.now().isoformat(timespec="seconds")}))


def ambil_jawaban(conn) -> dict | None:
    """Jawaban captcha yang ditulis bot, kalau masih segar."""
    from . import db

    raw = db.get_note(conn, JAWABAN_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    try:
        umur = (datetime.now() - datetime.fromisoformat(data["at"])).total_seconds()
    except (KeyError, ValueError):
        return None
    if umur > BATAS_JAWABAN_DETIK:
        return None
    return data


def tulis_jawaban(conn, teks: str) -> None:
    """Dipanggil bot: simpan jawaban captcha untuk dibaca resep."""
    from . import db

    db.set_note(conn, JAWABAN_KEY,
                json.dumps({"teks": teks,
                            "at": datetime.now().isoformat(timespec="seconds")}))


def pending_aktif(conn) -> bool:
    from . import db

    raw = db.get_note(conn, PENDING_KEY)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not data.get("aktif"):
        return False
    try:
        umur = (datetime.now() - datetime.fromisoformat(data["mulai"])).total_seconds()
    except (KeyError, ValueError):
        return False
    return umur <= BATAS_JAWABAN_DETIK * 3


def _tunggu_jawaban(cfg: "Config", detik: int) -> str:
    """Tunggu jawaban yang ditulis bot ke DB.

    Bot adalah satu-satunya pembaca `getUpdates`. Kalau resep ikut polling,
    keduanya berebut pesan yang sama dan jawaban captcha bisa hilang di salah
    satu pihak — gejalanya "captcha tidak pernah dijawab" padahal user sudah
    membalas. Jadi jalur ini lewat DB.
    """
    from . import db as _db

    mulai = time.time()
    while time.time() - mulai < detik:
        try:
            conn = _db.connect(cfg.db_path)
            try:
                data = ambil_jawaban(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            data = None
        if data and data.get("teks"):
            return str(data["teks"])
        time.sleep(2)
    return ""


def login_dengan_captcha(cfg: "Config", detik_tunggu: int = 600
                         ) -> tuple[requests.Session, str, list[str]] | None:
    """Login: kirim gambar captcha ke Telegram, tunggu jawaban, POST /login.

    Return (sesi, user, log) atau None kalau gagal.
    """
    from . import db

    log: list[str] = []
    kred = kredensial()
    if not kred:
        _log(log, f"Kredensial item '{BW_ITEM}' tidak terbaca (vault terkunci?)")
        return None
    user, pwd = kred
    _log(log, f"Kredensial: {user} ({len(pwd)} karakter)")

    s = _sesi_baru()

    # Hapus jawaban lama supaya yang terbaca benar-benar jawaban baru.
    try:
        conn = db.connect(cfg.db_path)
        try:
            db.set_note(conn, JAWABAN_KEY, "")
            _tulis_pending(conn, True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    try:
        return _login_loop(cfg, s, user, pwd, log, detik_tunggu)
    finally:
        try:
            conn = db.connect(cfg.db_path)
            try:
                _tulis_pending(conn, False)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass


def _login_loop(cfg: "Config", s: requests.Session, user: str, pwd: str,
                log: list[str], detik_tunggu: int
                ) -> tuple[requests.Session, str, list[str]] | None:
    for i in range(1, MAKS_CAPTCHA + 1):
        try:
            html = s.get(LOGIN, timeout=60).text
        except requests.RequestException as e:
            _log(log, f"GET /login gagal: {type(e).__name__}")
            return None
        tok = _token(html)
        if not tok:
            _log(log, "Token CSRF tidak ditemukan di halaman login")
            return None

        try:
            img = s.get(f"{BASE}/captcha/default?{time.time()}", timeout=60).content
        except requests.RequestException as e:
            _log(log, f"Captcha gagal diambil: {type(e).__name__}")
            return None

        if not _kirim_foto(cfg, img, f"🔐 Captcha absen #{i}/{MAKS_CAPTCHA} — balas kodenya"):
            _log(log, "Gagal mengirim captcha ke Telegram")
            return None
        _log(log, f"Captcha #{i} dikirim ke Telegram, menunggu jawaban")

        jawab = _tunggu_jawaban(cfg, detik_tunggu)
        if not jawab:
            _log(log, "Tidak ada jawaban captcha")
            return None

        try:
            r = s.post(
                LOGIN,
                data={"email": user, "password": pwd,
                      "captcha": jawab, "_token": tok},
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": LOGIN, "Origin": BASE},
                timeout=60,
            )
            hasil = r.json()
        except (requests.RequestException, ValueError):
            _log(log, "Respons login tidak bisa dibaca")
            return None

        if hasil.get("success"):
            _log(log, f"Login berhasil ({hasil.get('message') or 'ok'})")
            _simpan_sesi(cfg, s, user)
            _kirim_teks(cfg, "✅ Login absen berhasil, sesi disimpan.")
            return s, user, log

        pesan = str(hasil.get("message") or "ditolak")
        _log(log, f"Captcha #{i} ditolak: {pesan}")
        _kirim_teks(cfg, f"❌ Captcha ditolak: {pesan}")

    return None


def pastikan_sesi(cfg: "Config") -> tuple[requests.Session, str, list[str]] | None:
    """Pakai sesi tersimpan bila masih hidup, kalau tidak login ulang."""
    s = sesi_aktif(cfg)
    if s is not None:
        return s, "", []
    return login_dengan_captcha(cfg)


# ---------- Data presensi ----------

def _token_opd(s: requests.Session, log: list[str]) -> tuple[str, str]:
    """Token CSRF halaman rekap + token OPD untuk path ASN."""
    html = s.get(REKAP_HARIAN, timeout=60).text
    tok = _token(html)
    data = s.get(
        REKAP_HARIAN,
        params={
            "draw": 1, "start": 0, "length": 200,
            "search[value]": "",
            "order[0][column]": 1, "order[0][dir]": "asc",
            "columns[0][data]": "id_opd",
            "columns[1][data]": "nama_opd",
            "columns[2][data]": "kode_opd",
        },
        headers={"_token": tok, "_req_from": "BE",
                 "X-Requested-With": "XMLHttpRequest"},
        timeout=60,
    )
    try:
        rows = data.json().get("aaData") or []
    except ValueError:
        _log(log, "Daftar OPD tidak bisa dibaca")
        return tok, ""
    for r in rows:
        if "PENANAMAN MODAL" in str(r.get("nama_opd") or "").upper():
            return tok, str(r.get("kode_opd_token") or "")
    _log(log, f"OPD tidak ditemukan di {len(rows)} baris")
    return tok, ""


def presensi_harian(cfg: "Config", tanggal: str = "") -> dict:
    """Presensi semua ASN satu OPD pada satu tanggal.

    `tanggal` format dd-mm-yyyy. Kosong = hari ini.
    """
    log: list[str] = []
    tgl = tanggal or date.today().strftime("%d-%m-%Y")

    sesi = pastikan_sesi(cfg)
    if sesi is None:
        return {"ok": False, "step": "login", "log": log,
                "error": "Login absen gagal (captcha tidak terjawab / kredensial salah)."}
    s, _user, log_login = sesi
    log.extend(log_login)

    tok, kode_opd = _token_opd(s, log)
    if not kode_opd:
        return {"ok": False, "step": "opd", "log": log,
                "error": "Token OPD tidak ditemukan."}

    # Nama OPD dipakai apa adanya oleh endpoint jam kerja & rekap.
    try:
        halaman = s.get(f"{BASE}/presensi/rekap-harian/asn/{kode_opd}", timeout=60)
        opd = re.search(r"var\s+opd\s*=\s*'([^']+)'", halaman.text)
        opd = opd.group(1) if opd else ""
        tok = _token(halaman.text) or tok
    except requests.RequestException:
        opd = ""
    if not opd:
        opd = "DINAS PENANAMAN MODAL DAN PELAYANAN TERPADU SATU PINTU"
    _log(log, f"OPD: {opd} · tanggal {tgl}")

    try:
        jk = s.post(
            f"{BASE}/web-service/get-jam-kerja",
            data={"_token": tok, "tanggal": tgl, "opd": opd},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": REKAP_HARIAN},
            timeout=60,
        ).json()
    except (requests.RequestException, ValueError):
        return {"ok": False, "step": "jam-kerja", "log": log,
                "error": "Jam kerja tidak bisa dibaca (sesi habis?)."}
    if not jk.get("success"):
        return {"ok": False, "step": "jam-kerja", "log": log,
                "error": str(jk.get("message") or "get-jam-kerja ditolak")[:200]}

    daftar_jam = jk.get("data") or []
    if not daftar_jam:
        return {"ok": False, "step": "jam-kerja", "log": log,
                "error": f"Tidak ada jam kerja untuk {tgl} (hari libur?)."}
    id_jam = str(daftar_jam[0].get("id") or "")
    _log(log, f"jam kerja: {daftar_jam[0].get('jam_masuk')} - {daftar_jam[0].get('jam_pulang')} "
              f"({daftar_jam[0].get('alias')})")

    try:
        r = s.get(
            f"{BASE}/web-service/list-pegawai-asn-all-harian",
            params={"_token": tok, "opd": opd, "tanggal": tgl,
                    "id_jam_kerja": id_jam},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": REKAP_HARIAN},
            timeout=120,
        )
        hasil = json.loads(r.text)
        if isinstance(hasil, str):
            hasil = json.loads(hasil)
    except (requests.RequestException, ValueError):
        return {"ok": False, "step": "rekap", "log": log,
                "error": "Data rekap tidak bisa dibaca."}

    if not hasil.get("success"):
        return {"ok": False, "step": "rekap", "log": log,
                "error": "Rekap ditolak server."}

    baris = hasil.get("data") or []
    _log(log, f"{len(baris)} pegawai terbaca")

    def kosong(v) -> bool:
        return v is None or str(v).strip() in ("", "-")

    belum_hadir = [b for b in baris if kosong(b.get("absen_hadir"))]
    belum_pulang = [b for b in baris if kosong(b.get("absen_pulang"))]

    return {
        "ok": True,
        "step": "done",
        "tanggal": tgl,
        "opd": opd,
        "total": len(baris),
        "belum_hadir": len(belum_hadir),
        "belum_pulang": len(belum_pulang),
        "detail_belum_hadir": [
            {"nip": b.get("nip"), "nama": b.get("nama")} for b in belum_hadir
        ],
        "detail_belum_pulang": [
            {"nip": b.get("nip"), "nama": b.get("nama")} for b in belum_pulang
        ],
        "pegawai": baris,
        "log": log,
    }


def cek_presensi(cfg: "Config", sesi_shift: str = "") -> dict:
    """Recipe: laporkan presensi SATU pegawai (Riyanto Nugroho).

    `sesi_shift`: `hadir` (pagi) atau `pulang` (sore). Kosong = ditentukan jam.
    Notifikasi hanya dikirim kalau presensinya belum ada — yang sudah absen
    tidak pernah dilaporkan ulang, jadi run 07:50 lalu 07:55 tidak dobel.
    """
    log: list[str] = []
    jam = datetime.now().hour
    shift = (sesi_shift or "").strip().lower()
    if shift not in ("hadir", "pulang"):
        shift = "hadir" if jam < 12 else "pulang"

    nip = _nip_target()
    hasil = presensi_harian(cfg)
    if not hasil.get("ok"):
        hasil["log"] = list(hasil.get("log") or []) + log
        return hasil

    baris = hasil.get("pegawai") or []
    target = [b for b in baris if str(b.get("nip") or "").strip() == nip]

    # Jangan pernah jatuh ke "semua pegawai" — resep ini khusus satu orang.
    # Kalau barisnya tidak ada, itu masalah data yang harus terlihat, bukan
    # alasan melaporkan 58 nama lain.
    if not target:
        return {
            "ok": False, "step": "target", "log": hasil.get("log") or [],
            "error": f"NIP {nip} tidak ada di rekap ({len(baris)} pegawai terbaca).",
        }

    orang = target[0]
    if shift == "hadir":
        belum = _kosong(orang.get("absen_hadir"))
        label = "belum absen HADIR"
        jam_tercatat = orang.get("absen_hadir")
    else:
        belum = _kosong(orang.get("absen_pulang"))
        label = "belum absen PULANG"
        jam_tercatat = orang.get("absen_pulang")

    _simpan_ke_db(cfg, hasil["tanggal"], target, shift)
    if not belum:
        return {
            "ok": True, "step": "done", "shift": shift,
            "tanggal": hasil["tanggal"], "sudah_absen": True,
            "nama": orang.get("nama"), "nip": nip,
            "jam": jam_tercatat, "jumlah_kosong": 0,
            "log": hasil.get("log") or [],
        }

    return {
        "ok": True,
        "step": "done",
        "shift": shift,
        "tanggal": hasil["tanggal"],
        "opd": hasil.get("opd"),
        "sudah_absen": False,
        "nama": orang.get("nama"),
        "nip": nip,
        "label": label,
        "jumlah_kosong": 1,
        "detail_kosong": [{"nip": nip, "nama": orang.get("nama")}],
        "log": hasil.get("log") or [],
    }


def _kosong(v) -> bool:
    return v is None or str(v).strip() in ("", "-")


def _nip_target() -> str:
    import os

    return os.getenv("ABSEN_NIP", NIP_DEFAULT).strip() or NIP_DEFAULT


def _simpan_ke_db(cfg: "Config", tanggal: str, baris: list[dict], shift: str) -> None:
    from . import db

    try:
        conn = db.connect(cfg.db_path)
        try:
            db.simpan_absen_presensi(conn, tanggal, baris, shift)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
