"""Web search providers for discovery: Yandex Search API (async by default, it is much cheaper), Serper (Google),
DuckDuckGo as a last resort. A provider without credentials is skipped. Errors carry no text: URLs and keys stay out of logs."""
import base64
import html
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .. import settings

YANDEX_SEARCH = "https://searchapi.api.cloud.yandex.net/v2/web/search"
YANDEX_ASYNC = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
YANDEX_OPERATION = "https://operation.api.cloud.yandex.net/operations/{id}"
SERPER = "https://google.serper.dev/search"
DDG = "https://html.duckduckgo.com/html/"
UA = {"User-Agent": "Mozilla/5.0"}
ASYNC_WAIT_SECONDS = 600


class SearchError(Exception):
    """A provider failed; the message is a short code, never a URL or response text."""


def _yandex_body(query: str, page: int = 0) -> dict:
    body = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query[:400], "page": page},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": 20, "docsInGroup": 1},
        "maxPassages": 2, "l10n": "LOCALIZATION_RU", "responseFormat": "FORMAT_XML",
    }
    if settings.get("YANDEX_SEARCH_FOLDER_ID"):
        body["folderId"] = settings.get("YANDEX_SEARCH_FOLDER_ID")
    return body


def _yandex_headers() -> dict:
    return {"Authorization": f"Api-Key {settings.get('YANDEX_SEARCH_API_KEY')}"}


def _text(node) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _parse_yandex_xml(raw: bytes) -> list[dict]:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise SearchError("yandex_xml")  # a search result never needs entity declarations; refuse them (entity expansion attacks)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SearchError("yandex_xml")
    hits = []
    for doc in root.iter("doc"):
        url = _text(doc.find("url"))
        if url:
            hits.append({"url": url, "title": _text(doc.find("title")), "snippet": " ".join(_text(p) for p in doc.iter("passage"))})
    return hits


def _decode_raw(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)  # rawData is a protobuf bytes field: base64 in JSON
    except ValueError:
        return data.encode("utf-8")


def _yandex_sync(query: str, page: int = 0) -> list[dict]:
    try:
        r = requests.post(YANDEX_SEARCH, json=_yandex_body(query, page), headers=_yandex_headers(), timeout=60)
        if r.status_code != 200:
            raise SearchError(f"yandex_http_{r.status_code}")
        return _parse_yandex_xml(_decode_raw(r.json().get("rawData", "")))
    except (requests.RequestException, ValueError):
        raise SearchError("yandex_net")


def _yandex_async(jobs: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
    """Submit every (query, page), then poll the operations until all are done."""
    pending: dict[str, tuple[str, int]] = {}
    for job in jobs:
        try:
            r = requests.post(YANDEX_ASYNC, json=_yandex_body(*job), headers=_yandex_headers(), timeout=60)
            if r.status_code != 200:
                raise SearchError(f"yandex_http_{r.status_code}")
            pending[r.json()["id"]] = job
        except (requests.RequestException, ValueError, KeyError):
            raise SearchError("yandex_net")
    results: dict[tuple[str, int], list[dict]] = {}
    deadline = time.time() + ASYNC_WAIT_SECONDS
    while pending and time.time() < deadline:
        time.sleep(5)
        for op_id, job in list(pending.items()):
            try:
                r = requests.get(YANDEX_OPERATION.format(id=op_id), headers=_yandex_headers(), timeout=60)
                data = r.json() if r.status_code == 200 else {}
            except (requests.RequestException, ValueError):
                continue
            if not data.get("done"):
                continue
            del pending[op_id]
            try:
                raw = data.get("response", {}).get("rawData")
                results[job] = _parse_yandex_xml(_decode_raw(raw)) if raw else []
            except SearchError:
                results[job] = []
    return results


def _serper(query: str, page: int = 0) -> list[dict]:
    try:
        r = requests.post(SERPER, json={"q": query, "gl": "ru", "hl": "ru", "num": 10, "page": page + 1},  # up to 10 results is one credit, more costs two
                          headers={"X-API-KEY": settings.get("SERPER_API_KEY")}, timeout=40)
        if r.status_code != 200:
            raise SearchError(f"serper_http_{r.status_code}")
        return [{"url": o["link"], "title": o.get("title", ""), "snippet": o.get("snippet", "")} for o in r.json().get("organic", []) if o.get("link")]
    except (requests.RequestException, ValueError):
        raise SearchError("serper_net")


def _ddg(query: str, page: int = 0) -> list[dict]:
    if page:
        return []  # the fallback only serves the first page
    try:
        r = requests.post(DDG, data={"q": query, "kl": "ru-ru"}, headers=UA, timeout=40)
    except requests.RequestException:
        raise SearchError("ddg_net")
    if r.status_code != 200 or "result__a" not in r.text:
        raise SearchError("ddg_blocked")  # a captcha or a changed page: the markup is not an API
    hits = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        href = html.unescape(m.group(1))
        target = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        hits.append({"url": unquote(target), "title": html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), "snippet": ""})
    return hits


def _has_credentials(provider: str) -> bool:
    return {"yandex": bool(settings.get("YANDEX_SEARCH_API_KEY")), "serper": bool(settings.get("SERPER_API_KEY")), "ddg": True}.get(provider, False)


def _run(provider: str, jobs: list[tuple[str, int]], yandex_mode: str) -> dict[tuple[str, int], list[dict]]:
    if provider == "yandex" and yandex_mode == "async":
        return _yandex_async(jobs)
    run = {"yandex": _yandex_sync, "serper": _serper, "ddg": _ddg}[provider]
    per_job: dict[tuple[str, int], list[dict]] = {}
    for job in jobs:
        for attempt in range(2):  # one query failing (a rate limit, a hiccup) must not drop the whole batch
            try:
                per_job[job] = run(*job)
                break
            except SearchError:
                time.sleep(3)
        time.sleep(2 if provider == "ddg" else 1)
    if not per_job:
        raise SearchError("all_failed")
    return per_job


def search(jobs: list[tuple[str, int]], providers: list[str], yandex_mode: str = "async", combine: bool = False) -> tuple[dict[tuple[str, int], list[dict]], str]:
    """Run the (query, page) jobs. Returns ({job: hits}, names of the providers used joined with "+").
    combine=False: the first provider that works wins, the others are fallbacks.
    combine=True: every provider with a key is asked and the results are merged without duplicate urls (a wider pool).
    DuckDuckGo never joins a merge: it answers servers with a bot check, so it only runs when nothing else produced results.
    A job that failed is missing from the result, so the caller can tell "no results" from "did not run"."""
    merged: dict[tuple[str, int], list[dict]] = {}
    used: list[str] = []
    for provider in providers:
        if not _has_credentials(provider):
            continue
        if used and (not combine or provider == "ddg"):
            break
        try:
            per_job = _run(provider, jobs, yandex_mode)
        except SearchError:
            continue
        if not any(per_job.values()):
            continue
        for job, hits in per_job.items():
            seen = {h["url"] for h in merged.setdefault(job, [])}
            merged[job] += [h for h in hits if h["url"] not in seen]
        used.append(provider)
    return merged, "+".join(used)
