"""Story artwork: pick the best image an RSS feed already told us about.

Deliberately URL-ONLY. Nothing here downloads, decodes or stores image bytes —
the instance this runs on has 512MB of RAM and a small disk, and a single
full-size news photo is several MB (see render-512mb-oom-limit for what
unbounded reads already cost us once). Clients load the URL directly, which
also means the publisher's CDN serves it, not us.

That constraint shapes what "visually most clear, hi-res" can mean. Without
decoding pixels we cannot measure sharpness, so we rank on the signals feeds
actually carry — declared width/height, dimensions embedded in the URL, aspect
ratio, and format — and reject the furniture (logos, avatars, tracking pixels,
sprite sheets, emoji) that scraping <img> out of summary HTML drags in. Real
example this was written against: a WordPress feed whose first <img> was
`s.w.org/images/core/emoji/17.0.2/72x72/1f4c9.png`.

The other half of the answer is corroboration, which the pipeline already
does for free: a story groups the same event across outlets, so `best_of`
choosing the highest-scoring image over ALL a story's articles routinely
upgrades a 240x135 wire thumbnail to a 1600x900 frame from another outlet.
"""
import re

# Feeds are inconsistent about where artwork lives, so every mechanism is
# harvested and then scored against the others rather than trusting an order.
# Measured across the 40 configured feeds (557 entries, 78% carried an image):
#   media_content 279, media_thumbnail 149, img-in-HTML 47, enclosure 37.
_IMG_IN_HTML = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)
_DIMS_IN_URL = re.compile(r"(?<!\d)(\d{3,4})\s*[xX]\s*(\d{3,4})(?!\d)")

# Substrings that mean "this is site furniture, not the story's picture".
_JUNK = ("emoji", "avatar", "gravatar", "logo", "icon", "sprite", "spacer",
         "blank.", "pixel", "placeholder", "default-", "1x1", "transparent",
         "/ads/", "doubleclick", "s.w.org", "feedburner", "share-", "badge")
# Raster formats only. SVG is almost always a logo; GIF is usually an animation
# or a spacer. A URL with no extension at all is allowed — many CDNs omit it.
_BAD_EXT = (".svg", ".gif", ".ico", ".bmp")

# Below this an image is too small to sit at the top of a story without
# looking soft when upscaled. Kept as a floor, not a filter: see score().
MIN_WIDTH, MIN_HEIGHT = 200, 120
# Banners and skyscrapers are never the story's photo.
MIN_ASPECT, MAX_ASPECT = 0.4, 3.5


# What BBC thumbnails are rewritten up to. 1024 comfortably clears every
# consumer of these dimensions (card, reader hero, and the 300x200 floor a
# social crawler needs before it will render a large preview).
UPGRADE_WIDTH = 1024
_BBC_SIZED = re.compile(r"(ichef\.bbci\.co\.uk/(?:ace/)?[a-z]+/)(\d{2,4})(/)")


def _upgrade(url, width=None, height=None):
    """Rewrite a known thumbnail URL to its full-size variant, RETURNING THE
    DIMENSIONS THAT NOW APPLY.

    BBC is the case that forces this: its feeds hand out 240x135 and the width
    lives in the path (`/ace/standard/240/...`), which is iChef's documented
    resize parameter.

    Scaling the declared size along with the URL is not cosmetic. The feed's
    width/height describe the thumbnail that was just replaced, so carrying
    them over makes a 1024px photograph look like a 240px one to every size
    check downstream — which is exactly what made shared BBC stories unfurl
    with the site logo instead of their own picture: the Open Graph card read
    240x135, judged it too small for a rich preview, and substituted the
    fallback.

    NOTE: the rewrite itself could not be verified from the dev network —
    ichef.bbci.co.uk answers 403 to any request from here, including the
    unmodified URL — so it stays limited to that one well-known pattern, and a
    wrong guess degrades to a broken <img> the clients already handle.
    """
    if not url:
        return url, width, height
    m = _BBC_SIZED.search(url)
    if not m:
        return url, width, height
    if int(m.group(2)) >= UPGRADE_WIDTH:
        return url, width, height          # already at least this big
    new_url = url[:m.start(2)] + str(UPGRADE_WIDTH) + url[m.end(2):]
    w, h = _as_int(width), _as_int(height)
    if w and h and w > 0:
        return new_url, UPGRADE_WIDTH, int(round(h * (UPGRADE_WIDTH / float(w))))
    # No declared size to scale from — report unknown rather than invent one.
    return new_url, None, None


