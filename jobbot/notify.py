"""Telegram reports through a proxy (api.telegram.org is blocked from Russian IPs)."""
import re
import time

import requests

from . import settings
from .filters import CFG, resume_checklist

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000  # Telegram caps a message at 4096 characters
DISMISS = {"inline_keyboard": [[{"text": "✕", "callback_data": "dismiss"}]]}  # handled by listener.py


def _keyboard(vacancy_id: str | None) -> dict:
    """✕ always; ✅ (applied) only for vacancies. callback_data is capped at 64 bytes by Telegram."""
    if not vacancy_id or len(f"applied:{vacancy_id}".encode()) > 64:
        return DISMISS
    return {"inline_keyboard": [[{"text": "✅ Откликнулся", "callback_data": f"applied:{vacancy_id}"}, *DISMISS["inline_keyboard"][0]]]}


def send(text: str, vacancy_id: str | None = None) -> bool:
    """Send plain text. Returns True on success. Never logs the URL (it contains the bot token)."""
    token, chat = settings.get("TELEGRAM_BOT_TOKEN"), settings.get("TELEGRAM_CHAT_ID")
    proxy = settings.telegram_proxy()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    for chunk in (text[i : i + LIMIT] for i in range(0, len(text), LIMIT)):
        for attempt in range(2):  # one retry covers short proxy hiccups
            error = ""
            try:
                r = requests.post(
                    API.format(token=token, method="sendMessage"),
                    json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True, "reply_markup": _keyboard(vacancy_id)},
                    proxies=proxies,
                    timeout=30,
                )
                if r.status_code == 200:
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
                return False
            time.sleep(2)
        time.sleep(1.1)  # stay under Telegram's per-chat rate limit when sending a batch
    return True


def format_telegram_post(row) -> str:
    stack = {"flutter": "Flutter", "devops": "DevOps / инфраструктура"}.get(row["stack"], "")
    lines = [
        f"Telegram @{row['employer']}: {row['name']}",
        f"Оценка: {row['fit']}" + (f" · стек: {stack}" if stack else ""),
        f"{row['reason']}",
    ]
    if row["issue"]:
        lines.append(f"Внимание: {row['issue']}")
    lines += ["", f"Пост: {row['snippet']}", "", "Сообщение автору (отправьте в личные или по контактам из поста):", row["letter"], "", row["url"]]
    return "\n".join(lines) + resume_checklist(row)


CONTACT_TOKENS = {"[TELEGRAM]": "telegram", "[PHONE]": "phone", "[WHATSAPP]": "whatsapp"}


def contacts() -> dict:
    phone, telegram = settings.get("APPLY_PHONE"), settings.get("APPLY_TELEGRAM")
    return {
        "telegram": "@" + telegram.lstrip("@") if telegram else "",
        "phone": phone,
        "whatsapp": phone,
    }


def ready_answers(mask: bool = False) -> str:
    """Basic answers (salary, city, experience, contacts), one per line. With mask the contacts are placeholders
    such as [PHONE], so the real values never reach the model; fill_contacts() puts them back."""
    ans = CFG.get("form_answers") or {}
    have = contacts()
    shown = {t: ((t if have[k] else "не указан, напиши «подставьте сами»") if mask else have[k]) for t, k in CONTACT_TOKENS.items()}
    pairs = [
        ("Желаемая оплата", ans.get("salary")),
        ("Город", ans.get("city")),
        ("Опыт Flutter Android/iOS", ans.get("mobile_experience")),
        ("Опыт Flutter Web", ans.get("web_experience")),
        ("Telegram", shown["[TELEGRAM]"]),
        ("Телефон", shown["[PHONE]"]),
        ("WhatsApp (можно писать)", shown["[WHATSAPP]"]),
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


def format_vacancy(row) -> str:
    if str(row["id"]).startswith("tg:"):
        return format_telegram_post(row)
    priority = "приоритет обычный" if row["priority"] == "normal" else "приоритет низкий"
    stack = {"flutter": "Flutter", "devops": "DevOps / инфраструктура"}.get(row["stack"], "")
    lines = [
        f"{row['name']}",
        f"{row['employer']} · {row['area']} · {row['experience']}",
        f"Оценка: {row['fit']} ({priority})" + (f" · стек: {stack}" if stack else ""),
        f"{row['reason']}",
    ]
    if row["issue"]:
        lines.append(f"Внимание: {row['issue']}")
    if row["letter"]:
        lines += ["", "Письмо:", row["letter"]]
    lines += ["", row["url"]]
    return "\n".join(lines) + resume_checklist(row) + form_answers(row)
