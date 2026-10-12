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

from . import candidates, getmatch, habr, hh, llm, notify, settings, stats, store, tgchannels, trudvsem
from .filters import CFG, SOURCE_NAMES, WORK_FORMATS, set_source, source_on
from .outreach import conf as outreach_conf, targets as outreach_targets
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
    elif data.startswith("later:"):
        vacancy_id = data.partition(":")[2]
        if vacancy_id:
            store.mark_later(store.connect(), vacancy_id, int(outreach_conf.cfg()["later_days"]))  # same period as the hidden market
        if not _call("deleteMessage", target).get("ok"):
            _call("editMessageText", {**target, "text": "⏳ отложено", "reply_markup": {"inline_keyboard": []}})
    elif data.startswith("applied:"):
        store.mark_applied(store.connect(), data[len("applied:"):])
        if not _call("deleteMessage", target).get("ok"):  # older than 48 hours: keep a short mark instead
            _call("editMessageText", {**target, "text": applied_label(True), "reply_markup": {"inline_keyboard": []}})
    elif data.startswith("t:"):
        _target_action(data, target)
    elif data.startswith("hlp:"):
        for mid in data[len("hlp:"):].split(","):
            _call("deleteMessage", {"chat_id": chat_id, "message_id": int(mid)})
    elif data == "hid:toggle":
        db = store.connect()
        outreach_conf.set_enabled(db, not outreach_conf.enabled(db))
        _call("editMessageText", {**target, "text": _hidden_text(db), "reply_markup": _hidden_markup(db)})
    elif data == "src:close":
        if not _call("deleteMessage", target).get("ok"):  # older than 48 hours: keep a mark, but no buttons
            _call("editMessageText", {**target, "text": "Источники: меню закрыто.", "reply_markup": {"inline_keyboard": []}})
    elif data.startswith("src:"):
        name = data[len("src:"):]
        if name in SOURCE_NAMES:
            set_source(name, not source_on(name))
        _call("editMessageReplyMarkup", {**target, "reply_markup": _switch_markup()})


def _target_allows(status: str, action: str) -> bool:
    """Replied and not-interested are final. After "wrote" only the outcome buttons count: pressing "wrote" on an old card
    would schedule a second reminder, and "later" would take the target out of the wait for an answer."""
    if status in ("replied", "dead"):
        return False
    if status == "sent":
        return action in ("reply", "dead")
    return True


def _target_action(data: str, target: dict) -> None:
    """Buttons of an outreach card: t:sent|reply|later|dead:<target id>."""
    parts = data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return
    action, target_id = parts[1], int(parts[2])
    db = store.connect()
    row = outreach_targets.get(db, target_id)
    if not row:
        return
    if not _target_allows(row["status"], action):  # an old card or reminder still in the chat: the target has moved on
        _call("editMessageReplyMarkup", {**target, "reply_markup": {"inline_keyboard": []}})
        return
    if action == "sent":
        outreach_targets.mark_sent(db, target_id)
        _call("editMessageReplyMarkup", {**target, "reply_markup": notify.target_markup(target_id, "sent")})
    elif action == "reply":
        outreach_targets.set_status(db, target_id, "replied")
        _call("editMessageText", {**target, "text": f"💬 Ответили: {row['name']}", "reply_markup": {"inline_keyboard": []}})
    elif action in ("later", "dead"):
        if action == "later":
            outreach_targets.mark_later(db, target_id)
        else:
            outreach_targets.set_status(db, target_id, "dead")
        if not _call("deleteMessage", target).get("ok"):
            _call("editMessageText", {**target, "text": "⏳ отложено" if action == "later" else "❌ убрано", "reply_markup": {"inline_keyboard": []}})


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


FORMAT_NAMES = {"REMOTE": "удалённо", "HYBRID": "гибрид", "ON_SITE": "офис"}


