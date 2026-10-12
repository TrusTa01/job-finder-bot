"""Telegram reports through a proxy (api.telegram.org is blocked from Russian IPs)."""
import re
import time

import requests

from . import settings
from .filters import CFG, resume_checklist

STACK_WORD = CFG.get("stack_word") or "стек"  # the word before the field on the card: стек for IT, сфера for others
FORMAT_NAMES = {"REMOTE": "удалённо", "HYBRID": "гибрид", "ON_SITE": "офис"}
STACK_LABELS = CFG.get("stack_labels") or {}  # config: stack value from the screening prompt -> label on the card
FORM_LABELS = CFG.get("form_labels") or {}  # config: form_answers key -> question label in the card

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000  # Telegram caps a message at 4096 characters
DISMISS = {"inline_keyboard": [[{"text": "Скрыть ❌", "callback_data": "dismiss"}]]}  # handled by listener.py


def dismiss_button(vacancy_id: str | None) -> dict:
    """The ✕ button; with a vacancy id the listener also records the dismissal in the database."""
    return {"text": "Неинтересно ❌", "callback_data": f"dismiss:{vacancy_id}" if vacancy_id else "dismiss"}


def later_button(vacancy_id: str) -> dict:
    """Позже ⏳: the card comes back after later_days (config outreach.later_days, same as the hidden market)."""
    return {"text": "Позже ⏳", "callback_data": f"later:{vacancy_id}"}


def dismiss_markup(vacancy_id: str | None) -> dict:
    return {"inline_keyboard": [[dismiss_button(vacancy_id)]]}


def applied_label(done: bool) -> str:
    """Button text (done=False) or the mark on a card (done=True), in the candidate's gender (candidate_gender)."""
    female = CFG.get("candidate_gender") == "female"
    if done:
        return "ОТКЛИКНУЛАСЬ ✅" if female else "ОТКЛИКНУЛСЯ ✅"
    return "Откликнулась ✅" if female else "Откликнулся ✅"


def _keyboard(vacancy_id: str | None) -> dict:
    """✕ always; ✅ (applied) only for vacancies. callback_data is capped at 64 bytes by Telegram."""
    if not vacancy_id or len(f"applied:{vacancy_id}".encode()) > 64:
        return DISMISS
    return {"inline_keyboard": [
        [{"text": applied_label(False), "callback_data": f"applied:{vacancy_id}"}, later_button(vacancy_id)],
        [dismiss_button(vacancy_id)],
    ]}


def call_api(method: str, payload: dict) -> dict:
    """Plain Telegram method call (for edits and deletes). Never logs the URL (it contains the bot token)."""
    token = settings.get("TELEGRAM_BOT_TOKEN")
    proxy = settings.telegram_proxy()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    try:
        return requests.post(API.format(token=token, method=method), json=payload, proxies=proxies, timeout=30).json()
    except (requests.RequestException, ValueError):
        return {}


def send(text: str, vacancy_id: str | None = None) -> bool:
    """Send plain text. Returns True on success. Never logs the URL (it contains the bot token)."""
    return send_card(text, vacancy_id) is not None


def send_card(text: str, vacancy_id: str | None = None, markup: dict | None = None) -> int | None:
    """Send a report. Returns the id of its first message (to delete or replace it later), or None on failure.
    markup replaces the default buttons (outreach cards have their own)."""
    token, chat = settings.get("TELEGRAM_BOT_TOKEN"), settings.get("TELEGRAM_CHAT_ID")
    proxy = settings.telegram_proxy()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    first_id = None
    token, chat = settings.get("TELEGRAM_BOT_TOKEN"), settings.get("TELEGRAM_CHAT_ID")
    proxy = settings.telegram_proxy()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    for chunk in (text[i : i + LIMIT] for i in range(0, len(text), LIMIT)):
        for attempt in range(2):  # one retry covers short proxy hiccups
            error = ""
            try:
                r = requests.post(
                    API.format(token=token, method="sendMessage"),
                    json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True, "reply_markup": markup or _keyboard(vacancy_id)},
                    proxies=proxies,
                    timeout=30,
                )
                if r.status_code == 200:
                    if first_id is None:
                        first_id = r.json()["result"]["message_id"]
                    break
                error = f"HTTP {r.status_code}"
                if r.status_code == 429:  # flood control: wait as long as Telegram asks
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                    except ValueError:
                        wait = 3
                    time.sleep(wait + 1)
            except requests.RequestException as e:
                error = type(e).__name__
            if attempt == 1:
                print(f"! telegram send failed: {error}")
                return None
            time.sleep(2)
        time.sleep(1.1)  # stay under Telegram's per-chat rate limit when sending a batch
    return first_id


def format_telegram_post(row) -> str:
    stack = STACK_LABELS.get(row["stack"], "")
    lines = [
        f"Telegram @{row['employer']}: {row['name']}",
        f"Оценка: {row['fit']}" + (f" · {STACK_WORD}: {stack}" if stack else ""),
        f"{row['reason']}",
    ]
    if row["issue"]:
        lines.append(f"Внимание: {row['issue']}")
    lines += ["", f"Пост: {row['snippet']}", "", "Сообщение автору (отправьте в личные или по контактам из поста):", row["letter"], "", row["url"]]
    return "\n".join(lines) + resume_checklist(row)


CONTACT_TOKENS = {"[TELEGRAM]": "telegram", "[PHONE]": "phone", "[WHATSAPP]": "whatsapp", "[EMAIL]": "email"}
CONTACT_KEYS = ("telegram", "phone", "whatsapp", "email", "mail", "tg")  # contacts come from .env only; form_answers must not carry them


