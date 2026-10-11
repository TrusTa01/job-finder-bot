"""Public Telegram job channels, read through the t.me/s/<channel> web preview.

A post is taken when it mentions Flutter/Dart, is a vacancy (not a CV) and is not senior-only; then the LLM
screens it and drafts a personal message to its author (see candidates.py). Runs every few hours through the
same proxy as the bot; ids are stored with a "tg:" prefix.
"""
import html
import re
import time
from datetime import datetime, timezone

import requests

from . import candidates, settings, store
from .filters import CFG

CHANNELS = tuple(CFG.get("telegram_channels") or ())  # channel names without @, set in config.yaml
CHECK_EVERY_SECONDS = 2 * 3600
MAX_PER_RUN = 5
MAX_AGE_DAYS = 30
SIGNAL = re.compile(r"flutter|\bdart\b", re.I)
SENIOR = re.compile(r"senior|сеньор|\blead\b|тимлид|ведущий|head of|principal|staff", re.I)
LEVEL_OK = re.compile(r"middle|junior|джун|миддл|стаж|trainee|\bintern\b", re.I)
CV_POST = re.compile(r"#ищу|#резюме|#cv\b|ищу работу", re.I)
POST = re.compile(r'data-post="([^"]+)".*?(?=data-post="|\Z)', re.S)


def channels(db) -> tuple:
    """Channels from config.yaml plus the ones added in the chat with /addchannel (kept in the meta table)."""
    extra = [c for c in store.get_meta(db, "extra_channels", "").split(",") if c]
    return tuple(dict.fromkeys([*CHANNELS, *extra]))


def add_channel(db, name: str) -> str:
    """Check a public channel and remember it. Returns a short answer for the chat."""
    name = name.strip().lstrip("@").removeprefix("https://t.me/").removeprefix("t.me/").strip("/")
    if not re.fullmatch("[A-Za-z0-9_]{5,32}", name):
        return "Имя канала: только латинские буквы, цифры и _, от 5 символов. Пример: /addchannel some_job_channel"
    if name in channels(db):
        return f"@{name} уже в списке."
    if not _fetch(name):
        return f"@{name} не открывается как публичный канал. Проверьте имя."
    extra = [c for c in store.get_meta(db, "extra_channels", "").split(",") if c] + [name]
    store.set_meta(db, "extra_channels", ",".join(extra))
    return f"Канал @{name} добавлен. Проверка по нему начнётся со следующего прохода."

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
    profile = candidates.load_profile()
    if not send or not profile:
        return 0  # the preview mode never calls the LLM

    known = store.known_ids(db)
    reported = 0
    for channel in channels(db):
        for p in _posts(_fetch(channel)):
            key = f"tg:{p['post']}"
            if key in known or not _fresh(p["date"]) or not _relevant(p["text"]):
                continue
            lines = [ln.strip() for ln in p["text"].splitlines() if ln.strip() and not ln.strip().startswith("#")]
            entry = {
                "id": key, "published_at": p["date"], "name": lines[0][:100] if lines else "", "employer": {"name": channel},
                "area": {"name": ""}, "experience": {"name": ""}, "alternate_url": f"https://t.me/{p['post']}",
            }
            result = candidates.handle(db, profile, entry, p["text"], direct_message=True, snippet=" ".join(lines)[:500])
            if result == candidates.BUDGET:
                return reported
            reported += result == candidates.SENT
            if reported >= MAX_PER_RUN:
                return reported
    return reported