def _sources_text() -> str:
    """The "where the bot searches" list, built from the config and the source modules, so every copy shows its own."""
    queries = list(dict.fromkeys(q["text"] for q in CFG["search"]["queries"]))
    lines = []
    if source_on("hh"):
        lines.append(f"- hh.ru, при каждом запуске бота. Формат: {', '.join(FORMAT_NAMES.get(f, f) for f in WORK_FORMATS)}. "
                     f"Запросы: {', '.join(queries)}.")
    if source_on("habr") and habr.SIGNALS and CFG.get("habr_queries"):
        lines.append(f"- Habr Career, раз в {habr.CHECK_EVERY_SECONDS // 3600} ч.")
    if source_on("getmatch") and getmatch.SIGNALS:
        lines.append(f"- GetMatch, раз в {getmatch.CHECK_EVERY_SECONDS // 3600} ч.")
    if source_on("trudvsem") and CFG.get("trudvsem_queries"):
        regions = CFG.get("trudvsem_regions") or []
        where = f" Регионы: {', '.join(map(str, regions))}." if regions else " Вся Россия."
        lines.append(f"- Работа в России (trudvsem.ru), раз в {trudvsem.CHECK_EVERY_SECONDS // 3600} ч.{where}")
    if source_on("telegram") and tgchannels.CHANNELS:
        lines.append(f"- Telegram-каналы, раз в {tgchannels.CHECK_EVERY_SECONDS // 3600} ч, посты не старше {tgchannels.MAX_AGE_DAYS} дней. "
                     "Только эти каналы (вместо письма бот готовит короткое личное сообщение автору поста):")
        lines += [f"  - @{c}" for c in tgchannels.CHANNELS]
    if lines:
        lines.append("Другие источники бот не смотрит.")
    elif all(not source_on(n) for n in SOURCE_NAMES):
        lines.append("Все источники отключены в config.yaml (sources).")
    else:
        lines.append("Включённые источники пока ничего не ищут: в config.yaml не заданы запросы или каналы.")
    return chr(10).join(lines)


def _parts(text: str, limit: int = 3800) -> list[str]:
    """Telegram refuses messages over 4096 characters: split by paragraphs, never inside one."""
    parts, cur = [], ""
    for para in text.split(chr(10) * 2):
        if cur and len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}{chr(10)}{chr(10)}{para}" if cur else para
    return parts + ([cur] if cur else [])


def _send_help(chat) -> None:
    text = HELP_PATH.read_text(encoding="utf-8").replace("{sources}", _sources_text()) if HELP_PATH.exists() else "Инструкции пока нет."
    ids = []
    for part in _parts(text):
        ids.append(_call("sendMessage", {"chat_id": chat, "text": part, "disable_web_page_preview": True})["result"]["message_id"])
    # only the last part carries the hide button; it removes every part of this help at once
    markup = {"inline_keyboard": [[{"text": "Скрыть справку ❌", "callback_data": "hlp:" + ",".join(map(str, ids))}]]}
    _call("editMessageReplyMarkup", {"chat_id": chat, "message_id": ids[-1], "reply_markup": markup})


def _delete_message(message: dict) -> None:
    """Commands are not kept in the chat: once handled, the bot removes the user's message (allowed for private chats, within 48 hours)."""
    _call("deleteMessage", {"chat_id": message["chat"]["id"], "message_id": message["message_id"]})


