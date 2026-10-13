"""Hard prefilter and priority for vacancies, driven by config.yaml (no LLM).

Two-phase: evaluate(vacancy) may return reason "needs_description" for vacancies without
Flutter in the title; the caller then fetches the full description and calls
evaluate(vacancy, description) again.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"  # personal, gitignored
if not CONFIG_PATH.exists():
    raise SystemExit("config.yaml not found: copy config.example.yaml to config.yaml and edit it")
CFG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

NEEDS_DESCRIPTION = "needs_description"
# hh work_format ids the candidate accepts: REMOTE, HYBRID, ON_SITE (config.yaml work_formats)
def words(key: str) -> tuple[str, ...]:
    """A config list of words as lowercase strings; blanks dropped, a bare scalar counts as one word."""
    raw = CFG.get(key) or []
    return tuple(t for t in (str(w).strip().lower() for w in ([raw] if not isinstance(raw, list) else raw)) if t)


_raw_formats = CFG.get("work_formats") or [CFG.get("required_work_format", "REMOTE")]
WORK_FORMATS = [str(f).strip().upper() for f in (_raw_formats if isinstance(_raw_formats, list) else [_raw_formats])]
_unknown = [f for f in WORK_FORMATS if f not in ("REMOTE", "HYBRID", "ON_SITE")]
# Office and hybrid are taken only in these cities (config.yaml office_cities); remote always.
OFFICE_CITIES = words("office_cities")
REMOTE_RE = re.compile("удал|remote|из дома", re.I)
OFFICE_RE = re.compile("на месте|в офисе|офисн|очно|гибрид", re.I)
if _unknown:
    raise SystemExit(f"config.yaml work_formats: unknown value(s) {', '.join(_unknown)}; use REMOTE, HYBRID, ON_SITE")


SOURCE_NAMES = ("hh", "habr", "getmatch", "trudvsem", "telegram")
_sources = CFG.get("sources") or {}


def sources_problem() -> str | None:
    """What is wrong in config.yaml `sources`, or None. Not raised at import: the caller reports it and the run goes on."""
    if not isinstance(_sources, dict):
        return "config.yaml sources: ожидается список вида {hh: true, habr: false}, все источники считаются включёнными."
    bad = [f"{k}={v!r}" for k, v in _sources.items() if k not in SOURCE_NAMES or not isinstance(v, (bool, int))]
    if bad:
        return (f"config.yaml sources: непонятные записи {', '.join(bad)} (имена: {', '.join(SOURCE_NAMES)}, "
                "значения true или false), такие источники считаются включёнными.")
    return None


SWITCH_PATH = CONFIG_PATH.parent / "data" / "sources.json"  # switches set from the chat (/switch), override config.yaml


def _switched(name: str) -> bool | None:
    try:
        value = json.loads(SWITCH_PATH.read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def in_office_city(text: str) -> bool:
    """The vacancy is in one of office_cities: the candidate is on the same clock, so no time difference to mention."""
    low = text.lower()
    return any(city in low for city in OFFICE_CITIES)


def place_ok(formats: set, text: str) -> bool:
    """Remote is always fine. Office or hybrid is fine only when the text names one of office_cities."""
    if "REMOTE" in formats or REMOTE_RE.search(text):
        return True
    if not (formats & {"ON_SITE", "HYBRID"}) and not OFFICE_RE.search(text):
        return True
    low = text.lower()
    return any(city in low for city in OFFICE_CITIES)


def source_on(name: str) -> bool:
    """Job sources can be switched off in config.yaml: sources: {hh: false, habr: false, ...}, or from the chat (/switch),
    which wins. Missing or unreadable means on."""
    switched = _switched(name)
    if switched is not None:
        return switched
    value = _sources.get(name, True) if isinstance(_sources, dict) else True
    return bool(value) if isinstance(value, (bool, int)) else True


def set_source(name: str, on: bool) -> None:
    """Write one switch to data/sources.json, atomically: the run and the listener read it at any time."""
    try:
        data = json.loads(SWITCH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = on
    SWITCH_PATH.parent.mkdir(exist_ok=True)
    tmp = SWITCH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SWITCH_PATH)


@dataclass
class Verdict:
    skip: bool
    priority: str  # "normal" | "low"
    reason: str = ""


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def salary_rub_net(salary: dict | None) -> int | None:
    """hh returns {'from','to','currency','gross'}; convert to RUB net."""
    if not salary or salary.get("currency") != "RUR":
        return None
    value = salary.get("from") or salary.get("to")
    if not value:
        return None
    return int(value * 0.87) if salary.get("gross") else int(value)


def below_salary_floor(vacancy: dict, floor: int) -> bool:
    """True only for a full-time monthly rouble salary whose top figure (as printed in the ad) is under the floor.
    No salary, part-time or project work, per-shift or hourly pay and other currencies are never judged here."""
    if (vacancy.get("employment") or {}).get("id") != "full":
        return False
    pay = vacancy.get("salary_range") or vacancy.get("salary") or {}
    if (pay.get("mode") or {}).get("id", "MONTH") != "MONTH" or pay.get("currency") != "RUR":
        return False
    top = pay.get("to") or pay.get("from")
    return bool(top) and top < floor


def evaluate(vacancy: dict, description: str | None = None) -> Verdict:
    title = (vacancy.get("name") or "").lower()
    snippet = vacancy.get("snippet") or {}
    text = " ".join(snippet.get(k) or "" for k in ("requirement", "responsibility")).lower()
    full = (description or "").lower()
    formats = {f.get("id") for f in vacancy.get("work_format") or []}

    if not any(f in formats for f in WORK_FORMATS):
        return Verdict(True, "low", "work format")
    if not place_ok(formats, (vacancy.get("area") or {}).get("name", "")):
        return Verdict(True, "low", "office outside the accepted cities")
    floor = CFG["salary"].get("min_monthly_rub", 0)
    if floor and below_salary_floor(vacancy, floor):
        return Verdict(True, "low", "salary below the minimum")

    sen = CFG["seniority"]
    if _has(title, sen["skip"]) and not _has(title, sen["ok_markers"]):
        return Verdict(True, "low", "senior/lead")

    title_match = _has(title, CFG["title_signals"])
    devops = False
    if not title_match:
        if _has(title, CFG.get("devops_title_signals", [])):
            # DevOps family: always low priority, the LLM rejects heavy requirements
            devops = True
        elif _has(title, CFG["stop_roles_keywords"]):
            return Verdict(True, "low", "stop role in title")
        else:
            # Generic title: keep only if the description contains a signal
            if description is None:
                return Verdict(False, "low", NEEDS_DESCRIPTION)
            if not _has(full, CFG["description_signals"]):
                return Verdict(True, "low", "no signal in description")

    body = title + " " + text + " " + full
    if _has(body, CFG["stop_text_keywords"]):
        return Verdict(True, "low", "pure layout")

    low = _has(body, CFG["low_priority_keywords"]) or devops
    net = salary_rub_net(vacancy.get("salary"))
    if net is None or net < CFG["salary"]["normal_priority_from_rub"]:
        low = True
    reason = "devops" if devops else ("signal in description" if not title_match else "")
    return Verdict(False, "low" if low else "normal", reason)


def priority_after_screen(fit: str, grade: str | None, issue: str) -> str:
    """Priority from the LLM screening: salary is rarely published, so it is not used.

    Low when the fit is only partial, the vacancy asks for more than junior/middle experience
    or the country/schedule looks problematic; otherwise normal.
    """
    if fit != "yes" or issue or grade in ("senior", "middle"):
        return "low"
    return "normal"


def resume_checklist(row) -> str:
    """Which of the configured resumes fit this vacancy (config key `resumes`); empty when not configured."""
    resumes = CFG.get("resumes") or []
    title = (row["name"] or "").lower()
    lines = []
    for r in resumes:
        fits = row["stack"] in r.get("stacks", []) or any(w in title for w in r.get("title_any", []))
        lines.append(f"{'✓' if fits else '✗'} {r['name']}")
    ok = [l for l in lines if l.startswith("✓")]
    if not ok:
        return ""
    return "\n".join(["", "Резюме для отклика:", *lines])
