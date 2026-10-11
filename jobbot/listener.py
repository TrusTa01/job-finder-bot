"""Handles the buttons under the bot's messages: ✕ deletes the message, ✅ marks the vacancy as applied.
A text reply to a vacancy card is treated as the employer's questions and answered from the profile.
Runs as a service: python -m jobbot.listener

Long polling through the same proxy as notify.py. Only presses from the configured chat are honoured.
If Telegram refuses to delete (messages older than 48 hours), the text is replaced by a short stub.
"""
import html
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

from . import candidates, getmatch, habr, hh, llm, notify, settings, stats, store, tgchannels
from .filters import CFG
from .settings import ROOT
from .notify import API, DISMISS, applied_label, dismiss_markup, fill_contacts, ready_answers


def _call(method: str, payload: dict, timeout: int = 30) -> dict:
    proxy = settings.telegram_proxy()
    proxies = {"https": proxy, "http": proxy} if proxy else None
    url = API.format(token=settings.get("TELEGRAM_BOT_TOKEN"), method=method)
    return requests.post(url, json=payload, proxies=proxies, timeout=timeout).json()


def _handle(callback: dict, chat: str) -> None:
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    _call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    data = callback.get("data") or ""
    if str(chat_id) != chat:
        return
    target = {"chat_id": chat_id, "message_id": message["message_id"]}
    if data.startswith("dismiss"):
        vacancy_id = data.partition(":")[2]
        if vacancy_id:
            store.mark_dismissed(store.connect(), vacancy_id)
        if not _call("deleteMessage", target).get("ok"):
            _call("editMessageText", {**target, "text": "❌ убрано"})
    elif data.startswith("applied:"):
        store.mark_applied(store.connect(), data[len("applied:"):])
        if not _call("deleteMessage", target).get("ok"):  # older than 48 hours: keep a short mark instead
            _call("editMessageText", {**target, "text": applied_label(True), "reply_markup": {"inline_keyboard": []}})


