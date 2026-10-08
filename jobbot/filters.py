"""Hard prefilter and priority for vacancies, driven by config.yaml (no LLM).

Two-phase: evaluate(vacancy) may return reason "needs_description" for vacancies without
Flutter in the title; the caller then fetches the full description and calls
evaluate(vacancy, description) again.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"  # personal, gitignored
if not CONFIG_PATH.exists():
    raise SystemExit("config.yaml not found: copy config.example.yaml to config.yaml and edit it")
CFG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

NEEDS_DESCRIPTION = "needs_description"


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


def evaluate(vacancy: dict, description: str | None = None) -> Verdict:
    title = (vacancy.get("name") or "").lower()
    snippet = vacancy.get("snippet") or {}
    text = " ".join(snippet.get(k) or "" for k in ("requirement", "responsibility")).lower()
    full = (description or "").lower()
    formats = {f.get("id") for f in vacancy.get("work_format") or []}

    if CFG["required_work_format"] not in formats:
        return Verdict(True, "low", "not remote")

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
