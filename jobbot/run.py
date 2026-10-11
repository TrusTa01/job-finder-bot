"""One pass: search -> hard filter -> LLM screening -> cover letter -> store -> Telegram.

Nothing is applied automatically: the report carries the vacancy link and a draft letter.
Problems (hh unreachable, model failing, crash) are reported to Telegram; see alerts.py.
Usage: python -m jobbot.run [--days N] [--limit N] [--no-notify]
"""
import argparse
import sys
from datetime import datetime, timezone

from . import alerts, getmatch, habr, hh, llm, notify, stats, store, tgchannels
from .filters import CFG, NEEDS_DESCRIPTION, evaluate, priority_after_screen
from .settings import PROFILE_PATH


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=CFG["search"]["period_days"])
    ap.add_argument("--limit", type=int, default=8, help="max new vacancies sent to the LLM in this run")
    ap.add_argument("--no-notify", action="store_true", help="store results but do not send to Telegram")
    args = ap.parse_args()
    alerts.enabled = not args.no_notify

    if not PROFILE_PATH.exists():
        raise SystemExit(f"{PROFILE_PATH} not found: write your candidate profile there first")
    profile = PROFILE_PATH.read_text(encoding="utf-8")

    db = store.connect()
    now = datetime.now(timezone.utc)
    month_key = f"{now.year}-{now.month:02d}"
    if now.day == 1 and store.get_meta(db, "monthly_sent", "") != month_key:  # monthly figures, once
        if not args.no_notify:
            notify.send(stats.month(db, *stats.previous_month(now)))
        store.set_meta(db, "monthly_sent", month_key)
    if store.get_meta(db, "paused", "0") == "1":  # /stop in the chat; /start resumes
        print("paused: send /start in the chat to resume the search")
        return
    resent = 0
    if not args.no_notify:
        # Retry reports that failed earlier (for example while the proxy was down), oldest first
        for row in db.execute("SELECT * FROM vacancies WHERE status='ready' ORDER BY first_seen").fetchall():
            message_id = notify.send_card(notify.format_vacancy(row), row["id"])
            if message_id is None:
                break  # Telegram is unreachable, try again next run
            store.mark_notified(db, row["id"], message_id)
            resent += 1

    known = store.known_ids(db)
    found = hh.search_all(args.days)
    if not found:
        # A 30-day search never legitimately returns nothing, so hh is unreachable or the session expired
        alerts.problem(
            db, "hh",
            "hh.ru не вернул ни одной вакансии: сессия могла истечь или hh недоступен. " + alerts.HH_HELP,
        )
        print("no vacancies returned by hh")
        return
    alerts.recovered(db, "hh", "связь с hh.ru работает.")

    new = sorted((v for v in found.values() if v["id"] not in known), key=lambda v: v["published_at"], reverse=True)
    cache = hh.load_cache()
    stats = {"found": len(found), "new": len(new), "filtered": 0, "screened_out": 0, "ready": 0, "sent": 0, "resent": resent, "errors": 0, "hh_errors": 0}
    processed = 0
    for v in new:
        verdict = evaluate(v)
        if verdict.reason == NEEDS_DESCRIPTION:
            try:
                verdict = evaluate(v, hh.get_description(v["id"], cache))
            except RuntimeError:
                stats["hh_errors"] += 1  # not saved, so the next run retries it
                continue
        if verdict.skip:
            store.save_vacancy(db, v, "filtered", reason=verdict.reason)
            stats["filtered"] += 1
            continue
        if processed >= args.limit:
            break  # leave the rest unseen so the next run picks them up
        budget = CFG["limits"].get("daily_llm_budget_usd", 0.25)
        if store.spend_last_day(db) >= budget:
            alerts.problem(
                db, "budget",
                f"достигнут суточный лимит расходов на ИИ (${budget:.2f} за 24 часа), разбор вакансий приостановлен "
                "до снижения расхода. Лимит меняется в config.yaml (limits.daily_llm_budget_usd).",
            )
            break
        processed += 1  # every vacancy sent to the LLM counts toward --limit
        try:
            description = hh.get_description(v["id"], cache)
        except RuntimeError:
            stats["hh_errors"] += 1
            continue
        try:
            decision, usage = llm.screen(profile, v, description)
            store.log_usage(db, *usage)
            fit = decision.get("fit", "no")
            issue = decision.get("location_or_schedule_issue", "")
            if fit == "no":
                store.save_vacancy(
                    db, v, "screened_out", verdict.priority, fit=fit, reason=decision.get("reason"),
                    stack=decision.get("stack"),
                )
                stats["screened_out"] += 1
                continue
            letter, usage = llm.write_letter(profile, v, description, issue)
            store.log_usage(db, *usage)
        except RuntimeError:
            stats["errors"] += 1  # not saved, so the next run retries it
            continue
        store.save_vacancy(
            db, v, "ready", priority_after_screen(fit, decision.get("required_grade"), issue), fit=fit, reason=decision.get("reason"),
            issue=issue, grade=decision.get("required_grade"), stack=decision.get("stack"), letter=letter,
        )
        stats["ready"] += 1
        if not args.no_notify:
            row = db.execute("SELECT * FROM vacancies WHERE id=?", (v["id"],)).fetchone()
            message_id = notify.send_card(notify.format_vacancy(row), v["id"])
            if message_id is not None:
                store.mark_notified(db, v["id"], message_id)
                stats["sent"] += 1
    hh.save_cache(cache)

    if stats["hh_errors"] and not (stats["ready"] or stats["screened_out"]):
        alerts.problem(
            db, "hh_desc",
            f"hh.ru не отдаёт описания вакансий (ошибок в этом запуске: {stats['hh_errors']}): сессия могла истечь. " + alerts.HH_HELP,
        )
    elif not stats["hh_errors"]:
        alerts.recovered(db, "hh_desc", "описания вакансий с hh.ru снова загружаются.")

    if stats["errors"] and not (stats["ready"] or stats["screened_out"]):
        alerts.problem(
            db, "llm",
            f"не удаётся получить ответ от модели DeepSeek (ошибок в этом запуске: {stats['errors']}). "
            "Проверьте ключ и баланс на сервере.",
        )
    elif stats["ready"] or stats["screened_out"]:
        alerts.recovered(db, "llm", "модель отвечает.")

    if not args.no_notify:
        stats["habr"] = habr.check(db)  # rare, so a simple link is enough
        stats["getmatch"] = getmatch.check(db)
        stats["tg"] = tgchannels.check(db)

    alerts.heartbeat(db)
    print(stats, f"| total LLM spend: ${store.total_cost(db):.4f}")


def cli() -> None:
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - last line of defence, report and re-raise for the log
        alerts.enabled = "--no-notify" not in sys.argv
        try:
            alerts.problem(
                store.connect(), "crash",
                f"бот упал при запуске: {type(e).__name__}. Подробности в data/run.log на сервере.",
            )
        finally:
            raise


if __name__ == "__main__":
    cli()
