"""Collect a small labelled-test sample: vacancies that pass the hard filter (what the LLM would
see in production) plus a few that were rejected only for "no signal in description" (should be
rejected by the LLM too). Writes data/sample.json (gitignored). Read-only against hh.
"""
import json

from jobbot.filters import CFG, NEEDS_DESCRIPTION, evaluate
from jobbot.hh import get_description, load_cache, save_cache, search
from jobbot.settings import DATA_DIR

MAX_DESC = 3500
NEGATIVES = 4


def main() -> None:
    found: dict[str, dict] = {}
    for q in CFG["search"]["queries"]:
        for v in search(q, CFG["search"]["period_days"]):
            found.setdefault(v["id"], v)

    cache = load_cache()
    kept, negatives = [], []
    for v in found.values():
        verdict = evaluate(v)
        if verdict.reason == NEEDS_DESCRIPTION:
            verdict = evaluate(v, get_description(v["id"], cache))
        if not verdict.skip:
            kept.append(v)
        elif verdict.reason == "no signal in description" and len(negatives) < NEGATIVES:
            negatives.append(v)

    out = []
    for group, label in ((kept, "passed_filter"), (negatives, "rejected_no_signal")):
        for v in group:
            desc = get_description(v["id"], cache)
            out.append(
                {
                    "id": v["id"],
                    "group": label,
                    "name": v["name"],
                    "employer": (v.get("employer") or {}).get("name", ""),
                    "area": (v.get("area") or {}).get("name", ""),
                    "experience": (v.get("experience") or {}).get("name", ""),
                    "work_format": [f["id"] for f in v.get("work_format") or []],
                    "salary": v.get("salary"),
                    "url": v["alternate_url"],
                    "description": " ".join(desc.split())[:MAX_DESC],
                }
            )
    save_cache(cache)
    (DATA_DIR / "tests" / "sample.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"sample: {len(kept)} passed filter + {len(negatives)} negatives = {len(out)} -> data/sample.json")


if __name__ == "__main__":
    main()
