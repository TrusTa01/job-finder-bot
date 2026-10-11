"""Search statistics: applications (✅), not interesting (❌), interviews and offers entered by hand.

Interviews and offers are commands in the chat (/interview, /offer). A rejection is not entered by hand: an
application older than WAIT_DAYS without an interview or an offer counts as rejected. Nothing here talks to hh.ru.
"""
import time
from datetime import datetime, timezone

KINDS = {"/interview": "interview", "/offer": "offer"}
WAIT_DAYS = 7
MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def connect(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER NOT NULL, kind TEXT NOT NULL)")
    db.commit()


def add(db, kind: str, ts: int) -> None:
    connect(db)
    db.execute("INSERT INTO events (ts, kind) VALUES (?, ?)", (ts, kind))
    db.commit()


def _month_bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def window(db, start: int, end: int, title: str, now: int | None = None) -> str:
    """The summary for one period [start, end): the same figures for a month, a year or all time."""
    connect(db)
    cutoff = (now or int(time.time())) - WAIT_DAYS * 86400
    applied = db.execute("SELECT COUNT(*) FROM vacancies WHERE applied_at >= ? AND applied_at < ?", (start, end)).fetchone()[0]
    dismissed = db.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='dismissed' AND dismissed_at >= ? AND dismissed_at < ?", (start, end)
    ).fetchone()[0]
    waiting = db.execute(
        "SELECT COUNT(*) FROM vacancies WHERE applied_at >= ? AND applied_at < ? AND applied_at >= ?", (start, end, cutoff)
    ).fetchone()[0]
    counts = {k: 0 for k in KINDS.values()}
    for kind, n in db.execute("SELECT kind, COUNT(*) FROM events WHERE ts >= ? AND ts < ? GROUP BY kind", (start, end)):
        counts[kind] = n
    rejected = max(0, applied - waiting - counts["interview"] - counts["offer"])
    lines = [
        title,
        f"Откликов (✅): {applied}",
        f"Неинтересно (❌, не в счёт откликов): {dismissed}",
        f"Собеседований: {counts['interview']}",
        f"Офферов: {counts['offer']}",
        f"Без ответа больше {WAIT_DAYS} дней (считаются отказами): {rejected}",
        f"Ещё ждём ответа (моложе {WAIT_DAYS} дней): {waiting}",
    ]
    return "\n".join(lines)


def month(db, year: int, month: int, now: int | None = None) -> str:
    start, end = _month_bounds(year, month)
    return window(db, start, end, f"Статистика за {MONTHS[month]} {year}", now)


def year_to_date(db, year: int, now: int | None = None) -> str:
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end = (now or int(time.time())) + 1
    return window(db, start, end, f"Статистика за {year} год (с 1 января по сегодня)", now)


def all_time(db, now: int | None = None) -> str:
    return window(db, 0, (now or int(time.time())) + 1, "Статистика за всё время", now)


def previous_month(now: datetime) -> tuple[int, int]:
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
