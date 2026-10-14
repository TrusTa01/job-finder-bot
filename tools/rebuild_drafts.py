"""Rebuild the drafts you have not used yet, after a change to the prompts or the profile.

For each stored vacancy with a letter and without the ✅ mark it asks the LLM again, saves the new text and sends
the card once more, marked as updated. Cards already marked ✅ are never touched.
A vacancy that the current config would reject (for example a DevOps title) is not rebuilt: its card is removed and
the vacancy is marked ✕, so /restore brings it back. Hidden-market targets you have not written to are rebuilt too.
Usage (on the server, from the project folder):
    python -m tools.rebuild_drafts --dry-run      # how many drafts would be rebuilt, nothing sent
    python -m tools.rebuild_drafts                # rebuild and send
    python -m tools.rebuild_drafts --ids hh-id1 tg:channel/12   # only these vacancy ids (targets are skipped)
An old card is removed only after its new text is written. Telegram cannot delete cards older than 48 hours or cards
sent before message ids were stored: those stay in the chat (the older ones get a short "updated" mark instead).
"""
import argparse

import time

from jobbot import candidates, hh, llm, notify, settings, store
from jobbot.filters import CFG, _has
from jobbot.outreach import conf, targets


def _description(row, cache: dict) -> str:
    """Full text when it is known (hh vacancies are cached); otherwise the snippet or the reason the LLM gave."""
    if str(row["id"]).isdigit():
        return cache.get(str(row["id"]), "") or (row["reason"] or "")
    return row["snippet"] or row["reason"] or ""


def _entry(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "employer": {"name": row["employer"]},
        "area": {"name": row["area"]}, "experience": {"name": row["experience"]}, "work_format": [], "salary": None,
    }


def _retire_old_card(row, note: str = "Письмо обновлено, новая карточка ниже.") -> None:
    """Remove the card the user already has: delete it if Telegram allows (under 48 hours), otherwise replace its text.
    Cards sent before message ids were stored have no id and stay in the chat."""
    if not row["message_id"]:
        return
    target = {"chat_id": settings.get("TELEGRAM_CHAT_ID"), "message_id": row["message_id"]}
    young = row["notified_at"] and time.time() - row["notified_at"] < 48 * 3600
    if young and notify.call_api("deleteMessage", target).get("ok"):
        return
    notify.call_api("editMessageText", {**target, "text": note, "reply_markup": {"inline_keyboard": []}})


def _rejected_now(row) -> bool:
    """True when the current config would not let this vacancy through: a stop role in the title without Flutter/Dart,
    or a DevOps stack set by the screening. Mirrors the title rules of filters.evaluate."""
    title = (row["name"] or "").lower()
    if _has(title, CFG["title_signals"]):
        return False
    return row["stack"] == "devops" or _has(title, CFG["stop_roles_keywords"])


def _rebuild_targets(db, profile: str, dry_run: bool) -> tuple[int, int]:
    """Hidden-market targets the user has not written to: draft the chat message and the e-mail letter again.
    A target whose card was already sent gets a new card (the old one is removed); a target still waiting for its
    card only gets the new text and goes out in the usual daily flow."""
    rows = db.execute("SELECT * FROM targets WHERE status IN ('drafted', 'notified') AND (pitch <> '' OR mail_pitch <> '') ORDER BY id").fetchall()
    print(f"targets to rebuild: {len(rows)}")
    if dry_run:
        return 0, 0
    cfg = conf.cfg()
    done = failed = 0
    for row in rows:
        if store.spend_last_day(db) >= CFG["limits"].get("daily_llm_budget_usd", 0.5):
            print("stopped: daily LLM budget reached")
            break
        variant = cfg["pitch_variants"].get(row["kind"], "contract")
        try:
            chat, mail = targets.wants(row)
            pitch = subject = mail_pitch = ""
            if chat:
                pitch, _, usage = llm.write_pitch(profile, dict(row), variant)
                store.log_usage(db, *usage)
            if mail:
                letter, subject, usage = llm.write_pitch(profile, dict(row), variant, mail=True)
                mail_pitch = conf.signed(letter)
                store.log_usage(db, *usage)
        except RuntimeError:
            failed += 1
            continue
        was_sent = row["status"] == "notified"
        targets.set_pitch(db, row["id"], pitch, variant, subject, mail_pitch)
        done += 1
        if not was_sent:
            continue
        _retire_old_card(row)
        fresh = targets.get(db, row["id"])
        message_id = notify.send_card("ОБНОВЛЕНО: сообщение написано заново по новым правилам\n\n" + notify.format_target(fresh), markup=notify.target_markup(row["id"]))
        if message_id is not None:
            targets.mark_returned(db, row["id"], message_id)
    return done, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="only count the drafts, call nothing")
    parser.add_argument("--ids", nargs="*", help="rebuild only these vacancy ids")
    args = parser.parse_args()

    db = store.connect()
    profile = candidates.load_profile()
    if not profile:
        raise SystemExit("No data/profile.txt: nothing to write from.")
    rows = db.execute(
        "SELECT * FROM vacancies WHERE letter IS NOT NULL AND letter <> '' AND applied_at IS NULL "
        "AND status IN ('ready', 'notified') ORDER BY first_seen"
    ).fetchall()
    if args.ids:
        rows = [r for r in rows if r["id"] in set(args.ids)]
    rejected = [r for r in rows if _rejected_now(r)]
    rows = [r for r in rows if not _rejected_now(r)]
    print(f"drafts to rebuild: {len(rows)}, to remove (rejected by the current config): {len(rejected)}")
    t_done = t_failed = 0
    if not args.ids:
        t_done, t_failed = _rebuild_targets(db, profile, args.dry_run)
    if args.dry_run:
        return
    for row in rejected:
        store.mark_dismissed(db, row["id"])
        _retire_old_card(row, "Вакансия убрана: не подходит под текущие критерии. Вернуть: /restore.")
    if not rows and not t_done:
        if not args.ids:
            notify.send(f"Пересобирать нечего. Убрано вакансий, не подходящих под критерии: {len(rejected)}.")
        return

    cache = hh.load_cache()
    done = failed = 0
    for row in rows:
        if store.spend_last_day(db) >= CFG["limits"].get("daily_llm_budget_usd", 0.5):
            print("stopped: daily LLM budget reached")
            break
        try:
            letter, usage = llm.write_letter(
                profile, _entry(row), _description(row, cache), row["issue"] or "",
                direct_message=str(row["id"]).startswith("tg:"),
            )
            store.log_usage(db, *usage)
        except RuntimeError:
            failed += 1
            continue
        db.execute("UPDATE vacancies SET letter=? WHERE id=?", (letter, row["id"]))
        db.commit()
        _retire_old_card(row)
        fresh = db.execute("SELECT * FROM vacancies WHERE id=?", (row["id"],)).fetchone()
        message_id = notify.send_card("ОБНОВЛЕНО: письмо написано заново по новым правилам\n\n" + notify.format_vacancy(fresh), row["id"])
        if message_id is not None:
            store.mark_notified(db, row["id"], message_id)
        done += 1
    print(f"rebuilt: {done}, failed: {failed}")
    if not args.ids:
        notify.send(
            f"Пересборка готова: вакансий обновлено {done}, не удалось {failed}; скрытый рынок обновлено {t_done}, не удалось {t_failed}; "
            f"убрано по текущим критериям {len(rejected)} (вернуть: /restore). "
            "Карточки старше 48 часов Telegram не даёт удалить: вместо них стоит короткая отметка."
        )


if __name__ == "__main__":
    main()
