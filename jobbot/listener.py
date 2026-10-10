"""Handles the buttons under the bot's messages: ✕ deletes the message, ✅ marks the vacancy as applied.
A text reply to a vacancy card is treated as the employer's questions and answered from the profile.
Runs as a service: python -m jobbot.listener

Long polling through the same proxy as notify.py. Only presses from the configured chat are honoured.
If Telegram refuses to delete (messages older than 48 hours), the text is replaced by a short stub.
"""
import html
import time

import requests

from . import candidates, getmatch, habr, hh, llm, settings, store, tgchannels
from .filters import CFG
from .settings import ROOT
from .notify import API, DISMISS, fill_contacts, ready_answers


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
    if data == "dismiss":
        if not _call("deleteMessage", target).get("ok"):
            _call("editMessageText", {**target, "text": "✕ убрано"})
    elif data.startswith("applied:"):
        store.mark_applied(store.connect(), data[len("applied:"):])
        text = message.get("text") or ""
        if not text.startswith("✅"):
            _call("editMessageText", {**target, "text": "✅ ОТКЛИКНУЛСЯ" + "\n" + text,
                                       "disable_web_page_preview": True, "reply_markup": DISMISS})


def _vacancy_id(card: dict) -> str | None:
    """The id sits in the callback data of the card's ✅ button."""
    for row in (card.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data") or ""
            if data.startswith("applied:"):
                return data[len("applied:"):]
    return None


def _reply(message: dict, text: str, parse_mode: str | None = None) -> None:
    payload = {"chat_id": message["chat"]["id"], "text": text[:4000], "reply_to_message_id": message["message_id"],
               "allow_sending_without_reply": True, "disable_web_page_preview": True, "reply_markup": DISMISS}
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


def _handle_message(message: dict, chat: str) -> None:
    """Answer /help and /start, or employer questions pasted as a reply to a vacancy card."""
    if str(message["chat"]["id"]) == chat and (message.get("text") or "").split("@")[0].strip() in ("/help", "/start"):
        return _send_help(chat)
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


def _announce(chat: str) -> None:
    """On the first start of a bot copy: register the command menu and send the instruction once."""
    _call("setMyCommands", {"commands": [{"command": "help", "description": "Как пользоваться ботом"}]})
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
