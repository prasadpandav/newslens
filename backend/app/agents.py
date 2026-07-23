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
    # 2-letter tokens matter here: "AI", "EU", "US" are the subject of many trends.
    return [w for w in re.findall(r"[a-z0-9]{2,}", text.lower()) if w not in stop]


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


def _containment(a, b):
    """Overlap relative to the SMALLER set. Jaccard punishes subset relations —
    a fresh trend carrying only the recent window's articles scores low against
    the stored trend whose id set has grown by unions, even when it is fully
    contained in it. Containment treats a subset as a full match."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _by_topic(rows):
    """Group DB rows (which must carry a 'topic' column) into {topic: [rows]},
    preserving order. This is the 'unit' the per-topic trend/forecast calls run over."""
    groups = {}
    for r in rows:
        groups.setdefault(r["topic"], []).append(r)
    return groups


def _norm_name(s):
    """Lowercase, strip punctuation and stray prefixes for exact-duplicate matching."""
    s = re.sub(r"^(early signal|rising focus|trend|focus on)\s*:?\s*", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ------------------------------------------------------ story-reference linking
_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")


def linkify(text, id_to_headline):
    """Replace bare 12-hex story-id tokens in `text` with the story's HEADLINE so
    raw ids never reach a reader. Only ids present in `id_to_headline` (the story's
    own evidence set) are touched — random hex is left alone. Used at read time on
    signal/story/trend prose; stored text stays raw. Returns the rewritten string."""
    if not text:
        return text
    return _ID_RE.sub(
        lambda m: id_to_headline.get(m.group(0), m.group(0)), text)


def story_refs(id_to_headline):
    """The [{story_id, headline}] list clients use to turn headline spans back into
    tappable links after linkify() has substituted them into the prose."""
    return [{"story_id": sid, "headline": h} for sid, h in id_to_headline.items()]


# ---------------------------------------------------------- breaking heuristic
# Words that, in a headline, strongly imply an urgent/breaking event. Kept small
# and high-precision so the no-LLM sweep stays cheap and rarely false-positives.
_BREAKING_WORDS = set(
    "breaking killed dead dies died explosion attack strikes strike quake "
    "earthquake evacuated evacuation resigns resign ousted coup ceasefire "
    "wins win victory champions final verdict guilty acquitted crash crashes "
    "outage recall emergency landslide flood wildfire eruption hostage".split())


def _breaking_score(title, source_count, age_hours, window_hours, min_sources):
    """0..1 urgency. Corroboration (many distinct sources fast) OR a high-signal
    keyword lifts a recent story; everything decays to 0 past the window."""
    if age_hours > window_hours:
        return 0.0
    recency = max(0.0, 1.0 - age_hours / window_hours)
    corroboration = min(1.0, source_count / max(min_sources, 1))
    kw = 1.0 if (_tokens(title) and set(_tokens(title)) & _BREAKING_WORDS) else 0.0
    # Either strong corroboration or a keyword hit qualifies; recency scales it.
    base = max(corroboration if source_count >= min_sources else 0.0, kw * 0.9)
    return round(base * (0.5 + 0.5 * recency), 3)


def _dedupe_trends(con, kind, name_sim=0.6, overlap=0.5):
    """Collapse near-duplicate trends of one kind. Two trends are duplicates when their
    NAMES match (identical after normalization, or high name-token similarity) OR they
    share most of their articles. Matching keys on the NAME only — NOT name+narrative —
    because per-unit generation produces cross-topic dupes with the same name but
    different narratives and no shared articles, which name+narrative would dilute and
    miss. The newest trend in each group is kept and absorbs the others' article_ids;
    the rest are deleted. Returns how many were removed."""
    rows = con.execute(
        "SELECT id,name,narrative,article_ids,velocity,created_at FROM trends "
        "WHERE kind=? ORDER BY created_at DESC", (kind,)).fetchall()
    trends = [{"id": r["id"], "norm": _norm_name(r["name"]),
               "ids": db.uj(r["article_ids"], []),
               "vel": r["velocity"] or 0.0,
               "vec": _tf(r["name"] or "")}
              for r in rows]
    keep, removed = [], 0
    for t in trends:
        dup_of = next(
            (k for k in keep
             if (t["norm"] and t["norm"] == k["norm"])
             or cosine(t["vec"], k["vec"]) >= name_sim
             or _containment(t["ids"], k["ids"]) >= overlap), None)
        if dup_of is None:
            keep.append(t)
            continue
        union = sorted(set(dup_of["ids"]) | set(t["ids"]))
        if kind == "macro":  # macro velocity == article count
            con.execute("UPDATE trends SET article_ids=?, velocity=? WHERE id=?",
                        (db.j(union), float(len(union)), dup_of["id"]))
        else:  # micro velocity is a ratio — keep the livelier of the two
            dup_of["vel"] = max(dup_of["vel"], t["vel"])
            con.execute("UPDATE trends SET article_ids=?, velocity=? WHERE id=?",
                        (db.j(union), dup_of["vel"], dup_of["id"]))
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
                    for attr in ("published_parsed", "updated_parsed"):
                        st = getattr(e, attr, None)
                        if st:  # feed timestamps are UTC struct_times
                            import calendar
                            pub = calendar.timegm(st)
                            break
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
# --------------------------------------------------- Near-duplicate merging
def assign_groups(con):
    """Group near-duplicate articles — the SAME story reported by different sources —
    so the LLM stages process each real-world event ONCE instead of once per source.
    Titles are clustered by cosine similarity (>= DEDUPE_SIMILARITY); every member is
    stamped with a shared group_id = the earliest member (the representative). No LLM
    calls. Cross-source rows are kept (corroboration still counts distinct sources);
    only the redundant LLM work is removed."""
    cutoff = db.now() - config.DEDUPE_WINDOW_DAYS * 86400
    rows = con.execute(
        "SELECT id, title FROM articles WHERE fetched_at > ? ORDER BY fetched_at ASC",
        (cutoff,)).fetchall()
    if not rows:
        db.log_run(con, "dedupe", "ok", "no recent articles to group")
        return 0
    # Earliest-first ordering makes the first article in each cluster its stable rep.
    clusters = greedy_cluster([(r["id"], r["title"] or "") for r in rows],
                              config.DEDUPE_SIMILARITY)
    merged = 0
    for cluster in clusters:
        rep = cluster[0]
        for aid in cluster:
            con.execute("UPDATE articles SET group_id=? WHERE id=?", (rep, aid))
        merged += len(cluster) - 1
    con.commit()
    db.log_run(con, "dedupe", "ok",
               f"{len(clusters)} groups from {len(rows)} articles, "
               f"{merged} cross-source duplicates merged")
    return merged


class Deduper:
    """Pipeline stage wrapper for assign_groups — runs right after Scout, before the
    LLM stages, so entities/stories operate per group."""
    def run(self, con):
        return assign_groups(con)


class EntityTagger:
    MAX_PER_RUN = 80  # cap untagged articles pulled per run (grouping means far
                      # fewer actual LLM calls than this)

    def run(self, con):
        rows = con.execute(
            "SELECT id,group_id,title,summary FROM articles WHERE entities='' "
            "ORDER BY fetched_at DESC LIMIT ?", (self.MAX_PER_RUN,)).fetchall()
        # One extraction per GROUP, not per article. If the group's representative is
        # already tagged, just copy its entities to the untagged members (no call).
        groups = {}
        for r in rows:
            groups.setdefault(r["group_id"] or r["id"], []).append(r)
        called = copied = 0
        for gid, members in groups.items():
            rep = con.execute("SELECT entities,title,summary FROM articles WHERE id=?",
                              (gid,)).fetchone()
            if rep and rep["entities"]:
                ent = rep["entities"]
                copied += 1
            else:
                src = rep if rep else members[0]
                out = llm.complete_json("entities", prompt(
                    "entities", title=src["title"], summary=src["summary"]))
                if out is None:
                    continue  # LLM unavailable; retried next run
                ent = db.j(out)
                called += 1
            for m in members:
                con.execute("UPDATE articles SET entities=? WHERE id=?", (ent, m["id"]))
        con.commit()
        db.log_run(con, "entities", "ok",
                   f"{called} groups tagged + {copied} copied from reps "
                   f"over {len(rows)} articles ({len(groups)} groups)")
        return called + copied


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
    # Match on NAME only (same key as _dedupe_trends): narratives are re-worded
    # every generation, and folding them into the vector dilutes real name matches.
    existing = [{"id": r["id"], "ids": db.uj(r["article_ids"], []),
                 "vec": _tf(r["name"] or "")}
                for r in con.execute(
                    "SELECT id,name,article_ids FROM trends WHERE kind=?",
                    (kind,)).fetchall()]
    matched, new_ct, upd_ct = set(), 0, 0
    for f in fresh:
        fvec = _tf(f["name"])
        cand = next((e for e in existing if e["id"] not in matched
                     and (cosine(fvec, e["vec"]) >= name_sim
                          or _containment(f["article_ids"], e["ids"]) >= overlap)), None)
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
        # Previous 72h window counts PER TOPIC — the acceleration baseline for a
        # micro-trend is its own beat, not the global article firehose.
        prev_counts = {r["topic"]: r["c"] for r in con.execute(
            "SELECT topic, COUNT(*) c FROM articles WHERE fetched_at BETWEEN ? AND ? "
            "GROUP BY topic", (db.now() - 144 * 3600, cutoff72)).fetchall()}
        # Show each unit call what is already tracked so the LLM reuses names
        # verbatim instead of re-inventing wording (which defeats dedupe).
        existing_names = [r["name"] for r in con.execute(
            "SELECT name FROM trends ORDER BY velocity DESC, created_at DESC "
            "LIMIT 40").fetchall()]
        existing_txt = "\n".join(f"- {n}" for n in existing_names) or "(none yet)"
        fresh_macro, fresh_micro, calls, failed = [], [], 0, 0
        for topic, arts in _by_topic(rows).items():
            arts = arts[:self.MAX_PER_UNIT]
            if len(arts) < min_size:
                continue
            out = llm.complete_json("trend", prompt(
                "trend", topic=topic, n=len(arts), items=_numbered_items(arts),
                existing=existing_txt))
            calls += 1
            if out is None:
                failed += 1
                continue
            fresh_macro += _parse_trends(out.get("trends", []), arts, min_size, "macro")
            micro = _parse_trends(out.get("micro_trends", []), arts, 2, "micro")
            micro = [m for m in micro if 2 <= len(m["article_ids"]) <= 5]
            recent_ids = {a["id"] for a in arts if a["fetched_at"] > cutoff72}
            for m in micro:  # velocity = recent share vs this topic's previous window
                m["velocity"] = (len(set(m["article_ids"]) & recent_ids)
                                 / max(prev_counts.get(topic, 0), 1))
            fresh_micro += micro
        if not fresh_macro and not fresh_micro:
            db.log_run(con, "trends", "ok",
                       f"no valid trends this run ({calls} unit calls); kept existing set")
            return 0
        # Don't prune (delete) stale trends if any unit call failed — avoids wiping.
        prune = failed == 0
        mn, mu, mr = _reconcile_trends(con, "macro", fresh_macro, prune=prune)
        cn, cu, cr = _reconcile_trends(con, "micro", fresh_micro, prune=prune)
        # Collapse near-duplicates. Essential with per-unit generation: a trend that
        # spans two topics gets named independently in each topic's call, and reconcile
        # doesn't dedupe within a single fresh batch — this pass does.
        ddup = _dedupe_trends(con, "macro") + _dedupe_trends(con, "micro")
        con.commit()
        db.log_run(con, "trends", "ok",
                   f"macro {mn} new/{mu} upd/-{mr}, micro {cn} new/{cu} upd/-{cr}, "
                   f"{ddup} dupes collapsed ({calls} unit calls, {failed} failed)")
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
        """Turn each macro trend cluster (and orphan articles) into a story.
        A trend that already has a story gets that story UPDATED in place when new
        articles arrive (same id, refreshed narrative) — never a second, near-
        duplicate story for the same trend."""
        prior = [{"id": r["id"], "aids": set(db.uj(r["article_ids"], [])),
                  "tids": set(db.uj(r["trend_ids"], []))}
                 for r in con.execute(
                     "SELECT id, article_ids, trend_ids FROM stories").fetchall()]
        done_ids = set()
        for p in prior:
            done_ids.update(p["aids"])
        new_ct = upd_ct = absorbed = 0
        trends = con.execute("SELECT * FROM trends WHERE kind='macro'").fetchall()
        live_tids = {t["id"] for t in trends}
        groups = [(t, db.uj(t["article_ids"], [])) for t in trends]
        # Orphan articles (not in any trend) become stories too — but near-duplicates
        # from different sources are merged into ONE story per group_id, so the same
        # event isn't storied (and LLM-called) once per source.
        in_trend = {i for _, ids in groups for i in ids}
        orphan_groups = {}
        for o in con.execute(
                "SELECT id, group_id FROM articles WHERE fetched_at > ?",
                (db.now() - 7 * 86400,)).fetchall():
            if o["id"] not in in_trend:
                orphan_groups.setdefault(o["group_id"] or o["id"], []).append(o["id"])
        for ids in orphan_groups.values():
            groups.append((None, ids))
        for trend, ids in groups:
            if new_ct + upd_ct >= self.MAX_PER_RUN:
                break
            if not ids:
                continue
            # The story already telling this trend: linked by trend id, or (for
            # stories from before relinking) holding most of the same articles.
            mine = None
            if trend:
                mine = next(
                    (p for p in prior if trend["id"] in p["tids"]
                     or _containment(ids, p["aids"]) >= 0.5), None)
            if mine:
                ids = sorted(set(ids) | mine["aids"])  # keep the story's history
                if not (set(ids) - mine["aids"]):
                    continue  # nothing new since the story was last told
            elif set(ids) <= done_ids:
                continue  # articles already covered by other stories
            arts = [con.execute("SELECT * FROM articles WHERE id=?", (i,)).fetchone()
                    for i in ids]
            arts = [a for a in arts if a]
            if not arts:
                continue
            # Annotate each line with its source so a single LLM call over a merged,
            # multi-source cluster knows the corroborating outlets.
            items = "\n".join(
                f"- [{a['source']}] {a['title']}: {a['summary'][:200]}" for a in arts)
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
            if mine:  # developing story: refresh content, bump to top of the feed
                con.execute(
                    "UPDATE stories SET headline=?,narrative=?,credibility=?,"
                    "credibility_note=?,claims=?,article_ids=?,trend_ids=?,"
                    "connection_ids=?,created_at=? WHERE id=?",
                    (headline, out.get("narrative", items), score, note,
                     db.j({"claims": claims, "verdicts": verdicts}), db.j(ids),
                     db.j(sorted(mine["tids"] | {trend["id"]})), db.j(conn_ids),
                     db.now(), mine["id"]))
                mine["aids"] = set(ids)
                upd_ct += 1
            else:
                con.execute(
                    "INSERT INTO stories (id,headline,narrative,credibility,credibility_note,"
                    "claims,topic,article_ids,trend_ids,connection_ids,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (db.new_id(), headline, out.get("narrative", items), score, note,
                     db.j({"claims": claims, "verdicts": verdicts}), arts[0]["topic"],
                     db.j(ids), db.j([trend["id"]] if trend else []), db.j(conn_ids), db.now()))
                new_ct += 1
            done_ids.update(ids)
            # Absorb superseded stories now fully covered by this trend story:
            # an orphan story whose article joined the trend, or a story left
            # behind when its trend was merged away. Their articles must not
            # keep a second, stale telling in the feed.
            if trend:
                ids_set = set(ids)
                for p in [p for p in prior
                          if p is not mine and p["aids"]
                          and p["aids"] <= ids_set
                          and not (p["tids"] & live_tids)]:
                    con.execute("DELETE FROM stories WHERE id=?", (p["id"],))
                    con.execute("DELETE FROM feed_items WHERE story_id=?", (p["id"],))
                    prior.remove(p)
                    absorbed += 1
        con.commit()
        db.log_run(con, "storyteller", "ok",
                   f"{new_ct} new + {upd_ct} updated stories, {absorbed} absorbed")
        return new_ct + upd_ct


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
    STORY_OVERLAP = 0.5         # story_id containment above this = same forecast

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
        # Show the LLM what is already forecast so it reuses titles verbatim
        # instead of re-wording the same forecast (which defeats dedupe).
        existing_titles = [r["title"] for r in con.execute(
            "SELECT title FROM signals ORDER BY confidence DESC, created_at DESC "
            "LIMIT 30").fetchall()]
        existing_txt = "\n".join(f"- {t}" for t in existing_titles) or "(none yet)"
        fresh, calls, failed = [], 0, 0
        # One call per unit: within-domain foresight for each topic.
        for topic, sts in by_topic.items():
            sts = sts[:self.MAX_PER_UNIT]
            if len(sts) < 2:
                continue
            digests = [self._digest(con, s) for s in sts]
            out = llm.complete_json("signals_unit", prompt(
                "signals_unit", topic=topic, n=len(digests), digests="\n".join(digests),
                existing=existing_txt))
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
                "signals", n=len(digests), digests="\n".join(digests),
                existing=existing_txt))
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
                 or _containment(sids, e["story_ids"]) >= self.STORY_OVERLAP), None)
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
def _phrase_in(phrase, words):
    """True when every word of `phrase` appears as a WHOLE word in `words`.
    Substring matching burned us: interest "ai" matched "said" and "train"."""
    toks = re.findall(r"[a-z0-9]{2,}", phrase.lower())
    return bool(toks) and all(t in words for t in toks)


class Personalizer:
    def run(self, con):
        users = con.execute("SELECT * FROM users").fetchall()
        stories = con.execute(
            "SELECT * FROM stories WHERE created_at > ?", (db.now() - 7 * 86400,)).fetchall()
        made = 0
        for u in users:
            ctx = db.uj(u["context"])
            interests = {s.lower() for s in ctx.get("interests", [])}
            # Location VALUES only — matching on str(dict) leaked the keys
            # ("city", "region", "country") and tagged nearly every story.
            loc_words = {w for v in ctx.get("location", {}).values()
                         for w in re.findall(r"[a-z]{3,}", str(v).lower())}
            for s in stories:
                exists = con.execute(
                    "SELECT 1 FROM feed_items WHERE user_id=? AND story_id=?",
                    (u["id"], s["id"])).fetchone()
                if exists:
                    continue
                text = (s["headline"] + " " + s["narrative"] + " " + s["topic"]).lower()
                words = set(re.findall(r"[a-z0-9]{2,}", text))
                relevant = (s["topic"].lower() in interests
                            or any(_phrase_in(i, words) for i in interests)
                            or bool(loc_words & words))
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


# ------------------------------------------------------- breaking detection
def detect_breaking(con, limit=8):
    """Scan recent stories with the no-LLM heuristic and return breaking-card dicts
    (highest urgency first). Source count = distinct sources across a story's
    member articles; age from created_at. Pure read — the caller writes cards."""
    window = config.BREAKING_WINDOW_HOURS
    min_src = config.BREAKING_MIN_SOURCES
    rows = con.execute(
        "SELECT id, headline, topic, article_ids, credibility, created_at "
        "FROM stories WHERE created_at > ? ORDER BY created_at DESC LIMIT 60",
        (db.now() - window * 3600,)).fetchall()
    cards = []
    for s in rows:
        aids = db.uj(s["article_ids"], [])
        if not aids:
            continue
        srcs = {r["source"] for r in con.execute(
            "SELECT DISTINCT source FROM articles WHERE id IN (%s)"
            % ",".join("?" * len(aids)), aids).fetchall() if r["source"]}
        age_h = (db.now() - (s["created_at"] or db.now())) / 3600.0
        score = _breaking_score(s["headline"], len(srcs), age_h, window, min_src)
        if score <= 0:
            continue
        cards.append({
            "type": "breaking", "priority": score, "title": s["headline"],
            "subtitle": f"{len(srcs)} sources · {s['topic']}",
            "detail": "", "story_id": s["id"], "url": "",
            "payload": {"sources": len(srcs),
                        "credibility": s["credibility"] or 0,
                        "topic": s["topic"]}})
    cards.sort(key=lambda c: -c["priority"])
    return cards[:limit]
