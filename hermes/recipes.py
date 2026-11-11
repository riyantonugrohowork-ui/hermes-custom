"""Registry alur (recipe) Hermes.

Recipe = wujud utama Hermes. Setiap recipe mendeklarasikan:
  - steps   : langkah yang dijalankan
  - inputs  : field yang HARUS diisi manusia (kosong = jalan tanpa tanya apa pun)
  - schedule: cron agar berjalan sendiri (None = hanya manual)

Dashboard hanya merender daftar ini; tidak ada yang perlu tahu isi recipe.
"""
from __future__ import annotations

import html
import logging
import os
import sqlite3
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("hermes.recipes")

# Toast default: kalau recipe punya input bertipe date, isi otomatis.
RECIPES: list[dict] = [
    {
        "key": "ecuti_rekap",
        "title": "Rekap E-Cuti",
        "description": "Export rekap cuti dari E-Cuti lalu impor ke kepegawaian. Tanpa input: sebulan terakhir.",
        "triggers": ["rekap e-cuti", "export rekap cuti", "impor cuti"],
        "steps": [
            "Login SIMPATIK (Bitwarden)",
            "Ambil token aplikasi e-cuti",
            "Export Excel rentang tanggal",
            "Buka kepegawaian (sesi Google)",
            "Impor Excel",
        ],
        "inputs": [
            {
                "name": "tanggal_mulai",
                "label": "Tanggal mulai",
                "type": "date",
                "required": False,
                "help": "Kosongkan untuk otomatis: sebulan terakhir.",
            },
            {
                "name": "tanggal_selesai",
                "label": "Tanggal selesai",
                "type": "date",
                "required": False,
                "help": "Kosongkan untuk otomatis: hari ini.",
            },
            {
                "name": "bw_item",
                "label": "Item Bitwarden",
                "type": "text",
                "required": False,
                "default": "2017skpd019",
                "help": "Nama item login SIMPATIK di vault.",
            },
        ],
        # Butuh kredensial dari vault: dibuka otomatis sebelum alur berjalan.
        "needs_vault": True,
        # Kirim ringkasan ke Telegram (hanya kalau ada file/gagal).
        "notify": True,
        # Jadwal diambil dari env agar .env lama tetap berlaku.
        "schedule_env": ("ECUTI_DAILY_AT", "0 {hour} * * *"),
        "run_on_start_env": "ECUTI_ON_START",
    },
    {
        "key": "penomoran_cuti",
        "title": "Penomoran Surat Cuti",
        "description": (
            "Isi No Surat + sisa cuti untuk pengajuan berstatus "
            "'Pemberian Nomor Surat OPD', lalu catat nomornya ke Google Sheet."
        ),
        "triggers": ["nomor surat cuti", "penomoran cuti", "isi nomor surat"],
        "steps": [
            "Login SIMPATIK (Bitwarden)",
            "Baca tabel Pengajuan e-cuti",
            "Lewati yang sudah bernomor",
            "Ambil sisa cuti dari API kepegawaian",
            "Isi No Surat di SIMPATIK",
            "Catat nomor ke Google Sheet",
        ],
        # Tanpa input: cukup satu tombol "Jalankan", tanpa dialog.
        "inputs": [],
        "needs_vault": True,
        # Kirim ringkasan ke Telegram, tapi hanya kalau ada nomor terisi/gagal.
        "notify": True,
        # Tiap 2 jam. Aman diulang: yang sudah bernomor & cuti tahun lalu dilewati.
        "schedule": "0 */2 * * *",
    },
    {
        "key": "magang_baru",
        "title": "Cek Pengajuan Magang",
        "description": (
            "Pantau izin.semarangkota.go.id: laporkan permohonan magang "
            "berstatus 'Pengajuan Baru' yang belum pernah dilaporkan."
        ),
        "triggers": ["magang", "pengajuan magang", "permohonan magang"],
        "steps": [
            "Login portal izin (Bitwarden)",
            "Buka Kelola Data Magang",
            "Baca status permohonan",
            "Lewati yang sudah dilaporkan",
            "Ambil detail peserta",
        ],
        # Tanpa input: cukup satu tombol "Jalankan", tanpa dialog.
        "inputs": [],
        "needs_vault": True,
        # Kirim ke Telegram hanya bila ada pengajuan baru.
        "notify": True,
        "schedule": "0 8 * * *",
    },
    {
        "key": "duk_mingguan",
        "title": "Impor DUK Mingguan",
        "description": (
            "Cetak Daftar Urut Kepangkatan dari SISDM lalu impor ke "
            "kepegawaian sebagai batch baru."
        ),
        "triggers": ["duk", "daftar urut kepangkatan", "impor duk"],
        "steps": [
            "Login SIMPATIK (Bitwarden)",
            "Ambil token SISDM",
            "Cetak halaman DUK ke PDF",
            "Unggah ke API impor-duk",
        ],
        # Tanpa input: satu tombol "Jalankan", tanpa dialog.
        "inputs": [],
        "needs_vault": True,
        "notify": True,
        # Senin 07:00, seminggu sekali. Batch lama tidak dihapus oleh API,
        # jadi menjalankan ulang aman (hanya menambah batch).
        "schedule": "0 7 * * 1",
    },
    {
        "key": "pegawai_harian",
        "title": "Data Pegawai Aktif",
        "description": (
            "Ekstrak profil + riwayat pegawai aktif dari SISDM, lalu impor "
            "ke kepegawaian sebagai snapshot baru."
        ),
        "triggers": ["data pegawai", "pegawai aktif", "impor pegawai"],
        "steps": [
            "Login SIMPATIK (Bitwarden)",
            "Ambil token SISDM",
            "Filter OPD + tampilkan semua pegawai",
            "Ekstrak profil & riwayat tiap pegawai aktif",
            "Unggah ke API impor-pegawai",
        ],
        "inputs": [
            {
                "name": "kd_skpd",
                "label": "Kode OPD",
                "type": "text",
                "required": False,
                "help": "Kosongkan untuk 019 (Dinas Penanaman Modal dan Pelayanan Terpadu Satu Pintu).",
            },
        ],
        "needs_vault": True,
        "notify": True,
        # Jumat 07:00, seminggu sekali.
        "schedule": "0 7 * * 5",
    },
    {
        "key": "bersih_output",
        "title": "Bersihkan Berkas Unduhan",
        "description": (
            "Hapus hasil unduhan lama di folder output agar tidak menumpuk. "
            "Berkas yang lebih baru dari batas retensi dibiarkan."
        ),
        "triggers": ["bersihkan output", "hapus berkas lama", "bersihkan unduhan"],
        "steps": [
            "Pindai folder output",
            "Hapus berkas melewati batas retensi",
            "Laporkan jumlah yang dihapus",
        ],
        "inputs": [
            {
                "name": "retensi",
                "label": "Retensi (hari)",
                "type": "number",
                "required": False,
                "help": "Kosongkan untuk memakai HERMES_OUTPUT_RETENSI_HARI (default 7).",
            },
        ],
        # Tidak menyentuh portal mana pun — cukup filesystem lokal.
        "needs_vault": False,
        "notify": True,
        # Minggu 23:00: bersihkan setelah semua alur mingguan selesai.
        "schedule": "0 23 * * 0",
    },
]

