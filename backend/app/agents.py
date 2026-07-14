"""All NewsLens agents.

Each agent is a small class with .run(con) that reads and writes SQLite.
Prompts come from prompts.yaml. The LLM client handles mock/real providers.
"""
import json
import math
import re
import time
import yaml
from . import config, db, llm

PROMPTS = yaml.safe_load(config.PROMPTS_FILE.read_text())


def prompt(name, **kw):
    return PROMPTS[name].format(**kw)


# ------------------------------------------------------------- similarity
def _tokens(text):
    stop = set("the a an of to in on for and or is are was were with from by at as "
               "it its this that new says said will would could more amid".split())
    return [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in stop]


def _tf(text):
    v = {}
    for t in _tokens(text):
        v[t] = v.get(t, 0) + 1
    return v


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b.get(k, 0) for k in a)
    na = math.sqrt(sum(x * x for x in a.values()))
    nb = math.sqrt(sum(x * x for x in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def greedy_cluster(items, threshold):
    """items: list of (id, text). Returns list of lists of ids."""
    vecs = [(i, _tf(t)) for i, t in items]
    clusters = []
    for i, v in vecs:
        placed = False
        for c in clusters:
            if cosine(v, c["centroid"]) >= threshold:
                c["ids"].append(i)
                for k, n in v.items():
                    c["centroid"][k] = c["centroid"].get(k, 0) + n
                placed = True
                break
        if not placed:
            clusters.append({"ids": [i], "centroid": dict(v)})
    return [c["ids"] for c in clusters]


# ------------------------------------------------------------------ Scout
class Scout:
    """Fetches news via RSS (primary). Falls back to bundled sample articles
    so the pipeline always has data (e.g. offline / first run)."""

    def run(self, con, topics=None):
        added = 0
        try:
            added = self._fetch_rss(con, topics)
        except Exception as e:  # noqa: BLE001
            db.log_run(con, "scout", "error", f"rss failed: {e}")
        # Bundled sample articles are for first-boot demos ONLY: load them just
        # when the database has no articles at all. "0 new articles" on a
        # populated DB simply means the feeds had nothing new — that's normal.
        if added == 0 and con.execute(
                "SELECT COUNT(*) c FROM articles").fetchone()["c"] == 0:
            added = self._load_samples(con)
        db.log_run(con, "scout", "ok", f"{added} new articles")
        return added

    def _is_same_source_dup(self, con, source, title, threshold=0.85):
        """True if this source already gave us a near-identical title recently.
        Same/similar stories from *different* sources are kept — they feed
        corroboration and trends. Repeats from the SAME source (new tracking
        URLs, minor headline edits) are skipped so they aren't reprocessed."""
        vec = _tf(title)
        rows = con.execute(
            "SELECT title FROM articles WHERE source=? AND fetched_at > ?",
            (source, db.now() - 7 * 86400)).fetchall()
        return any(cosine(vec, _tf(r["title"])) >= threshold for r in rows)

    def _fetch_rss(self, con, topics):
        import feedparser
        feeds = yaml.safe_load(config.FEEDS_FILE.read_text())
        added = 0
        skipped_dups = 0
        for topic, urls in feeds.items():
            if topics and topic not in topics:
                continue
            for url in urls:
                try:
                    # Fetch with httpx (bundled certs + custom UA), parse the bytes.
                    # feedparser's own fetching hits SSL/UA issues on some systems.
                    import httpx
                    resp = httpx.get(url, timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "NewsLensBot/0.1 (+beta)"})
                    parsed = feedparser.parse(resp.content)
                except Exception:
                    continue
                for e in parsed.entries[:15]:
                    link = getattr(e, "link", "")
                    title = getattr(e, "title", "")
                    if not link or not title:
                        continue
                    summary = re.sub(r"<[^>]+>", " ", getattr(e, "summary", ""))[:600]
                    source = re.sub(r"^www\.", "", re.sub(r"https?://([^/]+).*", r"\1", link))
                    if self._is_same_source_dup(con, source, title):
                        skipped_dups += 1
                        continue
                    pub = time.time()
                    try:
                        con.execute(
                            "INSERT INTO articles (id,url,title,summary,source,topic,"
                            "published,entities,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (db.new_id(), link, title, summary, source, topic,
                             pub, "", db.now()))
                        added += 1
                    except Exception:
                        pass  # duplicate url
        con.commit()
        if skipped_dups:
            db.log_run(con, "scout_dedup", "ok",
                       f"{skipped_dups} same-source repeats skipped")
        return added

    def _load_samples(self, con):
        added = 0
        for a in json.loads(config.SAMPLE_FILE.read_text()):
            try:
                con.execute(
                    "INSERT INTO articles (id,url,title,summary,source,topic,published,"
                    "entities,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (db.new_id(), a["url"], a["title"], a["summary"], a["source"],
                     a["topic"], db.now(), "", db.now()))
                added += 1
            except Exception:
                pass
        con.commit()
        return added


