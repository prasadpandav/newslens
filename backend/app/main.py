"""FastAPI app: the API the iOS client talks to."""
import gc
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
from . import (config, db, diag, gazetteer, images, llm, llmcache, llmcost, live,
               analytics, ranking, textmerge)
from .agents import (prompt, _dedupe_trends, linkify, story_refs, Verifier,
                     Personalizer, personalization_relevant, verdict_counts,
                     depth_hint, clean_beats, clean_anchors,
                     detect_correction, Framer, _topic_of)
from . import fulltext
from .orchestrator import run_pipeline, STAGES
from .finance import kg as fin_kg
from .finance import causal as fin_causal
from .finance.orchestrator import (run_finance_pipeline, unresolved_report,
                                   health as finance_health,
                                   STAGES as FIN_STAGES)
from .schemas import api as wire

API_DESCRIPTION = """
Descry reads the same news from many outlets and reports how well each story is
corroborated, rather than asserting it. A second pipeline re-tells the
financially significant subset with extracted figures, actors and relationships.

**Base URL:** `https://newslens-rmv6.onrender.com` — the API is served from this
host only. `descry.in` is the web portal; it answers every unknown path with the
single-page app, so requesting `/openapi.json` there returns HTML.

### Finance stories reach clients two ways

* `GET /feed` merges them into the ordinary feed, carrying `kind: "finance"`
  and their own `topic`, so they land under the existing topic chips alongside
  regular coverage. When both pipelines told the same event, only the finance
  telling is sent.
* `GET /finance/*` exposes them directly, plus the trends, forecasts and
  knowledge graph built on top.

`GET /story/{id}` resolves ids from **both** pipelines, so a feed item opens the
same way whatever produced it.

### Auth

Reader endpoints take `Authorization: Bearer <token>` from `POST /auth/google`,
plus `user_id` as a query parameter. The finance endpoints are unauthenticated.
Signed-out callers get most content; connection chains stay locked.

### Reading the numbers

`credibility` (0-100) is how much corroboration a story has, not how true it is.
The median story sits near 25, so scale any colour band accordingly. Forecast
`disclaimer` text must be displayed wherever the forecast is.
"""

TAGS = [
    {"name": "feed", "description": "The reader's feed and story detail. "
                                    "Finance stories appear here too."},
    {"name": "finance", "description": "Finance-pipeline stories, trends, "
                                       "forecasts, knowledge graph and causal "
                                       "chain simulation."},
    {"name": "admin", "description": "Operational endpoints. Require ADMIN_TOKEN."},
]

app = FastAPI(title="Descry API", version="0.1",
              description=API_DESCRIPTION, openapi_tags=TAGS,
              servers=[{"url": "https://newslens-rmv6.onrender.com",
                        "description": "Production"}])
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


# Request-level memory checkpointing. uvicorn's own access log (what you
# already see: "GET /feed ... 200 OK") says a path returned 200 but nothing
# about how long it took or what the process's memory looked like at the
# time — the exact gap that made a SIGKILL undiagnosable. Only logs the
# outliers (slow, or already running while memory is elevated); logging every
# request would just bury the ones that matter.
_SLOW_REQUEST_SECONDS = 1.0
_ELEVATED_MEM_MB = 350   # ~70% of the 512MB ceiling


@app.middleware("http")
async def _log_heavy_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    dur = time.time() - t0
    mem = diag.rss_mb()
    if dur >= _SLOW_REQUEST_SECONDS or mem >= _ELEVATED_MEM_MB:
        diag.checkpoint(f"request {request.method} {request.url.path} "
                        f"dur={dur:.2f}s status={response.status_code}")
    return response


def _require_admin(authorization: str = "", token: str = ""):
    """Gate for /admin/*: the API is public, so admin actions (pipeline runs,
    intel wipes, usage internals) need ADMIN_TOKEN — via Authorization: Bearer
    or ?token= for curl convenience. No token configured = admin disabled.

    The three ways this can fail used to return one identical message. Opening
    /admin/usage in a browser sends no credential at all, and answering that
    with "bad admin token" says the token you did not send is wrong — which
    reads as "my token stopped working" and sends you looking in the wrong
    place. Each cause now names itself. Saying "you sent nothing" leaks
    nothing: it is what `WWW-Authenticate` is for, and the header is sent too
    so a browser can offer to log in.
    """
    if not config.ADMIN_TOKEN:
        raise HTTPException(403, "admin endpoints are disabled — set ADMIN_TOKEN "
                                 "in the environment to enable them")
    # Stripped on BOTH sides. A token pasted into Render's environment editor
    # very often carries a trailing newline or space, and one copied into a URL
    # can arrive with %20 on the end; either produced a mismatch that looked
    # exactly like a wrong password.
    supplied = (token or authorization.removeprefix("Bearer ")).strip()
    if not supplied:
        raise HTTPException(
            401,
            "no admin token supplied — send `Authorization: Bearer <ADMIN_TOKEN>` "
            "or ?token=<ADMIN_TOKEN>. The admin console at /admin does this for you.",
            headers={"WWW-Authenticate": 'Bearer realm="descry-admin"'})
    # compare_digest raises TypeError on a non-ASCII str, which turned a wrong
    # password into a 500. Compare bytes so any token value is answerable.
    if not secrets.compare_digest(supplied.encode("utf-8"),
                                  config.ADMIN_TOKEN.strip().encode("utf-8")):
        raise HTTPException(401, "admin token does not match the one set on this server")

# One pipeline at a time — shared guard for scheduled AND manual runs.
_pipeline_lock = threading.Lock()


def _failed_stages(results):
    """Stages of a news run that ended in error.

    run_pipeline catches per stage and stores the failure as the string
    "error: ..." in its results dict, so a failed run still returns normally —
    which is why this reads the values rather than relying on an exception."""
    required = config.FINANCE_REQUIRED_STAGES
    return [s for s, v in (results or {}).items()
            if (not required or s in required)
            and isinstance(v, str) and v.startswith("error:")]


def _chain_finance(results):
    """Run the finance pipeline straight after a clean news run, on this thread.

    Called with _pipeline_lock ALREADY HELD, so it calls run_finance_pipeline
    directly rather than guarded_finance_run: the lock is a plain Lock, not an
    RLock, so the guarded wrapper would fail to re-acquire it and silently
    return None — a chained run that never ran and never said so.

    A skip is written to `runs` on purpose. "Finance did not run" was invisible
    before, and a gate that blocks silently is the same bug in a new place."""
    blocked = _failed_stages(results)
    if not blocked:
        return run_finance_pipeline()
    con = db.connect()
    try:
        gate = (",".join(config.FINANCE_REQUIRED_STAGES)
                if config.FINANCE_REQUIRED_STAGES else "all stages")
        db.log_run(con, "finance_pipeline", "skipped",
                   f"news run failed at {blocked} (gate: {gate}) — "
                   f"finance skipped, will run after the next clean news run")
    finally:
        con.close()
    return f"skipped: {blocked} failed"


def guarded_run(stage: str | None = None):
    if not _pipeline_lock.acquire(blocking=False):
        return None  # a run is already in progress; skip
    try:
        results = run_pipeline(stage)
        # Only a FULL run chains. A single-stage admin trigger
        # (/admin/run?stage=entities) is a targeted repair, not a cycle, and
        # must not drag a whole finance run along behind it.
        if (stage is None and config.FINANCE_AFTER_PIPELINE
                and config.FINANCE_TOPICS):
            results["finance"] = _chain_finance(results)
        return results
    finally:
        _pipeline_lock.release()


def guarded_finance_run(stage: str | None = None):
    """The finance pipeline, sharing the general pipeline's lock ON PURPOSE.

    They write to different tables, so correctness doesn't need it — but they
    are both LLM pipelines with the whole working set of a run in memory, and
    letting them overlap inside one 512MB instance is how you get an OOM with
    no traceback. In its normal deployment this never contends: the finance
    pipeline runs as its own worker process (FINANCE_IN_PROCESS=0)."""
    if not _pipeline_lock.acquire(blocking=False):
        return None
    try:
        return run_finance_pipeline(stage)
    finally:
        _pipeline_lock.release()


@app.on_event("startup")
def _start():
    # Unconditional RSS log every 30s — catches growth that isn't tied to any
    # single stage/request boundary (e.g. many small requests each leaving a
    # sliver behind). See diag.py for why this matters on a 512MB instance
    # where an OOM-SIGKILL leaves no traceback.
    diag.start_heartbeat()
    # Say once, at boot, what state admin auth is in. Every /admin/* failure is
    # otherwise diagnosed by guessing, and a short token on a public API is a
    # standing invitation — it is guessable at internet scale and there is no
    # rate limit in front of it.
    if not config.ADMIN_TOKEN:
        diag.checkpoint("ADMIN_TOKEN is not set — /admin/* endpoints are disabled")
    elif len(config.ADMIN_TOKEN) < config.ADMIN_TOKEN_MIN_LEN:
        diag.checkpoint(
            f"ADMIN_TOKEN is only {len(config.ADMIN_TOKEN)} characters — "
            f"use at least {config.ADMIN_TOKEN_MIN_LEN} "
            "(`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`)")
    # Both of these fail SILENTLY and both were live in production: a model the
    # pacing table doesn't recognise is sent at the global cap until the provider
    # 429s it (which then hands the run to whichever provider is last in the
    # order), and a model with no rate is costed at $0 — so the spend report
    # reads as if it were free. Neither is visible without being told.
    for problem in config._price_errors:
        diag.checkpoint("LLM_PRICES: " + problem)
    warn = llm.unpaced_models()
    if warn["unpaced"]:
        diag.checkpoint("UNPACED models (will be sent at the global rate cap and "
                        "rate-limited): " + ", ".join(warn["unpaced"]))
    if warn["unpriced"]:
        diag.checkpoint("UNPRICED models — their spend is recorded as $0.00 and the "
                        "cost report understates the real bill. Set LLM_PRICES and "
                        "POST /admin/llm-reprice: " + ", ".join(warn["unpriced"]))
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
    # The finance pipeline is OFF in this process by default and runs as its own
    # worker/cron (`python run_finance_pipeline.py`). Scheduling it here is a
    # local-development convenience: on the 512MB instance it would put a second
    # LLM pipeline in the process that serves requests, which is precisely the
    # coupling that let a pipeline OOM take the API down with it.
    if config.FINANCE_AFTER_PIPELINE and config.FINANCE_TOPICS:
        # Chained to the news run (see guarded_run), so it gets NO clock of its
        # own — a second trigger would either double-run it or be swallowed by
        # the pipeline lock, and both look like "finance is flaky".
        diag.checkpoint(
            "finance pipeline CHAINED after each full news run "
            f"(every {config.PIPELINE_INTERVAL_HOURS}h, gated on "
            + (",".join(config.FINANCE_REQUIRED_STAGES)
               if config.FINANCE_REQUIRED_STAGES else "all stages")
            + " succeeding)")
    elif config.FINANCE_IN_PROCESS:
        scheduler.add_job(guarded_finance_run, "interval",
                          hours=config.FINANCE_INTERVAL_HOURS,
                          id="finance_pipeline", replace_existing=True,
                          next_run_time=datetime.now() + timedelta(minutes=6),
                          coalesce=True, misfire_grace_time=3600, max_instances=1)
        diag.checkpoint("finance pipeline scheduled IN-PROCESS "
                        f"(every {config.FINANCE_INTERVAL_HOURS}h)")
    scheduler.start()


def _purge_analytics_job():
    con = db.connect()
    try:
        n = analytics.purge(con)
        if n:
            db.log_run(con, "purge_analytics", "ok",
                       f"deleted {n} visit rows older than "
                       f"{config.ANALYTICS_RETAIN_DAYS:g} days")
        # Spend rows only if LLM_USAGE_RETAIN_DAYS is set — off by default,
        # because these are aggregates that do not grow with traffic and
        # deleting them is deleting the cost history someone is answering for.
        n = llmcost.purge(con)
        if n:
            db.log_run(con, "purge_analytics", "ok",
                       f"deleted {n} LLM spend rows older than "
                       f"{config.LLM_USAGE_RETAIN_DAYS} days")
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


# ---------------------------------------------------------------- evidence
# The redesign puts evidence on the surface of every card: "6 verified · 2
# unproven", "14 sources · 3 primary", "2 points of disagreement". None of that
# needed new computation — the verdicts, the article list and the merge stats
# were all already stored per story and simply never serialized. This is the
# one place that turns a stories row into those numbers, so the feed, the story
# page, bookmarks and the read list can't drift apart on how they're counted.
_VERIFIER = None


