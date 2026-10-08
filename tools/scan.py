"""Read-only scan: run the configured hh searches, apply the hard filter, print the result.

Nothing is sent and nothing is stored. Usage: python scan.py [--days N]
"""
import argparse

from jobbot.filters import CFG, NEEDS_DESCRIPTION, evaluate
from jobbot.hh import get_description, load_cache, save_cache, search_all


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=CFG["search"]["period_days"])
    args = ap.parse_args()

    found = search_all(args.days)
    cache = load_cache()
    kept, skipped = [], {}
    for v in found.values():
        verdict = evaluate(v)
        if verdict.reason == NEEDS_DESCRIPTION:
            verdict = evaluate(v, get_description(v["id"], cache))
        if verdict.skip:
            skipped[verdict.reason] = skipped.get(verdict.reason, 0) + 1
        else:
            kept.append((verdict, v))
    save_cache(cache)

    kept.sort(key=lambda t: t[1]["published_at"], reverse=True)
    print(f"unique vacancies: {len(found)}, kept: {len(kept)}, skipped: {sum(skipped.values())}")
    print("skipped by reason:", skipped)
    print()
    for verdict, v in kept:
        exp = (v.get("experience") or {}).get("name", "")
        area = (v.get("area") or {}).get("name", "")
        emp = (v.get("employer") or {}).get("name", "")
        note = f" ({verdict.reason})" if verdict.reason else ""
        print(f"[{verdict.priority:6}] {v['published_at'][:10]} | {v['name'][:48]} | {emp[:24]} | {area[:14]} | {exp}{note}")
        print(f"         {v['alternate_url']}")


if __name__ == "__main__":
    main()
