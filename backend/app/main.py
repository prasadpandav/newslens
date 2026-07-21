"""FastAPI app: the API the iOS client talks to."""
import os
import secrets
import threading
from datetime import datetime, timedelta
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from . import config, db, llm, live
from .agents import prompt, _dedupe_trends, linkify, story_refs
from .orchestrator import run_pipeline, STAGES

app = FastAPI(title="Descry API", version="0.1")
# Browser origin allowlist — ALLOWED_ORIGINS env, default * for the beta portal.
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])
scheduler = BackgroundScheduler()


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
    scheduler.start()


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


def _auth(con, user_id, authorization):
    row = con.execute("SELECT token FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "user not found")
    if authorization != f"Bearer {row['token']}":
        raise HTTPException(401, "bad token")


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
    # Ordering: default "recent" keeps the feed chronological so ALL news stays
    # accessible and nothing is buried by preferences (only-some stories get an
    # impact score, so impact-ordering would sink everything else). sort=foryou
    # opts into a personalized ranking (impact first) for those who want it.
    order = ("COALESCE(f.impact_score, 0) DESC, s.created_at DESC"
             if sort == "foryou" else "s.created_at DESC")
    # `since` (epoch) returns only stories newer than the client's newest — the
    # cheap incremental fetch behind the "N new stories" banner. created_at is
    # exposed so the client can diff/merge without a second call.
    floor = max(since, db.now() - 7 * 86400)
    rows = con.execute(
        f"""SELECT s.id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic, s.created_at,
                  COALESCE(f.impact_text, '')  AS impact_text,
                  COALESCE(f.impact_score, 0)  AS impact_score
           FROM stories s
           LEFT JOIN feed_items f ON f.story_id = s.id AND f.user_id = ?
           WHERE s.created_at > ?
           ORDER BY {order}
           LIMIT 100""",
        (user_id, floor)).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


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
        "SELECT id,kind,name,narrative,velocity FROM trends WHERE id IN (%s)" %
        ",".join("?" * len(db.uj(s["trend_ids"], []))),
        db.uj(s["trend_ids"], [])).fetchall()] if db.uj(s["trend_ids"], []) else []
    conns = []
    for cid in db.uj(s["connection_ids"], []):
        c = con.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if c:
            other = c["article_b"] if c["article_a"] in art_ids else c["article_a"]
            oa = con.execute("SELECT title,url FROM articles WHERE id=?", (other,)).fetchone()
            conns.append({"chain": c["chain"], "confidence": c["confidence"],
                          "other_title": oa["title"] if oa else "",
                          "other_url": oa["url"] if oa else ""})
    fi = con.execute("SELECT impact_text,impact_score FROM feed_items "
                     "WHERE user_id=? AND story_id=?", (user_id, story_id)).fetchone()
    con.close()
    return {"id": s["id"], "headline": s["headline"], "narrative": s["narrative"],
            "credibility": s["credibility"], "credibility_note": s["credibility_note"],
            "claims": db.uj(s["claims"]), "topic": s["topic"], "sources": articles,
            "trends": trends, "connections": conns,
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
    rows = con.execute(
        "SELECT id, headline, narrative, credibility, credibility_note, topic, "
        "created_at FROM stories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/trends")
def trends():
    # Ranked per kind: macro velocity is an article count while micro velocity is
    # a small ratio, so one mixed velocity sort would push every micro trend
    # (the portal's "Early signals" tab) out of a shared limit.
    con = db.connect()
    q = ("SELECT id, kind, name, narrative, sectors, regions, article_ids, velocity, "
         "created_at FROM trends WHERE kind=? "
         "ORDER BY velocity DESC, created_at DESC LIMIT ?")
    rows = (con.execute(q, ("macro", 40)).fetchall()
            + con.execute(q, ("micro", 20)).fetchall())
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["sectors"] = db.uj(r["sectors"], [])
        d["regions"] = db.uj(r["regions"], [])
        d["article_count"] = len(db.uj(r["article_ids"], []))
        del d["article_ids"]
        out.append(d)
    return {"items": out}


@app.get("/signals")
def signals():
    """Foresight signals: cross-domain predictions with their supporting stories.
    Raw 12-hex story ids the model cited inline are rewritten to the story's
    headline (linkify), and story_refs lets clients make those spans tappable."""
    con = db.connect()
    out = []
    for g in con.execute(
            "SELECT * FROM signals ORDER BY confidence DESC, created_at DESC").fetchall():
        stories, id_head = [], {}
        for sid in db.uj(g["story_ids"], []):
            s = con.execute(
                "SELECT id, headline, narrative, credibility, credibility_note, topic "
                "FROM stories WHERE id=?", (sid,)).fetchone()
            if s:
                stories.append(dict(s))
                id_head[sid] = s["headline"]
        out.append({"id": g["id"],
                    "title": linkify(g["title"], id_head),
                    "prediction": linkify(g["prediction"], id_head),
                    "chain": linkify(g["chain"], id_head),
                    "watch": linkify(g["watch"], id_head),
                    "affected": db.uj(g["affected"], []), "horizon": g["horizon"],
                    "confidence": g["confidence"], "stories": stories,
                    "story_refs": story_refs(id_head)})
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
    return {"id": t["id"], "kind": t["kind"], "name": t["name"],
            "narrative": linkify(t["narrative"], id_head),
            "sectors": db.uj(t["sectors"], []),
            "regions": db.uj(t["regions"], []), "velocity": t["velocity"],
            "stories": stories, "story_refs": story_refs(id_head)}


@app.get("/search")
def search(q: str):
    """Simple LIKE search over stories and trends. Upgrade path: embeddings."""
    like = f"%{q.strip()}%"
    con = db.connect()
    story_rows = con.execute(
        "SELECT id, headline, narrative, credibility, topic FROM stories "
        "WHERE headline LIKE ? OR narrative LIKE ? OR topic LIKE ? "
        "ORDER BY created_at DESC LIMIT 15", (like, like, like)).fetchall()
    trend_rows = con.execute(
        "SELECT id, kind, name, narrative, velocity FROM trends "
        "WHERE name LIKE ? OR narrative LIKE ? "
        "ORDER BY created_at DESC LIMIT 10", (like, like)).fetchall()
    con.close()
    return {"stories": [dict(r) for r in story_rows],
            "trends": [dict(r) for r in trend_rows]}


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
    con.close()
    return {"session_llm_usage": llm.usage,
            "provider_status": llm.provider_status(),
            "provider_events": list(llm.provider_events),
            "recent_errors": list(llm.recent_errors),
            "recent_runs": runs}