def _verifier():
    """Lazily built and reused: it parses sources.yaml on construction, and
    /feed would otherwise re-read that file once per request."""
    global _VERIFIER
    if _VERIFIER is None:
        _VERIFIER = Verifier()
    return _VERIFIER


def _evidence(row, sources=None):
    """Evidence counts for a story row. Every key is omitted rather than
    guessed when the underlying data isn't there, so a client can tell "no
    disputed claims" from "we never checked" — the whole point of the panel."""
    out = {}
    keys = row.keys() if hasattr(row, "keys") else row
    claims = db.uj(row["claims"]) if "claims" in keys else {}
    verdicts = (claims or {}).get("verdicts") or []
    if verdicts:
        vc = verdict_counts(verdicts)
        out["claims_verified"] = vc["verified"]
        out["claims_disputed"] = vc["disputed"]
        out["claims_unverified"] = vc["unverified"]
        out["claims_total"] = len(verdicts)
    if "article_ids" in keys:
        out["source_count"] = len(db.uj(row["article_ids"], []))
    stats = db.uj(row["merge_stats"]) if "merge_stats" in keys else {}
    if stats:
        if stats.get("conflicts") is not None:
            out["conflicts"] = int(stats["conflicts"] or 0)
        kinds = stats.get("kinds") or {}
        if kinds:
            out["source_kinds"] = kinds
            # Surfaced separately because the reader shows it as its own figure.
            # Currently always 0: we ingest no primary-document feeds yet (see
            # the `kinds:` block in sources.yaml). Reporting the real 0 beats
            # relabelling newsrooms to make the panel look fuller.
            out["source_primary"] = int(kinds.get("primary") or 0)
    # Fallback for stories written before merge_stats existed: the outlet mix
    # can still be derived from the sources we're already returning.
    if "source_kinds" not in out and sources:
        kinds = _verifier().source_breakdown(
            s.get("source", "") for s in sources if isinstance(s, dict))
        if kinds:
            out["source_kinds"] = kinds
            out["source_primary"] = int(kinds.get("primary") or 0)
    return out


