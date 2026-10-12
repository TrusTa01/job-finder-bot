"""Polite page fetching for outreach: robots.txt is honoured, hosts are rate limited, private addresses are refused
(the URLs come from search results). Never prints or logs a URL or an exception text."""
import html
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (compatible; job-autofinder personal bot)"
HOST_DELAY = 2.0          # seconds between requests to one host
MAX_BYTES = 600_000


MAX_RULES, MAX_PATTERN, MAX_PATH = 200, 100, 1000   # robots.txt comes from the remote site: bound the work it can cause


def _glob(pattern: str, text: str) -> bool:
    """Whole-string match where * matches any run of characters, in O(len(pattern) * len(text)); no regex, so no backtracking blow-up."""
    p = t = star = mark = 0
    star = -1
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pattern) and pattern[p] == text[t]:
            p += 1
            t += 1
        elif star != -1:
            p, mark = star + 1, mark + 1
            t = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


class Robots:
    """robots.txt rules for our user agent, with the * and $ wildcards; the longest matching rule wins, Allow wins a tie.
    (urllib.robotparser reads "Disallow: /?" as "Disallow: /" and would close whole sites.)
    A file with more rules or longer patterns than the caps is treated as "everything disallowed"."""

    def __init__(self, text: str):
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        last_was_agent = False
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if ":" not in line:
                continue
            field, value = (s.strip() for s in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not last_was_agent:
                    groups.append(([], []))
                groups[-1][0].append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if field in ("allow", "disallow") and groups:
                groups[-1][1].append((field == "allow", value))
        ours = next((rules for agents, rules in groups if any(a != "*" and a in UA.lower() for a in agents)), None)
        if ours is None:
            ours = next((rules for agents, rules in groups if "*" in agents), [])
        ours = [(allow, path) for allow, path in ours if path]  # an empty Disallow allows everything
        self.deny_all = len(ours) > MAX_RULES or any(len(path) > MAX_PATTERN for _, path in ours)
        self.rules = [(allow, path[:-1] if path.endswith("$") else path, path.endswith("$"), len(path)) for allow, path in ours]

    def allows(self, path_and_query: str) -> bool:
        if self.deny_all or len(path_and_query) > MAX_PATH:
            return False
        best = None
        for allow, pattern, anchored, size in self.rules:
            if _glob(pattern if anchored else pattern + "*", path_and_query) and (best is None or size > best[0] or (size == best[0] and allow)):
                best = (size, allow)
        return True if best is None else best[1]


_robots: dict[str, Robots | None] = {}
_last_hit: dict[str, float] = {}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")  # bounded: an unbounded run on a page of one long word is quadratic
TME = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_/])", re.I)
SKIP_EMAIL = ("example.", "sentry", "wixpress", "@2x", ".png", ".jpg", ".svg", "noreply", "no-reply",
              "ivanov@", "ivan@", "petrov@", "name@", "email@", "your@", "user@", "test@", "mail@mail.", "@domain.", "@site.")  # placeholders from web templates


def host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")


def _public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


MAX_REDIRECTS = 5


def _peer_is_public(r) -> bool:
    """The address the socket really connected to must be public too: the name was checked before connecting, but a DNS
    answer can change in between (rebinding). The GET itself has already gone out, but nothing of the answer is read.
    When the socket cannot be inspected the check is skipped, the name check above still applies."""
    sock = getattr(getattr(r.raw, "connection", None), "sock", None)
    if sock is None:
        return True
    try:
        return ipaddress.ip_address(sock.getpeername()[0].split("%")[0]).is_global
    except (OSError, ValueError, IndexError):
        return False


def _open(url: str, check_robots: bool, timeout: int = 25):
    """GET with redirects followed by hand: every hop must be http(s) to a public address (a public site must not be able to
    send the bot to 127.0.0.1 or a cloud metadata address) and, for pages, allowed by that host's robots.txt.
    Returns an open streaming response, or None. The caller closes it."""
    for _ in range(MAX_REDIRECTS + 1):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not _public(p.hostname):
            return None
        if check_robots:
            if not _allowed(url):
                return None
            wait = HOST_DELAY - (time.time() - _last_hit.get(p.netloc, 0))
            if wait > 0:
                time.sleep(wait)
            _last_hit[p.netloc] = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}, timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException:
            return None
        if not _peer_is_public(r):
            r.close()
            return None
        if r.is_redirect:
            location = r.headers.get("Location")
            r.close()
            if not location:
                return None
            url = urljoin(url, location)
            continue
        return r
    return None


