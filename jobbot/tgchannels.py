"""Cheap watcher for public Telegram job channels, read through the t.me/s/<channel> web preview.

No LLM: a post is reported when it mentions Flutter/Dart, is a vacancy (not a CV) and is not senior-only.
Runs every few hours through the same proxy as the bot; ids are stored with a "tg:" prefix.
"""
import html
import re
import time
from datetime import datetime, timezone

import requests

from . import llm, notify, settings, store
from .filters import CFG

CHANNELS = ()
CHECK_EVERY_SECONDS = 2 * 3600
MAX_PER_RUN = 5
MAX_AGE_DAYS = 30
SIGNAL = re.compile(r"flutter|\bdart\b", re.I)
SENIOR = re.compile(r"senior|сеньор|\blead\b|тимлид|ведущий|head of|principal|staff", re.I)
LEVEL_OK = re.compile(r"middle|junior|джун|миддл|стаж|trainee|\bintern\b", re.I)
CV_POST = re.compile(r"#ищу|#резюме|#cv\b|ищу работу", re.I)
POST = re.compile(r'data-post="([^"]+)".*?(?=data-post="|\Z)', re.S)


def _fetch(channel: str) -> str:
    proxy = settings.telegram_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for _ in range(3):  # the proxy drops the occasional connection
        try:
            r = requests.get(f"https://t.me/s/{channel}", proxies=proxies, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if r.status_code == 200:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(2)
    return ""


def _posts(page: str) -> list[dict]:
    out = []
    for m in POST.finditer(page):
        block = m.group(0)
        text = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        when = re.search(r'datetime="([^"]+)"', block)
        if not text or not when:
            continue
        plain = re.sub(r"<br\s*/?>", "\n", text.group(1))
        plain = html.unescape(re.sub(r"<[^>]+>", "", plain)).strip()
        plain = plain.split("Мобильный трудоголик")[0].strip()  # channel advert footer mentions Flutter & Dart
        out.append({"post": m.group(1), "text": plain, "date": when.group(1)})
    return out


def _relevant(text: str) -> bool:
    head = text[:300]
    if not SIGNAL.search(text) or CV_POST.search(text):
        return False
    return not (SENIOR.search(head) and not LEVEL_OK.search(head))


def _fresh(date: str) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(date)).days <= MAX_AGE_DAYS
    except ValueError:
        return True


def check(db, send: bool = True) -> int:
    """Screen new relevant posts, write a personal message for each fitting one and report it. Returns how many were reported."""
    now = int(time.time())
    if now - int(store.get_meta(db, "tg_last_check", "0")) < CHECK_EVERY_SECONDS:
        return 0
    store.set_meta(db, "tg_last_check", str(now))
    if not send or not settings.PROFILE_PATH.exists():
        return 0  # the preview mode never calls the LLM
    profile = settings.PROFILE_PATH.read_text(encoding="utf-8")
    budget = CFG["limits"].get("daily_llm_budget_usd", 0.25)

    known = store.known_ids(db)
    reported = 0
    for channel in CHANNELS:
        for p in _posts(_fetch(channel)):
            key = f"tg:{p['post']}"
            if key in known or not _fresh(p["date"]) or not _relevant(p["text"]):
                continue
            if store.spend_last_day(db) >= budget:
                return reported  # the pass reports the budget problem; the posts stay unseen for the next check
            url = f"https://t.me/{p['post']}"
            lines = [ln.strip() for ln in p["text"].splitlines() if ln.strip() and not ln.strip().startswith("#")]
            title = lines[0][:100] if lines else ""
            snippet = " ".join(lines)[:500]
            entry = {
                "id": key, "published_at": p["date"], "name": title, "employer": {"name": channel},
                "area": {"name": ""}, "experience": {"name": ""}, "alternate_url": url,
            }
            try:
                decision, usage = llm.screen(profile, entry, p["text"])
                store.log_usage(db, *usage)
                fit, issue = decision.get("fit", "no"), decision.get("location_or_schedule_issue", "")
                if fit == "no":
                    store.save_vacancy(db, entry, "screened_out", fit=fit, reason=decision.get("reason"), stack=decision.get("stack"))
                    continue
                letter, usage = llm.write_letter(profile, entry, p["text"], issue, direct_message=True)
                store.log_usage(db, *usage)
            except RuntimeError:
                continue  # not saved, so the next check retries it
            store.save_vacancy(
                db, entry, "ready", fit=fit, reason=decision.get("reason"), issue=issue,
                stack=decision.get("stack"), letter=letter, snippet=snippet,
            )
            row = db.execute("SELECT * FROM vacancies WHERE id=?", (key,)).fetchone()
            if notify.send(notify.format_vacancy(row), key):
                store.mark_notified(db, key)
                reported += 1
            if reported >= MAX_PER_RUN:
                return reported
    return reported
