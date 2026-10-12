"""Storage helpers for outreach targets (companies to write to) and the sources discovery found on its own."""
import sqlite3
import time

from . import conf, web

DAY = 86400


def domain_key(url: str) -> str:
    return "co:" + web.host(url)


def excluded(url: str) -> bool:
    d = web.host(url)
    return not d or any(d == x or d.endswith("." + x) for x in conf.cfg()["exclude_domains"])


def add(db, key: str, name: str, kind: str, site: str, source: str, stack_hint: str = "", summary: str = "",
        contact_name: str = "", contact_role: str = "", contact_channel: str = "", contact_value: str = "") -> int | None:
    """Insert a new target. Returns its id, or None when the key already exists."""
    cur = db.execute(
        "INSERT OR IGNORE INTO targets (key, name, kind, site, source, stack_hint, summary, contact_name, contact_role, contact_channel, "
        "contact_value, status, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?)",
        (key, name, kind, site, source, stack_hint, summary, contact_name, contact_role, contact_channel, contact_value, int(time.time())),
    )
    db.commit()
    return cur.lastrowid if cur.rowcount else None


def known_keys(db) -> set[str]:
    return {r["key"] for r in db.execute("SELECT key FROM targets")}


def get(db, target_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()


def set_pitch(db, target_id: int, pitch: str, variant: str) -> None:
    db.execute("UPDATE targets SET pitch=?, pitch_variant=?, status='drafted' WHERE id=?", (pitch, variant, target_id))
    db.commit()


def mark_notified(db, target_id: int, message_id: int | None) -> None:
    db.execute("UPDATE targets SET status='notified', notified_at=?, message_id=? WHERE id=?", (int(time.time()), message_id, target_id))
    db.commit()


def set_status(db, target_id: int, status: str) -> None:
    db.execute("UPDATE targets SET status=? WHERE id=?", (status, target_id))
    db.commit()


def mark_sent(db, target_id: int) -> None:
    """The user wrote to the company: remember when, and schedule one reminder."""
    now = int(time.time())
    days = int(conf.cfg()["follow_up_days"])
    db.execute("UPDATE targets SET status='sent', sent_at=?, follow_up_at=?, follow_up_sent=0 WHERE id=?", (now, now + days * DAY, target_id))
    db.commit()


def mark_later(db, target_id: int) -> None:
    """The user pressed "Позже": the card comes back after `later_days`, on top of the daily cap."""
    when = int(time.time()) + int(conf.cfg()["later_days"]) * DAY
    db.execute("UPDATE targets SET status='later', follow_up_at=? WHERE id=?", (when, target_id))
    db.commit()


def due_later(db) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM targets WHERE status='later' AND follow_up_at<=? ORDER BY follow_up_at", (int(time.time()),)).fetchall()


def mark_returned(db, target_id: int, message_id: int | None) -> None:
    """A snoozed card was sent again. notified_at stays as it was, so it does not count against the daily cap."""
    db.execute("UPDATE targets SET status='notified', message_id=?, follow_up_at=NULL WHERE id=?", (message_id, target_id))
    db.commit()


def set_contact(db, target_id: int, channel: str, value: str) -> None:
    db.execute("UPDATE targets SET contact_channel=?, contact_value=? WHERE id=?", (channel, value, target_id))
    db.commit()


def due_followups(db) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM targets WHERE status='sent' AND follow_up_sent=0 AND follow_up_at<=? ORDER BY follow_up_at", (int(time.time()),)
    ).fetchall()


def mark_followup_sent(db, target_id: int) -> None:
    db.execute("UPDATE targets SET follow_up_sent=1 WHERE id=?", (target_id,))
    db.commit()


def new_cards_today(db) -> int:
    return db.execute("SELECT COUNT(*) FROM targets WHERE notified_at>=?", (int(time.time()) - DAY,)).fetchone()[0]


