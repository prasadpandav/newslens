"""Live dynamic-hero data: breaking, sports scores, finance.

The fast `refresh_live` job (scheduled every LIVE_REFRESH_MINUTES, separate from
the 3h story pipeline) rebuilds the `live_cards` table from three sources:

  breaking  — no-LLM heuristic over recent stories (agents.detect_breaking)
  score     — live/ongoing matches from a free sports API (SportsClient)
  market    — finance news + optional keyless index snapshot (FinanceProvider)

`/live` reads a filtered snapshot of that table; `/live/stream` diffs it on an
interval and pushes changes over SSE. Every source degrades gracefully: a missing
key or a network error just means that category has no cards this cycle.
"""
import asyncio
import json
import time
import httpx
from . import config, db
from .agents import detect_breaking

# Card-type ordering when priorities tie: breaking first, markets last.
_TYPE_RANK = {"breaking": 0, "event": 1, "score": 2, "market": 3}
# Which live_cards types each user-facing category maps to.
CATEGORY_TYPES = {
    "breaking": {"breaking"},
    "events": {"event"},
    "sports": {"score"},
    "finance": {"market"},
}


# --------------------------------------------------------------- sports
class SportsClient:
    """Live soccer + cricket scores from TheSportsDB free tier. The public test
    key '3' works for livescore endpoints; users can set their own SPORTS_API_KEY."""

    SPORTS = ("Soccer", "Cricket")

    def __init__(self):
        self.key = config.SPORTS_API_KEY or "3"  # '3' = TheSportsDB public test key

    def fetch(self):
        if config.SPORTS_PROVIDER != "thesportsdb":
            return []
        cards = []
        for sport in self.SPORTS:
            try:
                r = httpx.get(
                    f"https://www.thesportsdb.com/api/v2/json/{self.key}/livescore/{sport}",
                    timeout=12, headers={"User-Agent": "DescryBot/0.1"})
                if r.status_code != 200:
                    continue
                events = (r.json() or {}).get("livescore") or []
            except Exception:  # noqa: BLE001 — any failure just yields no cards
                continue
            for e in events[:10]:
                cards.append(self._card(sport, e))
        return [c for c in cards if c]

    def _card(self, sport, e):
        home = e.get("strHomeTeam") or ""
        away = e.get("strAwayTeam") or ""
        if not home or not away:
            return None
        hs, as_ = e.get("intHomeScore"), e.get("intAwayScore")
        score = f"{hs}–{as_}" if hs is not None and as_ is not None else "vs"
        status = (e.get("strStatus") or e.get("strProgress") or "LIVE").strip()
        league = e.get("strLeague") or sport
        eid = e.get("idEvent") or f"{home}-{away}"
        # Live matches rank above upcoming; keep the number in a sane 0..1 band.
        live = any(k in status.upper() for k in ("LIVE", "1H", "2H", "HT", "IN PLAY"))
        return {
            "id": f"score-{eid}", "type": "score", "priority": 0.6 if live else 0.3,
            "title": f"{home} {score} {away}", "subtitle": f"{league} · {status}",
            "detail": sport, "story_id": "", "url": "",
            "payload": {"sport": sport, "home": home, "away": away,
                        "score": score, "status": status, "league": league,
                        "live": live}}


# -------------------------------------------------------------- finance
class FinanceProvider:
    """Finance cards: recent finance/business headlines already ingested, plus an
    optional index snapshot from Stooq (keyless CSV). News works with no network."""

    INDICES = (("^spx", "S&P 500"), ("^ndq", "Nasdaq"), ("^dji", "Dow"))

    def cards(self, con):
        if not config.FINANCE_ENABLED:
            return []
        out = self._index_snapshot()
        rows = con.execute(
            "SELECT id, headline, topic, created_at FROM stories "
            "WHERE topic IN ('finance','business') AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 5", (db.now() - 24 * 3600,)).fetchall()
        for s in rows:
            out.append({
                "id": f"market-{s['id']}", "type": "market", "priority": 0.2,
                "title": s["headline"], "subtitle": "Finance", "detail": "",
                "story_id": s["id"], "url": "", "payload": {"topic": s["topic"]}})
        return out

    def _index_snapshot(self):
        cards = []
        for sym, label in self.INDICES:
            try:
                r = httpx.get(f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
                              timeout=8, headers={"User-Agent": "DescryBot/0.1"})
                if r.status_code != 200:
                    continue
                lines = r.text.strip().splitlines()
                if len(lines) < 2:
                    continue
                cols = dict(zip(lines[0].split(","), lines[1].split(",")))
                close, open_ = cols.get("Close"), cols.get("Open")
                if not close or close == "N/D":
                    continue
                pct = ""
                try:
                    c, o = float(close), float(open_)
                    if o:
                        pct = f"{(c - o) / o * 100:+.2f}%"
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
                cards.append({
                    "id": f"market-idx-{sym}", "type": "market", "priority": 0.25,
                    "title": f"{label} {close}", "subtitle": pct or "index",
                    "detail": "", "story_id": "", "url": "",
                    "payload": {"symbol": sym, "close": close, "change_pct": pct}})
            except Exception:  # noqa: BLE001
                continue
        return cards


