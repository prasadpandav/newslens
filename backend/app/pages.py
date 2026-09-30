"""Server-rendered, indexable pages for the web portal.

The portal is a single-page app: the static host hands every URL the same
index.html and the story is drawn by JavaScript that fetches it from this API.
A crawler that does not run JavaScript, or whose renderer times out before the
API answers, sees one generic page at every URL: the same title, the same
description, no headline and no text. That is the main reason the site's
articles were not being indexed.

The fix keeps the app and gives each detail URL real HTML. The web host proxies
/story/*, /trend/* and /signal/* to /page/... here (see web/_redirects). This
module takes the deployed index.html, rewrites its <head> for the page (title,
description, canonical, robots, Open Graph, JSON-LD) and puts the article itself
into <main id="view">. A browser still boots the same app on top of it. The app
sees `data-ssr` on #view and adopts the markup instead of flashing a loading
skeleton over it.

Everything here is pure string building, so it is cheap and testable without a
server. The one piece of state is the cached shell.
"""
import html
import json
import logging
import re
import threading
import time

import httpx

from . import config

log = logging.getLogger("newslens.pages")

# ---------------------------------------------------------------- the shell
# The deployed index.html, fetched from the web host rather than bundled: the
# API's image is built from backend/ only, and fetching it means a web deploy
# shows up here within SHELL_TTL with no API redeploy.
SHELL_TTL = 600
_shell = {"html": None, "at": 0.0}
_shell_lock = threading.Lock()


def shell():
    """The SPA's index.html, or None if it has never been fetched successfully.
    A failed refresh keeps serving the last good copy."""
    now = time.time()
    if _shell["html"] and now - _shell["at"] < SHELL_TTL:
        return _shell["html"]
    # One refresher at a time; everyone else serves the copy they have.
    if not _shell_lock.acquire(blocking=_shell["html"] is None):
        return _shell["html"]
    try:
        if _shell["html"] and time.time() - _shell["at"] < SHELL_TTL:
            return _shell["html"]
        r = httpx.get(f"{config.WEB_BASE_URL}/index.html", timeout=8,
                      follow_redirects=True)
        if r.status_code == 200 and '<main id="view">' in r.text:
            _shell.update(html=r.text, at=time.time())
        else:
            log.warning("shell fetch: %s from %s", r.status_code, config.WEB_BASE_URL)
            _shell["at"] = time.time() - SHELL_TTL + 60   # retry in a minute
    except httpx.HTTPError as e:
        log.warning("shell fetch failed: %s", e)
        _shell["at"] = time.time() - SHELL_TTL + 60
    finally:
        _shell_lock.release()
    return _shell["html"]


# ------------------------------------------------------------------ helpers
def esc(s):
    return html.escape(str(s or ""), quote=True)


def clip(text, n):
    """Trim to about n characters at a word boundary. Same rule as SEO.clip in
    the web app, so the server's description matches what the app then sets."""
    s = " ".join(str(text or "").split())
    if len(s) <= n:
        return s
    return re.sub(r"[\s,;:]+\S*$", "", s[: n - 1]) + "…"


def iso(epoch):
    return (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))
            if epoch else None)


def human_date(epoch):
    return time.strftime("%-d %b %Y", time.gmtime(epoch)) if epoch else ""


def topic_label(topic):
    t = str(topic or "").strip()
    if t.lower().startswith("local:"):
        return "Local"
    return {"ai": "AI", "india": "India"}.get(t.lower(), t[:1].upper() + t[1:]) or "News"


def paras(text):
    """Stored prose as <p>s. Blank-line or newline separated, never raw HTML."""
    return "".join(f"<p>{esc(p)}</p>" for p in str(text or "").split("\n") if p.strip())


def publisher(base):
    return {"@type": "Organization", "name": "Descry", "url": base + "/",
            "logo": {"@type": "ImageObject", "url": base + "/icon.svg"}}


def crumbs(base, items):
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": base + path}
        for i, (name, path) in enumerate(items)]}


def crumb_nav(items):
    """The visible trail that the BreadcrumbList describes. The last entry is
    the page itself, so it is text, not a link."""
    parts = [f'<a href="{esc(p)}">{esc(n)}</a>' for n, p in items[:-1]]
    parts.append(f'<span aria-current="page">{esc(items[-1][0])}</span>')
    return ('<nav class="ssr-crumbs" aria-label="Breadcrumb">'
            + " <span aria-hidden=\"true\">›</span> ".join(parts) + "</nav>")


def more_list(more, skip_id=""):
    """Plain links to the latest stories. Every rendered page carries them, so
    each story is reachable by a crawler from every other one, not only from
    the sitemap."""
    items = [f'<li><a href="/story/{esc(m["id"])}">{esc(m["headline"])}</a></li>'
             for m in more if m["id"] != skip_id][:10]
    if not items:
        return ""
    return ('<aside class="ssr-more"><h2>More from Descry</h2><ul>'
            + "".join(items) + "</ul></aside>")


