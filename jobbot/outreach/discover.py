"""Self-directed discovery. Once a day the bot searches the web, adds the pages it finds to a queue, and every pass
the LLM classifies a few queued pages: a company becomes a target, a catalog or article of studios becomes a source whose
links are queued in turn, a list of job channels adds the relevant public channels. New sources are connected without
asking; /sources and /removesource undo them. Nothing here depends on a specific stack: the words come from outreach.profile_terms.
"""
import json
import re
import time
from urllib.parse import urlparse

from .. import llm, store, tgchannels
from ..filters import CFG
from . import catalogs, conf, search, targets, web

FIND_EVERY_SECONDS = 24 * 3600
LISTING_HOSTS = ("vc.ru", "habr.com")      # content sites: an article there may list studios, but never counts as a company
COMPANY_ON_AGGREGATOR = ("career.habr.com",)  # the company page links to the company's own site, which is the real target
MAX_LISTING_LINKS = 40
MAX_CHANNELS_PER_PAGE = 10
PASS_SECONDS = 600         # time budget of one classification run; the cron line is hourly and takes a lock
CHANNEL_RECHECK_DAYS = 30  # a loaded channel that did not fit is looked at again after this long
MAX_PAGE = 5            # how deep into the results one query goes before it rests
DRY_RUNS_TO_REST = 2    # runs without a single new page before a query rests


def _queries() -> list[str]:
    """Every search query: each template for each profile term."""
    templates = conf.cfg()["search_queries"]
    # replace, not str.format: a template with other braces ("{city}", a literal "{") must not crash every pass
    return list(dict.fromkeys(t.replace("{term}", term) for term in conf.terms() for t in templates))


def _is_content_site(url: str) -> bool:
    host = web.host(url)
    return any(host == h or host.endswith("." + h) for h in LISTING_HOSTS)


def _worth_queueing(url: str) -> bool:
    return bool(web.host(url)) and (not targets.excluded(url) or _is_content_site(url))


