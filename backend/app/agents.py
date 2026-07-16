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


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _by_topic(rows):
    """Group DB rows (which must carry a 'topic' column) into {topic: [rows]},
    preserving order. This is the 'unit' the per-topic trend/forecast calls run over."""
    groups = {}
    for r in rows:
        groups.setdefault(r["topic"], []).append(r)
    return groups


def _dedupe_trends(con, kind, name_sim=0.6, overlap=0.5):
    """Collapse near-duplicate trends of one kind. Two trends are duplicates when
    their name+narrative are highly similar OR they share most of their articles.
    The newest trend in each duplicate group is kept (freshest naming) and absorbs
    the others' article_ids; the rest are deleted. Returns how many were removed.
    Safe to run every pipeline pass and as a one-off cleanup of accumulated dupes."""
    rows = con.execute(
        "SELECT id,name,narrative,article_ids,velocity,created_at FROM trends "
        "WHERE kind=? ORDER BY created_at DESC", (kind,)).fetchall()
    trends = [{"id": r["id"], "name": r["name"],
               "ids": db.uj(r["article_ids"], []),
               "vec": _tf((r["name"] or "") + " " + (r["narrative"] or ""))}
              for r in rows]
    keep, removed = [], 0
    for t in trends:
        dup_of = next(
            (k for k in keep if cosine(t["vec"], k["vec"]) >= name_sim
             or _jaccard(t["ids"], k["ids"]) >= overlap), None)
        if dup_of is None:
            keep.append(t)
            continue
        union = sorted(set(dup_of["ids"]) | set(t["ids"]))
        if kind == "macro":  # macro velocity == article count
            con.execute("UPDATE trends SET article_ids=?, velocity=? WHERE id=?",
                        (db.j(union), float(len(union)), dup_of["id"]))
        else:
            con.execute("UPDATE trends SET article_ids=? WHERE id=?",
                        (db.j(union), dup_of["id"]))
        dup_of["ids"] = union
        con.execute("DELETE FROM trends WHERE id=?", (t["id"],))
        removed += 1
    return removed


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
                                     headers={"User-Agent": "DescryBot/0.1 (+beta)"})
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
def _numbered_items(rows):
    """One indexed line per article for the (single) trend call: [i] source,
    title, entities/sectors, summary excerpt. The [i] index is how the LLM
    references which articles belong to each trend, so it must stay 1 per line."""
    lines = []
    for i, a in enumerate(rows, 1):
        keys = a.keys()
        ent = db.uj(a["entities"]) if "entities" in keys else {}
        tags = ", ".join((ent.get("entities", []) + ent.get("sectors", []))[:6])
        src = a["source"] if "source" in keys else ""
        title = (a["title"] or "").replace("\n", " ")
        summary = (a["summary"] or "").replace("\n", " ")[:220]
        lines.append(f"[{i}] [{src}] {title}"
                     + (f" | entities: {tags}" if tags else "")
                     + f" | {summary}")
    return "\n".join(lines)


def _parse_trends(items, rows, min_size, kind="macro"):
    """Turn the LLM's list of trends (with 1-based member indices into `rows`)
    into fresh trend dicts with resolved article_ids. Drops anything under
    min_size or with no valid members."""
    fresh = []
    for t in items or []:
        members = t.get("members") or t.get("article_ids") or []
        ids = []
        for m in members:
            try:
                idx = int(m) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(rows):
                ids.append(rows[idx]["id"])
        ids = sorted(set(ids))
        if len(ids) < min_size:
            continue
        fresh.append({
            "name": t.get("name", "Trend"),
            "narrative": t.get("narrative") or t.get("signal") or "",
            "sectors": t.get("sectors", []) if kind == "macro" else [],
            "regions": t.get("regions", []) if kind == "macro" else [],
            "article_ids": ids,
            "velocity": float(len(ids)),
        })
    return fresh