def contacts() -> dict:
    phone, telegram = settings.get("APPLY_PHONE"), settings.get("APPLY_TELEGRAM")
    return {
        "telegram": "@" + telegram.lstrip("@") if telegram else "",
        "phone": phone,
        "whatsapp": phone,
        "email": settings.get("APPLY_EMAIL"),
    }


def _is_contact_key(key) -> bool:
    return any(c in str(key).lower() for c in CONTACT_KEYS)


def ready_answers(mask: bool = False) -> str:
    """Basic answers (salary, city, experience, contacts), one per line. With mask the contacts are placeholders
    such as [PHONE], so the real values never reach the model; fill_contacts() puts them back."""
    ans = CFG.get("form_answers") or {}
    have = contacts()
    shown = {t: ((t if have[k] else "не указан, напиши «подставьте сами»") if mask else have[k]) for t, k in CONTACT_TOKENS.items()}
    pairs = [
        ("Желаемая оплата", ans.get("salary")),
        ("Город", ans.get("city")),
        *[(FORM_LABELS.get(key, key), value) for key, value in ans.items() if key not in ("salary", "city") and not _is_contact_key(key) and isinstance(value, (str, int, float))],
        ("Telegram", shown["[TELEGRAM]"]),
        ("Телефон", shown["[PHONE]"]),
        ("WhatsApp (можно писать)", shown["[WHATSAPP]"]),
        ("Почта", shown["[EMAIL]"]),
    ]
    return chr(10).join(f"{k}: {v}" for k, v in pairs if v)


def fill_contacts(text: str) -> str:
    """Replace the contact placeholders in the model's answer with the real values from .env."""
    have = contacts()
    for token, key in CONTACT_TOKENS.items():
        name = token.strip("[]")
        text = re.sub(re.escape("[") + " *" + name + " *" + re.escape("]"), lambda _: have[key] or "подставьте сами", text, flags=re.IGNORECASE)
    return text


def form_answers(row) -> str:
    """Warning and the basic answers for vacancies whose response has a questionnaire."""
    if not row["has_test"]:
        return ""
    lines = ready_answers()
    return "\n\n⚠ При отклике будет анкета. Базовые ответы для копирования:\n" + lines


TARGET_KINDS = {"studio": "студия", "outstaff": "аутстафф", "agency": "агентство", "product": "продуктовая компания"}
PITCH_VARIANTS = {"staff": "штат или удалёнка", "contract": "контракт или аутстафф", "later": "оставить контакт на будущее"}


def target_markup(target_id: int, state: str = "new") -> dict:
    """Outreach card buttons (callback data t:<action>:<id>, handled by listener.py). After "sent" only the outcome buttons remain."""
    outcome = [{"text": "Ответили 💬", "callback_data": f"t:reply:{target_id}"}, {"text": "Неинтересно ❌", "callback_data": f"t:dead:{target_id}"}]
    if state == "sent":
        return {"inline_keyboard": [outcome]}
    first = [{"text": "Написал ✅", "callback_data": f"t:sent:{target_id}"}, {"text": "Позже ⏳", "callback_data": f"t:later:{target_id}"}]
    return {"inline_keyboard": [first, [outcome[1]]]}


def format_target(row, reminder: bool = False, returned: bool = False) -> str:
    kind = TARGET_KINDS.get(row["kind"], row["kind"] or "")
    head = f"🎯 Скрытый рынок: {row['name']} ({kind})"
    if returned:
        head = "↩️ Вы отложили эту компанию, возвращаю карточку.\n\n" + head
    if reminder:
        head = f"⏰ Напоминание: вы писали {row['name']}, ответа в базе нет. Если не ответили, можно написать ещё раз коротко.\n\n" + head
    about = " ".join(p for p in (row["stack_hint"], row["summary"]) if p)
    person = ", ".join(p for p in (row["contact_name"], row["contact_role"]) if p)
    contact = row["contact_value"] or f"не найден: /setcontact {row['id']} @username или почта"
    lines = [head]
    if about:
        lines.append(about)
    lines += [f"Сайт: {row['site']}", f"Контакт: {contact}" + (f" ({person})" if person else ""), f"Вариант: {PITCH_VARIANTS.get(row['pitch_variant'], row['pitch_variant'] or '')}"]
    if row["pitch"]:
        lines += ["", "Сообщение (отправьте сами):", row["pitch"]]
    return "\n".join(lines)


def format_vacancy(row) -> str:
    if str(row["id"]).startswith("tg:"):
        return format_telegram_post(row)
    priority = "приоритет обычный" if row["priority"] == "normal" else "приоритет низкий"
    stack = STACK_LABELS.get(row["stack"], "")
    lines = [
        f"{row['name']}",
        f"{row['employer']} · {row['area']} · {row['experience']}",
        *([f"Формат: {', '.join(FORMAT_NAMES.get(x, x) for x in (row['work_format'] or '').split(',') if x)}"] if row['work_format'] else []),
        f"Оценка: {row['fit']} ({priority})" + (f" · {STACK_WORD}: {stack}" if stack else ""),
        f"{row['reason']}",
    ]
    if row["issue"]:
        lines.append(f"Внимание: {row['issue']}")
    if row["letter"]:
        lines += ["", "Письмо:", row["letter"]]
    lines += ["", row["url"]]
    return "\n".join(lines) + resume_checklist(row) + form_answers(row)
