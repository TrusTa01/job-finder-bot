"""SQLite state: which vacancies were seen, what the LLM decided, what was sent, what it cost."""
import sqlite3
import time

from .settings import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    published_at TEXT,
    name TEXT, employer TEXT, area TEXT, experience TEXT, url TEXT,
    status TEXT NOT NULL,          -- filtered | screened_out | ready | notified
    priority TEXT,                 -- normal | low
    fit TEXT, reason TEXT, issue TEXT, grade TEXT,
    letter TEXT,
    notified_at INTEGER
);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts INTEGER NOT NULL, task TEXT, model TEXT, effort TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for column in ("stack TEXT", "applied_at INTEGER"):  # migrations for databases created before these columns
        try:
            db.execute(f"ALTER TABLE vacancies ADD COLUMN {column}")
            db.commit()
        except sqlite3.OperationalError:
            pass
    return db


def known_ids(db: sqlite3.Connection) -> set[str]:
    return {r["id"] for r in db.execute("SELECT id FROM vacancies")}


def save_vacancy(db: sqlite3.Connection, v: dict, status: str, priority: str = "low", **extra) -> None:
    row = {
        "id": v["id"],
        "first_seen": int(time.time()),
        "published_at": v.get("published_at"),
        "name": v.get("name"),
        "employer": (v.get("employer") or {}).get("name", ""),
        "area": (v.get("area") or {}).get("name", ""),
        "experience": (v.get("experience") or {}).get("name", ""),
        "url": v.get("alternate_url"),
        "status": status,
        "priority": priority,
        "fit": extra.get("fit"),
        "reason": extra.get("reason"),
        "issue": extra.get("issue"),
        "grade": extra.get("grade"),
        "stack": extra.get("stack"),
        "letter": extra.get("letter"),
    }
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    db.execute(f"INSERT OR REPLACE INTO vacancies ({cols}) VALUES ({marks})", row)
    db.commit()


def mark_notified(db: sqlite3.Connection, vacancy_id: str) -> None:
    db.execute("UPDATE vacancies SET status='notified', notified_at=? WHERE id=?", (int(time.time()), vacancy_id))
    db.commit()


def mark_applied(db: sqlite3.Connection, vacancy_id: str) -> None:
    db.execute("UPDATE vacancies SET applied_at=? WHERE id=? AND applied_at IS NULL", (int(time.time()), vacancy_id))
    db.commit()


def log_usage(db: sqlite3.Connection, task: str, model: str, effort: str, t_in: int, t_out: int, cost: float) -> None:
    db.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?)", (int(time.time()), task, model, effort, t_in, t_out, cost))
    db.commit()


def spend_last_day(db: sqlite3.Connection) -> float:
    since = int(time.time()) - 86400
    return db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE ts>=?", (since,)).fetchone()[0]


def total_cost(db: sqlite3.Connection) -> float:
    return db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage").fetchone()[0]


def get_meta(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def day_stats(db: sqlite3.Connection) -> dict:
    """Numbers for the daily heartbeat: the last 24 hours."""
    since = int(time.time()) - 86400
    count = lambda where: db.execute(f"SELECT COUNT(*) FROM vacancies WHERE first_seen>=? AND {where}", (since,)).fetchone()[0]
    return {
        "seen": count("1=1"),
        "sent": count("status='notified'"),
        "applied": db.execute("SELECT COUNT(*) FROM vacancies WHERE applied_at>=?", (since,)).fetchone()[0],
        "undelivered": db.execute("SELECT COUNT(*) FROM vacancies WHERE status='ready'").fetchone()[0],
        "spend": db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE ts>=?", (since,)).fetchone()[0],
    }
