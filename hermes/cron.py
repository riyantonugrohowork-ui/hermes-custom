"""Parser cron 5-field (menit, jam, tanggal, bulan, hari) untuk jadwal recipe.

Tanpa dependency tambahan agar image Docker tidak perlu di-install ulang.

Format yang didukung:
  *  */n  a-b  a,b  kombinasi   serta alias @hourly/@daily/@weekly/@monthly
Hari: 0 = Minggu .. 6 = Sabtu (7 juga diterima sebagai Minggu).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


class CronError(ValueError):
    pass


class Spec(NamedTuple):
    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]
    dom_any: bool
    dow_any: bool


def _field(spec: str, lo: int, hi: int, name: str) -> tuple[set[int], bool]:
    spec = spec.strip()
    if not spec:
        raise CronError(f"Field {name} kosong.")
    unbounded = spec in ("*", "?")
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Field {name} tidak valid: {spec!r}")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            try:
                step = int(raw_step)
            except ValueError:
                raise CronError(f"Step {name} tidak valid: {raw_step!r}") from None
            if step <= 0:
                raise CronError(f"Step {name} harus > 0: {raw_step!r}")
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part:
            raw_a, raw_b = part.split("-", 1)
            try:
                start, end = int(raw_a), int(raw_b)
            except ValueError:
                raise CronError(f"Rentang {name} tidak valid: {part!r}") from None
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise CronError(f"Nilai {name} tidak valid: {part!r}") from None
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise CronError(f"Field {name} di luar {lo}-{hi}: {part!r}")
        values.update(range(start, end + 1, step))
    if name == "dow" and 7 in values:
        values.discard(7)
        values.add(0)
    return values, unbounded


def parse(expr: str) -> Spec:
    text = (expr or "").strip().lower()
    text = ALIASES.get(text, text)
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"Ekspresi cron harus 5 field: {expr!r}")
    minutes, _ = _field(parts[0], 0, 59, "menit")
    hours, _ = _field(parts[1], 0, 23, "jam")
    doms, dom_any = _field(parts[2], 1, 31, "tanggal")
    months, _ = _field(parts[3], 1, 12, "bulan")
    dows, dow_any = _field(parts[4], 0, 7, "hari")
    return Spec(minutes, hours, doms, months, dows, dom_any, dow_any)


def _day_matches(moment: datetime, spec: Spec) -> bool:
    dom_ok = moment.day in spec.doms
    dow_ok = (moment.weekday() + 1) % 7 in spec.dows  # Python: Senin=0 → cron: Minggu=0
    if spec.dom_any and spec.dow_any:
        return True
    if spec.dom_any:
        return dow_ok
    if spec.dow_any:
        return dom_ok
    return dom_ok or dow_ok


def next_run(expr: str, after: datetime) -> datetime:
    """Waktu (waktu lokal) berikutnya setelah `after`."""
    spec = parse(expr)
    cur = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cur + timedelta(days=366 * 5)
    while cur < limit:
        if cur.month not in spec.months:
            cur = (cur.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        if not _day_matches(cur, spec):
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cur.hour not in spec.hours:
            cur = (cur + timedelta(hours=1)).replace(minute=0)
            continue
        if cur.minute not in spec.minutes:
            cur += timedelta(minutes=1)
            continue
        return cur
    raise CronError(f"Tidak ada jadwal dalam 5 tahun: {expr!r}")


def prev_run(expr: str, before: datetime) -> datetime:
    """Waktu (waktu lokal) terakhir yang <= `before`.

    Dipakai untuk mengejar jadwal yang terlewat saat laptop tidur/mati:
    jika kemunculan terakhir belum dijalankan, jalankan sekali sekarang.
    """
    spec = parse(expr)
    cur = before.replace(second=0, microsecond=0)
    limit = cur - timedelta(days=366 * 5)
    while cur > limit:
        if cur.month not in spec.months:
            # Mundur ke akhir bulan sebelumnya.
            cur = cur.replace(day=1, hour=0, minute=0) - timedelta(minutes=1)
            continue
        if not _day_matches(cur, spec):
            cur = (cur - timedelta(days=1)).replace(hour=23, minute=59)
            continue
        if cur.hour not in spec.hours:
            cur = (cur - timedelta(hours=1)).replace(minute=59)
            continue
        if cur.minute not in spec.minutes:
            cur -= timedelta(minutes=1)
            continue
        return cur
    raise CronError(f"Tidak ada jadwal sebelumnya dalam 5 tahun: {expr!r}")


def is_due(expr: str, last_run: datetime, now: datetime) -> bool:
    try:
        return next_run(expr, last_run) <= now
    except CronError:
        return False