# ------------------------------------------------------- Entity extraction
class EntityTagger:
    MAX_PER_RUN = 80  # don't let a backlog of untagged articles starve the
                      # later stages' LLM budget in a single run

    def run(self, con):
        rows = con.execute(
            "SELECT id,title,summary FROM articles WHERE entities='' "
            "ORDER BY fetched_at DESC LIMIT ?", (self.MAX_PER_RUN,)).fetchall()
        tagged = 0
        for r in rows:
            out = llm.complete_json("entities", prompt("entities", title=r["title"],
                                                       summary=r["summary"]))
            if out is None:
                continue  # LLM unavailable; retried next run
            con.execute("UPDATE articles SET entities=? WHERE id=?", (db.j(out), r["id"]))
            tagged += 1
        con.commit()
        db.log_run(con, "entities", "ok",
                   f"{tagged} tagged, {len(rows) - tagged} deferred to next run")
        return tagged


# ----------------------------------------------------------- Trend Linker
class TrendLinker:
    """Macro trends: clusters across all recent articles, LLM names each."""

    def run(self, con, threshold=0.25, min_size=2):
        rows = con.execute(
            "SELECT id,title,summary FROM articles WHERE fetched_at > ?",
            (db.now() - 7 * 86400,)).fetchall()
        clusters = greedy_cluster([(r["id"], r["title"] + " " + r["summary"]) for r in rows],
                                  threshold)
        made = 0
        for ids in clusters:
            if len(ids) < min_size:
                continue
            arts = [r for r in rows if r["id"] in ids]
            items = "\n".join(f"- {a['title']}: {a['summary'][:150]}" for a in arts)
            out = llm.complete_json("trend", prompt("trend", n=len(arts), items=items))
            if out is None:
                continue
            con.execute(
                "INSERT INTO trends (id,kind,name,narrative,sectors,regions,article_ids,"
                "velocity,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (db.new_id(), "macro", out.get("name", "Trend"),
                 out.get("narrative", ""), db.j(out.get("sectors", [])),
                 db.j(out.get("regions", [])), db.j(ids), float(len(ids)), db.now()))
            made += 1
        con.commit()
        db.log_run(con, "trends", "ok", f"{made} macro trends")
        return made


