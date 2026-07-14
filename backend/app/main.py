"""FastAPI app: the API the iOS client talks to."""
import secrets
import threading
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from . import config, db, llm
from .agents import prompt
from .orchestrator import run_pipeline

app = FastAPI(title="NewsLens API", version="0.1")
# Allow the web portal (any origin) to call this API. Tighten for production.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
scheduler = BackgroundScheduler()

# One pipeline at a time — shared guard for scheduled AND manual runs.
_pipeline_lock = threading.Lock()


def guarded_run():
    if not _pipeline_lock.acquire(blocking=False):
        return None  # a run is already in progress; skip
    try:
        return run_pipeline()
    finally:
        _pipeline_lock.release()


@app.on_event("startup")
def _start():
    scheduler.add_job(guarded_run, "interval",
                      hours=config.PIPELINE_INTERVAL_HOURS,
                      id="pipeline", replace_existing=True)
    scheduler.start()


class ContextIn(BaseModel):
    interests: list[str] = []
    profession: str = ""
    line_of_business: str = ""
    role_seniority: str = ""
    location: dict = {}
    native_language: str = ""
    preferred_language: str = "English"
    micro: dict = {}


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
    client_id = __import__("os").environ.get("GOOGLE_CLIENT_ID", "")
    if client_id and info.get("aud") != client_id:
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
def feed(user_id: str, authorization: str = Header("")):
    con = db.connect()
    _auth(con, user_id, authorization)
    # LEFT JOIN: every recent story appears in the feed; personalization
    # (impact text/score) enriches stories where the Personalizer has run,
    # but never gates visibility — otherwise rate-limited personalization
    # would silently shrink the feed.
    rows = con.execute(
        """SELECT s.id, s.headline, s.narrative, s.credibility, s.credibility_note,
                  s.topic,
                  COALESCE(f.impact_text, '')  AS impact_text,
                  COALESCE(f.impact_score, 0)  AS impact_score
           FROM stories s
           LEFT JOIN feed_items f ON f.story_id = s.id AND f.user_id = ?
           WHERE s.created_at > ?
           ORDER BY COALESCE(f.impact_score, 0) DESC, s.created_at DESC
           LIMIT 100""",
        (user_id, db.now() - 7 * 86400)).fetchall()
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
    """Public recent stories — powers the portal before personalization."""
    con = db.connect()
    rows = con.execute(
        "SELECT id, headline, narrative, credibility, credibility_note, topic, "
        "created_at FROM stories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/trends")
def trends():
    con = db.connect()
    rows = con.execute(
        "SELECT id, kind, name, narrative, sectors, regions, article_ids, velocity, "
        "created_at FROM trends ORDER BY velocity DESC, created_at DESC LIMIT 60").fetchall()
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
    """Foresight signals: cross-domain predictions with their supporting stories."""
    con = db.connect()
    out = []
    for g in con.execute(
            "SELECT * FROM signals ORDER BY confidence DESC, created_at DESC").fetchall():
        stories = []
        for sid in db.uj(g["story_ids"], []):
            s = con.execute(
                "SELECT id, headline, narrative, credibility, credibility_note, topic "
                "FROM stories WHERE id=?", (sid,)).fetchone()
            if s:
                stories.append(dict(s))
        out.append({"id": g["id"], "title": g["title"], "prediction": g["prediction"],
                    "chain": g["chain"], "watch": g["watch"],
                    "affected": db.uj(g["affected"], []), "horizon": g["horizon"],
                    "confidence": g["confidence"], "stories": stories})
    con.close()
    return {"items": out}


@app.get("/trend/{trend_id}")
def trend_detail(trend_id: str):
    """A trend plus the stories built from its member articles — powers deep-dive."""
    con = db.connect()
    t = con.execute("SELECT * FROM trends WHERE id=?", (trend_id,)).fetchone()
    if not t:
        con.close()
        raise HTTPException(404, "trend not found")
    member_ids = set(db.uj(t["article_ids"], []))
    stories = []
    for s in con.execute(
            "SELECT id, headline, narrative, credibility, credibility_note, topic, "
            "article_ids FROM stories ORDER BY created_at DESC LIMIT 200").fetchall():
        if member_ids & set(db.uj(s["article_ids"], [])):
            d = dict(s)
            del d["article_ids"]
            stories.append(d)
    con.close()
    return {"id": t["id"], "kind": t["kind"], "name": t["name"],
            "narrative": t["narrative"], "sectors": db.uj(t["sectors"], []),
            "regions": db.uj(t["regions"], []), "velocity": t["velocity"],
            "stories": stories}


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
def ask(body: AskIn):
    """Ask-AI: question about a story (or general). Mock mode gives canned answers."""
    con = db.connect()
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
def admin_run():
    """Kick off a pipeline run in the background and return immediately.
    (A synchronous run outlives proxy timeouts — the connection resets even
    though the pipeline keeps running.) Poll /admin/usage for completion."""
    if _pipeline_lock.locked():
        return {"started": False, "status": "a pipeline run is already in progress",
                "check": "GET /admin/usage — look for stage='pipeline', status='done'"}
    threading.Thread(target=guarded_run, daemon=True).start()
    return {"started": True, "status": "running in background",
            "check": "GET /admin/usage — a new recent_runs row with "
                     "stage='pipeline', status='done' marks completion"}


@app.get("/admin/usage")
def admin_usage():
    con = db.connect()
    runs = [dict(r) for r in con.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT 30").fetchall()]
    con.close()
    return {"session_llm_usage": llm.usage,
            "provider_status": llm.provider_status(),
            "provider_events": list(llm.provider_events),
            "recent_errors": list(llm.recent_errors),
            "recent_runs": runs}
