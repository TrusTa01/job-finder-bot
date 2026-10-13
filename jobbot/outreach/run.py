"""One outreach pass: discovery (search once a day, classify queued pages), pitch drafts, Telegram cards, reminders.
Separate from the vacancy pass (jobbot.run) and from its cron line. Does nothing while outreach is switched off (/hidden).
Usage: python -m jobbot.outreach.run [--no-notify] [--force-find]
"""
import argparse
import sys
import time
from datetime import datetime

from .. import alerts, candidates, llm, notify, store
from ..filters import CFG
from . import conf, discover, targets

DIGEST_EVERY_SECONDS = 24 * 3600


def work_cards(db, profile: str, send: bool) -> dict:
    """Draft a pitch for new targets and send their cards, at most `max_new_per_day` cards in 24 hours."""
    out = {"drafted": 0, "sent": 0, "returned": 0}
    cfg = conf.cfg()
    for row in targets.due_later(db) if send else []:  # snoozed cards come back on top of the daily cap
        message_id = notify.send_card(notify.format_target(row, returned=True), markup=notify.target_markup(row["id"]))
        if message_id is None:
            break
        targets.mark_returned(db, row["id"], message_id)
        out["returned"] += 1
    room = int(cfg["max_new_per_day"]) - targets.new_cards_today(db)
    budget = CFG["limits"].get("daily_llm_budget_usd", 0.25)
    for row in db.execute("SELECT * FROM targets WHERE status IN ('new','drafted') ORDER BY id").fetchall():
        if room <= 0:
            break
        if row["status"] == "new":
            if store.spend_last_day(db) >= budget:
                break
            variant = cfg["pitch_variants"].get(row["kind"], "contract")
            try:
                pitch, usage = llm.write_pitch(profile, dict(row), variant)
                store.log_usage(db, *usage)
            except RuntimeError:
                break  # the model is down: try again next pass
            targets.set_pitch(db, row["id"], pitch, variant)
            row = targets.get(db, row["id"])
            out["drafted"] += 1
        room -= 1
        if not send:
            continue
        message_id = notify.send_card(notify.format_target(row), markup=notify.target_markup(row["id"]))
        if message_id is None:
            break  # Telegram is unreachable: the draft stays and goes out next pass
        targets.mark_notified(db, row["id"], message_id)
        out["sent"] += 1
    return out


def send_followups(db) -> int:
    sent = 0
    for row in targets.due_followups(db):
        if notify.send_card(notify.format_target(row, reminder=True), markup=notify.target_markup(row["id"], "sent")) is None:
            break
        targets.mark_followup_sent(db, row["id"])
        sent += 1
    return sent


def _counter(db, key: str) -> int:
    return int(store.get_meta(db, key, "0"))


def send_digest(db) -> None:
    """Once a day: what was found and sent, how many search requests were made (and roughly what they cost),
    and which sources discovery connected by itself, with the command that removes each."""
    last = int(store.get_meta(db, "outreach_digest_ts", "0"))
    if time.time() - last < DIGEST_EVERY_SECONDS or datetime.now(alerts.MSK).hour < alerts.HEARTBEAT_FROM_HOUR_MSK:
        return
    count = lambda where: db.execute(f"SELECT COUNT(*) FROM targets WHERE {where}", (last,)).fetchone()[0]
    waiting = db.execute("SELECT COUNT(*) FROM targets WHERE status IN ('new','drafted')").fetchone()[0]
    lines = [f"🎯 Скрытый рынок за сутки: найдено компаний {count('first_seen>?')}, карточек отправлено {count('notified_at>?')}, ждут своей очереди {waiting}."]
    cap = int(conf.cfg()["max_new_per_day"])
    if waiting and targets.new_cards_today(db) >= cap:
        free_at = datetime.fromtimestamp(targets.next_slot_at(db), alerts.MSK).strftime("%H:%M")
        lines.append(f"Лимит {cap} карточек в сутки исчерпан, следующая придёт после {free_at} МСК (outreach.max_new_per_day в config.yaml).")
    month, price = time.strftime("%Y-%m", time.gmtime()), float(conf.cfg()["discovery"].get("price_per_query_rub") or 0)
    for provider in conf.cfg()["discovery"]["providers"]:
        day, in_month = _counter(db, f"outreach_q_{provider}_since_digest"), _counter(db, f"outreach_q_{provider}_{month}")
        if in_month:
            cost = f", по тарифу около {in_month * price:.0f} ₽" if provider == "yandex" and price else ""
            lines.append(f"Запросов к поиску ({provider}): {day} за сутки, {in_month} в этом месяце{cost}.")
    rows = targets.sources_since(db, last)
    if rows:
        lines.append("Новые источники, подключены автоматически:")
        lines += [f"- {r['name']} ({'канал' if r['kind'] == 'channel' else 'каталог или статья'}): {r['url']}  убрать: /removesource {r['rid']}" for r in rows]
    if not notify.send(chr(10).join(lines)):
        return
    for provider in conf.cfg()["discovery"]["providers"]:
        store.set_meta(db, f"outreach_q_{provider}_since_digest", "0")
    store.set_meta(db, "outreach_digest_ts", str(int(time.time())))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true", help="store results and drafts but do not send to Telegram")
    ap.add_argument("--force-find", action="store_true", help="search now even if the daily search already ran")
    args = ap.parse_args()
    alerts.enabled = not args.no_notify
    db = store.connect()
    if not conf.enabled(db):
        print("outreach is off: /hidden on in the chat or outreach.enabled in config.yaml")
        return
    if store.get_meta(db, "paused", "0") == "1":
        print("paused: send /start in the chat to resume")
        return
    profile = candidates.load_profile()
    if not profile:
        raise SystemExit("data/profile.txt not found: write your candidate profile there first")
    if not conf.terms():
        raise SystemExit("outreach.profile_terms is empty in config.yaml: say what you do, for example [flutter, dart]")

    found = discover.find(db, force=args.force_find)
    if found.get("queries") and not found.get("hits"):
        alerts.problem(db, "outreach_search", "поиск скрытого рынка не вернул результатов: проверьте ключ поиска в .env (YANDEX_SEARCH_API_KEY) и баланс в Yandex Cloud.")
    elif found.get("hits"):
        alerts.recovered(db, "outreach_search", "поиск скрытого рынка работает.")
    processed = discover.process(db, int(conf.cfg()["discovery"]["max_pages_per_run"]))
    cards = work_cards(db, profile, send=not args.no_notify)
    reminders = 0 if args.no_notify else send_followups(db)
    if not args.no_notify:
        send_digest(db)
    print({"find": found, "process": processed, "cards": cards, "reminders": reminders, "funnel": targets.funnel(db)},
          f"| total LLM spend: ${store.total_cost(db):.4f}")


def cli() -> None:
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - last line of defence: report without the exception text and re-raise for the log
        alerts.enabled = "--no-notify" not in sys.argv
        try:
            alerts.problem(store.connect(), "outreach_crash", f"проход скрытого рынка упал: {type(e).__name__}. Подробности в data/outreach.log на сервере.")
        finally:
            raise


if __name__ == "__main__":
    cli()
