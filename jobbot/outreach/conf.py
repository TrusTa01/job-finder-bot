"""Outreach settings: the `outreach:` block of config.yaml over built-in defaults, plus the /hidden on|off switch."""
from .. import store
from ..filters import CFG

DEFAULTS = {
    "enabled": False,
    "profile_terms": [],          # what the user does, for example ["flutter", "dart"]; search queries and relevance checks use it
    "exclude_terms": [],
    "search_queries": [
        "студия разработки {term}",
        "{term} разработка под ключ студия",
        "аутстафф разработчиков {term}",
        "{term} разработчики аутсорс компания",
        "заказать разработку на {term}",
        "команда {term} разработчиков для проекта",
        "{term} development studio outsourcing",
        "hire {term} developers agency remote",
    ],
    "seed_sources": [],           # listing pages (catalogs, ratings) fetched on the first run: [{name, url}]
    "habr_queries": [],           # Habr Career vacancy searches whose employers become targets
    "kinds": ["studio", "outstaff", "agency", "product"],
    "pitch_variants": {"studio": "contract", "outstaff": "contract", "agency": "later", "product": "staff"},
    "max_new_per_day": 10,        # new target cards per rolling 24 hours; cards the user snoozed come back on top of this
    "follow_up_days": 7,          # one reminder this many days after "Написал"
    "later_days": 1,              # a card snoozed with "Позже" comes back after this many days
    "exclude_domains": [
        "hh.ru", "habr.com", "vc.ru", "t.me", "telegram.org", "github.com", "youtube.com", "vk.com", "linkedin.com",
        "facebook.com", "instagram.com", "twitter.com", "x.com", "wikipedia.org", "google.com", "yandex.ru", "ya.ru",
        "dzen.ru", "apple.com", "play.google.com", "rustore.ru", "medium.com", "pikabu.ru", "avito.ru", "superjob.ru",
        # social networks, CDNs, counters and ad networks that a company page links to
        "telegram.me", "ok.ru", "wa.me", "youtu.be", "tiktok.com", "rutube.ru", "behance.net", "dribbble.com", "habr.io", "habrastorage.org",
        "mail.ru", "yandex.com", "yandex.net", "cloudflare.com", "gstatic.com", "googleapis.com", "googletagmanager.com",
        "google-analytics.com", "doubleclick.net", "apps.apple.com",
    ],
    "discovery": {
        "enabled": True,
        "providers": ["yandex", "serper", "ddg"],   # one without a key in .env is skipped
        "combine": True,                            # ask every provider that has a key and merge (a wider pool); False: first working one only
        "yandex_mode": "sync",                      # "async" is cheaper but was not available for the author's account (HTTP 501)
        "max_queries_per_day": 30,                  # each query returns up to 20 pages, far more than the LLM can classify in a day
        "price_per_query_rub": 0.49,                # Yandex sync, incl. VAT (the docs example is 488 RUB per 1000); only for the estimate in the summary
        "rest_days": 14,                            # a query that found nothing new twice in a row rests this long
        "max_pages_per_run": 25,                    # pages sent to the LLM per run
    },
}


def _fits(default, value) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _merge(defaults: dict, user) -> dict:
    """The user's values over the defaults. A key with nothing after it in YAML is None, and a value of the wrong type is a
    typo: both keep the default instead of crashing every pass later. Lists of words keep only their non-empty strings."""
    user = user if isinstance(user, dict) else {}
    out = {}
    for key, default in defaults.items():
        value = user.get(key)
        if isinstance(default, dict):
            out[key] = {**_merge(default, value), **{k: v for k, v in (value if isinstance(value, dict) else {}).items() if k not in default}}
        elif value is None or not _fits(default, value):
            out[key] = default
        elif isinstance(default, list) and all(isinstance(x, str) for x in default):
            out[key] = [x.strip() for x in value if isinstance(x, str) and x.strip()]
        else:
            out[key] = value
    return {**{k: v for k, v in user.items() if k not in defaults and v is not None}, **out}


def cfg() -> dict:
    return _merge(DEFAULTS, CFG.get("outreach"))


def enabled(db) -> bool:
    """/hidden on|off (kept in meta) wins over the config value."""
    override = store.get_meta(db, "outreach_enabled", "")
    return override == "1" if override else bool(cfg()["enabled"])


def set_enabled(db, on: bool) -> None:
    store.set_meta(db, "outreach_enabled", "1" if on else "0")


def terms() -> list[str]:
    return [t for t in cfg()["profile_terms"] if t]