def _reconcile_trends(con, kind, fresh, name_sim=0.6, overlap=0.5, prune=True):
    """Reconcile a freshly-computed trend set against what's stored, keeping stable
    IDs. Each fresh trend is matched to an existing one by article overlap or name
    similarity: a match is UPDATED in place (id preserved, so story->trend links
    survive); unmatched fresh trends are INSERTED; existing trends absent from the
    fresh set are DELETED — but only when prune=True. Pass prune=False when some
    per-unit calls failed this run, so a transient error can't wipe good trends.
    Returns (new, updated, removed)."""
    existing = [{"id": r["id"], "ids": db.uj(r["article_ids"], []),
                 "vec": _tf((r["name"] or "") + " " + (r["narrative"] or ""))}
                for r in con.execute(
                    "SELECT id,name,narrative,article_ids FROM trends WHERE kind=?",
                    (kind,)).fetchall()]
    matched, new_ct, upd_ct = set(), 0, 0
    for f in fresh:
        fvec = _tf(f["name"] + " " + f["narrative"])
        cand = next((e for e in existing if e["id"] not in matched
                     and (cosine(fvec, e["vec"]) >= name_sim
                          or _jaccard(f["article_ids"], e["ids"]) >= overlap)), None)
        if cand:
            con.execute(
                "UPDATE trends SET name=?,narrative=?,sectors=?,regions=?,article_ids=?,"
                "velocity=?,created_at=? WHERE id=?",
                (f["name"], f["narrative"], db.j(f["sectors"]), db.j(f["regions"]),
                 db.j(f["article_ids"]), f["velocity"], db.now(), cand["id"]))
            matched.add(cand["id"])
            upd_ct += 1
        else:
            con.execute(
                "INSERT INTO trends (id,kind,name,narrative,sectors,regions,article_ids,"
                "velocity,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (db.new_id(), kind, f["name"], f["narrative"], db.j(f["sectors"]),
                 db.j(f["regions"]), db.j(f["article_ids"]), f["velocity"], db.now()))
            new_ct += 1
    removed = 0
    if prune:
        for e in existing:
            if e["id"] not in matched:  # no longer a current trend
                con.execute("DELETE FROM trends WHERE id=?", (e["id"],))
                removed += 1
    return new_ct, upd_ct, removed


class TrendLinker:
    """Per-UNIT trends: ONE reasoning call per topic ('unit') over that topic's recent
    articles, returning both established (macro) and emerging (micro) trends. Fresh
    trends are accumulated across all topics, then reconciled ONCE per kind (stable IDs
    preserved). So ~1 LLM call per topic, folding micro-trend detection into the same
    call. Also covers the old MicroTrendDetector, which is now retired."""

    MAX_PER_UNIT = 60  # cap per-topic articles so each prompt stays small/cheap

    def run(self, con, min_size=2):
        rows = con.execute(
            "SELECT id,title,summary,source,topic,entities,fetched_at FROM articles "
            "WHERE fetched_at > ? ORDER BY fetched_at DESC",
            (db.now() - 7 * 86400,)).fetchall()
        if len(rows) < min_size:
            db.log_run(con, "trends", "ok", "too few articles to synthesize")
            return 0
        cutoff72 = db.now() - 72 * 3600
        prev_count = con.execute(
            "SELECT COUNT(*) c FROM articles WHERE fetched_at BETWEEN ? AND ?",
            (db.now() - 144 * 3600, cutoff72)).fetchone()["c"]
        fresh_macro, fresh_micro, calls, failed = [], [], 0, 0
        for topic, arts in _by_topic(rows).items():
            arts = arts[:self.MAX_PER_UNIT]
            if len(arts) < min_size:
                continue
            out = llm.complete_json("trend", prompt(
                "trend", topic=topic, n=len(arts), items=_numbered_items(arts)))
            calls += 1
            if out is None:
                failed += 1
                continue
            fresh_macro += _parse_trends(out.get("trends", []), arts, min_size, "macro")
            micro = _parse_trends(out.get("micro_trends", []), arts, 2, "micro")
            micro = [m for m in micro if 2 <= len(m["article_ids"]) <= 5]
            recent_ids = {a["id"] for a in arts if a["fetched_at"] > cutoff72}
            for m in micro:  # velocity = share of the cluster that's from the last 72h
                m["velocity"] = len(set(m["article_ids"]) & recent_ids) / max(prev_count, 1)
            fresh_micro += micro
        if not fresh_macro and not fresh_micro:
            db.log_run(con, "trends", "ok",
                       f"no valid trends this run ({calls} unit calls); kept existing set")
            return 0
        # Don't prune (delete) stale trends if any unit call failed — avoids wiping.
        prune = failed == 0
        mn, mu, mr = _reconcile_trends(con, "macro", fresh_macro, prune=prune)
        cn, cu, cr = _reconcile_trends(con, "micro", fresh_micro, prune=prune)
        con.commit()
        db.log_run(con, "trends", "ok",
                   f"macro {mn} new/{mu} upd/-{mr}, micro {cn} new/{cu} upd/-{cr} "
                   f"({calls} unit calls, {failed} failed)")
        return mn + mu + cn + cu