def _dims_from_url(url):
    """Best-effort WxH parsed out of the URL, for the ~20% of feeds that
    declare no width/height (engadget, theverge, technologyreview, ...).
    Publishers encode it constantly: `.../1600x900/...`, `_625x300_`."""
    m = _DIMS_IN_URL.search(url or "")
    if not m:
        return None, None
    w, h = int(m.group(1)), int(m.group(2))
    # Guard against matching something that isn't a size (a date, an id).
    return (w, h) if 100 <= w <= 6000 and 100 <= h <= 6000 else (None, None)


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def declared_dims(url, width=None, height=None):
    """The most trustworthy (width, height) for an image URL.

    Stored dimensions can contradict the URL. Rows written before `_upgrade`
    learned to scale kept the feed's 240x135 beside a rewritten 1024px BBC
    URL, and those rows cannot fix themselves: repairing them by re-reading
    feeds only reaches articles still inside the publisher's current window,
    so anything older keeps lying about its size forever — which is why an
    already-shared story went on unfurling with the logo.

    Where the URL states a size it is the stronger evidence: it is the size the
    CDN will actually render. Only that specific disagreement is overridden;
    otherwise stored values win, since they came from the feed itself."""
    w, h = _as_int(width), _as_int(height)
    m = _BBC_SIZED.search(url or "")
    if m:
        url_w = int(m.group(2))
        if w and h and w > 0 and w != url_w:
            return url_w, int(round(h * (url_w / float(w))))   # scale to match
        if w and h:
            return w, h
        return url_w, None
    if w and h:
        return w, h
    return _dims_from_url(url or "")


def usable(url, width=None, height=None):
    """False for artwork that is structurally not a story photo."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    if low.split("?")[0].endswith(_BAD_EXT):
        return False
    w, h = _as_int(width), _as_int(height)
    if not (w and h):
        w, h = _dims_from_url(url)
    # Real dimensions outrank the name heuristic. Livemint serves genuine
    # 1600x900 article photos under a `/logo/` path segment, so matching _JUNK
    # as a bare substring would drop one of the best-resolution sources we
    # have; nothing that is actually a logo/avatar/emoji comes at this size.
    if w and h and w >= 400 and w * h >= 240_000:
        return True
    return not any(j in low for j in _JUNK)


def score(url, width=None, height=None):
    """Rank a candidate. Higher is better; 0 means unusable.

    Area dominates because it is the only real proxy for "hi-res" available
    without fetching, but it is damped (square root) so a merely-huge image
    can't beat a well-shaped one purely on pixel count, and shape is scored
    explicitly — a story header wants a landscape frame.
    """
    if not usable(url, width, height):
        return 0.0
    # declared_dims, not the raw pair: a stored row may still carry dimensions
    # that describe a thumbnail its URL has since been upgraded past, and
    # scoring those would keep picking a genuinely large photo last.
    w, h = declared_dims(url, width, height)
    if not (w and h):
        w2, h2 = _dims_from_url(url)
        w, h = w or w2, h or h2
    if not (w and h):
        # Unknown size: usable, but must lose to anything that proves it's big.
        return 1.0
    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return 0.3          # keep as a last resort rather than dropping it
    aspect = w / float(h)
    if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
        return 0.2          # banner/skyscraper shaped
    s = (w * h) ** 0.5 / 100.0
    if 1.3 <= aspect <= 2.1:
        s *= 1.25           # landscape hero, what both clients render
    return s


def candidates(entry):
    """Every image URL a feedparser entry offers, with declared dims."""
    out = []
    for mc in (getattr(entry, "media_content", None) or []):
        out.append((mc.get("url"), mc.get("width"), mc.get("height")))
    for mt in (getattr(entry, "media_thumbnail", None) or []):
        out.append((mt.get("url"), mt.get("width"), mt.get("height")))
    for enc in (getattr(entry, "enclosures", None) or []):
        if "image" in (enc.get("type") or ""):
            out.append((enc.get("href"), None, None))
    # Last resort: the first <img> in the summary/content HTML. This is the
    # mechanism that yields junk, so `usable` does the real work here.
    html = getattr(entry, "summary", "") or ""
    for c in (getattr(entry, "content", None) or []):
        html += c.get("value", "") or ""
    m = _IMG_IN_HTML.search(html)
    if m:
        out.append((m.group(1), None, None))
    return [(u, w, h) for u, w, h in out if u]


def from_entry(entry):
    """(url, width, height) of the best image on this entry, or (None, None, None).

    Width/height are returned as the feed declared them (or as parsed from the
    URL) so a later cross-article comparison in `best_of` can rank stored rows
    without re-reading the feed."""
    best, best_s = None, 0.0
    for url, w, h in candidates(entry):
        # Dimensions come back from _upgrade because rewriting the URL changes
        # what they describe — see that function.
        url, w, h = _upgrade(url, w, h)
        s = score(url, w, h)
        if s > best_s:
            wi, hi = _as_int(w), _as_int(h)
            if not (wi and hi):
                wi, hi = _dims_from_url(url)
            best, best_s = (url, wi, hi), s
    return best if best else (None, None, None)


def best_of(rows):
    """Pick the best image across a story's articles.

    This is where "based on multiple similar stories" pays off: the same event
    covered by several outlets gives several frames, and the highest-scoring
    one wins — which is what lifts a story off a 240px wire thumbnail when any
    other outlet carried a full-size photo. `rows` are article rows (anything
    supporting ["image_url"] / ["image_width"] / ["image_height"])."""
    best, best_s = None, 0.0
    for r in rows:
        try:
            url = r["image_url"]
        except (KeyError, IndexError, TypeError):
            continue
        if not url:
            continue
        try:
            w, h = r["image_width"], r["image_height"]
        except (KeyError, IndexError, TypeError):
            w = h = None
        s = score(url, w, h)
        if s > best_s:
            best, best_s = url, s
    return best
