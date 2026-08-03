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


def _upgrade(url):
    """Rewrite a known thumbnail URL to its full-size variant.

    BBC is the case that forces this: its feeds hand out 240x135 and the width
    lives in the path (`/ace/standard/240/...`), which is iChef's documented
    resize parameter. NOTE: this could not be verified from the dev network —
    ichef.bbci.co.uk answers 403 to any request from here, including the
    unmodified URL — so it is applied only to that one well-known pattern, and
    a wrong guess degrades to a broken <img> the clients already handle rather
    than to a crash.
    """
    return re.sub(r"(ichef\.bbci\.co\.uk/(?:ace/)?[a-z]+/)\d{2,4}(/)",
                  r"\g<1>1024\g<2>", url or "")


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
    w, h = _as_int(width), _as_int(height)
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
        url = _upgrade(url)
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