def _vacancy_id(card: dict) -> str | None:
    """The id sits in the callback data of the card's ✅ button."""
    for row in (card.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data") or ""
            if data.startswith("applied:"):
                return data[len("applied:"):]
    return None


def _reply(message: dict, text: str, parse_mode: str | None = None) -> None:
    payload = {"chat_id": message["chat"]["id"], "text": text[:4000],
               "disable_web_page_preview": True, "reply_markup": DISMISS}
    if not (message.get("text") or "").startswith("/"):  # a command message is removed once handled, so it is not quoted
        payload.update({"reply_to_message_id": message["message_id"], "allow_sending_without_reply": True})
    if parse_mode:
        payload["parse_mode"] = parse_mode
    _call("sendMessage", payload)


def _format_answers(items: list[tuple[str, str]]) -> str:
    """Telegram HTML: the question in bold, the answer below it."""
    blocks = [f"<b>{html.escape(llm.plain_dashes(q))}</b>" + chr(10) + html.escape(llm.plain_dashes(fill_contacts(a))) for q, a in items if a]
    return (chr(10) * 2).join(blocks)


HELP_PATH = ROOT / "docs" / "bot-help.txt"


def _sources_text() -> str:
    """The "where the bot searches" list, built from the config and the source modules, so every copy shows its own."""
    queries = list(dict.fromkeys(q["text"] for q in CFG["search"]["queries"]))
    lines = [
        f"- hh.ru, при каждом запуске бота. Формат: {'удалённо' if CFG.get('required_work_format') == 'REMOTE' else 'любой'}. "
        f"Запросы: {', '.join(queries)}.",
        f"- Habr Career, раз в {habr.CHECK_EVERY_SECONDS // 3600} ч.",
        f"- GetMatch, раз в {getmatch.CHECK_EVERY_SECONDS // 3600} ч.",
    ]
    if tgchannels.CHANNELS:
        lines.append(f"- Telegram-каналы, раз в {tgchannels.CHECK_EVERY_SECONDS // 3600} ч, посты не старше {tgchannels.MAX_AGE_DAYS} дней. "
                     "Только эти каналы (вместо письма бот готовит короткое личное сообщение автору поста):")
        lines += [f"  - @{c}" for c in tgchannels.CHANNELS]
    lines.append("Другие источники бот не смотрит.")
    return chr(10).join(lines)


def _send_help(chat) -> None:
    text = HELP_PATH.read_text(encoding="utf-8").replace("{sources}", _sources_text()) if HELP_PATH.exists() else "Инструкции пока нет."
    _call("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True, "reply_markup": DISMISS})


def _delete_message(message: dict) -> None:
    """Commands are not kept in the chat: once handled, the bot removes the user's message (allowed for private chats, within 48 hours)."""
    _call("deleteMessage", {"chat_id": message["chat"]["id"], "message_id": message["message_id"]})


def _handle_message(message: dict, chat: str) -> None:
    """Answer /help and /start, or employer questions pasted as a reply to a vacancy card."""
    words = (message.get("text") or "").split()
    command = words[0].split("@")[0] if words else ""
    if str(message["chat"]["id"]) == chat and command in ("/help", "/start", "/stop", "/stats", "/rebuild", "/restore", *stats.KINDS):
        _command(message, command, chat, words[1:])
        return _delete_message(message)
    card, question = message.get("reply_to_message"), (message.get("text") or "").strip()
    if str(message["chat"]["id"]) != chat or not card or not question or not (card.get("from") or {}).get("is_bot"):
        return
    db = store.connect()
    if store.spend_last_day(db) >= CFG["limits"].get("daily_llm_budget_usd", 0.25):
        return _reply(message, "Суточный лимит расходов на ИИ исчерпан, ответ будет позже.")
    profile = candidates.load_profile()
    if not profile:
        return _reply(message, "Нет файла профиля, ответить не могу.")
    vacancy_id = _vacancy_id(card)
    description = hh.load_cache().get(vacancy_id or "", "")
    ready = ready_answers(mask=True)  # the model never sees the real phone number
    try:
        items, usage = llm.answer_questions(profile, card.get("text") or "", description, ready, question)
        store.log_usage(db, *usage)
    except RuntimeError:
        return _reply(message, "Модель не ответила, попробуйте ещё раз.")
    text = _format_answers(items)
    if text:
        _reply(message, text, "HTML")
    else:
        _reply(message, "Не получилось составить ответ.")


def _report_failure(update: dict, chat: str) -> None:
    """Tell the user that their message was not handled, without the exception text (it may hold the token URL)."""
    message = update.get("message")
    if not message or str(message["chat"]["id"]) != chat:
        return
    try:
        _reply(message, "Не получилось обработать сообщение, попробуйте ещё раз.")
    except Exception:  # noqa: BLE001 - the reply itself failed; nothing more to do
        pass


def _command(message: dict, command: str, chat: str, args: list[str] | None = None) -> None:
    """Chat commands: help and start/stop of the search, the monthly figures and the hand-reported outcomes."""
    db = store.connect()
    if command in ("/help", "/start"):
        was_paused = store.get_meta(db, "paused", "0") == "1"
        if command == "/start":
            store.set_meta(db, "paused", "0")
        _send_help(chat)
        if command == "/start" and was_paused:
            _reply(message, "Поиск возобновлён.")
    elif command == "/stop":
        store.set_meta(db, "paused", "1")
        _reply(message, "Поиск остановлен: новые вакансии не ищутся и не присылаются. Команды работают. /start возобновит поиск.")
    elif command == "/stats":
        now = datetime.now(timezone.utc)
        _reply(message, stats.summary(db, now.year, now.month))
    elif command == "/rebuild":
        _start_rebuild(message)
    elif command == "/restore":
        _restore(message, args or [])
    else:  # an outcome: /interview or /offer
        stats.add(db, stats.KINDS[command], int(time.time()))
        _reply(message, f"Записал: {stats.KINDS[command]}.")


def _restore(message: dict, args: list[str]) -> None:
    """/restore N: send back the last N cards the user dismissed with ❌ (N defaults to 1, at most 20)."""
    try:
        count = int(args[0]) if args else 1
    except ValueError:
        return _reply(message, "Напишите число: /restore 3 вернёт три последние убранные карточки.")
    if not 1 <= count <= 20:
        return _reply(message, "Можно вернуть от 1 до 20 карточек.")
    db = store.connect()
    rows = db.execute("SELECT id FROM vacancies WHERE status='dismissed' ORDER BY dismissed_at DESC LIMIT ?", (count,)).fetchall()
    if not rows:
        return _reply(message, "Нечего возвращать: убранных карточек нет.")
    done = 0
    for (vacancy_id,) in rows:
        db.execute("UPDATE vacancies SET status='notified', dismissed_at=NULL WHERE id=?", (vacancy_id,))
        db.commit()
        fresh = db.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
        message_id = notify.send_card("ВОЗВРАЩЕНО: карточка снова в работе\n\n" + notify.format_vacancy(fresh), vacancy_id)
        if message_id is not None:
            store.mark_notified(db, vacancy_id, message_id)
            done += 1


def _start_rebuild(message: dict) -> None:
    """/rebuild: write the drafts again for vacancies without ✅ or ✕ (tools/rebuild_drafts.py), in a separate process.

    The listener only starts it and answers; the rebuild reports its result to the chat itself. A second start within
    5 minutes is refused, so repeated taps cannot send the cards twice.
    """
    db = store.connect()
    last = int(store.get_meta(db, "rebuild_started", "0"))
    if time.time() - last < 5 * 60:
        return _reply(message, "Пересборка уже идёт или запускалась недавно. Итог придёт в этот чат.")
    store.set_meta(db, "rebuild_started", str(int(time.time())))
    with open(ROOT / "data" / "rebuild.log", "a", encoding="utf-8") as log:
        log.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n")
        subprocess.Popen([sys.executable, "-m", "tools.rebuild_drafts"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    _reply(message, "Пересборка запущена: напишу заново письма для вакансий без ✅ и ✕. Итог придёт сюда.")


MENU = [
    ("start", "Как пользоваться и запустить поиск"),
    ("stop", "Остановить поиск"),
    ("stats", "Итоги текущего месяца"),
    ("interview", "Записать собеседование"),
    ("offer", "Записать оффер"),
    ("rebuild", "Написать письма заново для открытых вакансий"),
    ("restore", "Вернуть последние убранные карточки: /restore 3"),
    ("help", "Инструкция"),
]


def _announce(chat: str) -> None:
    """On the first start of a bot copy: register the command menu and send the instruction once."""
    _call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in MENU]})
    db = store.connect()
    if store.get_meta(db, "help_sent", "0") != "1":
        _send_help(chat)
        store.set_meta(db, "help_sent", "1")


def run() -> None:
    chat = settings.get("TELEGRAM_CHAT_ID")
    try:
        _announce(chat)
    except (requests.RequestException, ValueError, KeyError):  # Telegram may be unreachable at start; /help still works later
        pass
    offset = 0
    failures = 0
    while True:
        try:
            data = _call("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["callback_query", "message"]}, timeout=70)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        _handle(update["callback_query"], chat)
                    elif "message" in update:
                        _handle_message(update["message"], chat)
                except Exception:  # noqa: BLE001 - one bad update must not stop the others; never print (the URL holds the token)
                    _report_failure(update, chat)
            failures = 0
        except (requests.RequestException, ValueError, KeyError):  # never print the error: the URL holds the token
            failures += 1
            time.sleep(min(5 * failures, 60))


if __name__ == "__main__":
    run()
