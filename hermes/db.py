"""Basis data lokal (SQLite) — penyimpanan terstruktur Hermes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_records_dataset ON records(dataset);

-- Log aktivitas Hermes (untuk dashboard).
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,        -- info | success | warn | error
    event TEXT NOT NULL,        -- nama kejadian, mis: task_run, tool_call
    message TEXT NOT NULL,
    meta TEXT,                  -- JSON konteks tambahan
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);

-- Aktivitas harian e-Kinerja (satu baris = satu butir kegiatan).
-- Disimpan sendiri karena halaman e-kin tidak menyediakan total per hari;
-- angkanya dijumlahkan dari sini.
CREATE TABLE IF NOT EXISTS ekin_aktivitas (
    id_ekin INTEGER PRIMARY KEY,  -- id dari e-kin, bukan autoincrement
    akun TEXT NOT NULL,           -- NIP pemilik baris
    tanggal TEXT NOT NULL,        -- YYYY-MM-DD
    uraian TEXT NOT NULL,
    satuan INTEGER,               -- menit per satuan
    kuantitas INTEGER,
    waktu INTEGER NOT NULL,       -- menit (angka dari e-kin, bukan satuan*kuantitas)
    status TEXT,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ekin_akun_tanggal ON ekin_aktivitas(akun, tanggal);

-- Isian aktivitas e-Kinerja per tanggal: banyak butir per hari.
-- Disimpan di sini supaya tidak hilang saat browser/extension dibersihkan.
-- Baris = antrian: diisi di web kapan saja, dikirim ke SIMPATIK jam 15.00.
CREATE TABLE IF NOT EXISTS ekin_rencana (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    akun TEXT NOT NULL,
    tanggal TEXT NOT NULL,        -- YYYY-MM-DD
    uraian TEXT NOT NULL,
    catatan TEXT,
    kuantitas INTEGER NOT NULL DEFAULT 1,
    satuan INTEGER NOT NULL DEFAULT 0,   -- menit butir (dari ekin_butir)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | error
    error TEXT,
    sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ekin_rencana_tanggal ON ekin_rencana(akun, tanggal);

-- Status terakhir tiap alur (recipe).
CREATE TABLE IF NOT EXISTS recipe_state (
    key TEXT PRIMARY KEY,
    last_status TEXT,           -- ok | failed
    last_duration_ms INTEGER,
    runs INTEGER DEFAULT 0,
    last_reply TEXT,            -- ringkasan hasil terakhir untuk kartu dashboard
    last_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Presensi harian dari absen.semarangkota.go.id (BACA SAJA).
-- Satu baris = satu pegawai pada satu tanggal. Tidak pernah ditulis balik ke
-- situs absen; tabel ini hanya salinan lokal untuk penelusuran dari dashboard.
CREATE TABLE IF NOT EXISTS absen_presensi (
    tanggal TEXT NOT NULL,        -- YYYY-MM-DD
    nip TEXT NOT NULL,
    nama TEXT,
    absen_hadir TEXT,             -- jam masuk, atau NULL kalau belum
    absen_pulang TEXT,            -- jam pulang, atau NULL kalau belum
    sel_hadir TEXT,
    sel_pulang TEXT,
    id_hadir INTEGER,
    id_pulang INTEGER,
    shift_cek TEXT,               -- hadir | pulang (kapan terakhir diperiksa)
    diperiksa_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tanggal, nip)
);
CREATE INDEX IF NOT EXISTS idx_absen_presensi_tanggal ON absen_presensi(tanggal);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Tambah kolom bila belum ada (migrasi ringan untuk DB yang sudah jalan)."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in cols:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
        conn.commit()


def connect(db_path: Path, check_same_thread: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "recipe_state", "last_reply", "TEXT")
    _ensure_column(conn, "ekin_rencana", "status", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(conn, "ekin_rencana", "error", "TEXT")
    _ensure_column(conn, "ekin_rencana", "sent_at", "TIMESTAMP")
    return conn


def set_note(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO notes(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, value),
    )
    conn.commit()


def get_note(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM notes WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def list_notes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT key, value, updated_at FROM notes ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def add_record(conn: sqlite3.Connection, dataset: str, payload: str) -> int:
    cur = conn.execute(
        "INSERT INTO records(dataset, payload) VALUES(?, ?)", (dataset, payload)
    )
    conn.commit()
    return int(cur.lastrowid)


def list_records(conn: sqlite3.Connection, dataset: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, payload, created_at FROM records WHERE dataset=? "
        "ORDER BY id DESC LIMIT ?",
        (dataset, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Log aktivitas ----

def add_log(
    conn: sqlite3.Connection,
    level: str,
    event: str,
    message: str,
    meta: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO logs(level, event, message, meta) VALUES(?,?,?,?)",
        (
            level,
            event,
            message,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_logs(
    conn: sqlite3.Connection,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
) -> list[dict]:
    where, args = [], []
    if level:
        where.append("level=?")
        args.append(level)
    if event:
        where.append("event=?")
        args.append(event)
    sql = "SELECT id, level, event, message, meta, created_at FROM logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d["meta"]:
            try:
                d["meta"] = json.loads(d["meta"])
            except json.JSONDecodeError:
                pass
        result.append(d)
    return result


def log_stats(conn: sqlite3.Connection) -> dict:
    """Statistik aktivitas untuk dashboard."""
    total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    by_level = conn.execute(
        "SELECT level, COUNT(*) FROM logs GROUP BY level"
    ).fetchall()
    return {
        "total": total,
        "by_level": {r[0]: r[1] for r in by_level},
    }


def recipe_runs_by_day(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Jumlah eksekusi recipe per hari lokal (YYYY-MM-DD) dalam N hari terakhir."""
    rows = conn.execute(
        "SELECT DATE(created_at,'localtime') d, COUNT(*) c FROM logs "
        "WHERE event='recipe_done' AND DATE(created_at,'localtime') >= DATE('now','localtime', ?) "
        "GROUP BY d",
        (f"-{max(days - 1, 0)} day",),
    ).fetchall()
    return {r["d"]: r["c"] for r in rows}