_BY_KEY = {r["key"]: r for r in RECIPES}


def get_recipe(key: str) -> dict | None:
    return _BY_KEY.get(key)


def _env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in ("1", "true", "yes", "on")


def schedule_of(recipe: dict) -> str | None:
    """Ekspresi cron recipe (dari deklarasi langsung atau env)."""
    if recipe.get("schedule"):
        return recipe["schedule"]
    env = recipe.get("schedule_env")
    if not env:
        return None
    var, pattern = env
    raw = os.getenv(var, "").strip()
    if not raw:
        return None
    try:
        hour, _, minute = raw.partition(":")
        return pattern.format(hour=int(hour), minute=int(minute or 0))
    except (ValueError, KeyError):
        return None


def runs_on_start(recipe: dict) -> bool:
    var = recipe.get("run_on_start_env")
    return _env_flag(var) if var else False


# ---------- Input: validasi & pembersihan ----------

def input_defs(key: str) -> list[dict]:
    return list((get_recipe(key) or {}).get("inputs") or [])


def validate_inputs(key: str, raw: dict | None) -> tuple[dict, list[str]]:
    """Bersihkan input dari UI: buang yang kosong, cek wajib, konversi angka."""
    raw = raw or {}
    clean: dict = {}
    errors: list[str] = []
    for spec in input_defs(key):
        name = spec["name"]
        val = raw.get(name)
        val = "" if val is None else str(val).strip()
        if not val:
            val = str(spec.get("default") or "").strip()
        if not val:
            if spec.get("required"):
                errors.append(f"{spec.get('label', name)} wajib diisi.")
            continue
        if spec.get("type") == "number":
            try:
                clean[name] = int(val)
            except ValueError:
                errors.append(f"{spec.get('label', name)} harus angka.")
            continue
        clean[name] = val
    # Abaikan kunci yang tidak dideklarasikan (jangan teruskan ke fungsi alur).
    return clean, errors


