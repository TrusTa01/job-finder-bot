"""Monthly counts for the search: applications (✅), not interesting (❌) and outcomes reported by hand in the chat.

Outcomes are commands, not buttons: /interview, /offer, /rejected, /found. Each one is one event with a timestamp;
the monthly summary counts them. Nothing here talks to hh.ru.
"""
from datetime import datetime, timezone

from . import store

KINDS = {"/interview": "interview", "/offer": "offer", "/rejected": "rejected", "/found": "found"}
MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def connect(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER NOT NULL, kind TEXT NOT NULL)")
    db.commit()


def add(db, kind: str, ts: int) -> None:
    connect(db)
    db.execute("INSERT INTO events (ts, kind) VALUES (?, ?)", (ts, kind))
    db.commit()


def _bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def summary(db, year: int, month: int) -> str:
    connect(db)
    start, end = _bounds(year, month)
    applied = db.execute("SELECT COUNT(*) FROM vacancies WHERE applied_at >= ? AND applied_at < ?", (start, end)).fetchone()[0]
    dismissed = db.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='dismissed' AND dismissed_at >= ? AND dismissed_at < ?", (start, end)
    ).fetchone()[0]
    counts = {k: 0 for k in KINDS.values()}
    for kind, n in db.execute("SELECT kind, COUNT(*) FROM events WHERE ts >= ? AND ts < ? GROUP BY kind", (start, end)):
        counts[kind] = n
    no_answer = max(0, applied - counts["interview"] - counts["offer"] - counts["rejected"])
    return (
        f"Итоги за {MONTHS[month]} {year}\n"
        f"Откликов (✅): {applied}\n"
        f"Неинтересно (❌, не считается в откликах): {dismissed}\n"
        f"Собеседований: {counts['interview']}\n"
        f"Офферов: {counts['offer']}\n"
        f"Отказов: {counts['rejected']}\n"
        f"Без ответа (примерно, откликов минус отмеченные): {no_answer}"
    )


def previous_month(now: datetime) -> tuple[int, int]:
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
