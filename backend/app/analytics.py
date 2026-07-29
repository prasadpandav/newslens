"""First-party analytics: unique devices, page views and overall traffic.

Deliberately narrow in what it keeps. There is no IP address and no user-agent
anywhere in this module's storage:

  * `device`  a random id the CLIENT mints and keeps in its own storage. It
              identifies a browser profile, not a person, and carries nothing
              derived from the request.
  * `country` resolved from the edge's geo header at request time. The address
              it came from is read and discarded, never written.
  * `ref`     the referring site's HOST only ("news.ycombinator.com"), so the
              full referring URL — which can carry private query strings — is
              never stored.

Two shapes of data:
  visits   one row per page view (who/where/what), used for unique-device and
           audience reporting. Purged after ANALYTICS_RETAIN_DAYS.
  traffic  a running per-(day, route) counter for EVERY API request, so overall
           volume stays queryable forever without a row per request.
"""
import sqlite3
import time
from urllib.parse import urlsplit
from . import config, db

# Edge/CDN geo headers, best first. Render fronts services with Cloudflare, which
# sets CF-IPCountry; the others are here so a different proxy still works.
_GEO_HEADERS = ("cf-ipcountry", "x-vercel-ip-country", "x-geo-country",
                "x-appengine-country")

# Paths that would drown real signal: infra endpoints, the admin console and its
# APIs (that is us, not an audience), and the SSE stream which is one long-lived
# request per reader rather than a page view.
_IGNORED_PREFIXES = ("/admin", "/live/stream", "/robots.txt", "/sitemap.xml",
                     "/favicon", "/track", "/docs", "/openapi.json", "/redoc")

MAX_PATH = 120          # cap stored path length; longer paths are truncated
MAX_REF = 80