def _handle_message(message: dict, chat: str) -> None:
    """Answer /help and /start, or employer questions pasted as a reply to a vacancy card."""
    words = (message.get("text") or "").split()
    command = words[0].split("@")[0] if words else ""
    if str(message["chat"]["id"]) == chat and command in ("/help", "/start", "/stop", "/stats", "/stats_month", "/stats_year", "/stats_all", "/rebuild", "/restore", "/channels", "/addchannel", "/switch", *HIDDEN_COMMANDS, *stats.KINDS):
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
    elif command in ("/stats", "/stats_month"):
        now = datetime.now(timezone.utc)
        _reply(message, stats.month(db, now.year, now.month))
    elif command == "/stats_year":
        _reply(message, stats.year_to_date(db, datetime.now(timezone.utc).year))
    elif command == "/stats_all":
        _reply(message, stats.all_time(db))
    elif command == "/rebuild":
        _start_rebuild(message)
    elif command == "/restore":
        _restore(message, args or [])
    elif command == "/channels":
        _reply(message, "Каналы, которые проверяет бот:" + chr(10) + chr(10).join("@" + c for c in tgchannels.channels(db)))
    elif command == "/addchannel":
        _reply(message, tgchannels.add_channel(db, " ".join(args or [])))
    elif command == "/switch":
        _send_switch(chat)
    elif command == "/hidden" and not args:
        _call("sendMessage", {"chat_id": chat, "text": _hidden_text(db), "reply_markup": _hidden_markup(db)})
    elif command in HIDDEN_COMMANDS:
        _reply(message, _hidden_command(db, command, args or []))
    else:  # an outcome: /interview or /offer
        stats.add(db, stats.KINDS[command], int(time.time()))
        _reply(message, f"Записал: {stats.KINDS[command]}.")


HIDDEN_COMMANDS = ("/hidden", "/targets", "/followups", "/addtarget", "/setcontact", "/sources", "/removesource")
STATUS_RU = {"new": "найдено, сообщение не готово", "drafted": "сообщение готово", "notified": "карточка отправлена", "sent": "вы написали",
             "replied": "ответили", "later": "отложено", "dead": "не интересно"}


def _hidden_command(db, command: str, args: list[str]) -> str:
    """The outreach commands. Answers with a short text for the chat."""
    if command == "/hidden":
        sub = args[0].lower() if args else "status"
        if sub in ("on", "off"):
            outreach_conf.set_enabled(db, sub == "on")
            return ("Скрытый рынок включён: бот ищет студии и компании сам и присылает карточки с готовым сообщением." if sub == "on"
                    else "Скрытый рынок выключен: поиск, расходы и карточки остановлены. /hidden on включит снова.")
        if outreach_conf.enabled(db):
            return "Скрытый рынок" + chr(10) + "Статус: ✅ включён" + chr(10) + chr(10) + "Бот ищет компании сам и присылает карточки с готовым сообщением." + chr(10) + chr(10) + "Выключить: /hidden off"
        return "Скрытый рынок" + chr(10) + "Статус: ⛔ выключен" + chr(10) + chr(10) + "Поиск компаний и расходы остановлены." + chr(10) + chr(10) + "Включить: /hidden on"
    if command == "/targets":
        funnel = outreach_targets.funnel(db)
        if not funnel:
            return "Целей пока нет."
        return "Скрытый рынок, воронка:" + chr(10) + chr(10).join(f"- {STATUS_RU.get(s, s)}: {n}" for s, n in funnel.items())
    if command == "/followups":
        rows = db.execute("SELECT id, name, follow_up_at FROM targets WHERE status='sent' ORDER BY follow_up_at").fetchall()
        if not rows:
            return "Ждущих ответа нет."
        return "Ждём ответа:" + chr(10) + chr(10).join(
            f"- {r['name']}: напомню {datetime.fromtimestamp(r['follow_up_at'], timezone.utc):%d.%m}" for r in rows)
    if command == "/addtarget":
        url = args[0] if args else ""
        if not url.startswith(("http://", "https://")):
            return "Напишите адрес сайта: /addtarget https://studio.example"
        if not outreach_targets.queue(db, url, "manual"):
            return "Этот адрес уже есть в очереди или проверен."
        return "Добавил в очередь, разберу при следующем проходе."
    if command == "/setcontact":
        if len(args) < 2 or not args[0].isdigit():
            return "Формат: /setcontact 12 @username или /setcontact 12 почта@сайт.ру (номер цели в карточке)."
        value = " ".join(args[1:])
        channel = "telegram" if value.startswith("@") or "t.me/" in value else "email" if "@" in value else "other"
        if not outreach_targets.get(db, int(args[0])):
            return "Такой цели нет."
        outreach_targets.set_contact(db, int(args[0]), channel, value)
        return "Контакт записан."
    if command == "/sources":
        rows = outreach_targets.active_sources(db)
        if not rows:
            return "Найденных ботом источников пока нет."
        return "Источники, которые бот нашёл сам (убрать: /removesource номер):" + chr(10) + chr(10).join(
            f"{r['rid']}. {r['name']} ({'канал' if r['kind'] == 'channel' else 'каталог или статья'}) {r['url']}" for r in rows)
    # /removesource
    if not args or not args[0].isdigit():
        return "Формат: /removesource 5 (номер из /sources)."
    row = outreach_targets.remove_source_by_rowid(db, int(args[0]))
    if not row:
        return "Такого источника нет."
    if row["kind"] == "channel":
        tgchannels.forget_channel(db, row["name"])
    return f"Убрал: {row['name']}. Он больше не появится."


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


