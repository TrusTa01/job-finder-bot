"""Habr Career watcher: one search every few hours. Habr rarely has Flutter vacancies, so the LLM
screens and drafts a letter only for the few that appear (see candidates.py).
Ids are stored with a "habr:" prefix next to the hh ones.
"""
import html
import json
import re
import time

import requests

from . import candidates, store
from .filters import CFG

URL = "https://career.habr.com/api/frontend/vacancies"
# Searches and signals come from config.yaml (habr_queries, secondary_terms); the candidate's field decides them.
SIGNALS = tuple(w.lower() for w in CFG.get("secondary_terms") or [])
CHECK_EVERY_SECONDS = 3 * 3600
MAX_PER_RUN = 5


def _relevant(v: dict) -> bool:
    """Habr search is loose, so require the signal in the title or in the skills."""
    blob = v.get("title", "") + " " + " ".join(s.get("title", "") for s in v.get("skills") or [])
    return any(w in blob.lower() for w in SIGNALS)


def fetch() -> list[dict]:
    found: dict[int, dict] = {}
    for i, q in enumerate(q for q in CFG.get("habr_queries") or [] if q):
        if i:
            time.sleep(1)  # be polite between searches
        try:
            r = requests.get(URL, params={"q": q, "type": "all", "sort": "date", "per_page": 50}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            items = r.json().get("list", []) if r.status_code == 200 else []
        except (requests.RequestException, ValueError, AttributeError):
            items = []
        for v in items:
            if isinstance(v, dict) and v.get("id") is not None:
                found.setdefault(v["id"], v)
    return list(found.values())


def _description(href: str, fallback: str) -> str:
    """Vacancy text from the JSON-LD block of the vacancy page; the skills line when the page cannot be read."""
    try:
        page = requests.get("https://career.habr.com" + href, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
        start = page.find(">", page.find("application/ld+json")) + 1
        data = json.loads(page[start : page.find("</script>", start)])
        text = html.unescape(re.sub(r"<[^>]+>", " ", data.get("description", "")))
    except (requests.RequestException, ValueError):
        text = ""
    return text or fallback


def check(db, send: bool = True) -> int:
    """Screen new relevant vacancies, draft a letter for each fitting one and report it. Returns how many were reported."""
    now = int(time.time())
    if now - int(store.get_meta(db, "habr_last_check", "0")) < CHECK_EVERY_SECONDS:
        return 0
    store.set_meta(db, "habr_last_check", str(now))

    known = store.known_ids(db)
    fresh = [v for v in fetch() if f"habr:{v['id']}" not in known and _relevant(v)]
    profile = candidates.load_profile()
    if not send or not profile:
        return len(fresh) if not send else 0  # the preview mode never calls the LLM
    reported = 0
    for v in fresh:
        company = (v.get("company") or {}).get("title", "")
        salary = (v.get("salary") or {}).get("formatted") or "зарплата не указана"
        mode = "удалённо" if v.get("remoteWork") else "офис или гибрид"
        skills = ", ".join(s.get("title", "") for s in v.get("skills") or [])
        entry = {
            "id": f"habr:{v['id']}", "published_at": None, "name": v.get("title"), "employer": {"name": company},
            "area": {"name": mode}, "experience": {"name": (v.get("qualification") or {}).get("title", "")},
            "alternate_url": "https://career.habr.com" + v.get("href", ""),
        }
        description = _description(v.get("href", ""), f"Навыки: {skills}. {salary}.")
        result = candidates.handle(db, profile, entry, description)
        if result == candidates.BUDGET:
            break
        reported += result == candidates.SENT
        if reported >= MAX_PER_RUN:
            break
    return reported
