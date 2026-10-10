"""Handles the buttons under the bot's messages: ✕ deletes the message, ✅ marks the vacancy as applied.
A text reply to a vacancy card is treated as the employer's questions and answered from the profile.
Runs as a service: python -m jobbot.listener

Long polling through the same proxy as notify.py. Only presses from the configured chat are honoured.
If Telegram refuses to delete (messages older than 48 hours), the text is replaced by a short stub.
"""
import time

import requests

from . import candidates, hh, llm, settings, store
from .filters import CFG
from .notify import API, DISMISS, ready_answers


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


def _reply(message: dict, text: str) -> None:
    _call("sendMessage", {"chat_id": message["chat"]["id"], "text": text[:4000], "reply_to_message_id": message["message_id"],
                          "allow_sending_without_reply": True, "disable_web_page_preview": True})


def _handle_message(message: dict, chat: str) -> None:
    """Answer employer questions pasted as a reply to a vacancy card."""
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
    ready = ready_answers(contacts=False)  # the model never sees the phone number
    try:
        answer, usage = llm.answer_questions(profile, card.get("text") or "", description, ready, question)
        store.log_usage(db, *usage)
    except RuntimeError:
        return _reply(message, "Модель не ответила, попробуйте ещё раз.")
    _reply(message, answer or "Не получилось составить ответ.")


def run() -> None:
    chat = settings.get("TELEGRAM_CHAT_ID")
    offset = 0
    failures = 0
    while True:
        try:
            data = _call("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["callback_query", "message"]}, timeout=70)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    _handle(update["callback_query"], chat)
                elif "message" in update:
                    _handle_message(update["message"], chat)
            failures = 0
        except (requests.RequestException, ValueError, KeyError):  # never print the error: the URL holds the token
            failures += 1
            time.sleep(min(5 * failures, 60))


if __name__ == "__main__":
    run()