def _hidden_text(db) -> str:
    on = outreach_conf.enabled(db)
    return ("Скрытый рынок" + chr(10) + ("Статус: Включён ✅" if on else "Статус: Выключен ❌") + chr(10) + chr(10)
            + ("Бот ищет компании сам и присылает карточки с готовым сообщением." if on else "Поиск компаний и расходы остановлены."))


def _hidden_markup(db) -> dict:
    """One toggle button and a close button. The toggle shows the action, the text shows the state."""
    on = outreach_conf.enabled(db)
    return {"inline_keyboard": [[{"text": "Выключить ❌" if on else "Включить ✅", "callback_data": "hid:toggle"}],
                                [{"text": "Скрыть меню ❌", "callback_data": "src:close"}]]}


SWITCH_LABELS = {"hh": "hh.ru", "habr": "Habr Career", "getmatch": "GetMatch", "trudvsem": "Работа в России", "telegram": "Telegram-каналы"}


def _switch_markup() -> dict:
    """One button per job source: ✅ on, ⛔ off. Pressing one flips it."""
    rows = [[{"text": f"{SWITCH_LABELS[n]} {'✅' if source_on(n) else '⛔'}", "callback_data": f"src:{n}"}] for n in SOURCE_NAMES]
    return {"inline_keyboard": rows + [[{"text": "Скрыть меню ❌", "callback_data": "src:close"}]]}


def _send_switch(chat: str) -> None:
    _call("sendMessage", {"chat_id": chat, "text": "Источники поиска. Нажмите, чтобы включить или выключить. Действует со следующего прохода. "
                          "Скрытый рынок (/hidden) и список каналов (/channels) здесь не переключаются." + chr(10) + ("Скрытый рынок: Включён ✅." if outreach_conf.enabled(store.connect()) else "Скрытый рынок: Выключен ❌.") + " Меню скрытого рынка: /hidden.", "reply_markup": _switch_markup()})


MENU = [
    ("start", "Как пользоваться и запустить поиск"),
    ("stop", "Остановить поиск"),
    ("stats_month", "Статистика за текущий месяц"),
    ("stats_year", "Статистика за год"),
    ("stats_all", "Статистика за всё время"),
    ("interview", "Записать собеседование"),
    ("offer", "Записать оффер"),
    ("rebuild", "Написать письма заново для открытых вакансий"),
    ("restore", "Вернуть последние убранные карточки: /restore 3"),
    ("channels", "Каналы, которые проверяет бот"),
    ("switch", "Включить или выключить источники поиска"),
    ("addchannel", "Добавить канал: /addchannel имя"),
    ("hidden", "Скрытый рынок: /hidden on или off"),
    ("targets", "Скрытый рынок: воронка"),
    ("followups", "Скрытый рынок: ждём ответа"),
    ("addtarget", "Скрытый рынок: добавить сайт: /addtarget адрес"),
    ("setcontact", "Скрытый рынок: контакт: /setcontact номер @имя"),
    ("sources", "Скрытый рынок: найденные источники"),
    ("removesource", "Скрытый рынок: убрать источник: /removesource номер"),
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
