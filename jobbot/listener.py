"""Handles the buttons under the bot's messages: ✕ deletes the message, ✅ marks the vacancy as applied. Runs as a service: python -m jobbot.listener

Long polling through the same proxy as notify.py. Only presses from the configured chat are honoured.
If Telegram refuses to delete (messages older than 48 hours), the text is replaced by a short stub.
"""
import time

import requests

from . import settings, store
from .notify import API, DISMISS


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


def run() -> None:
    chat = settings.get("TELEGRAM_CHAT_ID")
    offset = 0
    failures = 0
    while True:
        try:
            data = _call("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["callback_query"]}, timeout=70)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    _handle(update["callback_query"], chat)
            failures = 0
        except (requests.RequestException, ValueError, KeyError):  # never print the error: the URL holds the token
            failures += 1
            time.sleep(min(5 * failures, 60))


if __name__ == "__main__":
    run()
