"""SQLite state: which vacancies were seen, what the LLM decided, what was sent, what it cost."""
import hashlib
import re
import sqlite3
import time
from urllib.parse import urlparse

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
-- Direct-outreach ("hidden market") leads: companies the user may write to. See jobbot/outreach/.
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,      -- co:<domain>
    name TEXT, kind TEXT,          -- studio | outstaff | agency | product
    site TEXT, source TEXT, stack_hint TEXT, summary TEXT,
    contact_name TEXT, contact_role TEXT, contact_channel TEXT, contact_value TEXT,
    status TEXT NOT NULL,          -- new | drafted | notified | sent | replied | later | dead
    pitch TEXT, pitch_variant TEXT,
    first_seen INTEGER NOT NULL, notified_at INTEGER, message_id INTEGER,
    sent_at INTEGER, follow_up_at INTEGER, follow_up_sent INTEGER, note TEXT
);
-- Pages and channels the outreach discovery found on its own (status active | removed by the user).
CREATE TABLE IF NOT EXISTS found_sources (
    key TEXT PRIMARY KEY, kind TEXT, name TEXT, url TEXT,
    status TEXT NOT NULL, first_seen INTEGER, last_checked INTEGER,
    tries INTEGER DEFAULT 0       -- failed fetches of a queued page
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for column in ("stack TEXT", "applied_at INTEGER", "snippet TEXT", "has_test INTEGER", "dismissed_at INTEGER", "message_id INTEGER", "desc_hash TEXT"):  # migrations for databases created before these columns
        try:
            db.execute(f"ALTER TABLE vacancies ADD COLUMN {column}")
            db.commit()
        except sqlite3.OperationalError:
            pass
    try:  # found_sources.tries for databases created before it existed
        db.execute("ALTER TABLE found_sources ADD COLUMN tries INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass
    return db


def known_ids(db: sqlite3.Connection) -> set[str]:
    return {r["id"] for r in db.execute("SELECT id FROM vacancies")}


def description_hash(text: str | None) -> str | None:
    """Fingerprint of the vacancy text: the same post on two sites gives the same value. Short texts are too generic to compare."""
    norm = re.sub(r"\W+", " ", (text or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest() if len(norm) >= 200 else None


def _host(url: str | None) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def find_repost(db: sqlite3.Connection, desc_hash: str | None, url: str | None) -> str | None:
    """Id of a stored vacancy with the same text from another site. The same site is not a repost: hh shows
    one job with different employment types as separate vacancies, and both are kept on purpose."""
    if not desc_hash:
        return None
    for row in db.execute("SELECT id, url FROM vacancies WHERE desc_hash=?", (desc_hash,)):
        if _host(row["url"]) != _host(url):
            return row["id"]
    return None


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
        "snippet": extra.get("snippet"),
        "has_test": 1 if v.get("has_test") else 0,
        "desc_hash": extra.get("desc_hash"),
    }
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    db.execute(f"INSERT OR REPLACE INTO vacancies ({cols}) VALUES ({marks})", row)
    db.commit()


def mark_notified(db: sqlite3.Connection, vacancy_id: str, message_id: int | None = None) -> None:
    db.execute(
        "UPDATE vacancies SET status='notified', notified_at=?, message_id=? WHERE id=?",
        (int(time.time()), message_id, vacancy_id),
    )
    db.commit()


def mark_dismissed(db: sqlite3.Connection, vacancy_id: str) -> None:
    """The user pressed ✕: the vacancy is not wanted, so rebuilds skip it and it is never sent again."""
    db.execute("UPDATE vacancies SET status='dismissed', dismissed_at=? WHERE id=? AND applied_at IS NULL", (int(time.time()), vacancy_id))
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
