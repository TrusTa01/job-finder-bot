"""Telegram reports through a proxy (api.telegram.org is blocked from Russian IPs)."""
import time

import requests

from . import settings

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000  # Telegram caps a message at 4096 characters
DISMISS = {"inline_keyboard": [[{"text": "✕", "callback_data": "dismiss"}]]}  # handled by listener.py


def send(text: str) -> bool:
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
                    json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True, "reply_markup": DISMISS},
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


def format_vacancy(row) -> str:
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
    return "\n".join(lines)
