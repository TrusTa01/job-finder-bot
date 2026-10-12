"""Preview of the outreach discovery: runs the search, classifies a few pages and drafts one sample message.
Writes only to a temporary database and sends nothing to Telegram. Costs a few LLM calls.
Usage: python -m tools.outreach_scan [--pages 5] [--queries 4] [--no-pitch]
"""
import argparse
import tempfile
from pathlib import Path

from jobbot import candidates, llm, store
from jobbot.outreach import conf, discover, search, targets, web


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5, help="how many pages to classify with the LLM")
    ap.add_argument("--queries", type=int, default=4, help="how many search queries to run")
    ap.add_argument("--no-pitch", action="store_true", help="skip drafting a sample message")
    ap.add_argument("--url", action="append", default=[], help="classify this page too (repeatable); useful without a search key")
    args = ap.parse_args()

    store.DB_PATH = Path(tempfile.mkdtemp()) / "preview.sqlite"  # a throwaway database: the real one is not touched
    db = store.connect()
    if not conf.terms():
        raise SystemExit("outreach.profile_terms is empty in config.yaml")

    settings = conf.cfg()["discovery"]
    queries = discover._queries()[: args.queries]
    print("queries:", *queries, sep="\n  ")
    per_job, provider = search.search([(q, 0) for q in queries], settings["providers"], settings["yandex_mode"], settings["combine"])
    hits = list({h["url"]: h for found in per_job.values() for h in found}.values())
    print(f"provider: {provider or 'none (no key in .env or all providers failed)'}; {len(hits)} hits")
    queued = [h for h in hits if discover._worth_queueing(h["url"])]
    for h in queued[:15]:
        print(f"  {h['title'][:70]} | {web.host(h['url'])}")
    for url in args.url:
        targets.queue(db, url, "manual")
    for h in queued:
        targets.queue(db, h["url"], "search")

    stats = {"triaged": 0}
    while stats["triaged"] < args.pages and targets.next_queued(db, 1):  # pages found while classifying (catalog links) join the queue
        stats = {k: stats.get(k, 0) + v for k, v in discover.process(db, args.pages - stats["triaged"]).items()}
    print("classified:", stats)
    for t in db.execute("SELECT * FROM targets").fetchall():
        print(f"  target [{t['kind']}] {t['name']} | {t['site']} | contact: {t['contact_value'] or '-'} | {t['summary']}")
    for s in targets.active_sources(db):
        print(f"  source [{s['kind']}] {s['name']} | {s['url']}")
    print(f"LLM spend: ${store.total_cost(db):.4f}")

    profile = candidates.load_profile()
    first = db.execute("SELECT * FROM targets ORDER BY id LIMIT 1").fetchone()
    if first and profile and not args.no_pitch:
        variant = conf.cfg()["pitch_variants"].get(first["kind"], "contract")
        pitch, _ = llm.write_pitch(profile, dict(first), variant)
        print(f"\nSample message for {first['name']} ({variant}):\n{pitch}")


if __name__ == "__main__":
    main()