# --------------------------------------------------- refresh + assembly
def _important_events(con):
    """High-impact stories that aren't already breaking — the 'important events'
    category. Uses the max personal impact_score recorded for a story as a proxy."""
    rows = con.execute(
        "SELECT s.id, s.headline, s.topic, MAX(f.impact_score) imp "
        "FROM stories s JOIN feed_items f ON f.story_id = s.id "
        "WHERE s.created_at > ? AND f.impact_score >= 2 "
        "GROUP BY s.id ORDER BY imp DESC, s.created_at DESC LIMIT 5",
        (db.now() - 24 * 3600,)).fetchall()
    return [{
        "id": f"event-{r['id']}", "type": "event", "priority": 0.4 + 0.1 * (r["imp"] or 0),
        "title": r["headline"], "subtitle": f"High impact · {r['topic']}",
        "detail": "", "story_id": r["id"], "url": "", "payload": {"topic": r["topic"]}}
        for r in rows]


def refresh_live(con):
    """Rebuild the live_cards table. Called by the fast scheduled job and on demand.
    Each source is independent — one failing never blocks the others."""
    cards = []
    now = db.now()
    try:
        for b in detect_breaking(con):
            b = dict(b)
            b.setdefault("id", f"breaking-{b['story_id']}")
            cards.append(b)
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "live_breaking", "error", str(e)[:200])
    try:
        cards += _important_events(con)
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "live_events", "error", str(e)[:200])
    try:
        cards += SportsClient().fetch()
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "live_sports", "error", str(e)[:200])
    try:
        cards += FinanceProvider().cards(con)
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "live_finance", "error", str(e)[:200])

    # Replace the whole set atomically: these are ephemeral, always-current cards.
    con.execute("DELETE FROM live_cards")
    for c in cards:
        con.execute(
            "INSERT INTO live_cards (id,type,priority,title,subtitle,detail,story_id,"
            "url,payload,starts_at,ends_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["type"], float(c.get("priority", 0)), c.get("title", ""),
             c.get("subtitle", ""), c.get("detail", ""), c.get("story_id", ""),
             c.get("url", ""), db.j(c.get("payload", {})), now, 0, now))
    con.commit()
    db.log_run(con, "refresh_live", "ok", f"{len(cards)} live cards")
    return len(cards)


def _row_to_card(r):
    return {"id": r["id"], "type": r["type"], "priority": r["priority"],
            "title": r["title"], "subtitle": r["subtitle"], "detail": r["detail"],
            "story_id": r["story_id"] or None, "url": r["url"] or None,
            "payload": db.uj(r["payload"], {}), "updated_at": r["updated_at"]}


def snapshot(con, categories=None, limit=12):
    """Ordered live cards, optionally filtered to the requested categories.
    `categories` is an iterable of user-facing names (breaking/sports/finance/events)."""
    types = None
    if categories:
        types = set()
        for c in categories:
            types |= CATEGORY_TYPES.get(c, set())
    rows = con.execute("SELECT * FROM live_cards").fetchall()
    cards = [_row_to_card(r) for r in rows if types is None or r["type"] in types]
    cards.sort(key=lambda c: (_TYPE_RANK.get(c["type"], 9), -c["priority"]))
    return cards[:limit]


def latest_story_marker(con):
    """(count, newest_id) over the last 7 days — the cheap 'is the feed newer?'
    signal pushed as a `feed-updated` SSE event so clients can show a banner."""
    row = con.execute(
        "SELECT COUNT(*) c, MAX(created_at) m FROM stories WHERE created_at > ?",
        (db.now() - 7 * 86400,)).fetchone()
    newest = con.execute(
        "SELECT id FROM stories WHERE created_at > ? ORDER BY created_at DESC LIMIT 1",
        (db.now() - 7 * 86400,)).fetchone()
    return {"count": row["c"] or 0, "newest_id": newest["id"] if newest else "",
            "at": row["m"] or 0}


# ------------------------------------------------------------------ SSE
async def sse_event_stream(categories=None, interval=10, max_seconds=600):
    """Async generator yielding SSE frames. Diffs the snapshot each `interval`s and
    emits only on change; also emits a `feed-updated` event when new stories land.
    Blocking sqlite reads run in a threadpool so the event loop never stalls.

    Client contract: two named events —
      event: live    data: {"cards":[...]}          (hero cards, on change)
      event: feed    data: {"count":N,"newest_id":…} (feed freshness, on change)
    plus `: heartbeat` comments to keep the connection alive."""
    from fastapi.concurrency import run_in_threadpool

    def _read():
        con = db.connect()
        try:
            return snapshot(con, categories), latest_story_marker(con)
        finally:
            con.close()

    start = time.time()
    last_cards = last_feed = None
    # Emit current state immediately so a fresh connection paints without waiting.
    first = True
    while time.time() - start < max_seconds:
        try:
            cards, feed = await run_in_threadpool(_read)
        except Exception:  # noqa: BLE001 — transient DB hiccup; try next tick
            yield ": heartbeat\n\n"
            await asyncio.sleep(interval)
            continue
        cards_json = json.dumps({"cards": cards}, ensure_ascii=False)
        if first or cards_json != last_cards:
            last_cards = cards_json
            yield f"event: live\ndata: {cards_json}\n\n"
        feed_json = json.dumps(feed, ensure_ascii=False)
        if first or feed_json != last_feed:
            last_feed = feed_json
            yield f"event: feed\ndata: {feed_json}\n\n"
        else:
            yield ": heartbeat\n\n"
        first = False
        await asyncio.sleep(interval)