def _jobs(limit: int, state: dict, now: int) -> list[tuple[str, int]]:
    """(query, page) pairs for today. A query that keeps finding new pages goes one page deeper each day; one that found
    nothing new twice in a row rests for `rest_days` (see _learn). Queries that are due take turns when there are more than `limit`."""
    due = [q for q in _queries() if state.get(q, {}).get("rest_until", 0) <= now]
    if not due:
        return []
    shift = (now // 86400 * limit) % len(due)
    return [(q, int(state.get(q, {}).get("page", 0))) for q in (due[shift:] + due[:shift])[:limit]]


def _learn(state: dict, job: tuple[str, int], new_pages: int, now: int, rest_days: int) -> None:
    query, page = job
    s = state.setdefault(query, {"page": 0, "dry": 0, "rest_until": 0})
    if new_pages:
        s["page"], s["dry"] = page + 1, 0
    else:
        s["dry"] += 1
        s["page"] = page + 1 if s["dry"] < DRY_RUNS_TO_REST else 0  # a dry page may still be followed by a good one
    if s["dry"] >= DRY_RUNS_TO_REST or s["page"] > MAX_PAGE:
        state[query] = {"page": 0, "dry": 0, "rest_until": now + rest_days * 86400}


def count_queries(db, provider: str, n: int) -> None:
    """Remember how many requests each search provider got: one counter since the last daily summary and one per month."""
    if not n or not provider:
        return
    for name in provider.split("+"):
        for key in (f"outreach_q_{name}_since_digest", f"outreach_q_{name}_{time.strftime('%Y-%m', time.gmtime())}"):
            store.set_meta(db, key, str(int(store.get_meta(db, key, "0")) + n))


def find(db, force: bool = False) -> dict:
    """Search the web (at most once a day) and queue every new page for classification."""
    stats = {"queries": 0, "hits": 0, "queued": 0, "provider": ""}
    settings = conf.cfg()["discovery"]
    if not settings["enabled"]:
        return stats
    now = int(time.time())
    if not force and now - int(store.get_meta(db, "outreach_find_ts", "0")) < FIND_EVERY_SECONDS:
        return stats
    store.set_meta(db, "outreach_find_ts", str(now))  # set first: a failing search must not be retried every 15 minutes
    stats["queued"] += catalogs.queue_seeds(db) + catalogs.queue_habr(db)
    try:
        state = json.loads(store.get_meta(db, "outreach_query_state", "{}"))
    except ValueError:
        state = {}
    jobs = _jobs(int(settings["max_queries_per_day"]), state, now)
    per_job, provider = search.search(jobs, settings["providers"], settings["yandex_mode"], settings["combine"]) if jobs else ({}, "")
    stats.update(queries=len(jobs), hits=sum(len(h) for h in per_job.values()), provider=provider)
    count_queries(db, provider, len(jobs))
    known = targets.known_keys(db)
    for job, hits in per_job.items():
        # one queue entry per company site (its /about, /services and /contacts would each cost an LLM call); content sites
        # such as vc.ru are listed per article, since each article is a different list of studios
        fresh = sum(bool(_worth_queueing(h["url"]) and targets.domain_key(h["url"]) not in known
                         and targets.queue(db, h["url"], "search", by_host=not _is_content_site(h["url"]))) for h in hits)
        stats["queued"] += fresh
        _learn(state, job, fresh, now, int(settings["rest_days"]))
    store.set_meta(db, "outreach_query_state", json.dumps(state, ensure_ascii=False))
    return stats


def _add_company(db, url: str, page: str, verdict: dict, source: str, stats: dict) -> None:
    kind = verdict.get("kind")
    if kind not in conf.cfg()["kinds"]:
        return
    site, contact_page = url, page
    if web.host(url) in COMPANY_ON_AGGREGATOR:
        # The first outside link of a Habr company page may be a social network, a counter or an advert, so it is not trusted
        # as "the company's site": its root only goes to the queue, and the page itself is classified like any other.
        own = next((link for link in web.links_of(page, url) if not targets.excluded(link) and web.host(link) != web.host(url)), None)
        if own:
            parts = urlparse(own)
            if targets.domain_key(f"{parts.scheme}://{parts.netloc}/") not in targets.known_keys(db):
                targets.queue(db, f"{parts.scheme}://{parts.netloc}/", "habr", by_host=True)
        return
    if targets.excluded(site):
        return
    emails, names = web.emails_of(contact_page), web.tme_names(contact_page)
    if not emails and not names:  # nothing on the home page: one try at the site's own contacts page
        contacts_url = next((link for link in web.links_of(contact_page, site)
                             if web.host(link) == web.host(site) and re.search(r"contact|kontakt|контакт", link, re.I)), None)
        if contacts_url:
            extra = web.fetch(contacts_url) or ""
            emails, names = web.emails_of(extra), web.tme_names(extra)
    contacts = [("telegram", "@" + names[0]) if names else None, ("email", emails[0]) if emails else None]
    contacts = [c for c in contacts if c]
    added = targets.add(
        db, targets.domain_key(site), (verdict.get("name") or web.host(site))[:120], kind, site, source,
        str(verdict.get("stack_hint") or "")[:120], str(verdict.get("summary") or "")[:300],
        str(verdict.get("contact_name") or "")[:80], str(verdict.get("contact_role") or "")[:80],
        "+".join(c[0] for c in contacts), "; ".join(c[1] for c in contacts),
    )
    stats["targets"] += added is not None


def _add_listing(db, url: str, page: str, verdict: dict, stats: dict) -> None:
    name = (verdict.get("name") or web.title_of(page) or web.host(url))[:120]
    if targets.register_source(db, "lst:" + url[:300], "listing", name, url):
        stats["sources"] += 1
    known, queued = targets.known_keys(db), 0
    for link in web.links_of(page, url):
        if web.host(link) == web.host(url) or targets.excluded(link):
            continue
        parts = urlparse(link)
        root = f"{parts.scheme}://{parts.netloc}/"
        if targets.domain_key(root) not in known and targets.queue(db, root, "listing", by_host=True):
            queued += 1
        if queued >= MAX_LISTING_LINKS:
            break


def consider_channel(db, name: str) -> bool:
    """Connect a public Telegram channel when its recent posts mention the user's field. True when it was added."""
    key = "tgch:" + name.lower()
    if name.lower() in {c.lower() for c in tgchannels.channels(db)}:
        return False
    row = targets.source_row(db, key)
    stale = row is not None and row["kind"] == "checked_channel" and time.time() - (row["last_checked"] or 0) > CHANNEL_RECHECK_DAYS * 86400
    if row is not None and not stale:
        return False  # connected, removed by the user, or looked at recently
    page = tgchannels._fetch(name)
    if not page:
        return False  # unreachable (proxy down, rate limit) is not a verdict on the channel: it is looked at again on a later pass
    url = f"https://t.me/s/{name}"
    # the same test the channel is read with afterwards (tgchannels), so "connected" means its posts can become candidates
    if not any(tgchannels.signal().search(p["text"]) for p in tgchannels._posts(page)):
        targets.touch_checked(db, key, name, url)  # loaded fine, but not about the user's field: not asked again for a while
        return False
    tgchannels.remember_channel(db, name)
    if row is not None:
        targets.promote_channel(db, key)
        return True
    return targets.register_source(db, key, "channel", name, url)


def _add_channels(db, page: str, stats: dict) -> None:
    for name in web.tme_names(page)[:MAX_CHANNELS_PER_PAGE]:
        stats["channels"] += consider_channel(db, name)


def process(db, limit: int) -> dict:
    """Classify up to `limit` queued pages. Stops early when the daily LLM budget is spent or the model is down."""
    stats = {"triaged": 0, "targets": 0, "sources": 0, "channels": 0, "budget": False}
    budget = CFG["limits"].get("daily_llm_budget_usd", 0.5)
    deadline = time.time() + PASS_SECONDS
    for row in targets.next_queued(db, limit):
        if time.time() > deadline or web.saturated():
            break  # out of time, or hostile servers are holding fetches open: what is left stays queued for the next pass
        if store.spend_last_day(db) >= budget:
            stats["budget"] = True
            break
        url = row["url"]
        page = web.fetch(url)
        if page is None:
            targets.fail_candidate(db, row["key"])  # a timeout or a robots.txt that did not load: tried again later, three times in all
            continue
        try:
            verdict, usage = llm.triage_page(conf.terms(), url, web.title_of(page), web.text_of(page), conf.cfg()["exclude_terms"])
            store.log_usage(db, *usage)
        except RuntimeError:
            break  # the model is down: the page stays queued for the next pass
        targets.finish_candidate(db, row["key"])
        stats["triaged"] += 1
        if not verdict.get("relevant"):
            continue
        page_type = verdict.get("page_type")
        if page_type == "company":
            _add_company(db, url, page, verdict, row["name"], stats)
        elif page_type == "listing":
            _add_listing(db, url, page, verdict, stats)
        elif page_type == "job_channels":
            _add_channels(db, page, stats)
    return stats