def funnel(db) -> dict[str, int]:
    return {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM targets GROUP BY status")}


# --- sources found by discovery -------------------------------------------------------------

def register_source(db, key: str, kind: str, name: str, url: str) -> bool:
    """Remember a source. Returns True only for a new one; a source the user removed stays removed."""
    cur = db.execute(
        "INSERT OR IGNORE INTO found_sources (key, kind, name, url, status, first_seen, last_checked) VALUES (?,?,?,?, 'active', ?, ?)",
        (key, kind, name, url, int(time.time()), int(time.time())),
    )
    db.commit()
    return bool(cur.rowcount)


def source_row(db, key: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM found_sources WHERE key=?", (key,)).fetchone()


def source_known(db, key: str) -> bool:
    return source_row(db, key) is not None


def touch_checked(db, key: str, name: str, url: str) -> None:
    """A channel that was loaded and did not mention the user's field: remembered, and looked at again only after a while."""
    db.execute(
        "INSERT INTO found_sources (key, kind, name, url, status, first_seen, last_checked) VALUES (?, 'checked_channel', ?, ?, 'active', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET last_checked=excluded.last_checked", (key, name, url, int(time.time()), int(time.time())),
    )
    db.commit()


def promote_channel(db, key: str) -> None:
    """A channel that was checked earlier and now fits becomes a real, listed source."""
    db.execute("UPDATE found_sources SET kind='channel', status='active', first_seen=?, last_checked=? WHERE key=?", (int(time.time()), int(time.time()), key))
    db.commit()


SHOWN_KINDS = ("listing", "channel")  # what /sources lists; candidate and checked_channel rows are internal bookkeeping


def active_sources(db) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT rowid AS rid, * FROM found_sources WHERE status='active' AND kind IN ('listing','channel') ORDER BY first_seen"
    ).fetchall()


def remove_source_by_rowid(db, rid: int) -> sqlite3.Row | None:
    row = db.execute("SELECT rowid AS rid, * FROM found_sources WHERE rowid=? AND kind IN ('listing','channel')", (rid,)).fetchone()
    if row:
        db.execute("UPDATE found_sources SET status='removed' WHERE rowid=?", (rid,))
        db.commit()
    return row


def sources_since(db, ts: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT rowid AS rid, * FROM found_sources WHERE status='active' AND kind IN ('listing','channel') AND first_seen>? ORDER BY first_seen", (ts,)
    ).fetchall()


# --- the queue of pages waiting for the LLM ---------------------------------------------------

MAX_TRIES = 3          # fetch attempts per queued page
RETRY_AFTER = 3600     # seconds before a failed page is tried again


def queue(db, url: str, source: str, by_host: bool = False) -> bool:
    """Put a page in the queue once; a page seen before (done or removed) is not queued again.
    by_host: one queue entry per site, whatever page of it was found (many pages of one studio would each cost an LLM call)."""
    key = "cand:host:" + web.host(url) if by_host else "cand:" + url[:300]
    cur = db.execute(
        "INSERT OR IGNORE INTO found_sources (key, kind, name, url, status, first_seen, last_checked, tries) VALUES (?, 'candidate', ?, ?, 'queued', ?, 0, 0)",
        (key, source, url, int(time.time())),
    )
    db.commit()
    return bool(cur.rowcount)


def next_queued(db, n: int) -> list[sqlite3.Row]:
    """Oldest first; a page that failed to load waits RETRY_AFTER before its next attempt and goes behind the fresh ones."""
    return db.execute(
        "SELECT * FROM found_sources WHERE kind='candidate' AND status='queued' AND last_checked<=? ORDER BY last_checked, first_seen LIMIT ?",
        (int(time.time()) - RETRY_AFTER, n),
    ).fetchall()


def finish_candidate(db, key: str) -> None:
    db.execute("UPDATE found_sources SET status='done', last_checked=? WHERE key=?", (int(time.time()), key))
    db.commit()


def fail_candidate(db, key: str) -> None:
    """The page could not be loaded (timeout, robots.txt unreachable, rate limit): try again on a later pass, MAX_TRIES times in all."""
    db.execute(
        "UPDATE found_sources SET tries=COALESCE(tries,0)+1, last_checked=?, status=CASE WHEN COALESCE(tries,0)+1>=? THEN 'done' ELSE 'queued' END WHERE key=?",
        (int(time.time()), MAX_TRIES, key),
    )
    db.commit()
