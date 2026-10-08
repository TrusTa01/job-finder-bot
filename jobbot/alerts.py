"""Problem alerts and a daily heartbeat, delivered to Telegram.

Repeated alerts about the same problem are throttled; a "recovered" message is sent once it clears.
The daily heartbeat doubles as a silence detector: if it stops arriving, the bot is not running.
"""
import time
from datetime import datetime, timedelta, timezone

from . import notify, store

MSK = timezone(timedelta(hours=3))
REPEAT_SECONDS = 6 * 3600
HEARTBEAT_FROM_HOUR_MSK = 9
enabled = True  # run.py switches this off with --no-notify

HH_HELP = (
    "Как обновить сессию: на своём компьютере выполните авторизацию (hh-applicant-tool authorize), "
    "потом скопируйте папку ~/.config/hh-applicant-tool на сервер пользователю bot в такую же папку."
)


def problem(db, key: str, text: str) -> None:
    store.set_meta(db, f"alert_active_{key}", "1")
    now = int(time.time())
    if not enabled or now - int(store.get_meta(db, f"alert_ts_{key}", "0")) < REPEAT_SECONDS:
        return
    if notify.send("ВНИМАНИЕ: " + text):
        store.set_meta(db, f"alert_ts_{key}", str(now))


def recovered(db, key: str, text: str) -> None:
    if store.get_meta(db, f"alert_active_{key}") == "1":
        store.set_meta(db, f"alert_active_{key}", "0")
        if enabled:
            notify.send("Восстановлено: " + text)


def heartbeat(db) -> None:
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    if not enabled or now.hour < HEARTBEAT_FROM_HOUR_MSK or store.get_meta(db, "heartbeat_date") == today:
        return
    s = store.day_stats(db)
    text = (
        f"Бот работает. За сутки: новых вакансий {s['seen']}, отправлено вам {s['sent']}, откликов {s['applied']}, "
        f"расходы на ИИ ${s['spend']:.3f}."
    )
    if s["undelivered"]:
        text += f" Не доставлено в Telegram: {s['undelivered']}."
    if notify.send(text):
        store.set_meta(db, "heartbeat_date", today)
