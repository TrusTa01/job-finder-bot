"""GetMatch watcher: its open API lists all offers, so we filter for Flutter locally.

Runs every few hours; the LLM screens and drafts a letter only for the few offers that pass (see candidates.py).
Ids are stored with a "getmatch:" prefix.
"""
import html
import re
import time

import requests

from . import candidates, store

URL = "https://getmatch.ru/api/offers"
PAGE = 100
SIGNALS = ("flutter", "dart")
NOT_A_DEV_ROLE = (
    "qa", "aqa", "тестир", "analyst", "аналитик", "менеджер", "manager", "маркет", "медиабайер",
    "руководитель", "lead", "тимлид", "head", "director", "директор",
)
NATIVE_ONLY = ("android", "андроид", "ios", "kotlin", "swift")  # a Flutter signal in skills alone is not enough
CHECK_EVERY_SECONDS = 3 * 3600
MAX_PER_RUN = 5


def _skills(offer: dict) -> list[str]:
    out = []
    for s in offer.get("skills_objects") or []:
        out.append(str(s.get("name") or s.get("title") or s.get("label") or "") if isinstance(s, dict) else str(s))
    return out


def _relevant(offer: dict) -> bool:
    """Flutter or Dart in the position or among the declared skills, and not a QA/analyst/manager role."""
    position = (offer.get("position") or "").lower()
    if any(w in position for w in NOT_A_DEV_ROLE):
        return False
    if any(w in position for w in SIGNALS):
        return True
    if any(w in position for w in NATIVE_ONLY):
        return False
    return any(w in " ".join(_skills(offer)).lower() for w in SIGNALS)


def fetch() -> list[dict]:
    offers: list[dict] = []
    offset = 0
    while True:
        try:
            r = requests.get(URL, params={"limit": PAGE, "offset": offset}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            data = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            break
        batch = data.get("offers", [])
        offers += batch
        offset += len(batch)
        if not batch or offset >= data.get("meta", {}).get("total", 0):
            break
    return offers


def _description(o: dict) -> str:
    raw = " ".join(str(o.get(k) or "") for k in ("description_html", "offer_description"))
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def check(db, send: bool = True) -> int:
    """Screen new relevant offers, draft a letter for each fitting one and report it. Returns how many were reported."""
    now = int(time.time())
    if now - int(store.get_meta(db, "getmatch_last_check", "0")) < CHECK_EVERY_SECONDS:
        return 0
    store.set_meta(db, "getmatch_last_check", str(now))

    known = store.known_ids(db)
    fresh = [o for o in fetch() if f"getmatch:{o['id']}" not in known and _relevant(o)]
    profile = candidates.load_profile()
    if not send or not profile:
        return len(fresh) if not send else 0  # the preview mode never calls the LLM
    reported = 0
    for o in fresh:
        company = (o.get("company") or {}).get("name", "")
        formats = sorted({i.get("format", "") for i in o.get("location_items") or [] if isinstance(i, dict)} - {""})
        fmt = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}
        where = ", ".join(fmt.get(f, f) for f in formats) or "формат не указан"
        lo, hi = o.get("salary_display_from"), o.get("salary_display_to")
        salary = f"{lo or ''}–{hi or ''} {o.get('salary_currency') or ''}".strip(" –") if (lo or hi) else "зарплата не указана"
        entry = {
            "id": f"getmatch:{o['id']}", "published_at": o.get("published_at"), "name": o.get("position"),
            "employer": {"name": company}, "area": {"name": where}, "experience": {"name": ""},
            "alternate_url": "https://getmatch.ru" + (o.get("url") or ""),
        }
        description = f"Зарплата: {salary}. " + _description(o)
        result = candidates.handle(db, profile, entry, description)
        if result == candidates.BUDGET:
            break
        reported += result == candidates.SENT
        if reported >= MAX_PER_RUN:
            break
    return reported
