"""Shared step for the secondary sources (Habr, GetMatch, Telegram channels):
LLM screening, a draft letter, storage and the Telegram report. hh.ru has its own loop in run.py.
"""
import re

from . import llm, notify, store
from .filters import CFG, WORK_FORMATS, priority_after_screen
from .settings import PROFILE_PATH

SENT, DROPPED, SAVED, RETRY, BUDGET = "sent", "dropped", "saved", "retry", "budget"
# Each unaccepted format gets its words: office words unless ON_SITE is accepted, "гибрид" unless HYBRID is
_office_words = ([] if "ON_SITE" in WORK_FORMATS else ["на месте", "в офисе", "офисн", r"\bочно"]) + ([] if "HYBRID" in WORK_FORMATS else ["гибрид"])
OFFICE = re.compile("|".join(_office_words), re.I) if _office_words else None
REMOTE = re.compile("удал|remote|из дома", re.I)


def onsite_only(entry: dict, description: str) -> bool:
    """Office or hybrid work and no mention of remote work: not for us, whatever the source. Only the formats missing from work_formats count."""
    if OFFICE is None:
        return False
    text = " ".join([(entry.get("area") or {}).get("name", ""), entry.get("name") or "", description or ""])
    return bool(OFFICE.search(text)) and not REMOTE.search(text)


def load_profile() -> str | None:
    return PROFILE_PATH.read_text(encoding="utf-8") if PROFILE_PATH.exists() else None


def handle(db, profile: str, entry: dict, description: str, direct_message: bool = False, snippet: str | None = None) -> str:
    """Screen one vacancy and, when it fits, store a draft letter and send the report.

    Returns SENT (reported), SAVED (stored, delivery retried by run.py), DROPPED (does not fit),
    RETRY (the model failed, nothing stored) or BUDGET (daily LLM budget reached, nothing done).
    """
    if onsite_only(entry, description):
        return DROPPED  # before any LLM call, so it costs nothing
    desc_hash = store.description_hash(description)
    original = store.find_repost(db, desc_hash, entry.get("alternate_url"))
    if original:
        store.save_vacancy(db, entry, "duplicate", desc_hash=desc_hash, reason=f"репост {original}")
        return DROPPED  # the same text already came from another site, before any LLM call
    if store.spend_last_day(db) >= CFG["limits"].get("daily_llm_budget_usd", 0.25):
        return BUDGET
    try:
        decision, usage = llm.screen(profile, entry, description)
        store.log_usage(db, *usage)
        fit, issue = decision.get("fit", "no"), decision.get("location_or_schedule_issue", "")
        if fit == "no":
            store.save_vacancy(db, entry, "screened_out", fit=fit, reason=decision.get("reason"), stack=decision.get("stack"), desc_hash=desc_hash)
            return DROPPED
        letter, usage = llm.write_letter(profile, entry, description, issue, direct_message=direct_message)
        store.log_usage(db, *usage)
    except RuntimeError:
        return RETRY  # not saved, so the next check retries it
    store.save_vacancy(
        db, entry, "ready", priority_after_screen(fit, decision.get("required_grade"), issue), fit=fit, reason=decision.get("reason"), issue=issue,
        stack=decision.get("stack"), letter=letter, snippet=snippet, desc_hash=desc_hash,
    )
    row = db.execute("SELECT * FROM vacancies WHERE id=?", (entry["id"],)).fetchone()
    message_id = notify.send_card(notify.format_vacancy(row), entry["id"])
    if message_id is not None:
        store.mark_notified(db, entry["id"], message_id)
        return SENT
    return SAVED