def recipe_run_counts(conn: sqlite3.Connection) -> dict:
    """Jumlah eksekusi & waktu terakhir per recipe, dihitung dari log."""
    rows = conn.execute(
        "SELECT meta, datetime(created_at,'localtime') AS ts FROM logs "
        "WHERE event='recipe_done' ORDER BY id DESC"
    ).fetchall()
    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except json.JSONDecodeError:
            continue
        key = meta.get("recipe")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        last.setdefault(f"{key}:last", r["ts"])
    return {**counts, **last}


# ---- Aktivitas harian e-Kinerja ----

def simpan_aktivitas_ekin(
    conn: sqlite3.Connection, akun: str, baris: list[dict], mulai: str, selesai: str
) -> int:
    """Ganti isi tabel aktivitas e-kin untuk rentang [mulai, selesai].

    Rentang diganti utuh (bukan di-upsert) karena e-kin adalah sumber
    kebenaran: aktivitas yang dihapus di sana tidak boleh tetap terhitung di
    sini. Baris di luar rentang dibiarkan, jadi riwayat bulan lalu tetap ada.
    Satu transaksi agar pembaca lain tidak pernah melihat tabel setengah isi.
    """
    with conn:
        conn.execute(
            "DELETE FROM ekin_aktivitas WHERE akun=? AND tanggal BETWEEN ? AND ?",
            (akun, mulai, selesai),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO ekin_aktivitas"
            "(id_ekin, akun, tanggal, uraian, satuan, kuantitas, waktu, status) "
            "VALUES(:id_ekin, :akun, :tanggal, :uraian, :satuan, :kuantitas, :waktu, :status)",
            [{**b, "akun": akun} for b in baris],
        )
    return len(baris)


def total_harian_ekin(conn: sqlite3.Connection, mulai: str, selesai: str) -> dict[str, int]:
    """Total menit per tanggal dalam rentang (inklusif)."""
    rows = conn.execute(
        "SELECT tanggal, SUM(waktu) AS total FROM ekin_aktivitas "
        "WHERE tanggal BETWEEN ? AND ? GROUP BY tanggal",
        (mulai, selesai),
    ).fetchall()
    return {r["tanggal"]: int(r["total"] or 0) for r in rows}


def daftar_aktivitas_ekin(conn: sqlite3.Connection, mulai: str, selesai: str) -> list[dict]:
    """Aktivitas dalam rentang, terbaru dulu."""
    rows = conn.execute(
        "SELECT id_ekin, akun, tanggal, uraian, satuan, kuantitas, waktu, status "
        "FROM ekin_aktivitas WHERE tanggal BETWEEN ? AND ? "
        "ORDER BY tanggal DESC, id_ekin DESC",
        (mulai, selesai),
    ).fetchall()
    return [dict(r) for r in rows]