def _allowed(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        r = _open(base + "/robots.txt", check_robots=False, timeout=20)
        if r is None:
            _robots[base] = None  # unreachable or refused: skip the host this run
        else:
            # 5xx: skip the host this run; 4xx: no robots.txt, everything allowed
            try:
                text = r.raw.read(MAX_BYTES, decode_content=True).decode("utf-8", "replace") if r.status_code < 400 else ""
            except (requests.RequestException, OSError):
                text = None
            _robots[base] = None if r.status_code >= 500 or text is None else Robots(text)
            r.close()
    rules = _robots[base]
    return rules is not None and rules.allows((p.path or "/") + ("?" + p.query if p.query else ""))


def fetch(url: str) -> str | None:
    """HTML of a public page, or None when it is refused, unreachable or not HTML."""
    r = _open(url, check_robots=True)
    if r is None:
        return None
    try:
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
            return None
        raw = r.raw.read(MAX_BYTES, decode_content=True)
    except (requests.RequestException, OSError):
        return None
    finally:
        r.close()
    declared = re.search(rb'charset=["\']?([A-Za-z0-9_-]+)', raw[:4000])
    for enc in ([declared.group(1).decode("ascii")] if declared else []) + ["utf-8", "cp1251"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


SKIP_BODIES = ("script", "style", "noscript", "svg")
TAG_NAME = re.compile(r"/?([a-z][a-z0-9]*)")
MAX_TAG = 2000


def _scan(page: str) -> tuple[str, str]:
    """(title, visible text) in one linear pass. Tags are cut with str.find inside a bounded window and the bodies of
    script, style and the like are skipped, so a hostile page cannot make this quadratic the way tag regexes can."""
    low = page.lower()
    text: list[str] = []
    title: list[str] = []
    in_title, i, n = False, 0, len(page)
    while i < n:
        j = page.find("<", i)
        chunk = page[i:] if j == -1 else page[i:j]
        (title if in_title else text).append(chunk)
        if j == -1:
            break
        if low.startswith("<!--", j):
            end = low.find("-->", j + 4)
            if end == -1:
                break
            i = end + 3
            continue
        k = page.find(">", j, j + MAX_TAG)
        if k == -1:  # a stray "<" that opens no tag: keep it as text
            text.append(" ")
            i = j + 1
            continue
        m = TAG_NAME.match(low, j + 1, k)
        if not m and low[j + 1 : j + 2] not in ("!", "?"):  # "1 < 2": not a tag (doctype and xml declarations are)
            text.append("<")
            i = j + 1
            continue
        name, closing = (m.group(1), low[j + 1] == "/") if m else ("", False)
        if name in SKIP_BODIES and not closing:
            end = low.find("</" + name, k)
            close = low.find(">", end) if end != -1 else -1
            if close == -1:
                break  # never closed: the rest is script
            i = close + 1
            continue
        if name == "title":
            in_title = not closing
        text.append(" ")
        i = k + 1
    return " ".join(html.unescape("".join(title)).split())[:200], " ".join(html.unescape("".join(text)).split())


def title_of(page: str) -> str:
    return _scan(page)[0]


def text_of(page: str, limit: int = 4000) -> str:
    return _scan(page)[1][:limit]


def links_of(page: str, base: str) -> list[str]:
    seen: dict[str, None] = {}
    for href in re.findall(r'href=["\']([^"\'#]+)', page):
        absolute = urljoin(base, html.unescape(href).strip())
        if absolute.startswith(("http://", "https://")):
            seen.setdefault(absolute, None)
    return list(seen)


def emails_of(page: str) -> list[str]:
    found = [e.lower() for e in EMAIL.findall(html.unescape(page)) if not any(s in e.lower() for s in SKIP_EMAIL)]
    return list(dict.fromkeys(found))[:5]


def tme_names(page: str) -> list[str]:
    return list(dict.fromkeys(n for n in TME.findall(page) if n.lower() not in {"share", "joinchat", "addstickers", "iv"}))