def day_key(ts=None):
    """UTC date stamp 'YYYY-MM-DD' — the bucket every report groups by."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


def country_of(headers):
    """Country code from an edge geo header, or '' when unknown. The client IP is
    never consulted here, so nothing derived from it can leak into storage."""
    for h in _GEO_HEADERS:
        v = (headers.get(h) or "").strip().upper()
        # Cloudflare sends XX for anonymised/unknown clients, T1 for Tor.
        if v and v not in ("XX", "T1") and len(v) == 2 and v.isalpha():
            return v
    return ""


def ref_host(referrer):
    """Referring HOST only — never the full URL, which may carry private query
    strings. Own-domain referrers collapse to '' so internal navigation doesn't
    register as an acquisition source."""
    if not referrer:
        return ""
    try:
        host = (urlsplit(referrer).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    host = host[4:] if host.startswith("www.") else host
    own = (urlsplit(config.WEB_BASE_URL).hostname or "").lower()
    own = own[4:] if own.startswith("www.") else own
    if host == own:
        return ""
    return host[:MAX_REF]


def is_trackable(path):
    """Whether a request path counts toward traffic at all."""
    return not path.startswith(_IGNORED_PREFIXES)


def record_visit(con, device, path, referrer="", country="", user_id=""):
    """Store one page view. Best-effort: analytics must never break a request."""
    if not device:
        return False
    try:
        con.execute(
            "INSERT INTO visits (id, device, day, path, ref, country, user_id, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (db.new_id(), str(device)[:64], day_key(), (path or "/")[:MAX_PATH],
             ref_host(referrer), country, user_id or "", db.now()))
        con.commit()
        return True
    except sqlite3.Error:
        return False


def bump_traffic(con, route):
    """Increment the (day, route) request counter. UPSERT keeps it to one row."""
    try:
        con.execute(
            "INSERT INTO traffic (day, route, hits) VALUES (?,?,1) "
            "ON CONFLICT(day, route) DO UPDATE SET hits = hits + 1",
            (day_key(), (route or "/")[:MAX_PATH]))
        con.commit()
        return True
    except sqlite3.Error:
        return False


def purge(con, retain_days=None):
    """Drop raw visit rows past the retention window. Daily traffic counters are
    aggregates with no device in them, so they are kept."""
    days = config.ANALYTICS_RETAIN_DAYS if retain_days is None else retain_days
    try:
        n = con.execute("DELETE FROM visits WHERE created_at < ?",
                        (db.now() - days * 86400,)).rowcount
        con.commit()
        return n
    except sqlite3.Error:
        return 0


# ------------------------------------------------------------------ reporting

def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _dense_days(rows, days, fields):
    """Fill in the days that had no activity with zeroes.

    SQL GROUP BY only returns days that HAVE rows, which makes a sparse series
    plot as a few full-height bars sitting next to each other — reading as
    constant activity rather than the occasional spike it really was. Charts
    need one entry per day in the window to be honest about the gaps."""
    by_day = {r["day"]: r for r in rows}
    start = time.time() - (days - 1) * 86400
    out = []
    for i in range(days):
        d = day_key(start + i * 86400)
        row = by_day.get(d)
        out.append(row if row else dict({"day": d}, **{f: 0 for f in fields}))
    return out


def visitor_report(con, days=30):
    """Audience: unique devices, new vs returning, and where they came from."""
    since_ts = db.now() - days * 86400
    since_day = day_key(since_ts)

    totals = con.execute(
        "SELECT COUNT(DISTINCT device) devices, COUNT(*) views "
        "FROM visits WHERE created_at >= ?", (since_ts,)).fetchone()

    # A device is "new" when its FIRST EVER visit falls inside the window, so a
    # returning reader is never miscounted as new just because the window moved.
    new_devices = con.execute(
        "SELECT COUNT(*) c FROM (SELECT device, MIN(created_at) first_seen "
        "FROM visits GROUP BY device HAVING first_seen >= ?)", (since_ts,)
    ).fetchone()["c"]

    daily = _dense_days(
        _rows(con, "SELECT day, COUNT(DISTINCT device) devices, COUNT(*) views "
                   "FROM visits WHERE day >= ? GROUP BY day ORDER BY day", (since_day,)),
        days, ("devices", "views"))
    pages = _rows(con,
                  "SELECT path, COUNT(*) views, COUNT(DISTINCT device) devices "
                  "FROM visits WHERE created_at >= ? GROUP BY path "
                  "ORDER BY views DESC LIMIT 15", (since_ts,))
    referrers = _rows(con,
                      "SELECT ref, COUNT(*) views, COUNT(DISTINCT device) devices "
                      "FROM visits WHERE created_at >= ? AND ref != '' "
                      "GROUP BY ref ORDER BY views DESC LIMIT 15", (since_ts,))
    countries = _rows(con,
                      "SELECT country, COUNT(*) views, COUNT(DISTINCT device) devices "
                      "FROM visits WHERE created_at >= ? AND country != '' "
                      "GROUP BY country ORDER BY views DESC LIMIT 15", (since_ts,))
    # Loyalty: how many distinct days each device showed up.
    depth = con.execute(
        "SELECT AVG(d) avg_days, MAX(d) max_days FROM "
        "(SELECT device, COUNT(DISTINCT day) d FROM visits WHERE created_at >= ? "
        "GROUP BY device)", (since_ts,)).fetchone()

    total_devices = totals["devices"] or 0
    return {"window_days": days,
            "unique_devices": total_devices,
            "page_views": totals["views"] or 0,
            "new_devices": new_devices,
            "returning_devices": max(0, total_devices - new_devices),
            "avg_active_days": round(depth["avg_days"] or 0, 2),
            "max_active_days": depth["max_days"] or 0,
            "daily": daily, "top_pages": pages,
            "referrers": referrers, "countries": countries}


def traffic_report(con, days=30):
    """Overall request volume: every API hit, not just page views."""
    since_day = day_key(db.now() - days * 86400)
    daily = _dense_days(
        _rows(con, "SELECT day, SUM(hits) hits FROM traffic WHERE day >= ? "
                   "GROUP BY day ORDER BY day", (since_day,)), days, ("hits",))
    routes = _rows(con, "SELECT route, SUM(hits) hits FROM traffic WHERE day >= ? "
                        "GROUP BY route ORDER BY hits DESC LIMIT 20", (since_day,))
    total = sum(d["hits"] or 0 for d in daily)
    busiest = max(daily, key=lambda d: d["hits"] or 0) if daily else None
    return {"window_days": days, "total_requests": total,
            "avg_per_day": round(total / max(len(daily), 1), 1),
            "busiest_day": busiest, "daily": daily, "top_routes": routes}


def user_report(con, days=30, limit=200):
    """Signed-in users: who they are, when they joined, and how active they are."""
    since_ts = db.now() - days * 86400
    total = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    google = con.execute(
        "SELECT COUNT(*) c FROM users WHERE email IS NOT NULL AND email != ''"
    ).fetchone()["c"]
    recent = con.execute("SELECT COUNT(*) c FROM users WHERE created_at >= ?",
                         (since_ts,)).fetchone()["c"]
    signups = _dense_days(
        _rows(con, "SELECT DATE(created_at, 'unixepoch') day, COUNT(*) signups "
                   "FROM users WHERE created_at >= ? GROUP BY day ORDER BY day",
              (since_ts,)), days, ("signups",))
    # EVERY account, not just the Google ones. A row with no email is a guest
    # account the app mints when someone personalises their feed without signing
    # in — still a real account with its own saved context and bookmarks, so
    # filtering them out made the table disagree with the totals above it.
    # `is_google` lets the UI label each row rather than hiding half of them.
    people = _rows(con, """
        SELECT u.id, u.email, u.name, u.picture, u.created_at,
               CASE WHEN u.email IS NOT NULL AND u.email != '' THEN 1 ELSE 0 END AS is_google,
               CASE WHEN COALESCE(u.context,'') NOT IN ('', '{}') THEN 1 ELSE 0 END AS personalized,
               (SELECT COUNT(*) FROM bookmarks b WHERE b.user_id = u.id)  saved,
               (SELECT COUNT(*) FROM feedback  f WHERE f.user_id = u.id)  reactions,
               (SELECT MAX(v.created_at) FROM visits v WHERE v.user_id = u.id) last_seen
        FROM users u
        ORDER BY u.created_at DESC LIMIT ?""", (limit,))
    return {"window_days": days, "total": total, "google_signed_up": google,
            "anonymous": max(0, total - google), "new_in_window": recent,
            "listed": len(people), "signups_by_day": signups, "users": people}