class MicroTrendDetector:
    """RETIRED. Micro-trend detection is now folded into TrendLinker's per-unit call
    (each topic call returns both macro and emerging/micro trends). Kept as a no-op
    so any lingering reference/stage runs harmlessly and makes no LLM calls."""

    def run(self, con):
        db.log_run(con, "micro_trends", "ok", "folded into per-unit trends (no-op)")
        return 0


# ----------------------------------------------------- Hidden connections
class ConnectionFinder:
    """Pairs of dissimilar stories that share entities/sectors -> LLM traces
    the second-order chain between them."""

    MAX_PAIRS = 12  # budget guard

    def run(self, con):
        rows = con.execute(
            "SELECT id,title,summary,entities FROM articles WHERE fetched_at > ?",
            (db.now() - 7 * 86400,)).fetchall()
        # Incremental: never re-evaluate a pair we've already asked the LLM about.
        seen_pairs = set()
        for r in con.execute("SELECT article_a, article_b FROM connections").fetchall():
            seen_pairs.add(frozenset((r["article_a"], r["article_b"])))
        cands = []
        for i in range(len(rows)):
            for k in range(i + 1, len(rows)):
                a, b = rows[i], rows[k]
                if frozenset((a["id"], b["id"])) in seen_pairs:
                    continue
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
            # Store every evaluated pair (including rejections at low confidence)
            # so it is never sent to the LLM again. Consumers filter to >= 0.6.
            con.execute(
                "INSERT INTO connections (id,article_a,article_b,chain,confidence,"
                "affected,created_at) VALUES (?,?,?,?,?,?,?)",
                (db.new_id(), a["id"], b["id"], out.get("chain", ""), conf,
                 db.j(out.get("affected", [])), db.now()))
            if conf >= 0.6:
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
    # Configurable via MAX_STORIES_PER_RUN env var; the rest roll to next run.
    MAX_PER_RUN = config.MAX_STORIES_PER_RUN

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
                "SELECT id FROM connections WHERE confidence >= 0.6 AND "
                "(article_a IN (%s) OR article_b IN (%s))"
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
    """Per-UNIT forecasting plus one cross-domain pass. For each topic it makes ONE
    reasoning call over that topic's recent stories (within-domain foresight), then a
    final call over a cross-topic sample to catch the indirect cross-domain chains that
    are this feature's differentiator. All fresh signals are reconciled/enhanced against
    stored ones and stale (>WINDOW_DAYS) forecasts pruned. ~1 call per topic + 1 cross."""

    WINDOW_DAYS = 7
    MAX_PER_UNIT = 30           # stories per topic fed to each unit call
    CROSS_SAMPLE_PER_TOPIC = 4  # stories per topic in the single cross-domain pass
    TITLE_SIM = 0.6             # title cosine above this = same forecast
    STORY_OVERLAP = 0.3         # story_id Jaccard above this = same forecast

    def _digest(self, con, s):
        """One rich digest block per story: topic, headline, credibility, the
        entities/sectors behind it, its top claims, and a narrative excerpt."""
        ents = []
        for aid in db.uj(s["article_ids"], [])[:3]:
            a = con.execute("SELECT entities FROM articles WHERE id=?", (aid,)).fetchone()
            if a:
                e = db.uj(a["entities"])
                ents += e.get("entities", [])[:4] + e.get("sectors", [])[:2]
        claims = db.uj(s["claims"]).get("claims") or []
        claim_txt = " ; ".join(str(c) for c in claims[:2])[:180]
        cred = s["credibility"] if "credibility" in s.keys() and s["credibility"] else 0
        narr = (s["narrative"] if "narrative" in s.keys() else "") or ""
        return (f"{s['id']} | {s['topic']} | {s['headline']} | cred {int(cred)} | "
                f"{', '.join(dict.fromkeys(ents))[:120]} | {claim_txt} | {narr[:160]}")

    def run(self, con):
        stories = con.execute(
            "SELECT id, headline, topic, narrative, credibility, claims, article_ids "
            "FROM stories WHERE created_at > ? ORDER BY created_at DESC",
            (db.now() - self.WINDOW_DAYS * 86400,)).fetchall()
        # Age out stale forecasts every run (instead of wiping all of them), so
        # a run that produces nothing new still leaves recent forecasts intact.
        pruned = con.execute(
            "DELETE FROM signals WHERE created_at < ?",
            (db.now() - self.WINDOW_DAYS * 86400,)).rowcount
        if len(stories) < 4:
            con.commit()
            db.log_run(con, "foresight", "ok",
                       f"too few stories to synthesize; pruned {pruned} stale")
            return 0
        by_topic = _by_topic(stories)
        fresh, calls, failed = [], 0, 0
        # One call per unit: within-domain foresight for each topic.
        for topic, sts in by_topic.items():
            sts = sts[:self.MAX_PER_UNIT]
            if len(sts) < 2:
                continue
            digests = [self._digest(con, s) for s in sts]
            out = llm.complete_json("signals_unit", prompt(
                "signals_unit", topic=topic, n=len(digests), digests="\n".join(digests)))
            calls += 1
            if out is None:
                failed += 1
                continue
            fresh += out.get("signals", [])
        # One cross-domain call over a sample spanning all topics — keeps the indirect,
        # cross-topic chains that a per-topic pass alone would miss.
        sample = []
        for sts in by_topic.values():
            sample += sts[:self.CROSS_SAMPLE_PER_TOPIC]
        if len(sample) >= 4:
            digests = [self._digest(con, s) for s in sample]
            out = llm.complete_json("signals", prompt(
                "signals", n=len(digests), digests="\n".join(digests)))
            calls += 1
            if out is None:
                failed += 1
            else:
                fresh += out.get("signals", [])
        if not fresh:
            # Never wipe a good set to replace it with nothing.
            con.commit()
            db.log_run(con, "foresight", "ok" if failed == 0 else "error",
                       f"no valid signals ({calls} calls); pruned {pruned}; kept previous")
            return 0
        valid = {s["id"] for s in stories}
        # Load surviving forecasts so new ones can enhance them rather than duplicate.
        existing = [{"id": r["id"], "title": r["title"], "vec": _tf(r["title"] or ""),
                     "story_ids": db.uj(r["story_ids"], [])}
                    for r in con.execute(
                        "SELECT id, title, story_ids FROM signals").fetchall()]
        new_ct = upd_ct = 0
        for sig in fresh:
            sids = [i for i in sig.get("story_ids", []) if i in valid]
            conf = float(sig.get("confidence", 0))
            if len(sids) < 2 or conf < 0.35:
                continue  # evidence bar: 2+ real stories, non-trivial confidence
            conf = min(conf, 0.85)
            vec = _tf(sig.get("title", ""))
            match = next(
                (e for e in existing
                 if cosine(vec, e["vec"]) >= self.TITLE_SIM
                 or _jaccard(sids, e["story_ids"]) >= self.STORY_OVERLAP), None)
            if match:  # enhance the existing forecast with the newer synthesis
                union = sorted(set(match["story_ids"]) | set(sids))
                con.execute(
                    "UPDATE signals SET title=?, prediction=?, chain=?, watch=?, "
                    "affected=?, horizon=?, confidence=?, story_ids=?, created_at=? "
                    "WHERE id=?",
                    (sig.get("title", match["title"]), sig.get("prediction", ""),
                     sig.get("chain", ""), sig.get("watch", ""),
                     db.j(sig.get("affected", [])), sig.get("horizon", ""), conf,
                     db.j(union), db.now(), match["id"]))
                match["story_ids"] = union
                match["vec"] = vec
                match["title"] = sig.get("title", match["title"])
                upd_ct += 1
            else:
                nid = db.new_id()
                con.execute(
                    "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (nid, sig.get("title", "Signal"), sig.get("prediction", ""),
                     sig.get("chain", ""), sig.get("watch", ""),
                     db.j(sig.get("affected", [])), sig.get("horizon", ""), conf,
                     db.j(sids), db.now()))
                existing.append({"id": nid, "title": sig.get("title", "Signal"),
                                 "vec": vec, "story_ids": sids})
                new_ct += 1
        con.commit()
        db.log_run(con, "foresight", "ok",
                   f"{new_ct} new + {upd_ct} enhanced forecasts, {pruned} pruned "
                   f"({calls} calls: {len(by_topic)} units + cross-domain, {failed} failed)")
        return new_ct + upd_ct


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
