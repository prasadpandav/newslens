"""Polite, best-effort article full-text extraction.

RSS summaries average ~150 characters, which is too thin to "join the dots"
across outlets — there is barely any detail for one source to have and another to
lack. Fetching the article page gives the fact-merger real material to work with.

Deliberate constraints, because this fetches other people's pages:
  * robots.txt is honoured (stdlib urllib.robotparser, cached per host);
  * a descriptive User-Agent identifies the bot;
  * requests are spaced per domain and hard-capped in time and size;
  * text is used transiently to build the LLM prompt and is NOT persisted —
    ARCHITECTURE.md keeps only headline/summary/link;
  * every failure degrades silently to the RSS summary.

Set FULLTEXT_ENABLED=0 to switch the whole thing off.
"""
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from . import config
from .textmerge import clean_text

UA = "DescryBot/0.1 (+https://descry.onrender.com; news summarisation)"

_robots: dict = {}          # host -> RobotFileParser | None
_last_hit: dict = {}        # host -> timestamp
_lock = threading.Lock()
MIN_DOMAIN_INTERVAL = 1.5   # seconds between requests to the same host
# Guard against a pathological page, but stay generous: news sites front-load
# hundreds of KB of inline scripts/JSON before the article body, so a tight cap
# here silently truncates away the very text we came for.
MAX_HTML_CHARS = 2_000_000

# Whole blocks that never contain article prose.
_STRIP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|figure|iframe|svg)\b[^>]*>"
    r".*?</\1>", re.I | re.S)
_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.I | re.S)
_BLOCK_RE = re.compile(r"<(p|h1|h2|h3|li)\b[^>]*>(.*?)</\1>", re.I | re.S)


def _allowed(url):
    """robots.txt check. On any doubt we fetch nothing."""
    try:
        host = urlparse(url).netloc
        with _lock:
            rp = _robots.get(host, "missing")
        if rp == "missing":
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{urlparse(url).scheme}://{host}/robots.txt")
            try:
                rp.read()
            except Exception:  # noqa: BLE001 — no robots.txt reachable
                rp = None
            with _lock:
                _robots[host] = rp
        return True if rp is None else rp.can_fetch(UA, url)
    except Exception:  # noqa: BLE001
        return False


def _pace(host):
    with _lock:
        wait = MIN_DOMAIN_INTERVAL - (time.time() - _last_hit.get(host, 0.0))
        _last_hit[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def html_to_text(html_doc, max_chars=None):
    """Extract article prose from HTML with a text-density heuristic.

    No readability/lxml dependency — the container must stay small enough for the
    Render free tier, so this keeps paragraph-ish blocks with enough words to be
    prose and drops the furniture.
    """
    max_chars = max_chars or config.FULLTEXT_MAX_CHARS
    if not html_doc:
        return ""
    doc = _STRIP_BLOCKS.sub(" ", html_doc)
    inner = _ARTICLE_RE.search(doc)          # prefer the semantic <article> body
    if inner:
        doc = inner.group(1)
    out, seen = [], set()
    for _, block in _BLOCK_RE.findall(doc):
        text = clean_text(re.sub(r"<[^>]+>", " ", block))
        if len(text.split()) < 8:            # nav items, captions, bylines
            continue
        key = text[:80].lower()
        if key in seen:                      # repeated teasers/related-links
            continue
        seen.add(key)
        out.append(text)
        if sum(len(t) for t in out) >= max_chars:
            break
    return " ".join(out)[:max_chars]


def fetch_text(url, timeout=None):
    """Return extracted article text, or "" when unavailable/disallowed."""
    if not config.FULLTEXT_ENABLED or not url:
        return ""
    if not url.startswith(("http://", "https://")):
        return ""
    if not _allowed(url):
        return ""
    host = urlparse(url).netloc
    try:
        _pace(host)
        r = httpx.get(url, timeout=timeout or config.FULLTEXT_TIMEOUT,
                      follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "text/html"})
        if r.status_code != 200:
            return ""
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return ""
        # Cap what we even parse, so a huge page can't stall the pipeline.
        return html_to_text(r.text[:MAX_HTML_CHARS])
    except Exception:  # noqa: BLE001 — never let a publisher's site break a run
        return ""


def fetch_for_articles(articles, limit=None):
    """{article_id: text} for a story's articles. Bounded and failure-tolerant."""
    limit = limit or config.FULLTEXT_MAX_PER_STORY
    out = {}
    if not config.FULLTEXT_ENABLED:
        return out
    for a in articles[:limit]:
        url = a["url"] if "url" in a.keys() else ""
        text = fetch_text(url)
        if text:
            out[a["id"]] = text
    return out