class MicroTrendDetector:
    """Tighter clusters over a 72h window; flags accelerating coverage."""

    def run(self, con, threshold=0.4):
        recent = con.execute("SELECT id,title,summary FROM articles WHERE fetched_at > ?",
                             (db.now() - 72 * 3600,)).fetchall()
        prev_count = con.execute(
            "SELECT COUNT(*) c FROM articles WHERE fetched_at BETWEEN ? AND ?",
            (db.now() - 144 * 3600, db.now() - 72 * 3600)).fetchone()["c"]
        clusters = greedy_cluster([(r["id"], r["title"] + " " + r["summary"]) for r in recent],
                                  threshold)
        made = 0
        for ids in clusters:
            if not (2 <= len(ids) <= 5):
                continue  # micro = small but repeated
            velocity = len(ids) / max(prev_count, 1)
            arts = [r for r in recent if r["id"] in ids]
            items = "\n".join(f"- {a['title']}" for a in arts)
            out = llm.complete_json("micro_trend", prompt("micro_trend", items=items))
            if out is None:
                continue
            con.execute(
                "INSERT INTO trends (id,kind,name,narrative,sectors,regions,article_ids,"
                "velocity,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (db.new_id(), "micro", out.get("name", "Micro-trend"),
                 out.get("signal", ""), "[]", "[]", db.j(ids), velocity, db.now()))
            made += 1
        con.commit()
        db.log_run(con, "micro_trends", "ok", f"{made} micro trends")
        return made


# ----------------------------------------------------- Hidden connections
class ConnectionFinder:
    """Pairs of dissimilar stories that share entities/sectors -> LLM traces
    the second-order chain between them."""

    MAX_PAIRS = 12  # budget guard

    def run(self, con):
        rows = con.execute(
            "SELECT id,title,summary,entities FROM articles WHERE fetched_at > ?",
            (db.now() - 7 * 86400,)).fetchall()
        cands = []
        for i in range(len(rows)):
            for k in range(i + 1, len(rows)):
                a, b = rows[i], rows[k]
                surface = cosine(_tf(a["title"] + a["summary"]),
                                 _tf(b["title"] + b["summary"]))
                if surface > 0.3:
                    continue  # obviously related; trends handle those
                ea = set(db.uj(a["entities"]).get("entities", []) +
                         db.uj(a["entities"]).get("sectors", []))
                eb = set(db.uj(b["entities"]).get("entities", []) +
                         db.uj(b["entities"]).get("sectors", []))
                shared = ea & eb
                if shared:
                    cands.append((len(shared), a, b))
        cands.sort(key=lambda x: -x[0])
        made = 0
        for _, a, b in cands[: self.MAX_PAIRS]:
            out = llm.complete_json("connection", prompt(
                "connection",
                a=f"{a['title']} — {a['summary'][:200]}",
                b=f"{b['title']} — {b['summary'][:200]}"))
            if out is None:
                continue
            conf = float(out.get("confidence", 0))
            if conf >= 0.6:
                con.execute(
                    "INSERT INTO connections (id,article_a,article_b,chain,confidence,"
                    "affected,created_at) VALUES (?,?,?,?,?,?,?)",
                    (db.new_id(), a["id"], b["id"], out.get("chain", ""), conf,
                     db.j(out.get("affected", [])), db.now()))
                made += 1
        con.commit()
        db.log_run(con, "connections", "ok", f"{made} connections from {len(cands)} candidates")
        return made


# --------------------------------------------------------------- Verifier
class Verifier:
    def __init__(self):
        self.tiers = yaml.safe_load(config.SOURCES_FILE.read_text())

    def source_tier(self, source):
        for tier, hosts in self.tiers.items():
            if any(h in source for h in hosts):
                return int(tier[-1])
        return 4

    def run(self, con, cluster_article_ids, narrative_text):
        """Returns (claims, verdicts, score, note) or None if the LLM failed."""
        arts = [con.execute("SELECT * FROM articles WHERE id=?", (i,)).fetchone()
                for i in cluster_article_ids]
        arts = [a for a in arts if a]
        sources = {a["source"] for a in arts}
        best_tier = min((self.source_tier(a["source"]) for a in arts), default=4)
        claims_out = llm.complete_json("claims", prompt("claims", text=narrative_text))
        if claims_out is None:
            return None
        claims = claims_out.get("claims", [])[:4]
        out = llm.complete_json("verify", prompt(
            "verify", claims=db.j(claims), corroboration=len(sources), tier=best_tier))
        if out is None:
            return None
        # deterministic floor/ceiling from corroboration so mock mode is sane too
        base = min(95, 35 + 15 * len(sources) + (4 - best_tier) * 5)
        score = float(out.get("score", base))
        score = max(5.0, min(score, 98.0))
        return claims, out.get("verdicts", []), score, out.get(
            "note", f"{len(sources)} independent source(s), best tier {best_tier}")


