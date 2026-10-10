"""Rebuild the drafts of vacancies you have not applied to yet, after a change to the prompts or the profile.

For each stored vacancy with a letter and without the ✅ mark it asks the LLM again, saves the new text and sends
the card once more, marked as updated. Cards already marked ✅ are never touched.
Usage (on the server, from the project folder):
    python -m tools.rebuild_drafts --dry-run      # how many drafts would be rebuilt, nothing sent
    python -m tools.rebuild_drafts                # rebuild and send
    python -m tools.rebuild_drafts --ids hh-id1 tg:channel/12   # only these ids
The old cards stay in the chat; the database keeps only the new text.
"""
import argparse

from jobbot import candidates, hh, llm, notify, store
from jobbot.filters import CFG


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
    print(f"drafts to rebuild: {len(rows)}")
    if args.dry_run:
        return

    cache = hh.load_cache()
    done = failed = 0
    for row in rows:
        if store.spend_last_day(db) >= CFG["limits"].get("daily_llm_budget_usd", 0.25):
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
        fresh = db.execute("SELECT * FROM vacancies WHERE id=?", (row["id"],)).fetchone()
        notify.send("ОБНОВЛЕНО: письмо написано заново по новым правилам\n\n" + notify.format_vacancy(fresh), row["id"])
        done += 1
    print(f"rebuilt: {done}, failed: {failed}")


if __name__ == "__main__":
    main()