# ---------- Eksekusi ----------

def execute(cfg: "Config", key: str, inputs: dict) -> dict:
    """Jalankan recipe. `inputs` sudah divalidasi lewat validate_inputs()."""
    if key == "ecuti_rekap":
        from . import recipe_ecuti

        params = {}
        if inputs.get("tanggal_mulai"):
            params["tgl_mulai"] = inputs["tanggal_mulai"]
        if inputs.get("tanggal_selesai"):
            params["tgl_selesai"] = inputs["tanggal_selesai"]
        if inputs.get("bw_item"):
            params["bw_item"] = inputs["bw_item"]
        return recipe_ecuti.run_ecuti(cfg, **params)
    if key == "penomoran_cuti":
        from . import recipe_penomoran

        return recipe_penomoran.run_penomoran(cfg)
    if key == "magang_baru":
        from . import recipe_magang

        return recipe_magang.run_magang(cfg)
    if key == "duk_mingguan":
        from . import recipe_duk

        return recipe_duk.run_duk(cfg)
    if key == "bersih_output":
        from . import recipe_bersih

        return recipe_bersih.run_bersih(cfg, retensi=str(inputs.get("retensi") or ""))
    if key == "pegawai_harian":
        from . import recipe_pegawai

        return recipe_pegawai.run_pegawai(
            cfg, kd_skpd=str(inputs.get("kd_skpd") or "")
        )
    raise KeyError(f"Recipe '{key}' tidak punya pelaksana.")


def summarize(key: str, result: dict) -> str:
    """Ringkasan hasil recipe (dipakai dashboard, CLI, dan notifikasi)."""
    title = (get_recipe(key) or {}).get("title", key)
    if not isinstance(result, dict):
        return f"✅ {title} selesai."
    if result.get("ok") is False:
        parts = [f"❌ {title}: {result.get('error') or 'gagal'}"]
        if result.get("step"):
            parts.append(f"tahap: {result['step']}")
        if result.get("log"):
            parts.extend(f"· {x}" for x in result["log"])
        return "\n".join(parts)

    lines = [f"✅ {title} selesai"]
    if "total_pengajuan" in result:
        lines.append(
            f"📋 Pengajuan baru: {result.get('baru', 0)} · "
            f"total berstatus baru: {result.get('total_pengajuan', 0)}"
        )
        for d in (result.get("detail_baru") or [])[:10]:
            lines.append(f"· {d.get('kode')} — {d.get('kampus')}")
            lines.append(f"  PJ: {d.get('pj')} · {d.get('periode')}")
            for p in (d.get("peserta") or [])[:6]:
                lines.append(f"  – {p}")
    # Khusus hasil impor rekap e-cuti (magang juga punya key "baru").
    if "diperbarui" in result:
        lines.append(
            f"📥 Impor: {result.get('baru', 0)} data baru · "
            f"{result.get('diperbarui', 0)} diperbarui"
        )
        for d in result.get("dilewati") or []:
            lines.append(f"· dilewati {d['jumlah']}: {d['alasan']}")
        if result.get("total"):
            lines.append(f"Σ {result['total']} baris di file")
    if "terisi" in result:
        lines.append(
            f"✍️ Nomor diisi: {result.get('terisi', 0)} · "
            f"dilewati (sudah bernomor): {result.get('dilewati', 0)}"
        )
        if result.get("nomor_berikut"):
            lines.append(f"🔢 Nomor berikutnya: {result['nomor_berikut']}")
        for d in (result.get("detail_terisi") or [])[:15]:
            lines.append(f"· {d.get('nama', '?')} → {d.get('nomor', '?')}")
        for d in (result.get("detail_dilewati") or [])[:10]:
            lines.append(f"· (sudah) {d.get('nama', '?')} → {d.get('nomor', '?')}")
    if "dihapus" in result and "retensi_hari" in result:
        lines.append(
            f"🧹 Berkas dihapus: {result.get('dihapus', 0)} "
            f"({result.get('kb', 0)} KB) · retensi {result['retensi_hari']} hari"
        )
        for d in (result.get("detail_dihapus") or [])[:15]:
            lines.append(f"· {d.get('nama')} ({d.get('kb')} KB)")
    if "created" in result and "updated" in result:
        # Resume singkat: yang penting jumlah pegawai dan komposisi
        # PNS/PPPK, bukan daftar nama (57 baris nama = notifikasi tak terbaca).
        lines.append(f"👥 Pegawai aktif: {result.get('jumlah', 0)}")
        lines.append(
            f"· PNS: {result.get('pns', 0)} · PPPK: {result.get('pppk', 0)}"
        )
        if result.get("gagal"):
            lines.append(f"⚠️ Gagal diekstrak: {result['gagal']}")
        lines.append(
            f"📥 Impor: {result.get('created', 0)} baru · "
            f"{result.get('updated', 0)} diperbarui"
        )
        if result.get("durasi_detik"):
            lines.append(f"⏱️ {result['durasi_detik']} detik")
    if "batch_id" in result:
        lines.append(
            f"🗂️ Batch DUK #{result.get('batch_id')}: "
            f"{result.get('jumlah', 0)} baris"
        )
        if result.get("periode"):
            lines.append(f"📅 Periode: {result['periode']}")
        if result.get("opd"):
            lines.append(f"🏢 {result['opd']}")
    if result.get("pdf"):
        lines.append(f"📄 {result['pdf']}")
    if result.get("xlsx"):
        lines.append(f"📄 {result['xlsx']}")
    if result.get("range"):
        lines.append(f"📅 Rentang: {result['range']}")
    if result.get("warning"):
        lines.append(f"⚠️ {result['warning']}")
    # Log lengkap (mis. 57 nama pegawai) tetap tersimpan untuk tab Log
    # dashboard, tapi tidak dicetak di notifikasi — sudah diringkas di atas.
    ringkas_saja = "created" in result and "updated" in result
    if result.get("log") and not ringkas_saja:
        lines.append("→ " + " · ".join(result["log"]))
    return "\n".join(lines)


