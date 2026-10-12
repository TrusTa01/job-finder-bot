"""Trudvsem watcher (Работа в России, federal state job board): public open-data API, no key needed.

One search every few hours per region (or nationwide when config has no regions). Queries, signals and stop words
come from config.yaml (trudvsem_queries, title_signals, description_signals, stop_roles_keywords, seniority).
Ids are stored with a "trud:" prefix next to the hh ones.
"""
import time

import requests

from . import candidates, store
from .filters import CFG, _has

API = "https://opendata.trudvsem.ru/api/v1/vacancies"
CHECK_EVERY_SECONDS = 3 * 3600
MAX_PER_RUN = 5
PER_PAGE = 100


def _urls() -> list[str]:
    """One endpoint per region in config; nationwide when no regions are set."""
    regions = CFG.get("trudvsem_regions") or []
    return [f"{API}/region/{code}" for code in regions] or [API]


def fetch(query: str) -> list[dict]:
    """All vacancies for one query across the configured regions, de-duplicated by id."""
    found: dict[str, dict] = {}
    for url in _urls():
        try:
            r = requests.get(url, params={"text": query, "limit": PER_PAGE}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            items = r.json().get("results", {}).get("vacancies", []) if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            items = []
        for item in items:
            v = item.get("vacancy") or {}
            if v.get("id"):
                found.setdefault(v["id"], v)
    return list(found.values())


def relevant(title: str, description: str) -> bool:
    """Same rules as hh: seniority and stop roles from the title, then the title or description must match the field."""
    t, d = title.lower(), description.lower()
    sen = CFG["seniority"]
    if _has(t, sen["skip"]) and not _has(t, sen["ok_markers"]):
        return False
    if _has(t, CFG["title_signals"]):
        return True
    if _has(t, CFG["stop_roles_keywords"]):
        return False
    return _has(d, CFG["description_signals"])


def _entry(v: dict) -> dict:
    """Vacancy in the shape candidates.handle expects (the same keys as hh)."""
    addresses = (v.get("addresses") or {}).get("address") or []
    area = addresses[0].get("location", "") if addresses else (v.get("region") or {}).get("name", "")
    experience = (v.get("requirement") or {}).get("experience")
    return {
        "id": f"trud:{v['id']}",
        "published_at": v.get("date_modify"),
        "name": v.get("job-name"),
        "employer": {"name": (v.get("company") or {}).get("name", "")},
        "area": {"name": area},
        "experience": {"name": f"от {experience} лет" if experience else ""},
        "alternate_url": v.get("vac_url"),
        "salary": None,  # the salary text stays in the description; hh-only fields are not needed here
    }


def _description(v: dict) -> str:
    parts = [v.get("requirements") or "", v.get("duty") or "", f"Зарплата: {v.get('salary') or 'не указана'}. График: {v.get('schedule') or 'не указан'}."]
    return "\n".join(p for p in parts if p.strip())


def check(db, send: bool = True) -> int:
    """Screen new relevant vacancies, draft a letter for each fitting one and report it. Returns how many were reported."""
    now = int(time.time())
    if now - int(store.get_meta(db, "trud_last_check", "0")) < CHECK_EVERY_SECONDS:
        return 0
    store.set_meta(db, "trud_last_check", str(now))

    known = store.known_ids(db)
    seen: dict[str, dict] = {}
    for q in CFG.get("trudvsem_queries") or []:
        for v in fetch(q):
            seen.setdefault(v["id"], v)
    fresh = [v for v in seen.values() if f"trud:{v['id']}" not in known and relevant(v.get("job-name") or "", _description(v))]
    profile = candidates.load_profile()
    if not send or not profile:
        return len(fresh) if not send else 0  # the preview mode never calls the LLM
    reported = 0
    for v in fresh:
        result = candidates.handle(db, profile, _entry(v), _description(v))
        if result == candidates.BUDGET:
            break
        reported += result == candidates.SENT
        if reported >= MAX_PER_RUN:
            break
    return reported