# ------------------------------------------------------------------- pages
def story_page(s, more, base):
    """(head, body) for one story, from the same payload GET /story/{id} gives a
    signed-out reader, so the page and the app never disagree on what is
    public."""
    sid, headline = s["id"], s.get("headline") or "Story"
    path = f"/story/{sid}"
    topic = topic_label(s.get("topic"))
    trail = [("Descry", "/"), (topic, "/"), (headline, path)]
    desc = clip(s.get("why_matters") or s.get("narrative"), 155)
    img = s.get("image_url") or ""
    if not img.startswith(("http://", "https://")):
        img = ""
    sources = [x for x in (s.get("sources") or []) if x and x.get("url")]
    article = {
        "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": base + path},
        "headline": clip(headline, 110),
        "description": clip(s.get("narrative"), 250),
        "articleSection": topic,
        "datePublished": iso(s.get("created_at")),
        "dateModified": iso(s.get("updated_at") or s.get("created_at")),
        "isAccessibleForFree": True,
        # Written by the Descry pipeline from the sources it cites, so the
        # author is the organisation. Inventing a named person would be false.
        "author": {"@type": "Organization", "name": "Descry", "url": base + "/"},
        "publisher": publisher(base),
        "citation": [{"@type": "CreativeWork", "name": x.get("title") or x["url"],
                      "url": x["url"]} for x in sources[:10]],
    }
    if img:
        article["image"] = [img]
    jsonld = {"@context": "https://schema.org",
              "@graph": [article, crumbs(base, trail)]}

    parts = [crumb_nav(trail), '<article class="ssr-article">',
             f"<h1>{esc(headline)}</h1>",
             f'<p class="ssr-by">By the Descry desk · <time datetime="{esc(iso(s.get("created_at")))}">'
             f'{esc(human_date(s.get("created_at")))}</time></p>']
    if img:
        # alt is the headline: the photograph is the story's, and an empty alt
        # tells image search there is nothing here to index.
        parts.append(f'<img src="{esc(img)}" alt="{esc(headline)}" width="1200" height="675" '
                     f'decoding="async" referrerpolicy="no-referrer">')
    parts.append(paras(s.get("narrative")))
    if s.get("why_matters"):
        parts.append("<h2>Why it matters</h2>" + paras(s["why_matters"]))
    trends = s.get("trends") or []
    if trends:
        parts.append("<h2>The bigger picture</h2><ul>" + "".join(
            f'<li><a href="/trend/{esc(t["id"])}">{esc(t.get("name"))}</a>'
            f'{" — " + esc(clip(t.get("narrative"), 200)) if t.get("narrative") else ""}</li>'
            for t in trends) + "</ul>")
    if sources:
        parts.append("<h2>Where this came from</h2><ul>" + "".join(
            f'<li><a href="{esc(x["url"])}" rel="noopener">{esc(x.get("title") or x["url"])}</a>'
            f'{" · " + esc(x.get("source")) if x.get("source") else ""}</li>'
            for x in sources) + "</ul>")
    parts.append("</article>")
    parts.append(more_list(more, sid))
    head = dict(title=f"{headline} · Descry", desc=desc, path=path,
                og_type="article", image=img, jsonld=jsonld)
    return head, "".join(parts)


def trend_page(t, more, base):
    tid, name = t["id"], t.get("name") or "Trend"
    path = f"/trend/{tid}"
    trail = [("Descry", "/"), ("Trends", "/trends"), (name, path)]
    stories = t.get("stories") or []
    jsonld = {"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "name": name, "url": base + path,
         "description": clip(t.get("narrative"), 250),
         "isPartOf": {"@type": "WebSite", "name": "Descry", "url": base + "/"},
         "hasPart": [{"@type": "NewsArticle", "headline": clip(s.get("headline"), 110),
                      "url": f"{base}/story/{s['id']}"} for s in stories[:20] if s.get("id")]},
        crumbs(base, trail)]}
    parts = [crumb_nav(trail), '<article class="ssr-article">', f"<h1>{esc(name)}</h1>",
             paras(t.get("narrative"))]
    if stories:
        parts.append(f"<h2>The stories behind it</h2><ul>" + "".join(
            f'<li><a href="/story/{esc(s["id"])}">{esc(s.get("headline"))}</a></li>'
            for s in stories if s.get("id")) + "</ul>")
    parts.append("</article>")
    parts.append(more_list(more))
    head = dict(title=f"{name} · Trend · Descry", desc=clip(t.get("narrative"), 155),
                path=path, og_type="website", jsonld=jsonld,
                # A retired trend still answers, so old links work, but it is
                # no longer something to rank; the sitemap drops it too.
                robots="noindex,follow" if t.get("retired_at") else None)
    return head, "".join(parts)