def simpan_notifikasi(cfg: "Config", key: str, text: str) -> None:
    """Catat isi pesan yang dikirim ke Telegram agar bisa dilihat di dashboard.

    Kegagalan menyimpan tidak boleh menghentikan recipe.
    """
    from . import db

    try:
        meta = get_recipe(key) or {}
        conn = db.connect(cfg.db_path)
        try:
            db.add_log(
                conn, "info", "telegram_sent",
                meta.get("title", key),
                {"recipe": key, "text": text, "chat": ",".join(map(str, cfg.telegram_allowed))},
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.warning("Gagal menyimpan notifikasi %s", key, exc_info=True)


def notify_recipe(cfg: "Config", key: str, result: dict) -> None:
    """Kirim ringkasan ke Telegram — hanya bila perlu (bukan tiap run).

    Recipe penomoran berjalan tiap 2 jam; sebagian besar run tidak menemukan
    apa pun. Mengirim semuanya akan jadi spam, jadi yang dikirim hanya:
    gagal, atau run yang benar-benar mengisi nomor/menambahkan data.
    """
    from . import telegram_client

    if not getattr(cfg, "telegram_notify", True):
        return
    if not cfg.telegram_bot_token or not cfg.telegram_allowed:
        return

    meta = get_recipe(key) or {}
    if not meta.get("notify"):
        return  # recipe ini tidak minta notifikasi

    ok = bool(result.get("ok"))
    # "Berubah" berbeda per recipe: penomoran mengisi nomor, rekap menghasilkan
    # file, pemantauan magang hanya berarti kalau ada pengajuan baru, DUK dan
    # data pegawai selalu berarti karena setiap run menyimpan snapshot baru,
    # dan pembersihan hanya berarti kalau benar-benar ada berkas yang dihapus.
    if "total_pengajuan" in result:
        berubah = bool(result.get("baru"))
    elif "retensi_hari" in result:
        berubah = bool(result.get("dihapus"))
    elif "created" in result and "updated" in result:
        berubah = bool(result.get("jumlah"))
    elif "batch_id" in result:
        berubah = bool(result.get("jumlah"))
    else:
        berubah = bool(result.get("terisi") or result.get("xlsx"))
    if ok and not berubah:
        return  # tidak ada yang perlu dilaporkan

    text = summarize(key, result)
    if len(text) > 3800:
        text = text[:3800] + "…"
    # Simpan versi asli (belum di-escape) agar dashboard menampilkan teks yang
    # enak dibaca, sementara Telegram tetap menerima versi ter-escape.
    simpan_notifikasi(cfg, key, text)
    # Pesan dikirim dengan parse_mode=HTML; escape agar nama/error yang
    # mengandung <, >, atau & tidak membuat Telegram menolak pesannya.
    text = html.escape(text)
    # Notifikasi tidak boleh menggagalkan recipe: kegagalan kirim cukup dicatat.
    for chat_id in cfg.telegram_allowed:
        try:
            telegram_client.send_message(cfg, chat_id, text)
        except Exception:  # noqa: BLE001
            pass


def run_logged(conn: sqlite3.Connection, key: str, fn: Callable[[], dict]) -> dict:
    """Jalankan alur + catat mulai/selesai ke log & state."""
    from . import db

    meta = get_recipe(key) or {}
    title = meta.get("title", key)

    db.add_log(conn, "info", "recipe_start", f"Alur '{title}' dipanggil", {"recipe": key})
    started = time.time()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        dur = int((time.time() - started) * 1000)
        record_state(conn, key, "failed", dur, f"❌ {title}: {type(e).__name__}: {e}")
        db.add_log(
            conn, "error", "recipe_done",
            f"Alur '{title}' gagal: {type(e).__name__}",
            {"recipe": key, "status": "failed", "duration_ms": dur, "error": str(e)[:300]},
        )
        raise
    dur = int((time.time() - started) * 1000)
    ok = bool(result.get("ok")) if isinstance(result, dict) else True
    record_state(conn, key, "ok" if ok else "failed", dur, summarize(key, result))
    db.add_log(
        conn, "success" if ok else "warn", "recipe_done",
        f"Alur '{title}' selesai: {'ok' if ok else 'gagal'}",
        {"recipe": key, "status": "ok" if ok else "failed", "duration_ms": dur},
    )
    return result


def run_recipe(cfg: "Config", conn: sqlite3.Connection, key: str, inputs: dict | None = None) -> dict:
    """Validasi input lalu jalankan recipe (dipakai UI, CLI, scheduler, autopilot)."""
    meta = get_recipe(key)
    if meta is None:
        return {"ok": False, "error": f"Recipe '{key}' tidak dikenal."}
    clean, errors = validate_inputs(key, inputs)
    if errors:
        return {"ok": False, "error": " ".join(errors)}
    # Satu pintu untuk semua pemanggil: vault dibuka di sini, jadi CLI & tombol
    # dashboard tidak lagi gagal hanya karena BW_SESSION belum ada di prosesnya.
    if meta.get("needs_vault"):
        from . import autopilot

        if not autopilot.refresh_bw_session_if_needed(force=True):
            hasil = {
                "ok": False,
                "error": "Vault Bitwarden tidak bisa dibuka. Periksa secrets/bw_masterpass.",
            }
            notify_recipe(cfg, key, hasil)
            return hasil

    try:
        result = run_logged(conn, key, lambda: execute(cfg, key, clean))
    except Exception as e:  # noqa: BLE001
        notify_recipe(cfg, key, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        raise
    notify_recipe(cfg, key, result)
    return result


# ---------- State (kapan terakhir jalan) ----------

def record_state(
    conn: sqlite3.Connection, key: str, status: str, duration_ms: int, reply: str = ""
) -> None:
    conn.execute(
        "INSERT INTO recipe_state(key, last_status, last_duration_ms, last_reply, runs) "
        "VALUES(?,?,?,?,1) "
        "ON CONFLICT(key) DO UPDATE SET "
        "  last_status=excluded.last_status, "
        "  last_duration_ms=excluded.last_duration_ms, "
        "  last_reply=excluded.last_reply, "
        "  last_run_at=CURRENT_TIMESTAMP, "
        "  runs=recipe_state.runs+1",
        (key, status, duration_ms, reply),
    )
    conn.commit()


def state_of(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute(
        "SELECT datetime(last_run_at,'localtime') AS last_run_at, last_status, "
        "last_duration_ms, last_reply, runs FROM recipe_state WHERE key=?",
        (key,),
    ).fetchone()
    return dict(row) if row else {"last_run_at": None, "last_status": None,
                                  "last_duration_ms": None, "last_reply": None, "runs": 0}


def stats(conn: sqlite3.Connection) -> list[dict]:
    """Metadata recipe + state eksekusi, untuk dashboard."""
    from . import db

    per_recipe = db.recipe_run_counts(conn)
    out = []
    for r in RECIPES:
        st = state_of(conn, r["key"])
        runs = per_recipe.get(r["key"], 0)
        out.append({
            "key": r["key"],
            "title": r["title"],
            "description": r["description"],
            "steps": r.get("steps", []),
            "inputs": r.get("inputs", []),
            "schedule": schedule_of(r),
            "runs": runs,
            "last_run": st.get("last_run_at") or per_recipe.get(f"{r['key']}:last"),
            "last_status": st.get("last_status"),
            "last_duration_ms": st.get("last_duration_ms"),
            "last_reply": st.get("last_reply"),
        })
    return out
