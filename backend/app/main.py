"""FastAPI app: the API the iOS client talks to."""
import secrets
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


@app.on_event("startup")
def _start():
    scheduler.add_job(run_pipeline, "interval",
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


@app.post("/users")
def create_user():
    con = db.connect()
    uid, token = db.new_id(), secrets.token_hex(16)
    con.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, token, "{}", db.now()))
    con.commit(); con.close()
    return {"user_id": uid, "token": token}


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
    return run_pipeline()


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