def signal_page(g, more, base):
    gid, title = g["id"], g.get("title") or "Forecast"
    path = f"/signal/{gid}"
    trail = [("Descry", "/"), ("What's next", "/next"), (title, path)]
    conf = g.get("confidence")
    facts = []
    if g.get("horizon"):
        facts.append(f"Horizon: {esc(g['horizon'])}")
    if isinstance(conf, (int, float)):
        facts.append(f"Confidence: {round(conf * 100 if conf <= 1 else conf)}%")
    if g.get("story_count"):
        n = g["story_count"]
        facts.append(f"Built from {n} {'story' if n == 1 else 'stories'}")
    jsonld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": title, "url": base + path,
         "description": clip(g.get("prediction"), 250),
         "isPartOf": {"@type": "WebSite", "name": "Descry", "url": base + "/"}},
        crumbs(base, trail)]}
    parts = [crumb_nav(trail), '<article class="ssr-article">', f"<h1>{esc(title)}</h1>",
             paras(g.get("prediction"))]
    if facts:
        parts.append('<p class="ssr-by">' + " · ".join(facts) + "</p>")
    if g.get("chain"):
        parts.append("<h2>The reasoning</h2>" + paras(g["chain"]))
    parts.append("</article>")
    parts.append(more_list(more))
    head = dict(title=f"{title} · Forecast · Descry", desc=clip(g.get("prediction"), 155),
                path=path, og_type="website", jsonld=jsonld)
    return head, "".join(parts)


def missing_page(kind, path):
    """What a URL for something that no longer exists gets: a real 404 status,
    noindex, and the app, which tells the reader the page is gone."""
    head = dict(title=f"{kind.capitalize()} no longer available · Descry",
                desc="This page is no longer available on Descry.", path=path,
                robots="noindex,follow")
    body = (f'<div class="ssr-article"><h1>This {esc(kind)} is no longer available</h1>'
            f'<p><a href="/">Back to today\'s stories</a></p></div>')
    return head, body


# --------------------------------------------------------------- assembly
# Tags the shell carries that describe the HOME page. They are removed before
# the page's own are inserted, so no URL ever ships two titles or two
# canonicals.
_HEAD_TAGS = re.compile(
    r'<title>.*?</title>\s*'
    r'|<meta\s+(?:name|property)="(?:description|robots|og:[a-z_:]+|twitter:[a-z_:]+)"[^>]*>\s*'
    r'|<link\s+rel="canonical"[^>]*>\s*',
    re.S | re.I)

# Minimal styling for the moment before the app takes over, and for any reader
# without JavaScript. It uses the app's own tokens, so it follows the theme.
_SSR_CSS = """<style id="ssr-css">
.ssr{max-width:720px;margin:0 auto;padding:24px 20px 60px;font-family:var(--serif,Georgia,serif);color:var(--ink,var(--text,#111))}
.ssr h1{font-size:clamp(28px,4vw,40px);line-height:1.15;font-weight:400;margin:10px 0 12px}
.ssr h2{font-size:20px;font-weight:500;margin:28px 0 8px}
.ssr p,.ssr li{font-size:18px;line-height:1.6}
.ssr img{width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;border-radius:6px;margin:12px 0}
.ssr-crumbs,.ssr-by{font:13px/1.4 var(--sans,system-ui,sans-serif);color:var(--text-2,#666)}
.ssr-crumbs a,.ssr a{color:inherit}
.ssr-more{margin-top:40px;border-top:1px solid var(--rule-2,#ddd);padding-top:12px}
</style>"""


def head_block(base, title, desc, path, robots=None, og_type="website", image=None,
               jsonld=None):
    url = base + path
    img = image or config.OG_IMAGE_URL
    tags = [
        f"<title>{esc(title)}</title>",
        f'<meta name="description" content="{esc(desc)}">',
        f'<meta name="robots" content="{esc(robots or "index,follow,max-image-preview:large,max-snippet:-1")}">',
        f'<link rel="canonical" href="{esc(url)}">',
        '<meta property="og:site_name" content="Descry">',
        f'<meta property="og:type" content="{esc(og_type)}">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(desc)}">',
        f'<meta property="og:url" content="{esc(url)}">',
        f'<meta property="og:image" content="{esc(img)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        f'<meta name="twitter:description" content="{esc(desc)}">',
    ]
    if jsonld:
        # id="ld-route" is the element the app's SEO.set() replaces, so the
        # structured data is swapped, never duplicated, when the app takes over.
        # "</" is escaped so no string inside can close the script element.
        data = json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/")
        tags.append(f'<script type="application/ld+json" id="ld-route">{data}</script>')
    return "\n".join(tags) + "\n" + _SSR_CSS


def assemble(shell_html, head, body, base):
    """The final document. With a shell: the app, with this page's <head> and
    content. Without one (the web host could not be reached): a plain page with
    the same head and content, which is still a complete, indexable article."""
    block = head_block(base, **head)
    main = (f'<main id="view" data-ssr="{esc(head["path"])}">'
            f'<div class="ssr">{body}</div></main>')
    if shell_html:
        doc = _HEAD_TAGS.sub("", shell_html)
        doc = doc.replace("<head>", "<head>\n" + block, 1)
        return doc.replace('<main id="view"></main>', main, 1)
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            f'{block}</head><body>{main}</body></html>')
