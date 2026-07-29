"""FastAPI app: the API the iOS client talks to."""
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
import html
import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (StreamingResponse, HTMLResponse, PlainTextResponse,
                               Response)
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from . import config, db, llm, live, analytics, ranking
from .agents import prompt, _dedupe_trends, linkify, story_refs
from .orchestrator import run_pipeline, STAGES

app = FastAPI(title="Descry API", version="0.1")
# Browser origin allowlist — ALLOWED_ORIGINS env, default * for the beta portal.
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])
scheduler = BackgroundScheduler()


@app.middleware("http")
async def _no_index_api(request: Request, call_next):
    """Keep the API host out of search results without blocking it.

    robots.txt here has to stay permissive — Google's renderer fetches this host
    to see any article text on the portal at all. `noindex` as a header does the
    other half of the job: crawl it freely, never rank it. Two exemptions:
    /sitemap.xml (Google refuses to read a noindex sitemap) and /robots.txt."""
    response = await call_next(request)
    if request.url.path not in ("/sitemap.xml", "/robots.txt"):
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.middleware("http")
async def _count_traffic(request: Request, call_next):
    """Overall traffic: one counter bump per request. Wrapped so analytics can
    never turn into a 500 — a failed count is worth less than a served page."""
    response = await call_next(request)
    try:
        path = request.url.path
        if analytics.is_trackable(path):
            # Route template ("/story/{story_id}"), not the concrete path, so ids
            # don't explode the table into one row per story per day.
            route = request.scope.get("route")
            con = db.connect()
            analytics.bump_traffic(con, getattr(route, "path", None) or path)
            con.close()
    except Exception:
        pass
    return response


def _require_admin(authorization: str = "", token: str = ""):
    """Gate for /admin/*: the API is public, so admin actions (pipeline runs,
    intel wipes, usage internals) need ADMIN_TOKEN — via Authorization: Bearer
    or ?token= for curl convenience. No token configured = admin disabled."""
    if not config.ADMIN_TOKEN:
        raise HTTPException(403, "admin endpoints are disabled — set ADMIN_TOKEN "
                                 "in the environment to enable them")
    supplied = token or authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(401, "bad admin token")

# One pipeline at a time — shared guard for scheduled AND manual runs.
_pipeline_lock = threading.Lock()


def guarded_run(stage: str | None = None):
    if not _pipeline_lock.acquire(blocking=False):
        return None  # a run is already in progress; skip
    try:
        return run_pipeline(stage)
    finally:
        _pipeline_lock.release()


@app.on_event("startup")
def _start():
    # Interval jobs otherwise fire first at startup+interval; with frequent redeploys
    # that clock keeps resetting and a run may never happen. Kick the first run ~2 min
    # after boot, then every interval. coalesce + a wide misfire grace mean a busy or
    # skipped window collapses to a single catch-up run rather than being dropped.
    scheduler.add_job(guarded_run, "interval",
                      hours=config.PIPELINE_INTERVAL_HOURS,
                      id="pipeline", replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=2),
                      coalesce=True, misfire_grace_time=3600, max_instances=1)
    # Fast, lightweight live refresh (breaking sweep + sports/finance), separate
    # from the 3h story pipeline. First run ~20s after boot so /live isn't empty.
    scheduler.add_job(_refresh_live_job, "interval",
                      minutes=config.LIVE_REFRESH_MINUTES,
                      id="refresh_live", replace_existing=True,
                      next_run_time=datetime.now() + timedelta(seconds=20),
                      coalesce=True, misfire_grace_time=300, max_instances=1)
    # Analytics retention. Daily is plenty — the window is measured in months.
    scheduler.add_job(_purge_analytics_job, "interval", hours=24,
                      id="purge_analytics", replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=5),
                      coalesce=True, misfire_grace_time=3600, max_instances=1)
    scheduler.start()


def _purge_analytics_job():
    con = db.connect()
    try:
        n = analytics.purge(con)
        if n:
            db.log_run(con, "purge_analytics", "ok",
                       f"deleted {n} visit rows older than "
                       f"{config.ANALYTICS_RETAIN_DAYS:g} days")
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "purge_analytics", "error", str(e)[:300])
    finally:
        con.close()


def _refresh_live_job():
    con = db.connect()
    try:
        live.refresh_live(con)
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "refresh_live", "error", str(e)[:300])
    finally:
        con.close()


class ContextIn(BaseModel):
    interests: list[str] = []
    profession: str = ""
    line_of_business: str = ""
    role_seniority: str = ""
    location: dict = {}
    native_language: str = ""
    preferred_language: str = "English"
    micro: dict = {}
    # Dynamic-hero config (open bag — no migration): which categories show, order,
    # master on/off, followed sports. Read by /live and /live/stream.
    live_prefs: dict = {}
    # UI theme — "default" | "journal" | "signal". A personalization like anything
    # else in this bag: signed-in only, synced across devices. The client never
    # needs the server to interpret this value, only to hold and return it.
    theme: str = "default"