# ------------------------------------------------------------ Storyteller
class Storyteller:
    MAX_PER_RUN = 20  # fits free-tier budgets; the rest are picked up next run

    def run(self, con, verifier: Verifier):
        """Turn each macro trend cluster (and orphan articles) into a story."""
        done_ids = set()
        for row in con.execute("SELECT article_ids FROM stories").fetchall():
            done_ids.update(db.uj(row["article_ids"], []))
        made = 0
        trends = con.execute("SELECT * FROM trends WHERE kind='macro'").fetchall()
        groups = [(t, db.uj(t["article_ids"], [])) for t in trends]
        # orphan articles not in any trend become single-article stories
        in_trend = {i for _, ids in groups for i in ids}
        orphans = con.execute(
            "SELECT id FROM articles WHERE fetched_at > ?", (db.now() - 7 * 86400,)).fetchall()
        for o in orphans:
            if o["id"] not in in_trend:
                groups.append((None, [o["id"]]))
        for trend, ids in groups:
            if made >= self.MAX_PER_RUN:
                break
            if not ids or set(ids) <= done_ids:
                continue
            arts = [con.execute("SELECT * FROM articles WHERE id=?", (i,)).fetchone()
                    for i in ids]
            arts = [a for a in arts if a]
            if not arts:
                continue
            items = "\n".join(f"- {a['title']}: {a['summary'][:200]}" for a in arts)
            verified = verifier.run(con, ids, items)
            if verified is None:
                continue  # LLM unavailable — article kept for next run
            claims, verdicts, score, note = verified
            out = llm.complete_json("story", prompt("story", claims=db.j(claims), items=items))
            if out is None:
                continue
            headline = out.get("headline", arts[0]["title"])
            conn_ids = [r["id"] for r in con.execute(
                "SELECT id FROM connections WHERE article_a IN (%s) OR article_b IN (%s)"
                % (",".join("?" * len(ids)), ",".join("?" * len(ids))),
                ids + ids).fetchall()]
            con.execute(
                "INSERT INTO stories (id,headline,narrative,credibility,credibility_note,"
                "claims,topic,article_ids,trend_ids,connection_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), headline, out.get("narrative", items), score, note,
                 db.j({"claims": claims, "verdicts": verdicts}), arts[0]["topic"],
                 db.j(ids), db.j([trend["id"]] if trend else []), db.j(conn_ids), db.now()))
            done_ids.update(ids)
            made += 1
        con.commit()
        db.log_run(con, "storyteller", "ok", f"{made} stories")
        return made


