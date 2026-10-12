"""Starting points for discovery: the listing pages from `seed_sources` and the employers of Habr Career vacancy searches.
Both only put page urls in the queue; discovery.py decides with the LLM what each page is (no per-site HTML selectors)."""
import requests

from . import conf, targets

HABR_VACANCIES = "https://career.habr.com/api/frontend/vacancies"
HABR = "https://career.habr.com"


def queue_seeds(db) -> int:
    return sum(targets.queue(db, s["url"], "seed") for s in conf.cfg()["seed_sources"] if isinstance(s, dict) and s.get("url"))


def queue_habr(db) -> int:
    queued = 0
    for query in conf.cfg()["habr_queries"]:
        try:
            r = requests.get(HABR_VACANCIES, params={"q": query, "type": "all", "sort": "date", "per_page": 50},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            vacancies = r.json().get("list", []) if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            continue
        for v in vacancies:
            href = (v.get("company") or {}).get("href") or ""
            if href.startswith("/companies/"):
                queued += targets.queue(db, HABR + href.split("?")[0], "habr")
    return queued
