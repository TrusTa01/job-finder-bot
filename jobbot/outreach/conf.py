"""Outreach settings: the `outreach:` block of config.yaml over built-in defaults, plus the /hidden on|off switch."""
from .. import store
from ..filters import CFG

DEFAULTS = {
    "enabled": False,
    "profile_terms": [],          # what the user does, for example ["flutter", "dart"]; search queries and relevance checks use it
    "exclude_terms": [],
    "search_queries": [
        "студия разработки мобильных приложений {term}",
        "{term} разработка под ключ студия",
        "аутстафф разработчиков {term}",
        "{term} разработчики аутсорс компания",
        "заказать мобильное приложение на {term}",
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
    "later_days": 3,              # a card snoozed with "Позже" comes back after this many days
    "exclude_domains": [
        "hh.ru", "habr.com", "vc.ru", "t.me", "telegram.org", "github.com", "youtube.com", "vk.com", "linkedin.com",
        "facebook.com", "instagram.com", "twitter.com", "x.com", "wikipedia.org", "google.com", "yandex.ru", "ya.ru",
        "dzen.ru", "apple.com", "play.google.com", "rustore.ru", "medium.com", "pikabu.ru", "avito.ru", "superjob.ru",
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


def cfg() -> dict:
    user = CFG.get("outreach") or {}
    merged = {**DEFAULTS, **user}
    merged["discovery"] = {**DEFAULTS["discovery"], **(user.get("discovery") or {})}
    return merged


def enabled(db) -> bool:
    """/hidden on|off (kept in meta) wins over the config value."""
    override = store.get_meta(db, "outreach_enabled", "")
    return override == "1" if override else bool(cfg()["enabled"])


def set_enabled(db, on: bool) -> None:
    store.set_meta(db, "outreach_enabled", "1" if on else "0")


def terms() -> list[str]:
    return [t for t in cfg()["profile_terms"] if t]