def _auth(con, user_id, authorization):
    row = con.execute("SELECT token FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "user not found")
    if authorization != f"Bearer {row['token']}":
        raise HTTPException(401, "bad token")


def _is_authed(con, user_id, authorization):
    """True when the caller proved they own `user_id`. Unlike _auth this never
    raises — it answers "may this caller see signed-in-only content?" so an
    anonymous request still gets a normal response, just without the premium
    payload. Gating in the client alone only blurs pixels; the withheld fields
    have to actually not be sent."""
    if not user_id or not authorization:
        return False
    row = con.execute("SELECT token FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row) and secrets.compare_digest(authorization, f"Bearer {row['token']}")


class GoogleAuthIn(BaseModel):
    id_token: str


@app.post("/auth/google")
def auth_google(body: GoogleAuthIn):
    """Sign in with Google. Verifies the ID token against Google's tokeninfo
    endpoint, then finds-or-creates the user. Returns credentials plus whether
    the user already has a saved context (drives the app's personalize flow)."""
    try:
        r = httpx.get("https://oauth2.googleapis.com/tokeninfo",
                      params={"id_token": body.id_token}, timeout=15)
    except httpx.HTTPError:
        raise HTTPException(503, "could not reach Google to verify the token")
    if r.status_code != 200:
        raise HTTPException(401, "invalid Google token")
    info = r.json()
    # Allowlist may hold several client IDs (e.g. the iOS app AND the web portal
    # use different OAuth clients). Comma-separate them in GOOGLE_CLIENT_ID.
    allowed = {c.strip() for c in os.environ.get("GOOGLE_CLIENT_ID", "").split(",")
               if c.strip()}
    if allowed and info.get("aud") not in allowed:
        raise HTTPException(401, "token was issued for a different app")
    sub = info["sub"]
    email = info.get("email", "")
    name = info.get("name", "")
    picture = info.get("picture", "")
    con = db.connect()
    row = con.execute("SELECT id, token, context FROM users WHERE google_sub=?",
                      (sub,)).fetchone()
    if row:
        uid, token, ctx = row["id"], row["token"], row["context"]
        con.execute("UPDATE users SET email=?, name=?, picture=? WHERE id=?",
                    (email, name, picture, uid))
    else:
        uid, token, ctx = db.new_id(), secrets.token_hex(16), "{}"
        con.execute(
            "INSERT INTO users (id, token, context, created_at, google_sub, email, "
            "name, picture) VALUES (?,?,?,?,?,?,?,?)",
            (uid, token, "{}", db.now(), sub, email, name, picture))
    con.commit(); con.close()
    return {"user_id": uid, "token": token, "name": name, "email": email,
            "picture": picture, "has_context": ctx not in ("", "{}", None)}


@app.get("/users/{user_id}/profile")
def get_profile(user_id: str, authorization: str = Header("")):
    """Account details for the Profile screen. Lets the app refresh name,
    email and photo without a fresh Google sign-in."""
    con = db.connect()
    _auth(con, user_id, authorization)
    row = con.execute("SELECT name, email, picture, context FROM users WHERE id=?",
                      (user_id,)).fetchone()
    con.close()
    return {"name": row["name"] or "", "email": row["email"] or "",
            "picture": row["picture"] or "",
            "has_context": (row["context"] or "{}") not in ("", "{}")}


@app.post("/bookmarks")
def add_bookmark(user_id: str, story_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("INSERT OR IGNORE INTO bookmarks VALUES (?,?,?,?)",
                (db.new_id(), user_id, story_id, db.now()))
    con.commit(); con.close()
    return {"ok": True}


@app.delete("/bookmarks")
def remove_bookmark(user_id: str, story_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("DELETE FROM bookmarks WHERE user_id=? AND story_id=?",
                (user_id, story_id))
    con.commit(); con.close()
    return {"ok": True}


@app.get("/bookmarks")
def list_bookmarks(user_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    rows = con.execute(
        """SELECT s.id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic, '' AS impact_text, 0 AS impact_score
           FROM bookmarks b JOIN stories s ON s.id = b.story_id
           WHERE b.user_id = ? ORDER BY b.created_at DESC""",
        (user_id,)).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/users")
def create_user():
    con = db.connect()
    uid, token = db.new_id(), secrets.token_hex(16)
    con.execute("INSERT INTO users (id, token, context, created_at) VALUES (?,?,?,?)",
                (uid, token, "{}", db.now()))
    con.commit(); con.close()
    return {"user_id": uid, "token": token}


@app.get("/users/{user_id}/context")
def get_context(user_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    row = con.execute("SELECT context FROM users WHERE id=?", (user_id,)).fetchone()
    con.close()
    return db.uj(row["context"] if row else "{}")


@app.put("/users/{user_id}/context")
def put_context(user_id: str, ctx: ContextIn, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("UPDATE users SET context=? WHERE id=?",
                (ctx.model_dump_json(), user_id))
    con.commit(); con.close()
    return {"ok": True}


@app.get("/feed")
def feed(user_id: str, sort: str = "recent", since: float = 0.0,
         authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    # LEFT JOIN: every recent story appears in the feed; personalization
    # (impact text/score) enriches stories where the Personalizer has run,
    # but never gates visibility.
    # `since` (epoch) returns only stories newer than the client's newest — the
    # cheap incremental fetch behind the "N new stories" banner. created_at is
    # exposed so the client can diff/merge without a second call.
    floor = max(since, db.now() - 7 * 86400)
    # Candidate pool capped generously above realistic 7-day volume (a few
    # hundred at most — MAX_STORIES_PER_RUN * runs/week), so ranking still sees
    # every real candidate and can blend recency with impact (see module
    # docstring), while a future bug can't turn this into an unbounded scan the
    # way the un-capped /trends and /signals queries just did in production.
    rows = con.execute(
        """SELECT s.id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic, s.created_at, s.article_ids, s.trend_ids,
                  COALESCE(f.impact_text, '')  AS impact_text,
                  COALESCE(f.impact_score, 0)  AS impact_score
           FROM stories s
           LEFT JOIN feed_items f ON f.story_id = s.id AND f.user_id = ?
           WHERE s.created_at > ?
           ORDER BY s.created_at DESC LIMIT 1000""",
        (user_id, floor)).fetchall()
    con.close()
    items = [dict(r) for r in rows]
    # Impact metric: the personalized 0-3 relevance score for sort=foryou (that
    # IS this user's impact signal); otherwise how many outlets corroborate the
    # story — the same "how big is this" proxy trends already use (velocity).
    if sort == "foryou":
        impact_of = lambda it: it["impact_score"]
    else:
        impact_of = lambda it: len(db.uj(it["article_ids"], []))
    items = ranking.sort_by_rank(items, impact_of)[:100]
    for it in items:
        del it["article_ids"]   # internal-only, not part of the API shape
        # trend_ids IS part of the shape: the portal's story network draws the
        # story -> force edges from it, and without them it can only guess.
        it["trend_ids"] = db.uj(it["trend_ids"], [])
    return {"items": items}


@app.get("/story/{story_id}")
def story(story_id: str, user_id: str = "", authorization: str = Header("")):
    con = db.connect()
    if user_id:
        _auth(con, user_id, authorization)
    s = con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not s:
        con.close()
        raise HTTPException(404, "story not found")
    art_ids = db.uj(s["article_ids"], [])
    articles = [dict(con.execute(
        "SELECT title,url,source FROM articles WHERE id=?", (i,)).fetchone() or {})
        for i in art_ids]
    trends = [dict(r) for r in con.execute(
        "SELECT id,kind,name,narrative,velocity FROM trends "
        "WHERE retired_at IS NULL AND id IN (%s)" %
        ",".join("?" * len(db.uj(s["trend_ids"], []))),
        db.uj(s["trend_ids"], [])).fetchall()] if db.uj(s["trend_ids"], []) else []
    # Hidden connections are a signed-in feature. Guests still learn that N
    # connections exist and to which stories (enough for the client to show a
    # real, honest teaser) but the CHAIN — the actual inference, which is the
    # thing worth signing up for — is never put on the wire.
    authed = _is_authed(con, user_id, authorization)
    conns = []
    for cid in db.uj(s["connection_ids"], []):
        c = con.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if c:
            other = c["article_b"] if c["article_a"] in art_ids else c["article_a"]
            oa = con.execute("SELECT title,url FROM articles WHERE id=?", (other,)).fetchone()
            conns.append({"chain": c["chain"] if authed else "",
                          "confidence": c["confidence"] if authed else None,
                          "locked": not authed,
                          "other_title": oa["title"] if oa else "",
                          "other_url": (oa["url"] if oa else "") if authed else ""})
    fi = con.execute("SELECT impact_text,impact_score FROM feed_items "
                     "WHERE user_id=? AND story_id=?", (user_id, story_id)).fetchone()
    con.close()
    return {"id": s["id"], "headline": s["headline"], "narrative": s["narrative"],
            # Separate field, not a paragraph split of `narrative` — see the
            # Storyteller. Empty on stories written before the split existed;
            # clients fall back to the old behaviour when it is.
            "why_matters": s["why_matters"] or "",
            "credibility": s["credibility"], "credibility_note": s["credibility_note"],
            "claims": db.uj(s["claims"]), "topic": s["topic"], "sources": articles,
            "trends": trends, "connections": conns, "created_at": s["created_at"],
            "impact_text": fi["impact_text"] if fi else "",
            "impact_score": fi["impact_score"] if fi else 0}


@app.post("/feedback")
def feedback(user_id: str, story_id: str, action: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("INSERT INTO feedback VALUES (?,?,?,?,?)",
                (db.new_id(), user_id, story_id, action, db.now()))
    con.commit(); con.close()
    return {"ok": True}


@app.get("/stories")
def stories(limit: int = 30):
    """Public recent stories — powers the portal for anonymous visitors (30).
    Signed-in clients (web + iOS) use /feed instead, which returns up to 100."""
    con = db.connect()
    # Candidate pool generously wider than `limit` so ranking (recency blended
    # with source-count impact) has real older-but-bigger stories to consider,
    # not just whatever the last `limit` by raw created_at happened to include.
    rows = con.execute(
        "SELECT id, headline, narrative, credibility, credibility_note, topic, "
        "created_at, article_ids, trend_ids FROM stories ORDER BY created_at DESC LIMIT ?",
        (max(limit * 10, 300),)).fetchall()
    con.close()
    items = [dict(r) for r in rows]
    items = ranking.sort_by_rank(
        items, impact_of=lambda it: len(db.uj(it["article_ids"], [])))[:limit]
    for it in items:
        del it["article_ids"]
        it["trend_ids"] = db.uj(it["trend_ids"], [])   # powers the story network
    return {"items": items}


class TrackIn(BaseModel):
    device: str = ""       # random first-party id minted by the client
    path: str = "/"        # in-app route being viewed, e.g. "/story/abc123"
    ref: str = ""          # document.referrer; only its HOST is stored
    user_id: str = ""      # present once signed in, so reports can join users


@app.post("/track")
def track(body: TrackIn, request: Request):
    """Record one page view. Unauthenticated by design — it is called before a
    visitor has any identity, and it accepts nothing that identifies a person:
    the id is client-minted, the referrer is reduced to a host, and the country
    comes from the edge header while the IP itself is never stored."""
    if not body.device or not analytics.is_trackable(body.path or "/"):
        return {"ok": False}
    con = db.connect()
    ok = analytics.record_visit(
        con, device=body.device, path=body.path, referrer=body.ref,
        country=analytics.country_of(request.headers), user_id=body.user_id)
    con.close()
    return {"ok": ok}


@app.get("/trends")
def trends():
    # Ranked per kind: macro velocity is an article count while micro velocity is
    # a small ratio, so one mixed velocity sort would push every micro trend
    # (the portal's "Early signals" tab) out of a shared limit. The candidate
    # cap (500) is generous relative to the live-trend count reconciliation
    # actually maintains — a circuit breaker, not a normal-path constraint — so
    # ranking still sees every real candidate before slicing to 40/20; see
    # backend/app/ranking.py.
    con = db.connect()
    # retired_at IS NULL: trends the newest run no longer sees leave the radar,
    # but their rows (and links) survive — see _reconcile_trends. Indexed
    # (trends_kind_retired) so this stays fast as retired rows accumulate over
    # their TREND_RETIRE_PURGE_DAYS retention window instead of being deleted.
    q = ("SELECT id, kind, name, narrative, sectors, regions, article_ids, velocity, "
         "created_at FROM trends WHERE kind=? AND retired_at IS NULL "
         "ORDER BY created_at DESC LIMIT 500")
    macro = ranking.sort_by_rank(
        [dict(r) for r in con.execute(q, ("macro",)).fetchall()],
        impact_of=lambda t: t["velocity"] or 0.0)[:40]
    micro = ranking.sort_by_rank(
        [dict(r) for r in con.execute(q, ("micro",)).fetchall()],
        impact_of=lambda t: t["velocity"] or 0.0)[:20]
    con.close()
    out = []
    for d in macro + micro:
        d["sectors"] = db.uj(d["sectors"], [])
        d["regions"] = db.uj(d["regions"], [])
        d["article_count"] = len(db.uj(d["article_ids"], []))
        del d["article_ids"]
        out.append(d)
    return {"items": out}


def _fetch_stories_by_id(con, ids):
    """Batch story lookup: one query for however many ids are needed, instead of
    one query per id. /signals used to call this once PER STORY PER SIGNAL —
    harmless for a single forecast (/signal/{id}) but a real N+1 for the list."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = con.execute(
        "SELECT id, headline, narrative, credibility, credibility_note, topic, "
        f"created_at FROM stories WHERE id IN ({','.join('?' * len(ids))})",
        ids).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _shape_signal(g, story_of, authed=True):
    """Shape one forecast row for the API from a pre-fetched {id: story} lookup.
    Shared by /signals and /signal/{id} so a deep-linked forecast is
    byte-for-byte what the list would have shown.

    Forecasts are a signed-in feature. Guests get the hook — the title, the
    prediction itself, its confidence and how many stories back it — because
    that is what makes signing up worth it. The reasoning (`chain`), the
    watch-list and the supporting stories are withheld server-side, not merely
    hidden in the UI. `story_count` replaces the story list so the client can
    still say "built from 5 stories" without receiving them."""
    stories, id_head = [], {}
    for sid in db.uj(g["story_ids"], []):
        s = story_of.get(sid)
        if s:
            stories.append(s)
            id_head[sid] = s["headline"]
    out = {"id": g["id"],
           "title": linkify(g["title"], id_head),
           "prediction": linkify(g["prediction"], id_head),
           "affected": db.uj(g["affected"], []), "horizon": g["horizon"],
           "confidence": g["confidence"], "created_at": g["created_at"],
           "story_count": len(stories), "locked": not authed}
    if authed:
        out.update({"chain": linkify(g["chain"], id_head),
                    "watch": linkify(g["watch"], id_head),
                    "stories": stories, "story_refs": story_refs(id_head)})
    else:
        out.update({"chain": "", "watch": "", "stories": [], "story_refs": []})
    return out


def _signal_payload(con, g, authed=True):
    """Single-forecast shape (/signal/{id}) — only ever needs one batch lookup."""
    return _shape_signal(g, _fetch_stories_by_id(con, db.uj(g["story_ids"], [])),
                         authed=authed)


@app.get("/signals")
def signals(user_id: str = "", authorization: str = Header("")):
    """Foresight signals: cross-domain predictions with their supporting stories.
    Raw 12-hex story ids the model cited inline are rewritten to the story's
    headline (linkify), and story_refs lets clients make those spans tappable.
    Anonymous callers get the hook only — see _shape_signal."""
    con = db.connect()
    authed = _is_authed(con, user_id, authorization)
    # Defensive cap, same reasoning as /trends: Foresight already prunes this
    # table to WINDOW_DAYS, so 200 is generous headroom, not a normal-path limit.
    rows = ranking.sort_by_rank(
        [dict(r) for r in con.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT 200").fetchall()],
        impact_of=lambda g: g["confidence"] or 0.0)
    # One batched lookup for every story any signal in this list references,
    # instead of a query per (signal, story) pair.
    story_of = _fetch_stories_by_id(
        con, (sid for g in rows for sid in db.uj(g["story_ids"], [])))
    out = [_shape_signal(g, story_of, authed=authed) for g in rows]
    con.close()
    return {"items": out}


DEFAULT_LIVE_CATEGORIES = ["breaking", "sports", "finance", "events"]


def _user_live_categories(con, user_id, override):
    """Resolve which hero categories to serve: explicit ?categories= wins, else the
    user's saved live_prefs, else all. Returns None when the section is disabled."""
    if override:
        return [c.strip() for c in override.split(",") if c.strip()]
    if user_id:
        row = con.execute("SELECT context FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            prefs = db.uj(row["context"]).get("live_prefs", {}) or {}
            if prefs.get("enabled") is False:
                return None
            cats = prefs.get("categories")
            if cats:
                return [c for c in cats if c in DEFAULT_LIVE_CATEGORIES]
    return list(DEFAULT_LIVE_CATEGORIES)


@app.get("/live")
def live_snapshot(user_id: str = "", categories: str = ""):
    """Snapshot of the dynamic-hero cards — first paint and SSE fallback. Filtered
    to the user's configured categories (or ?categories= override)."""
    con = db.connect()
    cats = _user_live_categories(con, user_id, categories)
    items = [] if cats is None else live.snapshot(con, cats)
    con.close()
    return {"items": items, "enabled": cats is not None}


@app.get("/live/stream")
def live_stream(user_id: str = "", categories: str = ""):
    """Server-Sent Events: pushes hero-card changes (`event: live`) and feed
    freshness (`event: feed`) with heartbeats. Falls back to /live if unreachable."""
    con = db.connect()
    cats = _user_live_categories(con, user_id, categories)
    con.close()
    gen = live.sse_event_stream(cats)
    return StreamingResponse(
        gen, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"})  # disable proxy buffering for SSE


@app.get("/trend/{trend_id}")
def trend_detail(trend_id: str):
    """A trend plus the stories built from its member articles — powers deep-dive."""
    con = db.connect()
    t = con.execute("SELECT * FROM trends WHERE id=?", (trend_id,)).fetchone()
    if not t:
        con.close()
        raise HTTPException(404, "trend not found")
    member_ids = set(db.uj(t["article_ids"], []))
    stories, id_head = [], {}
    for s in con.execute(
            "SELECT id, headline, narrative, credibility, credibility_note, topic, "
            "article_ids FROM stories ORDER BY created_at DESC LIMIT 200").fetchall():
        if member_ids & set(db.uj(s["article_ids"], [])):
            d = dict(s)
            del d["article_ids"]
            stories.append(d)
            id_head[s["id"]] = s["headline"]
    con.close()
    # retired_at is returned (not hidden) so the client can say plainly that the
    # trend is no longer active instead of pretending it is current.
    return {"id": t["id"], "kind": t["kind"], "name": t["name"],
            "narrative": linkify(t["narrative"], id_head),
            "sectors": db.uj(t["sectors"], []),
            "regions": db.uj(t["regions"], []), "velocity": t["velocity"],
            "created_at": t["created_at"], "retired_at": t["retired_at"],
            "stories": stories, "story_refs": story_refs(id_head)}


@app.get("/signal/{signal_id}")
def signal_detail(signal_id: str, user_id: str = "", authorization: str = Header("")):
    """One forecast by id. Without this a shared forecast link could only be
    resolved from whatever /signals happened to return, so any prediction that
    had dropped out of that list read as deleted while its row still existed."""
    con = db.connect()
    g = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not g:
        con.close()
        raise HTTPException(404, "signal not found")
    out = _signal_payload(con, g, authed=_is_authed(con, user_id, authorization))
    con.close()
    return out


@app.get("/search")
def search(q: str):
    """Simple LIKE search over stories and trends. Upgrade path: embeddings."""
    like = f"%{q.strip()}%"
    con = db.connect()
    # why_matters is searched too: the implications half of a story is now its
    # own column, so leaving it out would quietly shrink what search can find.
    story_rows = con.execute(
        "SELECT id, headline, narrative, credibility, topic FROM stories "
        "WHERE headline LIKE ? OR narrative LIKE ? OR topic LIKE ? "
        "OR COALESCE(why_matters,'') LIKE ? "
        "ORDER BY created_at DESC LIMIT 15", (like, like, like, like)).fetchall()
    trend_rows = con.execute(
        "SELECT id, kind, name, narrative, velocity FROM trends "
        "WHERE (name LIKE ? OR narrative LIKE ?) AND retired_at IS NULL "
        "ORDER BY created_at DESC LIMIT 10", (like, like)).fetchall()
    con.close()
    return {"stories": [dict(r) for r in story_rows],
            "trends": [dict(r) for r in trend_rows]}


# --------------------------------------------- Shareable previews / SEO (OG)
def _clip(text, n=180):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _og_page(title, description, web_path):
    """The share card behind a social unfurl. Facebook, X, Slack, WhatsApp and
    friends read OG tags but do not run JS, so this has to be server-rendered.

    It is explicitly NOT the indexable page. `noindex` plus a canonical pointing
    at the real article on the web domain means it never competes with the page
    it advertises — two URLs serving the same headline is how a site ends up
    ranking its own doorway instead of its content. Everything is HTML-escaped."""
    t, d = html.escape(title or "Descry"), html.escape(description or "")
    canonical = f"{config.WEB_BASE_URL}/{web_path}"
    can = html.escape(canonical)
    img = html.escape(config.OG_IMAGE_URL)
    img_tags = (f'<meta property="og:image" content="{img}">'
                f'<meta name="twitter:image" content="{img}">') if config.OG_IMAGE_URL else ""
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t} · Descry</title>
<meta name="description" content="{d}">
<meta name="robots" content="noindex,follow">
<link rel="canonical" href="{can}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Descry">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:url" content="{can}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">{img_tags}
<script>location.replace("{can}");</script>
</head><body style="font-family:system-ui;background:#070B14;color:#E8ECF4;padding:40px">
<h1>{t}</h1><p>{d}</p><p><a href="{can}" style="color:#4D9FFF">Read on Descry →</a></p>
</body></html>""")


@app.get("/s/{story_id}", response_class=HTMLResponse)
def og_story(story_id: str):
    con = db.connect()
    s = con.execute("SELECT headline, narrative FROM stories WHERE id=?",
                    (story_id,)).fetchone()
    con.close()
    if not s:
        return HTMLResponse(f'<script>location.replace("{config.WEB_BASE_URL}/")</script>')
    return _og_page(s["headline"], _clip(s["narrative"]), f"story/{story_id}")


@app.get("/t/{trend_id}", response_class=HTMLResponse)
def og_trend(trend_id: str):
    con = db.connect()
    t = con.execute("SELECT name, narrative FROM trends WHERE id=?", (trend_id,)).fetchone()
    con.close()
    if not t:
        return HTMLResponse(f'<script>location.replace("{config.WEB_BASE_URL}/")</script>')
    return _og_page(t["name"], _clip(t["narrative"]), f"trend/{trend_id}")


@app.get("/g/{signal_id}", response_class=HTMLResponse)
def og_signal(signal_id: str):
    con = db.connect()
    g = con.execute("SELECT title, prediction FROM signals WHERE id=?",
                    (signal_id,)).fetchone()
    con.close()
    if not g:
        return HTMLResponse(f'<script>location.replace("{config.WEB_BASE_URL}/")</script>')
    return _og_page(g["title"], _clip(g["prediction"]), f"signal/{signal_id}")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    """The API's own robots — deliberately permissive, which is not the same as
    wanting this host indexed.

    Blocking it would break the site. The portal is client-rendered: Google's
    renderer fetches /feed, /story/{id} and friends from HERE to see any article
    text at all, and it honours robots.txt on those fetches — a Disallow would
    hand the crawler a set of empty pages. It would also hide the `noindex` on
    the /s /t /g share cards, since a blocked page's meta tags are never read.
    Keeping this host OUT of the index is the X-Robots-Tag header's job (see
    _no_index_api), not robots.txt's. The site's own robots.txt is web/robots.txt."""
    base = str(request.base_url).rstrip("/")     # Sitemap: must be absolute
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap():
    """URLs on the WEB domain, not this one. A sitemap exists to tell Google
    which pages to index; pointing it at the API host got the bridge pages
    crawled and left the actual article pages undiscovered. Served from
    www.descry.in via a rewrite (see render.yaml), which is also what makes
    listing www.descry.in URLs here legitimate.

    lastmod is the real story timestamp — for a news site it is the difference
    between "recrawl this, it changed" and "we'll get to it"."""
    base = config.WEB_BASE_URL
    con = db.connect()
    rows = [(f"{base}/story/{r['id']}", r["created_at"], "hourly", "0.9")
            for r in con.execute("SELECT id, created_at FROM stories "
                                 "ORDER BY created_at DESC LIMIT 2000").fetchall()]
    rows += [(f"{base}/trend/{r['id']}", r["created_at"], "daily", "0.7")
             for r in con.execute("SELECT id, created_at FROM trends "
                                  "WHERE retired_at IS NULL "
                                  "ORDER BY created_at DESC LIMIT 500").fetchall()]
    rows += [(f"{base}/signal/{r['id']}", r["created_at"], "daily", "0.6")
             for r in con.execute("SELECT id, created_at FROM signals "
                                  "ORDER BY created_at DESC LIMIT 500").fetchall()]
    con.close()
    # The two hubs, first and highest priority: they are the entry points that
    # link onward to everything else.
    items = [f'<url><loc>{base}/</loc><changefreq>hourly</changefreq>'
             f'<priority>1.0</priority></url>',
             f'<url><loc>{base}/trends</loc><changefreq>hourly</changefreq>'
             f'<priority>0.8</priority></url>']
    for loc, created, freq, prio in rows:
        stamp = ""
        if created:
            stamp = ("<lastmod>" + time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(created)) + "</lastmod>")
        items.append(f"<url><loc>{html.escape(loc)}</loc>{stamp}"
                     f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(items) + '</urlset>')
    return Response(xml, media_type="application/xml")


class AskIn(BaseModel):
    question: str
    story_id: str = ""
    user_id: str = ""


@app.post("/ask")
def ask(body: AskIn, authorization: str = Header("")):
    """Ask-AI: question about a story (or general). Mock mode gives canned answers.
    Passing a user_id requires that user's bearer token — a user's saved context
    must never be injectable into a prompt by whoever guesses their id."""
    con = db.connect()
    if body.user_id:
        _auth(con, body.user_id, authorization)
    story_ctx, user_ctx = "", "{}"
    if body.story_id:
        s = con.execute("SELECT headline, narrative, claims FROM stories WHERE id=?",
                        (body.story_id,)).fetchone()
        if s:
            story_ctx = f"Headline: {s['headline']}\nStory: {s['narrative']}\nClaims: {s['claims']}"
    if body.user_id:
        u = con.execute("SELECT context FROM users WHERE id=?", (body.user_id,)).fetchone()
        if u:
            user_ctx = u["context"]
    con.close()
    out = llm.complete_json("ask", prompt("ask", question=body.question,
                                          story=story_ctx or "(no specific story)",
                                          context=user_ctx))
    if out is None:
        return {"answer": "The assistant is rate-limited right now — please try again "
                          "in a minute.", "followups": []}
    return {"answer": out.get("answer", ""),
            "followups": out.get("followups", [])}


@app.get("/admin", response_class=HTMLResponse)
def admin_console():
    """Serve the admin dashboard (static HTML). The page itself is harmless without
    the ADMIN_TOKEN — every action it performs goes through the token-gated /admin/*
    APIs, so the real protection lives there, not on this page."""
    try:
        return HTMLResponse(config.ADMIN_PAGE.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "admin page not found")


@app.post("/admin/run")
def admin_run(stage: str = "", token: str = "", authorization: str = Header("")):
    """Kick off a pipeline run in the background and return immediately.
    Optional ?stage= runs a single stage only, e.g.:
      /admin/run?stage=trends     macro + micro trends (one per-topic pass)
      /admin/run?stage=signals    just Foresight predictions
    Poll /admin/usage for completion."""
    _require_admin(authorization, token)
    if stage and stage not in STAGES:
        raise HTTPException(400, f"unknown stage '{stage}'; valid: {STAGES}")
    if _pipeline_lock.locked():
        return {"started": False, "status": "a pipeline run is already in progress",
                "check": "GET /admin/usage — look for stage='pipeline', status='done'"}
    threading.Thread(target=guarded_run, args=(stage or None,), daemon=True).start()
    return {"started": True, "stage": stage or "all",
            "status": "running in background",
            "check": "GET /admin/usage — a new recent_runs row with "
                     "stage='pipeline', status='done' marks completion"}


def _rebuild_intel():
    """Wipe ALL trends + forecasts and recompute them from scratch, then re-link
    existing stories to the fresh trends by article overlap. Runs in the
    background under the pipeline lock. Uses whatever provider/reasoning models
    are configured (see REASONING_TASKS)."""
    from .agents import TrendLinker, Foresight, PROMPTS
    con = db.connect()
    try:
        # Guard: never wipe if the deployed prompts.yaml is out of sync with the code
        # (a stale file is missing these keys and would crash mid-rebuild after the wipe).
        missing = [k for k in ("trend", "signals_unit", "signals") if k not in PROMPTS]
        if missing:
            db.log_run(con, "rebuild_intel", "error",
                       f"prompts.yaml missing {missing} — redeploy; nothing deleted")
            con.close()
            return
        con.execute("DELETE FROM trends")
        con.execute("DELETE FROM signals")
        con.commit()
        TrendLinker().run(con)   # per-unit: macro + micro trends, 1 call per topic
        Foresight().run(con)     # per-unit forecasts + 1 cross-domain pass
        # Stories kept their old trend_ids (now stale) — relink to fresh trends.
        macro = [(t["id"], set(db.uj(t["article_ids"], [])))
                 for t in con.execute(
                     "SELECT id, article_ids FROM trends WHERE kind='macro'").fetchall()]
        for s in con.execute("SELECT id, article_ids FROM stories").fetchall():
            aids = set(db.uj(s["article_ids"], []))
            linked = [tid for tid, tids in macro if aids & tids]
            con.execute("UPDATE stories SET trend_ids=? WHERE id=?",
                        (db.j(linked), s["id"]))
        con.commit()
        db.log_run(con, "rebuild_intel", "ok",
                   "wiped and recomputed all trends + forecasts")
    except Exception as e:  # noqa: BLE001
        db.log_run(con, "rebuild_intel", "error", str(e)[:300])
    finally:
        con.close()


@app.post("/admin/rebuild-intel")
def admin_rebuild_intel(allow_mock: bool = False, token: str = "",
                        authorization: str = Header("")):
    """ONE-TIME reset: delete every trend and forecast, then recompute them all
    with the configured reasoning models. Guarded so nothing is deleted unless
    the model actually answers a probe first (avoids wiping then failing).
    Poll /admin/usage for a stage='rebuild_intel' row to see completion."""
    _require_admin(authorization, token)
    if config.LLM_PROVIDER == "mock" and not allow_mock:
        raise HTTPException(
            400, "LLM_PROVIDER=mock — set a real provider (e.g. deepseek) with a "
                 "reasoning model, or pass ?allow_mock=true to rebuild with "
                 "placeholder content.")
    # Preflight: confirm the reasoning path returns valid JSON BEFORE deleting.
    probe = llm.complete_json(
        "trend", 'Two related items: "A rises"; "B follows". Reply ONLY JSON '
                 '{"name":"x","narrative":"y","sectors":[],"regions":[]}')
    if probe is None:
        raise HTTPException(
            503, "reasoning provider unreachable (missing key / rate-limited). "
                 "Nothing was deleted — fix the provider and retry.")
    if _pipeline_lock.locked():
        return {"started": False,
                "status": "a pipeline run is already in progress; retry shortly"}

    def job():
        if not _pipeline_lock.acquire(blocking=False):
            return
        try:
            _rebuild_intel()
        finally:
            _pipeline_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return {"started": True, "provider": config.LLM_PROVIDER,
            "reasoning_tasks": sorted(config.REASONING_TASKS),
            "status": "rebuilding all trends + forecasts in background",
            "check": "GET /admin/usage — look for stage='rebuild_intel', status='ok'"}


@app.post("/admin/dedupe-trends")
def admin_dedupe_trends(token: str = "", authorization: str = Header("")):
    """One-off cleanup of already-accumulated duplicate trends. Collapses
    near-duplicate macro and micro trends in place (same logic the pipeline now
    runs every pass). Returns how many were removed per kind."""
    _require_admin(authorization, token)
    con = db.connect()
    macro = _dedupe_trends(con, "macro")
    micro = _dedupe_trends(con, "micro")
    con.commit()
    db.log_run(con, "dedupe_trends", "ok",
               f"cleanup removed {macro} macro + {micro} micro dupes")
    con.close()
    return {"removed_macro": macro, "removed_micro": micro}


@app.get("/admin/usage")
def admin_usage(token: str = "", authorization: str = Header("")):
    _require_admin(authorization, token)
    con = db.connect()
    runs = [dict(r) for r in con.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT 30").fetchall()]
    # Growth metric: total users and how many completed Google signup (have an email).
    total_users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    google_users = con.execute(
        "SELECT COUNT(*) c FROM users WHERE email IS NOT NULL AND email != ''").fetchone()["c"]
    con.close()
    return {"session_llm_usage": llm.usage,
            "users": {"total": total_users, "google_signed_up": google_users},
            "provider_status": llm.provider_status(),
            "pricing": llm.pricing_status(),
            "provider_events": list(llm.provider_events),
            "recent_errors": list(llm.recent_errors),
            "recent_runs": runs}


def _report_days(days: int) -> int:
    """Clamp a caller-supplied window to something a SQLite scan can serve."""
    return max(1, min(int(days or 30), 365))


@app.get("/admin/reports/users")
def admin_report_users(days: int = 30, limit: int = 200, token: str = "",
                       authorization: str = Header("")):
    """Signed-in users: totals, signup trend, and a per-person activity table."""
    _require_admin(authorization, token)
    con = db.connect()
    out = analytics.user_report(con, days=_report_days(days),
                                limit=max(1, min(int(limit or 200), 1000)))
    con.close()
    return out


@app.get("/admin/reports/visitors")
def admin_report_visitors(days: int = 30, token: str = "",
                          authorization: str = Header("")):
    """Audience: unique devices, new vs returning, top pages, referrers, countries."""
    _require_admin(authorization, token)
    con = db.connect()
    out = analytics.visitor_report(con, days=_report_days(days))
    con.close()
    return out


@app.get("/admin/reports/traffic")
def admin_report_traffic(days: int = 30, token: str = "",
                         authorization: str = Header("")):
    """Overall request volume across every API route, not just page views."""
    _require_admin(authorization, token)
    con = db.connect()
    out = analytics.traffic_report(con, days=_report_days(days))
    con.close()
    return out
