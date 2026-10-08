"""Cheap Habr Career watcher: one request every few hours, no LLM.

Habr currently has almost no Flutter vacancies, so this only reports a link when one appears.
A vacancy is reported once; ids are stored with a "habr:" prefix next to the hh ones.
"""
import time

import requests

from . import notify, store

URL = "https://career.habr.com/api/frontend/vacancies"
QUERY = {"q": "Flutter", "type": "all", "sort": "date", "per_page": 50}
SIGNALS = ("flutter", "dart", "флаттер")
CHECK_EVERY_SECONDS = 3 * 3600
MAX_PER_RUN = 5


def _relevant(v: dict) -> bool:
    """Habr search is loose, so require the signal in the title or in the skills."""
    blob = v.get("title", "") + " " + " ".join(s.get("title", "") for s in v.get("skills") or [])
    return any(w in blob.lower() for w in SIGNALS)


def fetch() -> list[dict]:
    try:
        r = requests.get(URL, params=QUERY, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        return r.json().get("list", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []


def check(db, send: bool = True) -> int:
    """Report new relevant vacancies. Returns how many were reported."""
    now = int(time.time())
    if now - int(store.get_meta(db, "habr_last_check", "0")) < CHECK_EVERY_SECONDS:
        return 0
    store.set_meta(db, "habr_last_check", str(now))

    known = store.known_ids(db)
    reported = 0
    for v in fetch():
        key = f"habr:{v['id']}"
        if key in known or not _relevant(v):
            continue
        company = (v.get("company") or {}).get("title", "")
        salary = (v.get("salary") or {}).get("formatted") or "зарплата не указана"
        mode = "удалённо" if v.get("remoteWork") else "офис или гибрид"
        url = "https://career.habr.com" + v.get("href", "")
        entry = {
            "id": key, "published_at": None, "name": v.get("title"), "employer": {"name": company},
            "area": {"name": mode}, "experience": {"name": ""}, "alternate_url": url,
        }
        text = f"Хабр Карьера, новая вакансия: {v.get('title')}\n{company} · {mode} · {salary}\n{url}"
        if send and notify.send(text):
            store.save_vacancy(db, entry, "notified")
            reported += 1
        elif not send:
            reported += 1
        if reported >= MAX_PER_RUN:
            break
    return reported