def _history(con, story_id, limit=12):
    """Corroboration over time, oldest first — the series behind '68 → 82'."""
    rows = con.execute(
        "SELECT credibility, source_count, verified, disputed, unverified, "
        "conflicts, created_at FROM story_history WHERE story_id=? "
        "ORDER BY created_at DESC LIMIT ?", (story_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _corrections(con, story_ids):
    """{story_id: correction} covering only the stories that HAVE one.

    One query for the whole page. Calling _history per row would put an N+1 on
    the feed's hot path — 100 indexed lookups per request — which is the exact
    shape that has stalled this box before. Stories with no history (everything
    written before phase 2, and every story that has never moved) are simply
    absent from the result, and absent means "nothing to report", never "stable":
    the client shows no notice either way, so the distinction costs nothing.

    Never labelled a retraction anywhere in this path. We can observe our own
    corroboration falling; we cannot observe a publisher withdrawing anything."""
    ids = [i for i in (story_ids or []) if i]
    if not ids:
        return {}
    by_story = {}
    for chunk in _chunks(ids):
        for r in con.execute(
                "SELECT story_id, credibility, disputed, conflicts, created_at "
                "FROM story_history WHERE story_id IN (%s) "
                "ORDER BY story_id, created_at" % ",".join("?" * len(chunk)),
                chunk).fetchall():
            by_story.setdefault(r["story_id"], []).append(r)
    out = {}
    for sid, rows in by_story.items():
        c = detect_correction(rows)
        if c:
            out[sid] = c
    return out


def _finance_feed_items(con, user_id, floor):
    """fin_stories shaped as feed items, tagged `kind: "finance"`.

    The finance pipeline reads the SAME articles as the news one, so both tell
    the same events. Left alone they would appear twice under the Finance chip —
    once plainly, once enriched — which is why the caller drops the plain twin
    (see _merge_finance). What survives is the richer telling of an event plus
    ordinary coverage of everything finance never got to.

    The shape deliberately matches a normal feed item, because the chip filters
    on `topic` client-side and the cards are rendered by the same code. Only
    `kind` and the four finance-only summary fields are new, and every client
    that does not know about them ignores them."""
    rows = con.execute(
        """SELECT s.id, s.event_id, s.headline, s.narrative, s.credibility,
                  s.credibility_note, s.topic, s.image_url, s.created_at,
                  s.updated_at, s.article_ids, s.claims, s.merge_stats,
                  s.event_type, s.sectors, s.tickers, s.sentiment_net, s.metrics
           FROM fin_stories s
           LEFT JOIN read_stories r ON r.story_id = s.id AND r.user_id = ?
           WHERE s.updated_at > ? AND r.story_id IS NULL
           ORDER BY s.updated_at DESC LIMIT 200""",
        (user_id, floor)).fetchall()
    items = []
    for r in rows:
        it = dict(r)
        it["kind"] = "finance"
        # Columns fin_stories does not have. Supplied rather than omitted so a
        # finance item is never the one row that makes a client's optional
        # handling matter.
        it["place"] = None
        it["trend_ids"] = "[]"        # decoded by the shared loop below
        it["impact_text"] = ""
        it["impact_score"] = 0
        # The finance-only summary: what makes the card worth badging.
        it["metric_count"] = len(db.uj(it.pop("metrics"), []))
        it["sectors"] = db.uj(it["sectors"], [])
        it["tickers"] = db.uj(it["tickers"], [])
        items.append(it)
    return items


def _merge_finance(regular, finance):
    """One item per event, preferring the finance telling.

    Identity is `event_id` — the same key Storyteller uses to decide whether it
    is continuing a story or starting one — so this suppresses the plain twin of
    an enriched story without touching stories that only one pipeline told."""
    covered = {it.get("event_id") for it in finance if it.get("event_id")}
    kept = [it for it in regular
            if not (it.get("event_id") and it["event_id"] in covered)]
    return kept + finance


def _finance_story_as_news(con, story_id):
    """A fin_story in the /story/{id} shape, or None if there is no such row.

    A SUPERSET: every key a news story sends, with the same meaning, plus the
    finance-only ones and `kind: "finance"`. Superset rather than a second shape
    so an existing client decodes it without changes and simply ignores what it
    does not know — while a client that DOES know can branch on `kind` and show
    the metrics table and actor sentiment the finance pipeline exists to produce.
    """
    s = con.execute("SELECT * FROM fin_stories WHERE id=?", (story_id,)).fetchone()
    if not s:
        return None
    articles = [dict(con.execute(
        "SELECT title,url,source FROM articles WHERE id=?", (i,)).fetchone() or {})
        for i in db.uj(s["article_ids"], [])]
    out = {"id": s["id"], "kind": "finance",
           "headline": s["headline"], "narrative": s["narrative"],
           "why_matters": s["why_matters"] or "",
           "credibility": s["credibility"],
           "credibility_note": s["credibility_note"],
           "claims": db.uj(s["claims"]), "topic": s["topic"],
           "beats": db.uj(s["beats"]) if s["beats"] else None,
           "anchors": db.uj(s["anchors"]) if s["anchors"] else None,
           # The finance pipeline has no framing/connections/personalization
           # stage. Sent as the same "never computed" values a news story uses
           # so the client's existing empty-state handling applies unchanged.
           "framing": None, "connections": [], "trends": [],
           "impact_text": "", "impact_score": 0,
           "image_url": s["image_url"] or "", "sources": articles,
           "created_at": s["created_at"],
           # ---- finance-only, ignored by clients that predate them
           "event_type": s["event_type"],
           "sectors": db.uj(s["sectors"], []),
           "tickers": db.uj(s["tickers"], []),
           "metrics": db.uj(s["metrics"], []),
           "entities": db.uj(s["entities"], {}),
           "sentiment": db.uj(s["sentiment"], {}),
           "sentiment_net": s["sentiment_net"],
           "economic_drivers": db.uj(s["economic_drivers"], []),
           "history": []}
    out.update(_evidence(s, articles))
    return out


def _attach_corrections(con, items):
    """Add `correction` to the items that have one, in place. Omitted entirely
    from the rest — the key's presence IS the signal."""
    fixes = _corrections(con, [i.get("id") for i in items])
    for it in items:
        c = fixes.get(it.get("id"))
        if c:
            it["correction"] = c
    return items


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
    # Corroboration AT THE MOMENT OF SAVING. The design's Saved screen leads with
    # "▲14 since you saved" — without this we would have to walk story_history
    # for every row on every request, and for a story saved before its first
    # history row exists there would be nothing to walk to.
    cur = con.execute("SELECT credibility FROM stories WHERE id=?",
                      (story_id,)).fetchone()
    con.execute("INSERT OR IGNORE INTO bookmarks "
                "(id, user_id, story_id, created_at, credibility_at_save) "
                "VALUES (?,?,?,?,?)",
                (db.new_id(), user_id, story_id, db.now(),
                 cur["credibility"] if cur else None))
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
                  s.topic, s.place, s.image_url, s.created_at, s.updated_at,
                  s.article_ids, s.claims, s.merge_stats,
                  b.created_at AS saved_at, b.credibility_at_save,
                  r.progress AS progress, r.completed_at AS completed_at,
                  r.beat AS stopped_at,
                  '' AS impact_text, 0 AS impact_score
           FROM bookmarks b JOIN stories s ON s.id = b.story_id
           LEFT JOIN read_stories r
                  ON r.story_id = s.id AND r.user_id = b.user_id
           WHERE b.user_id = ? ORDER BY b.created_at DESC""",
        (user_id,)).fetchall()
    items = []
    for r in rows:
        it = dict(r)
        it.update(_evidence(it))
        del it["article_ids"]; del it["claims"]; del it["merge_stats"]
        was = it.pop("credibility_at_save", None)
        # Only a real change is reported. None (saved before this shipped, or
        # the story had no score yet) means "we don't know", not "no change".
        it["credibility_delta"] = (
            round((it["credibility"] or 0) - was, 1) if was is not None else None)
        items.append(it)
    # Saved is where a correction matters most: these are stories the reader
    # chose to keep, and may come back to weeks after the reporting moved.
    _attach_corrections(con, items)
    con.close()
    weakened = sum(1 for i in items if i.get("correction"))
    return {"items": items, "stats": {"saved": len(items), "weakened": weakened}}


@app.post("/read")
def mark_read(user_id: str, story_id: str, authorization: str = Header("")):
    """Idempotent: called both when a user opens a story (auto) and when they
    swipe/dismiss one from the feed without opening it (explicit) — same
    underlying state either way, so a story never shows as read in one place
    and unread in another."""
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("INSERT OR IGNORE INTO read_stories "
                "(id, user_id, story_id, created_at) VALUES (?,?,?,?)",
                (db.new_id(), user_id, story_id, db.now()))
    con.commit(); con.close()
    return {"ok": True}


@app.delete("/read")
def unmark_read(user_id: str, story_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("DELETE FROM read_stories WHERE user_id=? AND story_id=?",
                (user_id, story_id))
    con.commit(); con.close()
    return {"ok": True}


# What counts as "read in full". Not 1.0: almost nobody scrolls past the last
# line of the closing paragraph, so demanding the very bottom would report a
# diligent reader as having abandoned everything.
_READ_COMPLETE_AT = 0.9


@app.post("/read/progress")
def set_read_progress(user_id: str, story_id: str, progress: float,
                      beat: str = "", authorization: str = Header("")):
    """How far into a story the reader actually got, 0.0-1.0.

    `read_stories` only ever recorded that a story was OPENED, which is why the
    Read screen could not honestly say "31 of 58 read in full" or name the
    topics you keep abandoning — opening and finishing looked identical.

    Monotonic: progress only ever moves forward, so scrolling back up (or
    reopening on a second device to check one line) can't erase the fact that
    you already read it. Completion is stamped once, the first time the reader
    crosses the threshold."""
    p = max(0.0, min(1.0, float(progress)))
    con = db.connect()
    _auth(con, user_id, authorization)
    con.execute("INSERT OR IGNORE INTO read_stories "
                "(id, user_id, story_id, created_at) VALUES (?,?,?,?)",
                (db.new_id(), user_id, story_id, db.now()))
    con.execute(
        "UPDATE read_stories SET progress=MAX(COALESCE(progress,0), ?), "
        "completed_at=COALESCE(completed_at, CASE WHEN ?>=? THEN ? END), "
        # Only moves forward with progress, so re-opening at the top doesn't
        # rewrite where you actually got to.
        "beat=CASE WHEN ?>COALESCE(progress,0) AND ?<>'' THEN ? ELSE beat END "
        "WHERE user_id=? AND story_id=?",
        (p, p, _READ_COMPLETE_AT, db.now(), p, beat, beat, user_id, story_id))
    con.commit(); con.close()
    return {"ok": True, "progress": p}


@app.get("/read")
def list_read(user_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    rows = con.execute(
        """SELECT s.id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic, s.place, s.image_url, s.created_at, s.updated_at,
                  s.article_ids, s.claims, s.merge_stats, s.framing,
                  r.created_at AS read_at, r.progress, r.completed_at,
                  r.beat AS stopped_at,
                  '' AS impact_text, 0 AS impact_score
           FROM read_stories r JOIN stories s ON s.id = r.story_id
           WHERE r.user_id = ? ORDER BY r.created_at DESC""",
        (user_id,)).fetchall()
    items = []
    framings = []
    for r in rows:
        it = dict(r)
        it.update(_evidence(it))
        del it["article_ids"]; del it["claims"]; del it["merge_stats"]
        f = db.uj(it.pop("framing", None) or "") or {}
        if f.get("positions"):
            framings.append(f)
        items.append(it)
    _attach_corrections(con, items)
    con.close()
    # The Read screen's summary panels: how much you finish, and the quality mix
    # of what you read. Both are counts over rows we already have in hand, so
    # they cost nothing extra and save the client three more requests.
    opened = len(items)
    finished = sum(1 for i in items if i.get("completed_at"))
    band = {"strong": 0, "mixed": 0, "thin": 0}
    for i in items:
        c = i.get("credibility") or 0
        band["strong" if c >= 70 else "mixed" if c >= 40 else "thin"] += 1
    # Topics opened but rarely finished — the design's "blind spots". Named
    # honestly: this is abandonment, not disinterest, and it is never fed back
    # into ranking.
    by_topic = {}
    for i in items:
        t = i.get("topic") or "other"
        s_ = by_topic.setdefault(t, {"topic": t, "opened": 0, "finished": 0})
        s_["opened"] += 1
        s_["finished"] += 1 if i.get("completed_at") else 0
    blind = sorted((t for t in by_topic.values()
                    if t["opened"] >= 3 and t["finished"] * 2 < t["opened"]),
                   key=lambda t: (t["finished"] / t["opened"], -t["opened"]))[:3]
    # "Breadth of framing" — how much disagreement in emphasis the reader was
    # actually exposed to. Only stories somebody has classified can contribute
    # (framing is on-demand), so the denominator is reported alongside it and
    # the client is expected to say "based on N of M". Claiming it covers
    # everything read would be the easiest lie on this page to tell and the
    # hardest for a reader to catch.
    breadth = None
    if framings:
        wide = sum(1 for f in framings if (f.get("span") or 0) >= 1.0)
        narrow = sum(1 for f in framings if (f.get("span") or 0) < 0.5)
        breadth = {"classified": len(framings), "of": opened,
                   "wide": wide, "narrow": narrow,
                   "mean_span": round(
                       sum(float(f.get("span") or 0) for f in framings) / len(framings), 2)}
    stats = {"opened": opened, "finished": finished,
             "abandoned": opened - finished, "bands": band,
             "blind_spots": blind,
             "weakened": sum(1 for i in items if i.get("correction"))}
    # Omitted, not zeroed: "no story you read has been classified" and "the
    # stories you read were framed identically" are different facts.
    if breadth:
        stats["framing"] = breadth
    return {"items": items, "stats": stats}


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


@app.get("/feed", tags=["feed"], summary="The reader's feed",
         responses={200: {"model": wire.FeedResponse}})
def feed(user_id: str, sort: str = "recent", since: float = 0.0,
         authorization: str = Header("")):
    """Up to 100 ranked stories from the last 7 days, read stories excluded.

    `sort=recent` (default) ranks by corroboration and the reader's topics;
    `sort=foryou` ranks by personalised impact. `since` (epoch seconds) returns
    only stories newer than that — the incremental fetch behind "N new stories".

    Finance-pipeline stories are merged in, tagged `kind: "finance"`, and carry
    `sectors`, `tickers`, `sentiment_net` and `metric_count`. They keep their
    own `topic`, so client-side topic chips pick them up with no change; badge
    them by testing for `kind == "finance"`.
    """
    con = db.connect()
    _auth(con, user_id, authorization)
    # LEFT JOIN: every recent story appears in the feed; personalization
    # (impact text/score) enriches stories the reader has actually opened
    # "What this means for you" on — see POST /story/{id}/personalize — but
    # never gates visibility.
    # `since` (epoch) returns only stories newer than the client's newest — the
    # cheap incremental fetch behind the "N new stories" banner. created_at is
    # exposed so the client can diff/merge without a second call.
    # Retention is measured from the last retell, not first telling: created_at
    # is immutable now, so a storyline still developing on day 8 would otherwise
    # drop out of the feed entirely instead of merely ranking lower.
    floor = max(since, db.now() - 7 * 86400)
    # Candidate pool capped generously above realistic 7-day volume (a few
    # hundred at most — MAX_STORIES_PER_RUN * runs/week), so ranking still sees
    # every real candidate and can blend recency with impact (see module
    # docstring), while a future bug can't turn this into an unbounded scan the
    # way the un-capped /trends and /signals queries just did in production.
    # Stories the user has already read/dismissed are excluded here (not just
    # deprioritized) — that's the point: a feed that never drops anything reads
    # as stale even when the ranking underneath is fresh. They stay reachable
    # via GET /read, and un-marking (DELETE /read) brings a story back.
    rows = con.execute(
        """SELECT s.id, s.event_id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic, s.place, s.image_url, s.created_at, s.updated_at, s.article_ids, s.trend_ids,
                  s.claims, s.merge_stats,
                  COALESCE(f.impact_text, '')  AS impact_text,
                  COALESCE(f.impact_score, 0)  AS impact_score
           FROM stories s
           LEFT JOIN feed_items f ON f.story_id = s.id AND f.user_id = ?
           LEFT JOIN read_stories r ON r.story_id = s.id AND r.user_id = ?
           WHERE s.updated_at > ? AND r.story_id IS NULL
           ORDER BY s.updated_at DESC LIMIT 1000""",
        (user_id, user_id, floor)).fetchall()
    # Personalization is on-demand now (see POST /story/{id}/personalize) — a
    # story only has a real impact_score once someone actually opened "What
    # this means for you" on it, so most rows here won't have one yet. For
    # foryou ranking, fall back to the same free (no LLM) relevance test that
    # gates whether a story is worth personalizing at all: a real cached score
    # still wins (it reflects an actual reader having opened it), unscored-but-
    # relevant stories rank above unscored-irrelevant ones, exactly the
    # ordering the LLM score used to approximate at proactive-batch cost.
    # The reader's lens is loaded for EVERY sort now, not just `foryou`. The
    # default feed used to ignore it, which is the whole of "half my feed is
    # sports and I never chose sports" — see RANK_INTEREST_BOOST.
    u = con.execute("SELECT context FROM users WHERE id=?", (user_id,)).fetchone()
    ctx = db.uj(u["context"] if u else "{}")
    interests = {str(i).lower() for i in (ctx.get("interests") or []) if str(i).strip()}
    # Finance-pipeline stories join the same pool, so they rank and filter with
    # everything else: the topic chips are built from the items themselves, so a
    # fin_story carrying topic=finance lands under the Finance chip with no
    # client change. Guarded because a deployment with the finance pipeline
    # switched off must behave exactly as it did before.
    items = [dict(r) for r in rows]
    if config.FINANCE_TOPICS:
        items = _merge_finance(items, _finance_feed_items(con, user_id, floor))
    if sort == "foryou":
        impact_of = lambda it: it["impact_score"] or (
            1 if personalization_relevant(ctx, it) else 0)
    else:
        def impact_of(it):
            # How many outlets corroborate the story — the same "how big is
            # this" proxy trends use (velocity), damped because a developing
            # storyline's count is unbounded.
            base = ranking.damped(len(db.uj(it["article_ids"], [])))
            # The reader's chosen topics lead. Matched on `topic` alone, not on
            # the full relevance test personalization_relevant() runs: that one
            # regexes every headline and narrative, and this path sorts up to
            # 1000 rows on every feed request.
            if interests and (it["topic"] or "").lower() in interests:
                base *= config.RANK_INTEREST_BOOST
            # Then corroboration — the number the card actually prints. Without
            # this a story scored 10 and a story scored 98 ranked identically
            # whenever both had one source, which is most of the catalogue.
            cred = max(0.0, min(float(it["credibility"] or 0), 100.0))
            return base * (config.RANK_CRED_FLOOR
                           + cred / 100.0 * config.RANK_CRED_WEIGHT)
    items = ranking.sort_by_rank(items, impact_of)[:100]
    # After the slice, so the correction lookup covers the 100 rows actually
    # being sent rather than the whole 1000-row candidate pool.
    _attach_corrections(con, items)
    con.close()
    for it in items:
        # Evidence counts BEFORE the raw columns are dropped — they are derived
        # from claims/article_ids/merge_stats, none of which belong on the wire.
        it.update(_evidence(it))
        del it["article_ids"]   # internal-only, not part of the API shape
        del it["claims"]; del it["merge_stats"]
        # event_id is how the two pipelines' tellings were matched above; it is
        # a join key, not something a reader or client needs.
        it.pop("event_id", None)
        # trend_ids IS part of the shape: the portal's story network draws the
        # story -> force edges from it, and without them it can only guess.
        it["trend_ids"] = db.uj(it["trend_ids"], [])
    return {"items": items}


@app.get("/story/{story_id}", tags=["feed"], summary="One story, either pipeline",
         responses={200: {"model": wire.StoryDetail},
                    404: {"description": "No story with that id."}})
def story(story_id: str, user_id: str = "", authorization: str = Header("")):
    """Resolves ids from BOTH pipelines. A finance story answers here as a
    superset of the news shape — every field a news story sends, with the same
    meaning, plus `kind: "finance"`, `metrics`, `entities` and `sentiment`.

    Pass `user_id` + `Authorization` to unlock connection chains and to have the
    open counted as a read. Signed-out callers get everything else.
    """
    con = db.connect()
    if user_id:
        _auth(con, user_id, authorization)
    s = con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not s:
        # A finance-pipeline story. Answered HERE rather than only at
        # /finance/story/{id} because this id now travels everywhere a news id
        # does — it is in the feed, so it reaches share links, the /s/ OG route
        # and any bookmark made from them. Serving it from one place means none
        # of those paths had to learn a second id space.
        fin = _finance_story_as_news(con, story_id)
        con.close()
        if fin:
            return fin
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
    if authed:  # opening a story counts as "read" — see POST /read for the
                # explicit (no-open) dismiss path, same underlying table
        con.execute("INSERT OR IGNORE INTO read_stories "
                    "(id, user_id, story_id, created_at) VALUES (?,?,?,?)",
                    (db.new_id(), user_id, story_id, db.now()))
        con.commit()
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
    history = _history(con, story_id)
    con.close()
    out = {"id": s["id"], "headline": s["headline"], "narrative": s["narrative"],
            # Separate field, not a paragraph split of `narrative` — see the
            # Storyteller. Empty on stories written before the split existed;
            # clients fall back to the old behaviour when it is.
            "why_matters": s["why_matters"] or "",
            "credibility": s["credibility"], "credibility_note": s["credibility_note"],
            "claims": db.uj(s["claims"]), "topic": s["topic"],
            # null, not [] — the clients distinguish "written before beats
            # existed" (fall back to the single narrative) from "structured but
            # empty", which should never happen.
            "beats": db.uj(s["beats"]) if "beats" in s.keys() and s["beats"] else None,
            "anchors": db.uj(s["anchors"]) if "anchors" in s.keys() and s["anchors"] else None,
            # Same rule: null means "nobody has opened the framing panel on this
            # story yet", which the client answers with a button rather than an
            # empty chart. A cached-but-unusable result (one outlet, or outlets
            # that all framed it the same way) comes back as an object WITHOUT
            # `positions`, and says so in words instead of drawing a spectrum.
            "framing": _framing_out(s),
            "image_url": s["image_url"] or "", "sources": articles,
            "trends": trends, "connections": conns, "created_at": s["created_at"],
            "impact_text": fi["impact_text"] if fi else "",
            "impact_score": fi["impact_score"] if fi else 0}
    # The evidence panel reads these; `sources` is passed so a story written
    # before merge_stats existed can still report its outlet mix.
    out.update(_evidence(s, articles))
    out["history"] = history
    c = detect_correction(history)
    if c:
        out["correction"] = c
    return out


def _framing_out(row):
    """The stored framing, or None when it has never been classified. Kept in
    one place so /story and POST /story/{id}/framing cannot disagree about what
    a cached "no spread" answer looks like on the wire."""
    keys = row.keys() if hasattr(row, "keys") else row
    raw = row["framing"] if "framing" in keys else None
    return db.uj(raw) or None if raw else None


@app.post("/story/{story_id}/framing")
def story_framing(story_id: str, user_id: str, authorization: str = Header("")):
    """"How outlets framed this" — classified on first open, then cached on the
    story for every later reader, signed in or not.

    Sign-in is required to TRIGGER it, for the same reason personalization is:
    this is the one place a request costs an LLM call, and an open endpoint is
    an open tab on someone else's bill. Reading a cached result costs nothing
    and needs no account — GET /story returns it to everyone.

    Works on stories written long before phase 5: it reads the articles the
    story already cites, so there is no old/new split here at all."""
    con = db.connect()
    _auth(con, user_id, authorization)
    s = con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not s:
        con.close()
        raise HTTPException(404, "story not found")
    llm.set_context(f"request framing story={story_id}")
    try:
        framing, reason = Framer().classify(con, s)
    finally:
        llm.set_context("")
    con.close()
    if reason == "llm_unavailable":
        # 503, not an empty 200: a rate limit is temporary and the client should
        # be able to offer "try again" rather than caching "no framing" in the UI.
        raise HTTPException(503, "framing is unavailable right now")
    return {"framing": framing, "reason": reason}


@app.post("/story/{story_id}/personalize")
def personalize_story(story_id: str, user_id: str, authorization: str = Header("")):
    """"What this means for you" — computed the first time a signed-in reader
    actually opens that module on this story, not proactively for every story
    on every pipeline run. GET /story already returns any cached result from a
    previous open at no cost; this is the endpoint the client calls only when
    that section is empty and gets expanded, so LLM spend tracks real reads
    instead of catalog size. Idempotent: a story already found irrelevant or
    already personalized returns the cached ("", 0) or real result without
    another LLM call — see Personalizer.personalize."""
    con = db.connect()
    _auth(con, user_id, authorization)
    u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    s = con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not s:
        con.close()
        raise HTTPException(404, "story not found")
    llm.set_context(f"request personalize story={story_id} user={user_id}")
    impact_text, impact_score = Personalizer().personalize(con, u, s)
    llm.set_context("")
    con.close()
    return {"impact_text": impact_text, "impact_score": impact_score}


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
        "image_url, created_at, updated_at, article_ids, trend_ids, claims, "
        "merge_stats FROM stories "
        "ORDER BY updated_at DESC LIMIT ?",
        (max(limit * 10, 300),)).fetchall()
    items = [dict(r) for r in rows]
    items = ranking.sort_by_rank(
        items, impact_of=lambda it: ranking.damped(len(db.uj(it["article_ids"], []))))[:limit]
    _attach_corrections(con, items)
    con.close()
    for it in items:
        it.update(_evidence(it))
        del it["article_ids"]; del it["claims"]; del it["merge_stats"]
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
         "created_at, updated_at FROM trends WHERE kind=? AND retired_at IS NULL "
         "ORDER BY updated_at DESC LIMIT 500")
    # Damped for the same reason as /feed: macro velocity is an article count
    # that grows for as long as a force stays live. Macro and micro are ranked
    # as separate lists, so compressing each independently is order-preserving
    # within the list the reader actually sees.
    macro = ranking.sort_by_rank(
        [dict(r) for r in con.execute(q, ("macro",)).fetchall()],
        impact_of=lambda t: ranking.damped(t["velocity"] or 0.0))[:40]
    micro = ranking.sort_by_rank(
        [dict(r) for r in con.execute(q, ("micro",)).fetchall()],
        impact_of=lambda t: ranking.damped(t["velocity"] or 0.0))[:20]
    # Corroboration per trend, for the Trends scatter: attention on one axis
    # against how well the reporting holds up on the other. A trend has no score
    # of its own, so it inherits the mean of the stories that belong to it —
    # stories carry trend_ids, so one pass over the recent catalogue builds the
    # whole map. Only stories still inside the feed window are counted, which is
    # the same horizon the page's "last 48 hours" framing implies.
    # `claims` comes along now because the reader's Trends page (design 3a)
    # states an "Agree / disagree" figure per trend. That is a sum of the
    # per-claim verdicts the Verifier already wrote on each story — no new
    # computation, just an aggregation nobody had asked for before.
    agg = {}
    window = [dict(r) for r in con.execute(
        "SELECT id, credibility, trend_ids, claims FROM stories WHERE updated_at > ?",
        (db.now() - 7 * 86400,)).fetchall()]
    for r in window:
        vc = verdict_counts((db.uj(r["claims"]) or {}).get("verdicts") or [])
        for tid in db.uj(r["trend_ids"], []):
            a = agg.setdefault(tid, [0.0, 0, 0, 0])
            a[0] += float(r["credibility"] or 0); a[1] += 1
            a[2] += vc["verified"]; a[3] += vc["disputed"]
    # Which trends are held up by reporting that has since weakened. One batched
    # history read over the same window the scatter already scans — this is the
    # global, cross-device answer to the question the page used to answer only
    # from this browser's localStorage snapshot.
    fixes = _corrections(con, [r["id"] for r in window])
    weakened = {}
    for r in window:
        if r["id"] in fixes:
            for tid in db.uj(r["trend_ids"], []):
                weakened[tid] = weakened.get(tid, 0) + 1
    con.close()
    out = []
    for d in macro + micro:
        d["sectors"] = db.uj(d["sectors"], [])
        d["regions"] = db.uj(d["regions"], [])
        d["article_count"] = len(db.uj(d["article_ids"], []))
        del d["article_ids"]
        tot, n, agree, disagree = agg.get(d["id"], (0.0, 0, 0, 0))
        # Omitted, not zeroed, when no story in the window carries this trend:
        # the page must be able to say "not enough to judge yet" rather than
        # report a zero as if we had checked and found nothing.
        d["credibility"] = round(tot / n, 1) if n else None
        d["story_count"] = n
        # "Agree / disagree" on the trend card. Only sent when claims were
        # actually checked on the stories underneath — 0/0 would read as
        # "nobody agrees", when it means "we haven't checked".
        if agree or disagree:
            d["agree"] = agree
            d["disagree"] = disagree
        # Only ever a positive count. 0 is left off for the same reason
        # `credibility` is: "no story here has weakened" and "no story here has
        # enough history to tell" are different, and the second is far commoner.
        if weakened.get(d["id"]):
            d["weakened_count"] = weakened[d["id"]]
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
                    # Omitted, not empty-stringed, when the model could not name
                    # one: absent means "we have no disproof for this", which the
                    # clients print as its own honest line.
                    "stories": stories, "story_refs": story_refs(id_head)})
        fals = (g["falsifier"] if "falsifier" in g.keys() else "") or ""
        if fals.strip():
            out["falsifier"] = linkify(fals, id_head)
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
    # Defensive cap, same reasoning as /trends: Foresight already retires this
    # table to WINDOW_DAYS, so 200 is generous headroom, not a normal-path limit.
    # retired_at IS NULL: forecasts past their window are kept now rather than
    # deleted (so a track record can one day be built from them), which means
    # this list has to say out loud that it wants the open ones.
    rows = ranking.sort_by_rank(
        [dict(r) for r in con.execute(
            "SELECT * FROM signals WHERE retired_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 200").fetchall()],
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
    # Membership is decided EXACTLY the way /trends counts it, because the card
    # and the page it opens have to agree. /trends counts a story when this
    # trend's id appears in `stories.trend_ids`; this endpoint used to match on
    # article-id overlap instead, and the two drift apart by design:
    #
    #   * a trend's `article_ids` are rewritten every run — new members, and
    #     merged trends absorbing each other's ids (see _reconcile_trends);
    #   * a story's `article_ids` are frozen when it is written, because a story
    #     with no new articles is skipped rather than re-told.
    #
    # So the trend moves and the story does not, and eventually they no longer
    # intersect. `trend_ids` survives it: the Storyteller re-labels by
    # `event_id`, which is stable for the life of the event.
    #
    # Measured on production before this change: of the 27 trends advertising a
    # story count, 9 opened onto an empty list and only 8 had a list matching
    # their card.
    #
    # Article overlap is kept, but strictly as a FALLBACK — never unioned in.
    # Unioning re-broke the same promise from the other side: measured locally,
    # a card reading "1 story" opened onto three, because overlap matches
    # stories the label rule (and therefore the count) never counted.
    #
    # So: if the trend has labelled stories, they ARE the list, and the page
    # matches its card exactly. Overlap only answers when nothing is labelled,
    # which is the pre-`trend_ids` case — old trends keep showing their stories
    # instead of going blank, and since the card reads 0 there, a non-empty list
    # cannot contradict it.
    #
    # Same window and same index (stories_updated) as /trends, so both endpoints
    # are looking at the same catalogue.
    #
    # The label match is done by SQLite, not by Python. `trend_ids` is a JSON
    # array of quoted ids, so matching '%"<id>"%' against the text is exact —
    # ids are 12 hex characters and the quotes stop a prefix from matching — and
    # it keeps the row out of Python entirely. The first cut of this endpoint
    # pulled every row in the window back and ran json.loads on TWO columns per
    # row on every request, which made the trend page slower than the trends
    # list it opens from.
    window = db.now() - 7 * 86400
    picked = [r["id"] for r in con.execute(
        "SELECT id FROM stories WHERE updated_at > ? AND trend_ids LIKE ?",
        (window, f'%"{trend_id}"%')).fetchall()]
    # Fallback only when nothing is labelled — the pre-`trend_ids` case. This is
    # the expensive shape (json per row), so it runs for old trends only, never
    # on the normal path.
    if not picked and member_ids:
        picked = [r["id"] for r in con.execute(
            "SELECT id, article_ids FROM stories WHERE updated_at > ?",
            (window,)).fetchall()
            if member_ids & set(db.uj(r["article_ids"], []))]
    # Two passes on purpose. The match above reads three small columns; the body
    # below reads the heavy ones (narrative, claims, merge_stats) for matches
    # only. The old single pass pulled all of that for 200 rows on every request
    # regardless of how few belonged — on a 512MB box that is worth avoiding.
    picked = picked[:200]
    stories, id_head = [], {}
    # Screen 4a needs each story's own evidence, not just its headline: the
    # growth chart is built from created_at, "what argues against this trend"
    # from the disputed-claim counts, and "who is saying it" from the outlet
    # mix. All of it already exists per story — it was simply never returned.
    if picked:
        for s in con.execute(
                "SELECT id, headline, narrative, credibility, credibility_note, topic, "
                "created_at, updated_at, claims, merge_stats, article_ids "
                "FROM stories WHERE id IN (%s) ORDER BY created_at DESC LIMIT 60"
                % ",".join("?" * len(picked)), picked).fetchall():
            d = {k: s[k] for k in ("id", "headline", "narrative", "credibility",
                                   "credibility_note", "topic", "created_at",
                                   "updated_at")}
            d.update(_evidence(s))
            stories.append(d)
            id_head[s["id"]] = s["headline"]
    # Aggregates over the trend's own stories. Summed here rather than in the
    # client because the client only ever sees the page it is on, and "11
    # original documents" has to be true of the whole trend.
    kinds, agree, disagree, disputed_stories = {}, 0, 0, 0
    for d in stories:
        for k, v in (d.get("source_kinds") or {}).items():
            kinds[k] = kinds.get(k, 0) + int(v or 0)
        agree += int(d.get("claims_verified") or 0)
        disagree += int(d.get("claims_disputed") or 0)
        if d.get("claims_disputed"):
            disputed_stories += 1
    # Forecasts standing on this trend: a signal whose supporting stories
    # overlap it. This is the real link — Foresight builds from stories, not
    # from trend ids — so it is computed rather than stored.
    sids = {d["id"] for d in stories}
    forecasts = []
    for g in con.execute("SELECT id, title, horizon, confidence, story_ids "
                         "FROM signals WHERE retired_at IS NULL "
                         "ORDER BY created_at DESC LIMIT 60").fetchall():
        shared = sids & set(db.uj(g["story_ids"], []))
        if shared:
            forecasts.append({"id": g["id"], "title": g["title"],
                              "horizon": g["horizon"], "confidence": g["confidence"],
                              "shared_stories": len(shared)})
    forecasts.sort(key=lambda f: -f["shared_stories"])
    con.close()
    # retired_at is returned (not hidden) so the client can say plainly that the
    # trend is no longer active instead of pretending it is current.
    out = {"id": t["id"], "kind": t["kind"], "name": t["name"],
           "narrative": linkify(t["narrative"], id_head),
           "sectors": db.uj(t["sectors"], []),
           "regions": db.uj(t["regions"], []), "velocity": t["velocity"],
           "created_at": t["created_at"], "retired_at": t["retired_at"],
           "article_count": len(member_ids), "story_count": len(stories),
           "stories": stories, "story_refs": story_refs(id_head),
           "forecasts": forecasts[:4]}
    # Omitted, not zeroed: "no outlet mix recorded" and "no original documents"
    # are different claims, and 4a prints one of them in words.
    if kinds:
        out["source_kinds"] = kinds
    if agree or disagree:
        out["agree"], out["disagree"] = agree, disagree
    if disputed_stories:
        out["disputed_stories"] = disputed_stories
    return out


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


# A shared card is only rich if the crawler accepts the image. Facebook's floor
# is 200x200 and X's summary_large_image wants ~300x157, so anything smaller
# renders as NO image at all — worse than the brand fallback. A wire thumbnail
# (BBC ships 240x135) therefore defers to the logo rather than silently
# producing a card with a blank slot.
OG_MIN_WIDTH, OG_MIN_HEIGHT = 300, 200


def _og_image_for(con, image_url):
    """(url, width, height) to advertise for a story, or the site logo.

    Dimensions aren't stored on `stories` — only the winning URL is — so they
    are read back from whichever article supplied it. That costs one indexed
    lookup on a request only crawlers make, and it is what lets the size check
    above be real instead of a guess. Unknown dimensions are treated as
    acceptable: most publisher artwork is 1200x675 or larger, and refusing
    everything unmeasurable would throw away the majority of real photos."""
    if not image_url or not image_url.startswith(("http://", "https://")):
        return config.OG_IMAGE_URL, None, None
    row = con.execute(
        "SELECT image_width, image_height FROM articles WHERE image_url=? "
        "AND image_width IS NOT NULL LIMIT 1", (image_url,)).fetchone()
    # Reconciled against the URL — a stored pair can describe a thumbnail the
    # URL has since been upgraded past, and trusting it verbatim is what made
    # a 1024px photograph read as 240px and lose to the logo here.
    w, h = images.declared_dims(image_url,
                                row["image_width"] if row else None,
                                row["image_height"] if row else None)
    if w and h and (w < OG_MIN_WIDTH or h < OG_MIN_HEIGHT):
        return config.OG_IMAGE_URL, None, None   # too small to unfurl richly
    return image_url, w, h


def _og_page(title, description, web_path, image=None, image_w=None, image_h=None):
    """The share card behind a social unfurl. Facebook, X, Slack, WhatsApp and
    friends read OG tags but do not run JS, so this has to be server-rendered.

    It is explicitly NOT the indexable page. `noindex` plus a canonical pointing
    at the real article on the web domain means it never competes with the page
    it advertises — two URLs serving the same headline is how a site ends up
    ranking its own doorway instead of its content. Everything is HTML-escaped.

    `image` overrides the brand logo, so a shared story unfurls with its own
    photograph. Third-party URL, so it is escaped like everything else here."""
    t, d = html.escape(title or "Descry"), html.escape(description or "")
    canonical = f"{config.WEB_BASE_URL}/{web_path}"
    can = html.escape(canonical)
    img = html.escape(image or config.OG_IMAGE_URL or "")
    # Declaring the size lets a crawler lay the card out without fetching the
    # image first, which is the difference between an unfurl that appears
    # instantly and one that appears a beat later (or not at all, on a timeout).
    dims = (f'<meta property="og:image:width" content="{int(image_w)}">'
            f'<meta property="og:image:height" content="{int(image_h)}">'
            if image_w and image_h else "")
    img_tags = (f'<meta property="og:image" content="{img}">'
                f'<meta property="og:image:alt" content="{t}">{dims}'
                f'<meta name="twitter:image" content="{img}">') if img else ""
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
    s = con.execute("SELECT headline, narrative, image_url FROM stories WHERE id=?",
                    (story_id,)).fetchone()
    if not s:
        con.close()
        return HTMLResponse(f'<script>location.replace("{config.WEB_BASE_URL}/")</script>')
    # The story's own photograph, not the Descry logo — a shared link should
    # look like the story it points at. Falls back to the logo when the story
    # has no artwork or the artwork is too small to unfurl (see _og_image_for).
    img, iw, ih = _og_image_for(con, s["image_url"])
    con.close()
    return _og_page(s["headline"], _clip(s["narrative"]), f"story/{story_id}",
                    image=img, image_w=iw, image_h=ih)


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
                                  "WHERE retired_at IS NULL "
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
        llm.set_context("admin rebuild-intel")
        diag.checkpoint("admin rebuild-intel start")
        try:
            _rebuild_intel()
        finally:
            gc.collect()   # reclaim any reference cycles from this batch before
                           # the next request runs on a 512MB instance
            diag.checkpoint("admin rebuild-intel done")
            llm.set_context("")
            _pipeline_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return {"started": True, "provider": config.LLM_PROVIDER,
            "reasoning_tasks": sorted(config.REASONING_TASKS),
            "status": "rebuilding all trends + forecasts in background",
            "check": "GET /admin/usage — look for stage='rebuild_intel', status='ok'"}


# How many writes to batch before committing. Small enough that the SQLite
# writer lock is released constantly (every other request writes to this same
# file via the traffic middleware), large enough not to fsync per row.
_BACKFILL_COMMIT_EVERY = 200


def _collect_feed_images():
    """Network phase, deliberately with NO database work in it: {link: (url, w, h)}.

    Reading feeds and writing rows used to be interleaved, which meant a write
    transaction stayed open across 40 sequential feed fetches — minutes of
    holding SQLite's single writer lock while every ordinary request (the
    traffic counter writes on all of them) queued behind it and eventually
    timed out. Collecting first costs almost nothing to hold: ~1900 short URL
    strings, well under a megabyte."""
    import feedparser
    import yaml as _yaml
    feeds = _yaml.safe_load(config.FEEDS_FILE.read_text())
    found = {}
    for _topic, urls in feeds.items():
        for url in urls:
            try:
                with httpx.stream("GET", url, timeout=15, follow_redirects=True,
                                  headers={"User-Agent": "DescryBot/0.1 (+beta)"}) as r:
                    buf, total = [], 0
                    for chunk in r.iter_bytes():
                        buf.append(chunk)
                        total += len(chunk)
                        if total >= config.RSS_MAX_BYTES:
                            break
                parsed = feedparser.parse(b"".join(buf))
            except Exception:  # noqa: BLE001 — one bad feed must not stop the sweep
                continue
            for e in parsed.entries[:200]:
                link = getattr(e, "link", "")
                if not link:
                    continue
                img, iw, ih = images.from_entry(e)
                if img:
                    found.setdefault(link, (img, iw, ih))
    return found


def _apply_image_backfill(con, found):
    """Write phase: attach artwork to articles, then to the stories built from
    them. Commits every _BACKFILL_COMMIT_EVERY rows so the writer lock is
    handed back constantly instead of being held for the whole batch."""
    arts_ct = story_ct = fixed_ct = 0
    for link, (img, iw, ih) in found.items():
        row = con.execute(
            "SELECT id, image_url, image_width, image_height FROM articles "
            "WHERE url=?", (link,)).fetchone()
        if not row:
            continue                                   # article not ingested
        if not row["image_url"]:
            con.execute(
                "UPDATE articles SET image_url=?, image_width=?, image_height=? "
                "WHERE id=?", (img, iw, ih, row["id"]))
            arts_ct += 1
        elif (row["image_url"] == img
              and (row["image_width"], row["image_height"]) != (iw, ih)):
            # Same picture, stale dimensions. Rows written before _upgrade
            # scaled its dimensions recorded a rewritten 1024px BBC URL against
            # the 240x135 the feed declared, which is small enough that the
            # share card refused it and fell back to the logo. Repairing needs
            # no re-fetch, so it happens on any backfill pass.
            con.execute(
                "UPDATE articles SET image_width=?, image_height=? WHERE id=?",
                (iw, ih, row["id"]))
            fixed_ct += 1
        else:
            continue
        if (arts_ct + fixed_ct) % _BACKFILL_COMMIT_EVERY == 0:
            con.commit()
    con.commit()
    # Now (re)pick each imageless story's artwork from its own articles. Read
    # the list up front (fetchall) so the cursor isn't live while we write to
    # the same table underneath it.
    todo = con.execute("SELECT id, article_ids FROM stories "
                       "WHERE image_url IS NULL OR image_url=''").fetchall()
    for s in todo:
        aids = db.uj(s["article_ids"], [])
        if not aids:
            continue
        arts = []
        for chunk in _chunks(aids):
            arts += con.execute(
                "SELECT image_url, image_width, image_height FROM articles "
                "WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
        best = images.best_of(arts)
        if best:
            con.execute("UPDATE stories SET image_url=? WHERE id=?", (best, s["id"]))
            story_ct += 1
            if story_ct % _BACKFILL_COMMIT_EVERY == 0:
                con.commit()
    con.commit()
    return arts_ct, story_ct, fixed_ct


@app.post("/admin/backfill-images")
def admin_backfill_images(dry_run: bool = True, token: str = "",
                          authorization: str = Header("")):
    """Attach artwork to articles ingested before images existed, then to the
    stories built from them.

    Feed entries aren't kept after ingest, so the only way to recover an image
    for an existing article is to re-read the feeds and match on the article
    URL — which is exactly what this does. That bounds it naturally: a feed
    only carries its current window, so this recovers the most recent runs'
    articles (the ones actually on screen) and not the whole archive. Older
    rows simply stay text-only, which both clients already render.

    Memory-safe by construction: feeds are streamed with the same byte cap as
    Scout, one at a time, and only URL/dimension strings are kept.
    dry_run=true (default) reports what it would attach without writing."""
    _require_admin(authorization, token)
    if dry_run:
        found = _collect_feed_images()
        con = db.connect()
        # A set, not a counter: the same article appears in several feeds (a
        # publisher's "latest" and "markets" overlap), so counting hits would
        # over-report and not match what the apply run then does.
        pending = set()
        for link in found:
            row = con.execute(
                "SELECT id, image_url FROM articles WHERE url=?", (link,)).fetchone()
            if row and not row["image_url"]:
                pending.add(row["id"])
        con.close()
        return {"dry_run": True, "feed_entries_with_images": len(found),
                "articles_matched": len(pending), "stories_updated": 0,
                "note": "re-run with dry_run=false to apply"}
    if _pipeline_lock.locked():
        return {"started": False,
                "status": "a pipeline run is already in progress; retry shortly"}

    def job():
        if not _pipeline_lock.acquire(blocking=False):
            return
        diag.checkpoint("admin backfill-images start")
        con2 = db.connect()
        try:
            # Feed reading happens HERE, not in the request: re-reading 40 feeds
            # takes ~15s and put the apply call within range of a proxy timeout.
            arts, stories, fixed = _apply_image_backfill(con2, _collect_feed_images())
            db.log_run(con2, "backfill_images", "ok",
                       f"{arts} articles and {stories} stories given artwork"
                       + (f", {fixed} stale dimensions repaired" if fixed else ""))
        except Exception as e:  # noqa: BLE001 — must not die silently in a thread
            db.log_run(con2, "backfill_images", "error", str(e)[:300])
        finally:
            con2.close()
            gc.collect()
            diag.checkpoint("admin backfill-images done")
            _pipeline_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return {"started": True,
            "status": "backfilling in background — check /admin/usage for "
                      "stage='backfill_images'"}


@app.post("/admin/fix-topics")
def admin_fix_topics(token: str = "", authorization: str = Header("")):
    """Recompute every story's `topic` from the articles it is actually built
    from, by majority (see agents._topic_of).

    A one-off repair for rows written while the topic was taken from
    `arts[0]["topic"]` — whichever article had the lowest random uuid. Stories
    carried by a single feed were right by luck; cross-beat merges got an
    arbitrary label and filtered under a chip their content had nothing to do
    with. New and retold stories fix themselves now, but a story nobody retells
    would keep its wrong beat forever, so it is done here in one pass.

    Reports the changes rather than just a count: a topic move is visible to
    every reader using the chips, and it should be reviewable."""
    _require_admin(authorization, token)
    con = db.connect()
    changed, examples = 0, []
    for s in con.execute("SELECT id, topic, headline, article_ids FROM stories").fetchall():
        ids = db.uj(s["article_ids"], [])
        if not ids:
            continue
        arts = [con.execute("SELECT topic FROM articles WHERE id=?", (i,)).fetchone()
                for i in ids]
        arts = [a for a in arts if a]
        want = _topic_of(arts)
        if want and want != (s["topic"] or ""):
            con.execute("UPDATE stories SET topic=? WHERE id=?", (want, s["id"]))
            changed += 1
            if len(examples) < 15:
                examples.append({"headline": (s["headline"] or "")[:70],
                                 "from": s["topic"], "to": want})
    con.commit()
    db.log_run(con, "fix_topics", "ok", f"recomputed topic on {changed} stories")
    con.close()
    return {"changed": changed, "examples": examples}


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


def _chunks(seq, n=400):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _idf_corpus(con, limit=1500):
    """Rarity statistics over a broad sample of recent news, computed once per
    split run and reused for every story — see textmerge.corpus_stats."""
    rows = con.execute(
        "SELECT id, title, summary, published, fetched_at FROM articles "
        "ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
    return textmerge.corpus_stats(rows) if rows else None


def _subjects_of(con, article_ids, stats=None):
    """Group a story's own articles by SUBJECT, using the same same-event scorer
    scorer in textmerge.subject_clusters.

    Deliberately NOT the stored group_id: group_id only ever merged near-duplicate
    reports of one event at ingest time, so a legacy row that swept up 200
    unrelated articles has 200 distinct group_ids and would "split" purely because
    it is big. Re-scoring the actual text answers the question that matters —
    are these articles about the same thing — so a heavily-corroborated single
    event stays one story no matter how many sources it has, and a two-article
    story about two unrelated things is correctly seen as two.

    `stats` supplies IDF measured over a broad sample of recent news rather than
    over this one story, so "distinctive word" keeps its usual meaning inside a
    set of articles that all talk about the same thing.

    Returns {subject_key: [article_id, ...]}.
    """
    own = []
    for chunk in _chunks(list(article_ids)):
        own += con.execute(
            "SELECT id, title, summary, published, fetched_at FROM articles "
            "WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
    if not own:
        return {}
    clusters = textmerge.subject_clusters(own, stats=stats)
    # Keyed by earliest member so the mapping is stable across runs.
    return {sorted(c)[0]: sorted(c) for c in clusters if c}


def _blob_candidates(con, min_articles):
    """Stories whose articles cover more than one subject, newest first.

    `min_articles` is only a cheap pre-filter (a 1-article story cannot be
    incoherent) — it is NOT the split criterion. Every story with at least that
    many articles is re-scored on content; only genuine multi-subject rows are
    returned."""
    stats = _idf_corpus(con)
    out = []
    for r in con.execute(
            "SELECT id, headline, narrative, credibility, article_ids, trend_ids, "
            "event_id FROM stories ORDER BY updated_at DESC").fetchall():
        ids = db.uj(r["article_ids"], [])
        if len(ids) < max(min_articles, 2):
            continue
        subjects = _subjects_of(con, ids, stats)
        if len(subjects) > 1:
            out.append((dict(r), subjects))
    return out


#: credibility_note stamped on a story whose text is a placeholder, not written
#: copy. _retell_candidates uses it (plus the legacy bullet shape) to find work.
PLACEHOLDER_NOTE = "split from a merged story; awaiting retelling"


def _placeholder_narrative(arts):
    """Readable stand-in text for a story that has not been written yet.

    Deliberately carries no source names: the story page already lists every
    source underneath, so repeating them inside the body is duplication, and
    starting the body with an outlet name reads like a wire slug rather than a
    story. clean_text also strips the raw HTML entities (&#039; and friends)
    that come through in feed summaries.
    """
    seen, parts = set(), []
    for a in sorted(arts, key=lambda x: x.get("published") or x.get("fetched_at") or 0):
        for piece in (a.get("summary"), a.get("title")):
            text = textmerge.clean_text(piece or "")
            if len(text) > 40 and text.lower() not in seen:
                seen.add(text.lower())
                parts.append(text if text.endswith((".", "!", "?")) else text + ".")
                break
    return " ".join(parts)


def _needs_retell(row):
    """True when a story's body is placeholder text rather than written copy."""
    if (row["credibility_note"] or "").startswith("split from a merged story"):
        return True
    return (row["narrative"] or "").lstrip().startswith("- [")


def _retell_story(con, row, verifier):
    """Rewrite one story's headline/narrative/why_matters properly.

    Same path the pipeline uses for any story — merge the member articles into an
    attributed brief, verify the claims, then have the model write it — so a
    repaired story is indistinguishable from a normally-produced one.

    created_at and updated_at are deliberately left alone: this repairs text, it
    is not new reporting, and touching updated_at would shove every repaired
    story back to the top of the feed.
    """
    ids = db.uj(row["article_ids"], [])
    if not ids:
        return False
    arts = []
    for chunk in _chunks(ids):
        arts += [dict(a) for a in con.execute(
            "SELECT * FROM articles WHERE id IN (%s)" % ",".join("?" * len(chunk)),
            chunk).fetchall()]
    if not arts:
        return False
    # Cap before build_brief — see RETELL_MAX_ARTICLES. Newest first, at most two
    # per outlet, so the sample stays source-diverse instead of being one wire
    # service repeated a dozen times.
    if len(arts) > RETELL_MAX_ARTICLES:
        arts.sort(key=lambda a: -(a.get("published") or a.get("fetched_at") or 0))
        per_source, picked = {}, []
        for a in arts:
            src = a.get("source") or ""
            if per_source.get(src, 0) >= 2:
                continue
            per_source[src] = per_source.get(src, 0) + 1
            picked.append(a)
            if len(picked) >= RETELL_MAX_ARTICLES:
                break
        arts = picked or arts[:RETELL_MAX_ARTICLES]
    texts = fulltext.fetch_for_articles(arts) if len(arts) > 1 else {}
    items, bstats = textmerge.build_brief(arts, texts=texts, tier_of=verifier.source_tier)
    if not items:
        items = "\n".join(
            f"- [{a['source']}] {a['title']}: {(a['summary'] or '')[:200]}" for a in arts)
    verified = verifier.run(con, ids, items)
    if verified is None:
        return False            # LLM unavailable — leave the row for a later run
    claims, verdicts, score, note = verified
    # `depth` is required by the story prompt since phase 4 — omitting it raises
    # KeyError from str.format, which would take this whole admin job down.
    out = llm.complete_json("story", prompt(
        "story", claims=db.j(claims), items=items,
        depth=depth_hint(bstats, len({a["source"] for a in arts}))))
    if out is None:
        return False
    narrative = out.get("what_happened") or out.get("narrative")
    if not narrative:
        return False            # never re-stamp the placeholder as if it were copy
    # Retelling is how a story written before phase 4 gains beats, so persist
    # them here too — otherwise the one job whose purpose is upgrading old
    # stories would leave them structureless forever.
    beats = clean_beats(out.get("beats"), narrative)
    anchors = clean_anchors(out.get("anchors"), claims, beats)
    if beats:
        narrative = "\n\n".join(b["text"] for b in beats)
    con.execute(
        "UPDATE stories SET headline=?, narrative=?, why_matters=?, credibility=?, "
        "credibility_note=?, claims=?, merge_stats=?, beats=?, anchors=? WHERE id=?",
        (out.get("headline") or row["headline"], narrative,
         out.get("why_it_matters", ""), score, note,
         db.j({"claims": claims, "verdicts": verdicts}),
         db.j(dict(bstats or {}, kinds=verifier.source_breakdown(
             a["source"] for a in arts))),
         db.j(beats) if beats else None,
         db.j(anchors) if anchors else None, row["id"]))
    con.commit()
    return True


#: Articles actually fed to build_brief. It compares facts pairwise, so its cost
#: is quadratic in article count — measured 0.04s at 20 articles but 4.7s at 234,
#: and Render's free CPU is far slower than the machine that was measured on.
#: Stories that kept a large subject would otherwise peg the instance for
#: minutes. The brief is capped by BRIEF_MAX_CHARS and reads only each article's
#: lead anyway, so a diverse dozen loses nothing a reader would notice.
RETELL_MAX_ARTICLES = 12
#: Hard wall-clock ceiling for one retell batch, whatever `limit` was asked for.
RETELL_MAX_SECONDS = float(os.environ.get("RETELL_MAX_SECONDS", "240"))


def _retell_where():
    """SQL predicate matching a placeholder body. Kept in SQL (not Python) so a
    scan never has to pull every story's narrative into memory just to discard
    almost all of them."""
    return ("(credibility_note LIKE 'split from a merged story%' "
            "OR substr(ltrim(narrative), 1, 3) = '- [')")


def _count_retell_pending(con):
    return con.execute(
        f"SELECT COUNT(*) c FROM stories WHERE updated_at > ? AND {_retell_where()}",
        (db.now() - 14 * 86400,)).fetchone()["c"]


def _retell_candidates(con, limit):
    """Placeholder-bodied stories, most visible first.

    Ordered by the same rank the feed uses, so a capped run repairs what readers
    are actually looking at rather than an arbitrary slice. Narrative text is
    deliberately not selected — only the handful of fields ranking needs."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, headline, credibility_note, article_ids, created_at, updated_at "
        f"FROM stories WHERE updated_at > ? AND {_retell_where()}",
        (db.now() - 14 * 86400,)).fetchall()]
    rows = ranking.sort_by_rank(
        rows, impact_of=lambda it: ranking.damped(len(db.uj(it["article_ids"], []))))
    return rows[:limit] if limit else rows


def _split_one_blob(con, row, subjects, now):
    """Apply one story's split and COMMIT it. Committing per-story (instead of one
    transaction for the whole batch) is what makes this safe to run against
    production: a batch covering a dozen-plus mixed stories previously ran as
    ONE uncommitted transaction holding the SQLite WAL writer lock the entire
    time, which is what took the server down when this was first tried. Now a
    kill/restart mid-batch loses at most the one story in flight, not everything
    already split, and every other request can still get a read/write in between."""
    if len(subjects) <= 1:
        key = next(iter(subjects), row["id"])
        con.execute("UPDATE stories SET event_id=? WHERE id=?", (key, row["id"]))
        con.commit()
        return 0  # coherent already — one subject, however many sources
    # Biggest subject keeps the original row: it is the one the existing headline
    # and narrative were actually written about, so bookmarks stay meaningful.
    events = sorted(subjects.items(), key=lambda kv: -len(kv[1]))
    (primary_key, primary_ids), rest = events[0], events[1:]
    # Re-pick the primary's artwork as well: its article set just shrank, so the
    # image it was carrying may belong to an article that moved to a split-off
    # story and would misrepresent what's left.
    primary_arts = []
    for chunk in _chunks(primary_ids):
        primary_arts += [dict(a) for a in con.execute(
            "SELECT * FROM articles WHERE id IN (%s)"
            % ",".join("?" * len(chunk)), chunk).fetchall()]
    # framing=NULL for the same reason the artwork is re-picked: this row's
    # source set just shrank, so a cached spectrum describes outlets that are no
    # longer part of this story. Split-off rows below start NULL anyway.
    con.execute(
        "UPDATE stories SET article_ids=?, event_id=?, updated_at=?, image_url=?, "
        "framing=NULL WHERE id=?",
        (db.j(primary_ids), primary_key, now, images.best_of(primary_arts), row["id"]))
    created = 0
    for key, ids in rest:
        arts = []
        for chunk in _chunks(ids):
            arts += [dict(a) for a in con.execute(
                "SELECT * FROM articles WHERE id IN (%s)"
                % ",".join("?" * len(chunk)), chunk).fetchall()]
        if not arts:
            continue
        lead = min(arts, key=lambda a: a.get("published") or a.get("fetched_at") or now)
        # Prose, with no "- [source]" prefixes and HTML entities unescaped. The
        # bullet form this used to write is the Storyteller's *fallback* string —
        # an LLM input, never finished copy — and shipping it made every split
        # story open with an outlet name that the sources list already shows.
        narrative = _placeholder_narrative(arts)
        con.execute(
            "INSERT INTO stories (id,headline,narrative,why_matters,credibility,"
            "credibility_note,claims,topic,article_ids,trend_ids,connection_ids,"
            "created_at,updated_at,event_id,image_url) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (db.new_id(), lead["title"], narrative, "", row["credibility"],
             PLACEHOLDER_NOTE, "{}",
             lead["topic"], db.j(ids), row["trend_ids"], "[]", now, now, key,
             images.best_of(arts)))
        created += 1
    con.commit()
    return created


@app.post("/admin/split-blob-stories")
def admin_split_blob_stories(min_articles: int = config.STORY_SPLIT_MIN_ARTICLES,
                             dry_run: bool = True,
                             token: str = "", authorization: str = Header("")):
    """Split stories that turned out to cover more than one subject.

    Size is NEVER the trigger. Each story's articles are re-scored against each
    other (textmerge.subject_clusters: shared anchors plus text similarity, with
    ambiguity resolved toward keeping the story together). A story is split only
    when that finds genuinely distinct subjects — so a single event with 200
    corroborating sources stays one story, while a 2-article story welding an AI
    story to an unrelated hardware story is correctly seen as two.

    Splits IN PLACE — nothing is deleted, no bookmarks are touched. The largest
    subject keeps the original story: same id, headline, narrative, bookmarks —
    just trimmed to its own articles. Every other subject becomes a NEW story
    row, mechanically titled off its own lead article (no LLM call here — that
    would mean an LLM call per subject, and production's worst row alone held
    ~225). A mechanically-titled story is a normal story from then on: the next
    time its event gets fresh coverage, the regular pipeline (Storyteller, which
    matches by event_id) UPDATEs it with a real LLM-written headline and
    narrative, same as any other developing story.

    dry_run=true (the default) is synchronous and read-only — safe to call
    anytime. dry_run=false does real writes across (as production showed)
    potentially thousands of rows, so it runs as a BACKGROUND job (same pattern
    as /admin/rebuild-intel): this call returns immediately with started=true;
    poll /admin/usage for a stage='split_blob_stories' row. An earlier version
    did all of this synchronously in one uncommitted transaction, which is what
    made the server stop responding the first time this was run against
    production — see _split_one_blob for the fix."""
    _require_admin(authorization, token)
    if dry_run:
        con = db.connect()
        candidates = _blob_candidates(con, min_articles)
        preview = [{"id": row["id"], "headline": row["headline"],
                    "articles": len(db.uj(row["article_ids"], [])),
                    "splits_into": len(subjects)}
                   for row, subjects in candidates]
        con.close()
        return {"dry_run": True, "matched": len(preview),
                "events_created": sum(p["splits_into"] - 1 for p in preview),
                "stories": preview[:50]}
    if _pipeline_lock.locked():
        return {"started": False,
                "status": "a pipeline run is already in progress; retry shortly"}

    def job():
        if not _pipeline_lock.acquire(blocking=False):
            return
        diag.checkpoint("admin split-blob-stories start")
        try:
            con = db.connect()
            now = db.now()
            candidates = _blob_candidates(con, min_articles)
            diag.checkpoint(f"admin split-blob-stories candidates={len(candidates)}")
            split_ct, created_ct = 0, 0
            for row, subjects in candidates:
                created = _split_one_blob(con, row, subjects, now)
                if created:
                    split_ct += 1
                    created_ct += created
            db.log_run(con, "split_blob_stories", "ok",
                       f"split {split_ct} multi-subject stories into "
                       f"{created_ct} new single-subject stories")
            con.close()
        except Exception as e:  # noqa: BLE001 — log, don't crash the thread silently
            con2 = db.connect()
            db.log_run(con2, "split_blob_stories", "error", str(e))
            con2.close()
        finally:
            # `candidates` alone can be hundreds of (row, subjects) tuples on a
            # blob-heavy DB — reclaim it before the next request runs.
            gc.collect()
            diag.checkpoint("admin split-blob-stories done")
            _pipeline_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return {"started": True,
            "status": "splitting in background — check /admin/usage for "
                     "stage='split_blob_stories'"}


@app.post("/admin/retell-stories")
def admin_retell_stories(limit: int = 8, dry_run: bool = True,
                         token: str = "", authorization: str = Header("")):
    """Rewrite stories whose body is placeholder text instead of written copy.

    The story splitter creates a row per subject but cannot write prose for it,
    so those rows carry a stand-in body until something rewrites them. This is
    that step: each story is re-told through the ordinary pipeline path (merge
    the member articles into an attributed brief, verify the claims, then have
    the model write headline / what happened / why it matters), leaving a result
    indistinguishable from a normally-produced story.

    Costs LLM budget — roughly two calls per story — so it is capped by `limit`
    and processes the highest-ranked stories first: a capped run repairs what
    readers actually see. It is resumable, so calling it repeatedly works through
    the backlog; a story is only marked done once real copy was written, and any
    story the model could not write is simply left for the next run.

    dry_run=true (default) just reports how many stories need repair. Runs in the
    background like the other bulk actions; poll /admin/usage for
    stage='retell_stories'. Ranking timestamps are never touched, so repaired
    stories keep their place in the feed instead of resurfacing as new."""
    _require_admin(authorization, token)
    if dry_run:
        con = db.connect()
        total = _count_retell_pending(con)          # COUNT(*), not a full load
        preview = [{"id": r["id"], "headline": r["headline"],
                    "articles": len(db.uj(r["article_ids"], []))}
                   for r in _retell_candidates(con, 25)]
        con.close()
        return {"dry_run": True, "pending": total,
                "would_repair_now": min(total, limit),
                "stories": preview}
    if _pipeline_lock.locked():
        return {"started": False,
                "status": "a pipeline run is already in progress; retry shortly"}

    def job():
        if not _pipeline_lock.acquire(blocking=False):
            return
        llm.set_context(f"admin retell-stories limit={limit}")
        diag.checkpoint("admin retell-stories start")
        try:
            con2 = db.connect()
            verifier = Verifier()
            done = failed = 0
            # Wall-clock budget as well as a count cap. Each story costs an LLM
            # round trip, up to 4 publisher fetches and a quadratic fact merge,
            # and this thread holds the pipeline lock throughout — so a slow
            # batch must stop on its own rather than tie the instance up.
            deadline = time.time() + RETELL_MAX_SECONDS
            stopped_early = False
            for row in _retell_candidates(con2, limit):
                if time.time() > deadline:
                    stopped_early = True
                    break
                try:
                    if _retell_story(con2, row, verifier):
                        done += 1
                    else:
                        failed += 1
                except Exception:  # noqa: BLE001 — one bad story must not stop the batch
                    failed += 1
            left = _count_retell_pending(con2)
            db.log_run(con2, "retell_stories", "ok",
                       f"rewrote {done} stories ({failed} skipped, {left} still pending"
                       + (", stopped at time budget)" if stopped_early else ")"))
            con2.close()
        except Exception as e:  # noqa: BLE001
            con3 = db.connect()
            db.log_run(con3, "retell_stories", "error", str(e)[:300])
            con3.close()
        finally:
            # Each story here pulled in up to 4 full publisher pages (fulltext)
            # plus a merged brief — reclaim that before the next request runs.
            gc.collect()
            diag.checkpoint("admin retell-stories done")
            llm.set_context("")
            _pipeline_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return {"started": True,
            "status": f"rewriting up to {limit} stories in background — check "
                      f"/admin/usage for stage='retell_stories'"}


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
    # Two different questions, so two different blocks. `session_llm_usage` is
    # what THIS process has spent since it booted — it answers "is the run I
    # just triggered doing anything" and resets on every restart.
    # `llm_totals` comes out of the llm_usage table and is the one that
    # survives: all-time and today's calls, tokens and cost. Cheap to read —
    # that table holds one row per model/task per day, not one per call.
    llm_totals = llmcost.totals(con)
    cache_stats = llmcache.stats(con)
    vocab = gazetteer.stats(con)
    con.close()
    return {"session_llm_usage": llm.usage,
            "llm_totals": llm_totals,
            "users": {"total": total_users, "google_signed_up": google_users},
            "provider_status": llm.provider_status(),
            "routing": llm.routing_status(),
            "llm_cache": cache_stats,
            "gazetteer": vocab,
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


@app.get("/admin/reports/llm")
def admin_report_llm(days: int = 30, token: str = "",
                     authorization: str = Header("")):
    """LLM consumption and spend: calls, tokens and cost broken down by
    provider, by model and by pipeline task, with per-call averages.

    Reads the durable `llm_usage` table, not the in-process counters, so the
    numbers cover the whole window regardless of how many times the service has
    restarted inside it."""
    _require_admin(authorization, token)
    con = db.connect()
    out = llmcost.report(con, days=_report_days(days))
    con.close()
    return out


@app.post("/admin/llm-reprice")
def admin_llm_reprice(days: int = 0, token: str = "",
                      authorization: str = Header("")):
    """Recompute stored costs from the CURRENT rate card. days=0 = all history.

    Cost is derived from a price that lives in configuration, so it is stored at
    the rate in force when the call was made. That is right until the rate
    itself was wrong — which it is by default for any model with no seeded
    price, and whenever a rate is first configured after spend has already been
    recorded. The token counts are the durable record and are never touched, so
    the money can always be rebuilt from them.

    Runs inline: this walks one small aggregate table, not a row per call."""
    _require_admin(authorization, token)
    con = db.connect()
    changed = llmcost.reprice(con, days=max(0, int(days or 0)))
    con.close()
    return {"repriced": changed,
            "scope": f"last {days} days" if days else "all history",
            "status": f"recomputed cost on {changed} row(s) "
                      f"from the current rate card"}


@app.get("/admin/reports/traffic")
def admin_report_traffic(days: int = 30, token: str = "",
                         authorization: str = Header("")):
    """Overall request volume across every API route, not just page views."""
    _require_admin(authorization, token)
    con = db.connect()
    out = analytics.traffic_report(con, days=_report_days(days))
    con.close()
    return out


# ---------------------------------------------------------------- finance
# Read surface for the finance pipeline. Entirely additive: separate paths over
# separate tables, so nothing an existing client calls changes shape or timing.
# Neither the iOS app nor the web portal reads these yet — the pipeline can run
# and be inspected before anything is surfaced to a reader.
def _fin_story_row(r, full=False):
    """Wire shape for a finance story. The heavy structures (entity table,
    metric table, actor sentiment) are only sent on the detail endpoint —
    inlining them in a list is how /signals grew to 107KB."""
    out = {"id": r["id"], "headline": r["headline"], "event_type": r["event_type"],
           "topic": r["topic"], "credibility": r["credibility"],
           "sectors": db.uj(r["sectors"], []), "tickers": db.uj(r["tickers"], []),
           "sentiment_net": r["sentiment_net"],
           "sentiment_dispersion": r["sentiment_dispersion"],
           "image_url": r["image_url"], "created_at": r["created_at"],
           "updated_at": r["updated_at"]}
    if not full:
        out["narrative"] = (r["narrative"] or "")[:280]
        out["metric_count"] = len(db.uj(r["metrics"], []))
        return out
    out.update({
        "narrative": r["narrative"], "why_matters": r["why_matters"],
        "credibility_note": r["credibility_note"], "claims": db.uj(r["claims"], {}),
        "article_ids": db.uj(r["article_ids"], []), "sources": db.uj(r["sources"], []),
        "geographies": db.uj(r["geographies"], []),
        "entities": db.uj(r["entities"], {}), "metrics": db.uj(r["metrics"], []),
        "sentiment": db.uj(r["sentiment"], {}),
        "economic_drivers": db.uj(r["economic_drivers"], []),
        "beats": db.uj(r["beats"], None), "anchors": db.uj(r["anchors"], None),
        "merge_stats": db.uj(r["merge_stats"], {}),
        "unresolved": db.uj(r["unresolved"], []),
        "schema_version": r["schema_version"]})
    return out


@app.get("/finance/stories", tags=["finance"], summary="Finance stories",
         responses={200: {"model": wire.FinanceStoryList}})
def finance_stories(limit: int = 30, event_type: str = ""):
    """Most recently updated first. `limit` is clamped to 100.

    `narrative` is truncated to 280 chars and the metric/entity/sentiment tables
    are omitted — fetch `/finance/story/{id}` for those. Filter with
    `event_type` (earnings, m_and_a, regulation, ...).
    """
    con = db.connect()
    limit = max(1, min(int(limit or 30), 100))
    if event_type:
        rows = con.execute(
            "SELECT * FROM fin_stories WHERE event_type=? "
            "ORDER BY updated_at DESC LIMIT ?", (event_type, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM fin_stories ORDER BY updated_at DESC "
                           "LIMIT ?", (limit,)).fetchall()
    out = [_fin_story_row(r) for r in rows]
    con.close()
    return {"stories": out, "count": len(out)}


@app.get("/finance/story/{story_id}", tags=["finance"],
         summary="One finance story, in full",
         responses={200: {"model": wire.FinanceStoryDetail},
                    404: {"description": "No finance story with that id."}})
def finance_story(story_id: str):
    """The full record: extracted `metrics`, the `entities` table with resolved
    tickers, per-actor `sentiment`, and the graph `relationships` this story
    established.

    `entities.unresolved` names things the reporting mentioned that we could not
    resolve — surface them as unlinked text rather than dropping them.
    """
    con = db.connect()
    r = con.execute("SELECT * FROM fin_stories WHERE id=?", (story_id,)).fetchone()
    if not r:
        con.close()
        raise HTTPException(404, "finance story not found")
    out = _fin_story_row(r, full=True)
    # The relationships THIS story established. They live in the graph rather
    # than on the story row (one edge, many stories), so they are read back by
    # story id. A LIKE over a JSON column is a scan — acceptable only because
    # fin_kg_edges is bounded by FIN_KG_RETAIN_DAYS and this is a single-story
    # endpoint. If it ever shows up in a profile, denormalise instead of
    # indexing a JSON column.
    # Node ids are canonical (a ticker symbol where one resolved), which is what
    # makes the graph joinable — and exactly what a reader should not be shown.
    # fin_kg_nodes keeps the name as the article wrote it, so the edge travels
    # with both: the id for anything that has to match, the name to print.
    names = {r["id"]: r["name"] for r in con.execute(
        "SELECT id, name FROM fin_kg_nodes WHERE namespace='finance'").fetchall()}
    out["relationships"] = [
        {"subject": e["subject"], "predicate": e["predicate"], "object": e["object"],
         "subject_name": names.get(e["subject"], e["subject"]),
         "object_name": names.get(e["object"], e["object"]),
         "subject_type": e["subject_type"], "object_type": e["object_type"],
         "event_type": e["event_type"], "confidence": e["confidence"]}
        for e in con.execute(
            "SELECT * FROM fin_kg_edges WHERE namespace='finance' AND story_ids LIKE ? "
            "ORDER BY confidence DESC LIMIT 12", (f'%"{story_id}"%',)).fetchall()]
    con.close()
    return out


@app.get("/finance/trends", tags=["finance"], summary="Cross-story financial forces",
         responses={200: {"model": wire.FinanceTrendList}})
def finance_trends(limit: int = 20):
    """Forces spanning several stories, highest confidence first. Retired trends
    are excluded. `limit` is clamped to 60.

    `cascade` is the second-order impact map — who else this reaches and by what
    mechanism — computed over stored relationships, not asked of a model.
    `arc` is the stages the force has moved through; `velocity` is how fast
    evidence is accruing.
    """
    con = db.connect()
    rows = con.execute(
        "SELECT * FROM fin_trends WHERE retired_at IS NULL "
        "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
        (max(1, min(int(limit or 20), 60)),)).fetchall()
    out = [{"id": r["id"], "name": r["name"], "narrative": r["narrative"],
            "arc": db.uj(r["arc"], []), "cascade": db.uj(r["cascade"], []),
            "story_ids": db.uj(r["story_ids"], []),
            "sectors": db.uj(r["sectors"], []), "tickers": db.uj(r["tickers"], []),
            "macro_factors": db.uj(r["macro_factors"], []),
            "window_days": r["window_days"], "velocity": r["velocity"],
            "confidence": r["confidence"], "created_at": r["created_at"],
            "updated_at": r["updated_at"]} for r in rows]
    con.close()
    return {"trends": out, "count": len(out)}


@app.get("/finance/forecasts", tags=["finance"], summary="Scenario forecasts",
         responses={200: {"model": wire.FinanceForecastList}})
def finance_forecasts(limit: int = 20):
    """Directional scenario analysis built on the trends. `limit` clamped to 60.

    Always exactly three `scenarios` — base, bull, bear — with probabilities
    summing to 1. `invalidation` lists the observables that would falsify the
    thesis, and is the most useful part to render.

    **`disclaimer` must be displayed wherever the forecast is.** It travels on
    every forecast rather than being assembled client-side precisely so it
    cannot be separated from the content. These are not investment advice.
    """
    con = db.connect()
    rows = con.execute(
        "SELECT * FROM fin_forecasts WHERE retired_at IS NULL "
        "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
        (max(1, min(int(limit or 20), 60)),)).fetchall()
    out = [{"id": r["id"], "title": r["title"],
            "trend_ids": db.uj(r["trend_ids"], []),
            "story_ids": db.uj(r["story_ids"], []),
            "scenarios": db.uj(r["scenarios"], []),
            "short_term": r["short_term"], "long_term": r["long_term"],
            "risks": db.uj(r["risks"], []),
            "dependencies": db.uj(r["dependencies"], []),
            "invalidation": db.uj(r["invalidation"], []),
            "confidence": r["confidence"],
            # Sent on every forecast, never assembled by the client: the
            # not-advice framing has to travel with the content wherever it
            # is rendered.
            "disclaimer": r["disclaimer"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}
           for r in rows]
    con.close()
    return {"forecasts": out, "count": len(out)}


@app.get("/finance/graph", tags=["finance"], summary="Knowledge graph",
         responses={200: {"model": wire.FinanceGraphResponse}})
def finance_graph(entity: str = "", hops: int = 2, limit: int = 40):
    """The knowledge graph: overall shape, or the cascade around one entity.

    **Two response shapes behind one path.** Without `entity`, the overview:
    `stats`, `top` entities by mention count, and the highest-confidence
    `links`. With `entity`, a breadth-first walk out from it: `resolved_to`
    reports what the name resolved to, and each link's `order` is hop depth.

    Confidence decays with distance — a three-hop chain asserted as firmly as a
    direct one would be dishonest, since every hop is another relationship that
    has to hold. `hops` is clamped to 4, `limit` to 100.
    """
    con = db.connect()
    try:
        if not entity:
            stats = fin_kg.stats(con)
            top = fin_kg.top_entities(con, limit=min(int(limit or 40), 100))
            rows = con.execute(
                "SELECT subject AS from_entity, object AS to_entity, predicate AS mechanism, "
                "confidence, 1 AS [order] FROM fin_kg_edges WHERE namespace='finance' "
                "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
                (min(int(limit or 60), 120),)).fetchall()
            edges = [dict(r) for r in rows]
            return {"stats": stats, "top": top, "links": edges}
        seed = fin_kg.tk.canonical(entity)
        links = fin_kg.cascade(con, [seed], max_hops=max(1, min(int(hops or 2), 4)),
                               max_links=max(1, min(int(limit or 40), 100)))
        return {"entity": entity, "resolved_to": seed,
                "links": [l.model_dump(mode="json") for l in links],
                "count": len(links)}
    finally:
        con.close()


@app.get("/finance/causal/chains", tags=["finance"],
         summary="Cause-and-effect transmission chains",
         responses={200: {"model": wire.CausalChainList}})
def finance_causal_chains(domain: str = ""):
    """How a catalyst propagates to a terminal outcome, step by step.

    Each chain carries its `steps` (with an `elasticity_score` per step),
    the `dampeners` that absorb the shock, a `historical_precedent`, and
    `corroborating_story_ids` pointing at live reporting — resolve those via
    `GET /story/{id}`.

    Filter with `domain`, matched against catalyst domain or transmission
    channel. Feed a chain's `id` to `/finance/causal/simulate` as `shock_id`.
    """
    con = db.connect()
    try:
        return {"chains": fin_causal.list_causal_chains(con, domain=domain)}
    finally:
        con.close()


@app.get("/finance/causal/simulate", tags=["finance"],
         summary="Simulate a shock through a chain",
         responses={200: {"model": wire.CausalSimulation}})
def finance_causal_simulate(shock_id: str = "chain-monetary-policy-lending", intensity: float = 0.0, horizon: str = ""):
    """Counterfactual: what a shock of `intensity` percent does to one chain.

    `shock_id` is a chain id from `/finance/causal/chains`. Returns the
    probability shift against `base_probability`, per-step `computed_impact_pct`
    and `direction`, and `ticker_impacts` keyed by symbol.

    Directional only — an exposure tier and a transmission story, never a price
    or a target. An unknown `shock_id` answers 200 with an `error` key.
    """
    return fin_causal.simulate_counterfactual_shock(shock_id=shock_id, intensity_pct=intensity, horizon_override=horizon)


@app.get("/finance/graph/{entity_name}/stories", tags=["finance"],
         summary="Stories mentioning an entity",
         responses={200: {"model": wire.FinanceEntityStories}})
def finance_entity_stories(entity_name: str, limit: int = 10):
    """Stories mentioning a specific entity in the knowledge graph.

    Matches finance stories by entity name or resolved ticker, falling back to
    the general news catalogue when the finance pipeline has nothing. `limit` is
    clamped to 50.

    Failures answer **200 with an `error` key**, not a 4xx — check for it.
    """
    con = db.connect()
    try:
        canonical_id = fin_kg.tk.canonical(entity_name)
        limit = max(1, min(int(limit or 10), 50))

        # Find stories that mention this entity. First try by canonical ID (ticker),
        # then by entity name.
        search_pattern = f"%{entity_name}%"
        search_ticker_pattern = f"%{canonical_id}%"

        rows = con.execute(
            "SELECT * FROM fin_stories "
            "WHERE entities LIKE ? OR tickers LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (search_pattern, search_ticker_pattern, limit)).fetchall()

        out = [_fin_story_row(r) for r in rows]

        # Fallback to main stories table if fin_stories is empty
        if not out:
            main_rows = con.execute(
                "SELECT * FROM stories "
                "WHERE headline LIKE ? OR narrative LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (search_pattern, search_pattern, limit)).fetchall()
            out = [{
                "id": r["id"], "headline": r["headline"],
                "narrative": r["narrative"], "credibility": r["credibility"],
                "credibility_note": r["credibility_note"],
                "created_at": r["created_at"]
            } for r in main_rows]

        return {"entity": entity_name, "resolved_to": canonical_id,
                "stories": out, "count": len(out)}
    except Exception as e:
        return {"entity": entity_name, "stories": [], "count": 0, "error": str(e)}
    finally:
        con.close()


@app.post("/admin/run-finance")
def admin_run_finance(stage: str = "", token: str = "",
                      authorization: str = Header("")):
    """Kick off a finance pipeline run in the background.
      /admin/run-finance?stage=fin_stories    extraction + KG only
      /admin/run-finance?stage=fin_trends     cascade linking only
      /admin/run-finance?stage=fin_forecasts  scenarios only
    Poll /admin/usage — recent_runs carries stage='finance_pipeline', status='done'."""
    _require_admin(authorization, token)
    if stage and stage not in FIN_STAGES:
        raise HTTPException(400, f"unknown stage '{stage}'; valid: {FIN_STAGES}")
    if _pipeline_lock.locked():
        return {"started": False, "status": "a pipeline run is already in progress",
                "check": "GET /admin/usage"}
    threading.Thread(target=guarded_finance_run, args=(stage or None,),
                     daemon=True).start()
    return {"started": True, "stage": stage or "all", "status": "running in background",
            "check": "GET /admin/usage — a recent_runs row with "
                     "stage='finance_pipeline', status='done' marks completion"}


@app.get("/admin/finance/health")
def admin_finance_health(token: str = "", authorization: str = Header("")):
    """Is the finance pipeline actually running, and if not, why not.

    Separate from /admin/usage on purpose: recent_runs is one LIMIT 30 list
    shared with the news pipeline, which logs ~10 rows per run four times a day
    — so a finance run that happened this morning is already off the bottom by
    lunchtime, and one that never happened looks exactly the same."""
    _require_admin(authorization, token)
    con = db.connect()
    try:
        return finance_health(con)
    finally:
        con.close()


@app.get("/admin/finance/unresolved")
def admin_finance_unresolved(token: str = "", authorization: str = Header("")):
    """Company names the pipeline saw but tickers.yaml could not resolve — the
    work queue for extending the map."""
    _require_admin(authorization, token)
    con = db.connect()
    rows = unresolved_report(con)
    con.close()
    return {"unresolved": [{"name": n, "seen": c} for n, c in rows],
            "count": len(rows)}