# -------------------------------------------------------------- Foresight
class Foresight:
    """The differentiator. Takes the per-story analysis the pipeline already
    produced (entities, claims, topics) and reasons over ALL of it in one LLM
    pass to find cross-domain signals: indirect causal chains where stories
    from different topics point to something that may happen next."""

    WINDOW_DAYS = 7
    MAX_DIGESTS = 60

    def run(self, con):
        stories = con.execute(
            "SELECT id, headline, topic, claims, article_ids FROM stories "
            "WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
            (db.now() - self.WINDOW_DAYS * 86400, self.MAX_DIGESTS)).fetchall()
        if len(stories) < 4:
            db.log_run(con, "foresight", "ok", "too few stories to synthesize")
            return 0
        digests = []
        for s in stories:
            ents = []
            for aid in db.uj(s["article_ids"], [])[:3]:
                a = con.execute("SELECT entities FROM articles WHERE id=?", (aid,)).fetchone()
                if a:
                    e = db.uj(a["entities"])
                    ents += e.get("entities", [])[:4] + e.get("sectors", [])[:2]
            claim = (db.uj(s["claims"]).get("claims") or [""])[0]
            digests.append(f"{s['id']} | {s['topic']} | {s['headline']} | "
                           f"{', '.join(dict.fromkeys(ents))[:120]} | {str(claim)[:100]}")
        out = llm.complete_json("signals", prompt(
            "signals", n=len(digests), digests="\n".join(digests)))
        if out is None:
            db.log_run(con, "foresight", "error", "LLM unavailable; retry next run")
            return 0
        valid = {s["id"] for s in stories}
        accepted = []
        for sig in out.get("signals", [])[:7]:
            sids = [i for i in sig.get("story_ids", []) if i in valid]
            conf = float(sig.get("confidence", 0))
            if len(sids) < 2 or conf < 0.35:
                continue  # evidence bar: 2+ real stories, non-trivial confidence
            accepted.append((db.new_id(), sig.get("title", "Signal"),
                             sig.get("prediction", ""), sig.get("chain", ""),
                             sig.get("watch", ""), db.j(sig.get("affected", [])),
                             sig.get("horizon", ""), min(conf, 0.85),
                             db.j(sids), db.now()))
        if not accepted:
            # Never wipe good signals to replace them with nothing — keep the
            # previous set until a run produces valid new ones.
            db.log_run(con, "foresight", "ok",
                       "no valid new signals this run; kept previous set")
            return 0
        con.execute("DELETE FROM signals")
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)", accepted)
        con.commit()
        db.log_run(con, "foresight", "ok",
                   f"{len(accepted)} signals from {len(digests)} digests")
        return len(accepted)


# ----------------------------------------------------------- Personalizer
class Personalizer:
    def run(self, con):
        users = con.execute("SELECT * FROM users").fetchall()
        stories = con.execute(
            "SELECT * FROM stories WHERE created_at > ?", (db.now() - 7 * 86400,)).fetchall()
        made = 0
        for u in users:
            ctx = db.uj(u["context"])
            interests = {s.lower() for s in ctx.get("interests", [])}
            loc = str(ctx.get("location", {})).lower()
            for s in stories:
                exists = con.execute(
                    "SELECT 1 FROM feed_items WHERE user_id=? AND story_id=?",
                    (u["id"], s["id"])).fetchone()
                if exists:
                    continue
                text = (s["headline"] + " " + s["narrative"] + " " + s["topic"]).lower()
                relevant = (s["topic"].lower() in interests
                            or any(i in text for i in interests)
                            or any(w in text for w in re.findall(r"[a-z]{4,}", loc)))
                if relevant:
                    trends = [t["name"] for t in con.execute(
                        "SELECT name FROM trends WHERE id IN (%s)" %
                        ",".join("?" * len(db.uj(s["trend_ids"], []))),
                        db.uj(s["trend_ids"], [])).fetchall()] if db.uj(s["trend_ids"], []) else []
                    out = llm.complete_json("personalize", prompt(
                        "personalize", context=db.j(ctx), headline=s["headline"],
                        narrative=s["narrative"][:600], trends=db.j(trends)))
                    if out is None:
                        continue  # skip; personalized next run
                    impact_text = out.get("impact_text", "")
                    impact = int(out.get("impact_score", 1))
                else:
                    impact_text, impact = "", 0
                con.execute(
                    "INSERT OR IGNORE INTO feed_items (id,user_id,story_id,impact_text,"
                    "impact_score,created_at) VALUES (?,?,?,?,?,?)",
                    (db.new_id(), u["id"], s["id"], impact_text, impact, db.now()))
                made += 1
        con.commit()
        db.log_run(con, "personalizer", "ok", f"{made} feed items")
        return made