def simpan_absen_presensi(
    conn: sqlite3.Connection, tanggal: str, baris: list[dict], shift: str = ""
) -> int:
    """Simpan salinan presensi satu tanggal (BACA SAJA dari absen).

    `tanggal` yang diterima dari situs berformat dd-mm-yyyy; disimpan sebagai
    YYYY-MM-DD agar sebanding dengan tabel lain. Baris di-upsert, bukan
    diganti utuh: rekap satu tanggal boleh diperiksa berkali-kali (pagi untuk
    hadir, sore untuk pulang) dan hasil terakhir yang menang.
    """
    import datetime as _dt

    try:
        iso = _dt.datetime.strptime(tanggal, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        iso = tanggal

    with conn:
        conn.executemany(
            "INSERT INTO absen_presensi"
            "(tanggal, nip, nama, absen_hadir, absen_pulang, sel_hadir, sel_pulang,"
            " id_hadir, id_pulang, shift_cek) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tanggal, nip) DO UPDATE SET "
            "  nama=excluded.nama, absen_hadir=excluded.absen_hadir, "
            "  absen_pulang=excluded.absen_pulang, sel_hadir=excluded.sel_hadir, "
            "  sel_pulang=excluded.sel_pulang, id_hadir=excluded.id_hadir, "
            "  id_pulang=excluded.id_pulang, shift_cek=excluded.shift_cek, "
            "  diperiksa_at=CURRENT_TIMESTAMP",
            [
                (
                    iso, str(b.get("nip") or ""), b.get("nama"),
                    b.get("absen_hadir"), b.get("absen_pulang"),
                    b.get("sel_hadir"), b.get("sel_pulang"),
                    b.get("id_hadir"), b.get("id_pulang"), shift,
                )
                for b in baris
            ],
        )
    return len(baris)


def presensi_tanggal(conn: sqlite3.Connection, tanggal: str) -> list[dict]:
    """Presensi tersimpan untuk satu tanggal (YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT tanggal, nip, nama, absen_hadir, absen_pulang, sel_hadir, "
        "sel_pulang, shift_cek, diperiksa_at FROM absen_presensi "
        "WHERE tanggal=? ORDER BY nama",
        (tanggal,),
    ).fetchall()
    return [dict(r) for r in rows]


def uraian_harian_ekin(conn: sqlite3.Connection, mulai: str, selesai: str) -> dict[str, list[str]]:
    """Uraian aktivitas yang sudah tercatat di e-kin, dikelompokkan per tanggal.

    Dipakai tab Kinerja untuk tahu butir apa yang sudah ada di e-kin pada
    tanggal itu (mis. apel pagi sudah diisi) supaya isian tidak dobel.
    """
    hasil: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT tanggal, uraian FROM ekin_aktivitas WHERE tanggal BETWEEN ? AND ?",
        (mulai, selesai),
    ).fetchall()
    for r in rows:
        hasil.setdefault(r["tanggal"], []).append(r["uraian"] or "")
    return hasil


# ---- Isian aktivitas per tanggal ----

def isi_rencana_ekin(
    conn: sqlite3.Connection, akun: str, tanggal: str, baris: list[dict]
) -> int:
    """Ganti seluruh isian satu tanggal dengan `baris` yang dikirim.

    Satu tanggal = satu set butir. Mengganti utuh membuat hasil edit di layar
    persis sama dengan yang tersimpan (tidak ada baris yatim dari versi lama).

    Status kirim dipertahankan untuk baris yang isinya tidak berubah: baris
    yang sudah masuk e-kin tidak boleh terkirim dua kali hanya karena tanggal
    itu disimpan ulang. Baris yang berubah kembali menjadi `pending`.
    """
    lama = {
        (r["uraian"], r["catatan"] or "", int(r["kuantitas"])): dict(r)
        for r in conn.execute(
            "SELECT uraian, catatan, kuantitas, status, sent_at, error "
            "FROM ekin_rencana WHERE akun=? AND tanggal=?",
            (akun, tanggal),
        )
    }
    isi = []
    for b in baris:
        uraian = (b.get("uraian") or "").strip()
        if not uraian:
            continue
        catatan = (b.get("catatan") or "").strip()
        kuantitas = max(1, int(b.get("kuantitas") or 1))
        sebelumnya = lama.get((uraian, catatan, kuantitas)) or {}
        isi.append({
            "akun": akun,
            "tanggal": tanggal,
            "uraian": uraian,
            "catatan": catatan or None,
            "kuantitas": kuantitas,
            "satuan": max(0, int(b.get("satuan") or 0)),
            "status": sebelumnya.get("status") or "pending",
            "sent_at": sebelumnya.get("sent_at"),
            "error": sebelumnya.get("error"),
        })
    with conn:
        conn.execute(
            "DELETE FROM ekin_rencana WHERE akun=? AND tanggal=?", (akun, tanggal)
        )
        conn.executemany(
            "INSERT INTO ekin_rencana"
            "(akun, tanggal, uraian, catatan, kuantitas, satuan, status, sent_at, error) "
            "VALUES(:akun, :tanggal, :uraian, :catatan, :kuantitas, :satuan, "
            ":status, :sent_at, :error)",
            isi,
        )
    return len(isi)


def rencana_per_tanggal(conn: sqlite3.Connection, akun: str, tanggal: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, uraian, catatan, kuantitas, satuan, status, error, sent_at "
        "FROM ekin_rencana WHERE akun=? AND tanggal=? ORDER BY id",
        (akun, tanggal),
    ).fetchall()
    return [dict(r) for r in rows]


def rencana_pending(conn: sqlite3.Connection, akun: str, sampai: str) -> list[dict]:
    """Baris yang belum masuk e-kin untuk tanggal <= `sampai`."""
    rows = conn.execute(
        "SELECT id, tanggal, uraian, catatan, kuantitas, satuan "
        "FROM ekin_rencana "
        "WHERE akun=? AND tanggal<=? AND status<>'sent' "
        "ORDER BY tanggal, id",
        (akun, sampai),
    ).fetchall()
    return [dict(r) for r in rows]


def tandai_rencana(
    conn: sqlite3.Connection, id_: int, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE ekin_rencana SET status=?, error=?, "
        "sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END "
        "WHERE id=?",
        (status, error, status, id_),
    )
    conn.commit()


def tanggal_rencana_ekin(conn: sqlite3.Connection, akun: str, limit: int = 60) -> list[dict]:
    """Daftar tanggal yang punya isian, beserta jumlah butir & menitnya."""
    rows = conn.execute(
        "SELECT tanggal, COUNT(*) AS butir, SUM(satuan * kuantitas) AS menit "
        "FROM ekin_rencana WHERE akun=? GROUP BY tanggal "
        "ORDER BY tanggal DESC LIMIT ?",
        (akun, limit),
    ).fetchall()
    return [
        {"tanggal": r["tanggal"], "butir": int(r["butir"]), "menit": int(r["menit"] or 0)}
        for r in rows
    ]


def hapus_rencana_tanggal(conn: sqlite3.Connection, akun: str, tanggal: str) -> int:
    cur = conn.execute(
        "DELETE FROM ekin_rencana WHERE akun=? AND tanggal=?", (akun, tanggal)
    )
    conn.commit()
    return cur.rowcount


def hapus_rencana_ekin(conn: sqlite3.Connection, id_: int) -> bool:
    cur = conn.execute("DELETE FROM ekin_rencana WHERE id=?", (id_,))
    conn.commit()
    return cur.rowcount > 0


def ringkasan_ekin(conn: sqlite3.Connection) -> dict:
    """Statistik tabel aktivitas untuk dashboard."""
    row = conn.execute(
        "SELECT COUNT(*) AS baris, COUNT(DISTINCT tanggal) AS hari, "
        "MIN(tanggal) AS awal, MAX(tanggal) AS akhir, SUM(waktu) AS menit "
        "FROM ekin_aktivitas"
    ).fetchone()
    return {
        "baris": int(row["baris"] or 0),
        "hari": int(row["hari"] or 0),
        "awal": row["awal"],
        "akhir": row["akhir"],
        "menit": int(row["menit"] or 0),
    }


def recipe_stats(conn: sqlite3.Connection) -> dict:
    """Ringkasan eksekusi recipe dari riwayat log (bukan hanya state terakhir)."""
    rows = conn.execute(
        "SELECT meta FROM logs WHERE event='recipe_done'"
    ).fetchall()
    ok = failed = 0
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except json.JSONDecodeError:
            meta = {}
        if meta.get("status") == "ok":
            ok += 1
        else:
            failed += 1
    last = conn.execute(
        "SELECT datetime(MAX(last_run_at),'localtime') FROM recipe_state"
    ).fetchone()[0]
    return {"runs": ok + failed, "ok": ok, "failed": failed, "last_run": last}

