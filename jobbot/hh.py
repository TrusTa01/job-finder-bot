"""Read-only hh.ru access through the authorized hh-applicant-tool session."""
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .filters import CFG
from .settings import DATA_DIR, DESCRIPTION_CACHE

_EXE = "hh-applicant-tool.exe" if os.name == "nt" else "hh-applicant-tool"
TOOL = Path(sys.executable).with_name(_EXE)
CONFIG_DIR = os.environ.get("HH_CONFIG_DIR", str(Path.home() / ".config" / "hh-applicant-tool"))


def call_api(endpoint: str, *params: str) -> dict:
    cmd = [str(TOOL), "-c", CONFIG_DIR, "call-api", endpoint, *params]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        return json.loads(out.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        print(f"! hh call failed: {endpoint}", file=sys.stderr)
        return {}


def search(query: dict, days: int) -> list[dict]:
    params = [
        f"text={query['text']}",
        f"period={days}",
        f"per_page={CFG['search']['per_page']}",
        "order_by=publication_time",
    ]
    if query.get("field") == "name":
        params.append("search_field=name")
    return call_api("/vacancies", *params).get("items", [])


def search_all(days: int) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for q in CFG["search"]["queries"]:
        for v in search(q, days):
            found.setdefault(v["id"], v)
    return found


def load_cache() -> dict[str, str]:
    if DESCRIPTION_CACHE.exists():
        return json.loads(DESCRIPTION_CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DESCRIPTION_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def get_description(vacancy_id: str, cache: dict[str, str]) -> str:
    if vacancy_id not in cache:
        data = call_api(f"/vacancies/{vacancy_id}")
        if "description" not in data:  # a failed call must not be cached as an empty description
            raise RuntimeError(f"no description for {vacancy_id}")
        cache[vacancy_id] = html.unescape(re.sub(r"<[^>]+>", " ", data["description"] or ""))
    return cache[vacancy_id]
