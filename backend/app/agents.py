"""All NewsLens agents.

Each agent is a small class with .run(con) that reads and writes SQLite.
Prompts come from prompts.yaml. The LLM client handles mock/real providers.
"""
import heapq
import json
import math
import re
import time
import yaml
from . import config, db, images, llm, textmerge, fulltext

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
    the rest are deleted. Returns how many were removed.

    Live trends only: a retired row must never win the "newest kept" contest and
    delete a trend that is still on the radar. Retired duplicates are invisible
    anyway and age out via the retirement purge."""
    rows = con.execute(
        "SELECT id,name,narrative,article_ids,velocity,created_at FROM trends "
        "WHERE kind=? AND retired_at IS NULL ORDER BY created_at DESC",
        (kind,)).fetchall()
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
        skipped_old = 0
        max_age = config.INGEST_MAX_AGE_HOURS
        for topic, urls in feeds.items():
            if topics and topic not in topics:
                continue
            for url in urls:
                try:
                    # Fetch with httpx (bundled certs + custom UA), parse the bytes.
                    # feedparser's own fetching hits SSL/UA issues on some systems.
                    # Streamed with a hard byte ceiling — httpx.get() used to read the
                    # whole body into memory before feedparser ever saw it, so one
                    # misconfigured feed (redirect to a large page, a non-XML host)
                    # was enough to OOM-kill the instance. See fulltext.fetch_text for
                    # the same fix applied to per-article fetches.
                    import httpx
                    with httpx.stream(
                            "GET", url, timeout=15, follow_redirects=True,
                            headers={"User-Agent": "DescryBot/0.1 (+beta)"}) as resp:
                        buf, total = [], 0
                        for chunk in resp.iter_bytes():
                            buf.append(chunk)
                            total += len(chunk)
                            if total >= config.RSS_MAX_BYTES:
                                break  # truncated feed still yields usable entries
                        content = b"".join(buf)
                    parsed = feedparser.parse(content)
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
                    pub, dated = time.time(), False
                    for attr in ("published_parsed", "updated_parsed"):
                        st = getattr(e, attr, None)
                        if st:  # feed timestamps are UTC struct_times
                            import calendar
                            pub = calendar.timegm(st)
                            dated = True
                            break
                    # Freshness gate: skip entries the feed says are old. Feeds carry
                    # a long tail (some hold 200 items), so without this every run
                    # re-considers week-old news. Only applied when the entry actually
                    # carries a date — an undated entry's age is unknown, and dropping
                    # it would silently lose whole feeds.
                    if (max_age and dated
                            and pub < db.now() - max_age * 3600):
                        skipped_old += 1
                        continue
                    # Artwork, if the feed carried any. URL only — see images.py.
                    img, iw, ih = images.from_entry(e)
                    try:
                        con.execute(
                            "INSERT INTO articles (id,url,title,summary,source,topic,"
                            "published,entities,fetched_at,image_url,image_width,"
                            "image_height,place) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (db.new_id(), link, title, summary, source, topic,
                             pub, "", db.now(), img, iw, ih,
                             # From the FEED url, not the article's: a city feed
                             # is what tells us the coverage area, and the
                             # article's own link often carries no city at all.
                             place_for_url(url)))
                        added += 1
                    except Exception:
                        pass  # duplicate url
                con.commit()   # per feed — see the note in Storyteller.run: one
                               # commit at the end would hold the write lock
                               # across every remaining feed's HTTP fetch
        con.commit()
        if skipped_dups or skipped_old:
            note = f"{skipped_dups} same-source repeats skipped"
            if max_age:
                note += f", {skipped_old} older than {max_age:g}h skipped"
            db.log_run(con, "scout_dedup", "ok", note)
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
# The `entities` prompt asks for flat lists of strings, but a model is free to
# answer {"entities": [{"name": "Reserve Bank of India", "type": "ORG"}]} instead
# — and one of our providers does. That shape reached the DB verbatim and then
# blew up wherever the terms were treated as strings: `set(...)` in
# ConnectionFinder raised "unhashable type: 'dict'" and killed the whole stage,
# and `", ".join(...)` in _numbered_items would have raised too.
#
# So terms are normalised on BOTH sides. On write, so new rows are clean; on
# read, because rows already stored the wrong way are not rewritten and must
# keep working. Anything unusable is dropped rather than stringified — a term
# only earns its place if it is a name we can actually compare.
_TERM_KEYS = ("name", "entity", "text", "label", "title", "value")


def _terms(raw):
    """Any JSON the model handed back -> a de-duplicated list of clean strings."""
    out = []
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            # Take the first recognised name key; a dict with none of them
            # carries no term we can use.
            item = next((item[k] for k in _TERM_KEYS
                         if isinstance(item.get(k), (str, int, float))), None)
        elif isinstance(item, list):
            out += _terms(item)          # one level of nesting, seen in the wild
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            item = str(item)
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return list(dict.fromkeys(out))


#: URL-substring -> place name, from sources.yaml. Read once; the file is small
#: and does not change between runs.
_PLACES = None


def place_for_url(url):
    """Which place a feed URL covers, or "" when we have no mapping for it.

    Longest key first, so a specific city path wins over a broader one from the
    same publisher. Returns "" rather than guessing: an unmapped local feed is
    honestly "Local" with no city named, which is better than naming the wrong
    one — the whole point of the field is that a reader in Delhi is not told
    Thane news is theirs.
    """
    global _PLACES
    if _PLACES is None:
        doc = yaml.safe_load(config.SOURCES_FILE.read_text()) or {}
        raw = doc.get("places") or {}
        _PLACES = sorted(((k.lower(), v) for k, v in raw.items()),
                         key=lambda kv: -len(kv[0]))
    u = (url or "").lower()
    for key, place in _PLACES:
        if key in u:
            return place
    return ""


def _place_of(arts):
    """The place a story is about: the commonest place among its articles.

    Commonest rather than first, because one event is often carried by several
    city feeds and the one that filed first is not necessarily the one it
    happened in. Ties break toward the earliest article, which is stable across
    runs so a story does not change city on a retell. "" when no article carries
    a place — most stories are not local at all.
    """
    counts = {}
    for a in arts:
        p = (a["place"] if "place" in a.keys() else "") or ""
        if p:
            counts[p] = counts.get(p, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def entity_terms(blob, *keys):
    """Clean terms for the given keys of a stored `articles.entities` blob.

    Tolerates every shape that column has ever held: missing, empty string,
    non-dict JSON, a key present but null, and lists of dicts.
    """
    doc = db.uj(blob)
    if not isinstance(doc, dict):
        return []
    out = []
    for k in (keys or ("entities", "sectors")):
        out += _terms(doc.get(k))
    return list(dict.fromkeys(out))


def clean_entities(out):
    """Normalise one `entities` LLM response before it is stored."""
    doc = out if isinstance(out, dict) else {}
    return {"entities": _terms(doc.get("entities")),
            "sectors": _terms(doc.get("sectors")),
            "regions": _terms(doc.get("regions"))}


# --------------------------------------------------- Near-duplicate merging
def _verify_same_story(pairs):
    """Resolve borderline pairs with ONE batched LLM call.

    `pairs` is [(titleA, titleB), ...]; returns the set of indices the model says
    describe the same event. Any failure returns an empty set, which simply leaves
    those pairs unmerged.
    """
    if not pairs:
        return set()
    listing = "\n".join(f"[{i}] A: {a}\n    B: {b}" for i, (a, b) in enumerate(pairs))
    out = llm.complete_json("same_story", prompt("same_story", pairs=listing))
    if not out:
        return set()
    accepted = set()
    for i in out.get("same", []) or []:
        try:
            accepted.add(int(i))
        except (TypeError, ValueError):
            continue
    return accepted


# Slack on top of the pairing window when deciding how far back assign_groups
# reads. Covers clock skew and feed timestamps that run slightly ahead/behind,
# so an article can never miss a partner it was genuinely eligible to pair with.
GROUPING_MARGIN_HOURS = 12


def assign_groups(con):
    """Group articles reporting the SAME event so the LLM stages process each
    real-world story ONCE instead of once per outlet.

    Uses textmerge.group_articles: TF-IDF over title+summary combined with
    proper-noun/number anchors and a time window. The previous title-only
    term-frequency match was effectively inert — on a 269-article corpus it found
    a single cross-source pair, because two newsrooms rarely word a headline alike.
    Every member is stamped with a shared group_id (the earliest member).
    Cross-source rows are kept — corroboration still counts distinct sources; only
    the redundant LLM work is removed."""
    # Load only what can still form a NEW pair, not the whole retention window.
    # textmerge.pair_score hard-rejects any pair more than DEDUPE_WINDOW_HOURS
    # apart, and the newest article is at most INGEST_MAX_AGE_HOURS old, so
    # nothing older than those combined (plus a margin) can group with anything
    # this run ingested — it would only re-derive the group_id a previous run
    # already stamped on it. That matters because group_articles costs
    # SUPER-LINEAR memory in the number of rows handed to it (measured on real
    # feed text: 250 rows -> 2.6MB, 1000 -> 11.7MB, 1902 -> 27.8MB, and far
    # worse on repetitive corpora), so re-grouping a week of accumulated
    # articles on every run is what turns a 512MB instance into an OOM kill.
    # min() so a deployment that deliberately sets a SHORTER DEDUPE_WINDOW_DAYS
    # still gets the shorter window.
    span_hours = (config.DEDUPE_WINDOW_HOURS + config.INGEST_MAX_AGE_HOURS
                  + GROUPING_MARGIN_HOURS)
    cutoff = db.now() - min(config.DEDUPE_WINDOW_DAYS * 86400, span_hours * 3600)
    rows = con.execute(
        "SELECT id, title, summary, source, published, fetched_at, group_id FROM articles "
        "WHERE fetched_at > ? ORDER BY fetched_at ASC", (cutoff,)).fetchall()
    if not rows:
        db.log_run(con, "dedupe", "ok", "no recent articles to group")
        return 0
    clusters, stats = textmerge.group_articles(rows, verify=_verify_same_story)
    # EXTEND the groups earlier runs established rather than recomputing them
    # from scratch. Required because the load window above is narrower than the
    # pairing gate's reach: an article inside the window can have a partner just
    # outside it, so blindly restamping rep=cluster[0] detaches it from its
    # existing group and the one event becomes two stories (verified: it does).
    # Rules: a lone article that already belongs somewhere is left attached; a
    # cluster inherits the earliest established group id among its members, so
    # new articles JOIN the running group instead of forking it; and a cluster
    # spanning two established groups merges them under the earlier id.
    prior = {r["id"]: (r["group_id"] or "") for r in rows}
    merged = 0
    for cluster in clusters:
        established = [prior[a] for a in cluster if prior.get(a)]
        if len(cluster) == 1 and established:
            continue                      # already grouped; nothing new to say
        rep = established[0] if established else cluster[0]
        for aid in cluster:
            if prior.get(aid) != rep:
                con.execute("UPDATE articles SET group_id=? WHERE id=?", (rep, aid))
        merged += len(cluster) - 1
    con.commit()
    db.log_run(con, "dedupe", "ok",
               f"{len(clusters)} groups from {len(rows)} articles "
               f"(last {span_hours:g}h), {merged} cross-source duplicates merged "
               f"({stats['borderline']} borderline, {stats['verified']} LLM-confirmed)")
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
                ent = db.j(clean_entities(out))
                called += 1
            for m in members:
                con.execute("UPDATE articles SET entities=? WHERE id=?", (ent, m["id"]))
            con.commit()   # per group — see the note in Storyteller.run: a commit
                           # only after the loop holds the write lock across every
                           # later group's LLM call and stalls signed-in readers
        con.commit()
        db.log_run(con, "entities", "ok",
                   f"{called} groups tagged + {copied} copied from reps "
                   f"over {len(rows)} articles ({len(groups)} groups)")
        return called + copied


# ----------------------------------------------------------- Trend Linker
# How long a retired trend stays readable before it is really deleted. Long
# enough that a shared link keeps working well past the news cycle it came from.
RETIRE_PURGE_DAYS = config.TREND_RETIRE_PURGE_DAYS


def _numbered_items(rows):
    """One indexed line per article for the (single) trend call: [i] source,
    title, entities/sectors, summary excerpt. The [i] index is how the LLM
    references which articles belong to each trend, so it must stay 1 per line."""
    lines = []
    for i, a in enumerate(rows, 1):
        keys = a.keys()
        tags = ", ".join(entity_terms(a["entities"] if "entities" in keys else "")[:6])
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
    fresh set are RETIRED — but only when prune=True. Pass prune=False when some
    per-unit calls failed this run, so a transient error can't retire good trends.

    Retiring is a soft delete (`retired_at` stamped, row kept): the trend leaves
    the radar but its page and any shared link still resolve, and if the story
    resurfaces later the match below un-retires it with its id and history intact.
    Rows are only really deleted once they've been retired for RETIRE_PURGE_DAYS.
    Returns (new, updated, retired)."""
    # Match on NAME only (same key as _dedupe_trends): narratives are re-worded
    # every generation, and folding them into the vector dilutes real name matches.
    # Retired trends take part in matching too — that is what lets one come back.
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
            # retired_at=NULL revives a previously-retired trend rather than
            # creating a duplicate under a new id.
            con.execute(
                "UPDATE trends SET name=?,narrative=?,sectors=?,regions=?,article_ids=?,"
                "velocity=?,updated_at=?,retired_at=NULL WHERE id=?",
                (f["name"], f["narrative"], db.j(f["sectors"]), db.j(f["regions"]),
                 db.j(f["article_ids"]), f["velocity"], db.now(), cand["id"]))
            matched.add(cand["id"])
            upd_ct += 1
        else:
            con.execute(
                # updated_at seeded to created_at, never NULL: readers sort on the
                # bare column so an index can serve them (see db.py migration).
                "INSERT INTO trends (id,kind,name,narrative,sectors,regions,article_ids,"
                "velocity,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), kind, f["name"], f["narrative"], db.j(f["sectors"]),
                 db.j(f["regions"]), db.j(f["article_ids"]), f["velocity"],
                 db.now(), db.now()))
            new_ct += 1
    retired = 0
    if prune:
        for e in existing:
            if e["id"] not in matched:  # no longer a current trend
                # Stamp only on the way out, so retired_at keeps recording when it
                # LEFT the radar rather than being refreshed by every later run.
                retired += con.execute(
                    "UPDATE trends SET retired_at=? WHERE id=? AND retired_at IS NULL",
                    (db.now(), e["id"])).rowcount
        # Real deletion, long after the link has stopped being useful to anyone.
        con.execute("DELETE FROM trends WHERE retired_at IS NOT NULL AND retired_at < ?",
                    (db.now() - RETIRE_PURGE_DAYS * 86400,))
    return new_ct, upd_ct, retired


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
            "SELECT name FROM trends WHERE retired_at IS NULL "
            "ORDER BY velocity DESC, created_at DESC LIMIT 40").fetchall()]
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
                   f"macro {mn} new/{mu} upd/{mr} retired, "
                   f"micro {cn} new/{cu} upd/{cr} retired, "
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

    MAX_PAIRS = 12  # LLM budget guard — NOT a memory guard, see MAX_ARTICLES

    def run(self, con):
        # This stage is inherently O(n²) in articles, which is why `n` has a hard
        # ceiling and the per-article work is done ONCE rather than per pair.
        #
        # It OOM-killed the 512MB instance repeatedly. Every article in a 7-day
        # window was loaded (production ingests ~2,300 per 72h, so n ≈ 5,000 →
        # ~13 million pairs), and inside the pair loop each side re-ran `_tf()`
        # (builds a term dict) and `entity_terms()` (which json.loads the blob) —
        # four parses per pair, ~50 million allocations — while `cands`
        # accumulated a tuple holding two full row objects for every pair that
        # shared any entity. All of it to choose twelve pairs. Adding four feeds
        # grew n by enough that the process stopped surviving the stage: the
        # pipeline reached `trends` and died before `stories` on every run.
        #
        # Three changes, none of which alter which pairs are considered best:
        #   1. only articles that CAN match are read — the candidate test needs a
        #      shared entity, so an article with no entities blob is dead weight;
        #   2. tf-vector and entity set are computed once per article;
        #   3. the top pairs are kept in a bounded heap instead of a list that
        #      grows with the square of the corpus.
        rows = con.execute(
            "SELECT id,title,summary,entities FROM articles "
            "WHERE fetched_at > ? AND entities IS NOT NULL AND entities != '' "
            "ORDER BY fetched_at DESC LIMIT ?",
            (db.now() - 7 * 86400, config.CONNECTIONS_MAX_ARTICLES)).fetchall()
        docs = []
        for r in rows:
            ents = set(entity_terms(r["entities"]))
            if ents:
                docs.append((r["id"], _tf((r["title"] or "") + (r["summary"] or "")),
                             ents, r))
        # Incremental: never re-evaluate a pair we've already asked the LLM about.
        seen_pairs = set()
        for r in con.execute("SELECT article_a, article_b FROM connections").fetchall():
            seen_pairs.add(frozenset((r["article_a"], r["article_b"])))
        # (count, tiebreak, a, b), smallest-first: the heap never holds more than
        # MAX_PAIRS entries, so peak memory no longer depends on how many pairs
        # happen to share an entity.
        best, tie = [], 0
        for i in range(len(docs)):
            a_id, a_tf, a_ents, a = docs[i]
            for k in range(i + 1, len(docs)):
                b_id, b_tf, b_ents, b = docs[k]
                if frozenset((a_id, b_id)) in seen_pairs:
                    continue
                # Entity overlap first: a set intersection on pre-built sets is
                # far cheaper than the cosine, and it rejects most pairs.
                shared = a_ents & b_ents
                if not shared:
                    continue
                if cosine(a_tf, b_tf) > 0.3:
                    continue  # obviously related; trends handle those
                tie += 1
                if len(best) < self.MAX_PAIRS:
                    heapq.heappush(best, (len(shared), tie, a, b))
                elif len(shared) > best[0][0]:
                    heapq.heapreplace(best, (len(shared), tie, a, b))
        cands = sorted(best, key=lambda x: -x[0])
        made = 0
        for _, _tie, a, b in cands:
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
            con.commit()   # per pair — see the note in Storyteller.run
            if conf >= 0.6:
                made += 1
        con.commit()
        db.log_run(con, "connections", "ok",
           f"{made} connections from {len(cands)} pairs kept "
           f"of {tie} scanned over {len(docs)} articles")
        return made


# --------------------------------------------------------------- Verifier
class Verifier:
    def __init__(self):
        doc = yaml.safe_load(config.SOURCES_FILE.read_text()) or {}
        # Only the tierN keys are tiers. `kinds` lives in the same file (one
        # place to describe a source) but is a different axis — iterating the
        # whole document here would try int("kinds"[-1]).
        self.tiers = {k: v for k, v in doc.items() if k.startswith("tier")}
        self.kinds = doc.get("kinds") or {}

    def source_tier(self, source):
        for tier, hosts in self.tiers.items():
            if any(h in source for h in hosts):
                return int(tier[-1])
        return 4

    def source_kind(self, source):
        """What relationship this outlet has to the story it reports.

        Distinct from tier: a reputable vendor is still an interested party.
        Unlisted hosts are "unknown", never assumed to be newsrooms — the count
        the reader sees has to be one we can actually stand behind."""
        for kind, hosts in self.kinds.items():
            if any(h in (source or "") for h in (hosts or [])):
                return kind
        return "unknown"

    def source_breakdown(self, sources):
        """{kind: count} over a story's outlets, for "Where it's coming from"."""
        out = {}
        for s in sources:
            k = self.source_kind(s)
            out[k] = out.get(k, 0) + 1
        return out

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
def verdict_counts(verdicts):
    """{verified, disputed, unverified} from the Verifier's per-claim verdicts.

    The vocabulary on the wire is corroborated|unverified|disputed; the reader
    says VERIFIED / DISPUTED / UNPROVEN. Mapping happens here, once, so the two
    clients and the history table can't drift apart on it."""
    out = {"verified": 0, "disputed": 0, "unverified": 0}
    for v in verdicts or []:
        got = str((v or {}).get("verdict", "")).lower()
        if got.startswith("corrob"):
            out["verified"] += 1
        elif got.startswith("disput"):
            out["disputed"] += 1
        else:
            out["unverified"] += 1
    return out


def depth_hint(bstats, source_count):
    """How much story the material can actually support.

    Median corroboration in production is ~25 — most stories are carried by a
    single outlet. Asking for 400-600 words from one wire summary is asking the
    model to pad, and padding in a news product means invention. So the target
    scales with the evidence actually present (corroborated facts and how many
    outlets carried them) instead of being a fixed number the prompt must hit."""
    core = int((bstats or {}).get("core") or 0)
    srcs = max(int((bstats or {}).get("sources") or 0), int(source_count or 0))
    if srcs >= 4 or core >= 5:
        return ("Aim for 4-5 beats totalling 420-600 words: this event is well "
                "covered and there is enough corroborated material to justify it.")
    if srcs >= 2 or core >= 2:
        return ("Aim for 3-4 beats totalling 280-420 words: several outlets "
                "carried this, but do not stretch beyond what they report.")
    return ("Aim for 2-3 beats totalling 180-260 words: this is thinly sourced, "
            "so keep it tight and do not elaborate beyond the brief.")


def clean_beats(raw, fallback_text):
    """[{label, text}] or None. None means "render the old way" — never a
    fabricated single beat, so the client can tell a real structure from a
    story written before beats existed."""
    if not isinstance(raw, list):
        return None
    out = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        label = str(b.get("label") or "").strip()[:60]
        text = str(b.get("text") or "").strip()
        if label and len(text) >= 40:
            out.append({"label": label, "text": text})
    # One beat is not a structure, it is the whole story with a heading on it.
    return out if len(out) >= 2 else None


def clean_anchors(raw, claims, beats):
    """[{claim, quote, verdict_index}] keeping only quotes that REALLY occur in
    the prose we are about to publish. The model is asked to copy verbatim, but
    an approximation would put a verdict on a sentence that never carried the
    claim — worse than showing no marking at all, so it is dropped here rather
    than left for the client to match loosely."""
    if not isinstance(raw, list) or not beats:
        return None
    body = "\n".join(b["text"] for b in beats)
    out, seen = [], set()
    for a in raw:
        if not isinstance(a, dict):
            continue
        try:
            idx = int(a.get("claim"))
        except (TypeError, ValueError):
            continue
        quote = str(a.get("quote") or "").strip()
        if not (0 <= idx < len(claims)) or len(quote) < 24 or quote in seen:
            continue
        if quote not in body:
            continue                      # not verbatim — drop, never fuzzy-match
        seen.add(quote)
        out.append({"claim": idx, "quote": quote})
    return out or None


# --------------------------------------------------------------- corrections
# A story we published can stop holding up: an outlet drops out, a claim we had
# corroborated starts being disputed, or outlets begin reporting different
# numbers. story_history already records every one of those moves; nothing read
# it back. This is what does.
#
# What this is NOT: a retraction notice. We observe our OWN corroboration
# falling — we cannot see a publisher retract anything, and saying "retracted"
# would put a claim in the reader's head that nobody made. Every string this
# produces says what actually happened: corroboration weakened, or outlets now
# disagree.
def detect_correction(rows, min_drop=None):
    """A correction for one story, or None. `rows` is its story_history in
    ASCENDING time order (what main._history already returns).

    Judged against the story's own PEAK, not against the previous row: a score
    that slides 71 -> 62 -> 54 over three runs never drops far enough between
    any two adjacent rows to be noticed, and that slow slide is exactly the case
    a reader most needs told."""
    drop = config.CORRECTION_MIN_DROP if min_drop is None else min_drop
    rows = [r for r in (rows or []) if r is not None]
    if len(rows) < 2:
        return None            # one data point is a starting value, not a change
    get = lambda r, k: (r[k] if not hasattr(r, "keys") or k in r.keys() else None)
    cur = rows[-1]
    now_c = float(get(cur, "credibility") or 0)
    peak = max(rows, key=lambda r: float(get(r, "credibility") or 0))
    peak_c = float(get(peak, "credibility") or 0)
    # Only claims that were checked at BOTH ends count. A story whose earliest
    # history row predates verdict counting has None there, and None is "we
    # don't know", never "there were zero disputed claims then".
    first_d = get(rows[0], "disputed")
    now_d = get(cur, "disputed")
    first_x = get(rows[0], "conflicts")
    now_x = get(cur, "conflicts")
    disputed_added = (int(now_d) - int(first_d)
                      if now_d is not None and first_d is not None else 0)
    conflicts_added = (int(now_x) - int(first_x)
                       if now_x is not None and first_x is not None else 0)
    fell = (peak_c - now_c) >= drop
    if not fell and disputed_added <= 0 and conflicts_added <= 0:
        return None
    # A claim flipping is a stronger, more specific statement than a score
    # sliding, so it wins the label when both happened.
    # PLAIN WORDS, as the design requires: these strings are printed verbatim on
    # the feed, the reader, Saved and Read, and the mockups are explicit that
    # nothing in the product says "corroborated", "verified" or "disputed".
    kind = "contested" if disputed_added > 0 else "weakened"
    if kind == "contested":
        note = (f"{disputed_added} fact{'' if disputed_added == 1 else 's'} we "
                f"checked {'is' if disputed_added == 1 else 'are'} now argued over "
                f"by the sources covering this.")
    elif conflicts_added > 0 and not fell:
        kind = "conflicting"
        note = (f"Outlets have started reporting {conflicts_added} figure"
                f"{'' if conflicts_added == 1 else 's'} differently since this was "
                f"first told.")
    else:
        note = (f"Fewer sources agree now than when this was first told — "
                f"down from {round(peak_c)} to {round(now_c)}.")
    return {"kind": kind,
            "from": round(peak_c, 1), "to": round(now_c, 1),
            "delta": round(now_c - peak_c, 1),
            "disputed_added": max(0, disputed_added),
            "conflicts_added": max(0, conflicts_added),
            "since": get(peak, "created_at"), "note": note}


# ------------------------------------------------------------------- framing
def clean_framing(raw, sources):
    """{axis, positions, spread, missing} or None.

    `sources` is the set of outlet hosts this story actually cites. Any position
    naming an outlet we did not ingest is dropped rather than shown: the panel's
    whole claim is "here is where the outlets we read sat", and an invented
    twelfth outlet would break it in the way readers are least able to check."""
    if not isinstance(raw, dict):
        return None
    axis = raw.get("axis") or {}
    left = str(axis.get("left") or "").strip()[:40]
    right = str(axis.get("right") or "").strip()[:40]
    if not (left and right):
        return None                       # an unnamed axis places nothing
    allowed = {str(s or "").strip().lower() for s in (sources or []) if s}
    seen, positions = set(), []
    for p in (raw.get("positions") or []):
        if not isinstance(p, dict):
            continue
        src = str(p.get("source") or "").strip()
        key = src.lower()
        if not src or key in seen:
            continue
        # Tolerant on shape (a model may answer "BBC News" or "www.bbc.co.uk"),
        # strict on membership: it has to resolve to an outlet we really cite.
        match = next((a for a in allowed if a == key or a in key or key in a), None)
        if not match:
            continue
        try:
            pos = max(-1.0, min(1.0, float(p.get("pos"))))
        except (TypeError, ValueError):
            continue
        seen.add(key)
        positions.append({"source": match, "pos": round(pos, 2),
                          "note": str(p.get("note") or "").strip()[:120]})
    if len(positions) < 2:
        return None       # a spectrum needs two points; one outlet is not a spread
    span = max(p["pos"] for p in positions) - min(p["pos"] for p in positions)
    return {"axis": {"left": left, "right": right},
            "positions": positions,
            # Named, not just numeric, so the client never has to invent the
            # threshold vocabulary and the two clients cannot disagree on it.
            "spread": "wide" if span >= 1.0 else "moderate" if span >= 0.5 else "narrow",
            "span": round(span, 2),
            "missing": str(raw.get("missing") or "").strip()[:200]}


class Framer:
    """"How outlets framed this" — computed the first time a reader opens the
    panel on a given story, then cached on the story row for everyone.

    Per STORY, not per user (unlike Personalizer), so the first reader pays for
    it and every later reader — including signed-out ones — gets it free. Works
    on stories written long before phase 5, because it needs only the articles
    the story already cites."""

    # Enough coverage per outlet for the model to see a frame, small enough that
    # a 20-source story doesn't blow the context. Ordered by the story's own
    # article order, so the outlets that led the reporting are the ones kept.
    MAX_SOURCES = 10
    EXCERPT = 400

    def classify(self, con, story_row, articles=None):
        """Returns (framing_dict_or_None, reason). `reason` is why it is None:
        'ok' | 'cached' | 'single_source' | 'llm_unavailable' | 'no_spread'."""
        cached = story_row["framing"] if "framing" in story_row.keys() else None
        if cached:
            return db.uj(cached) or None, "cached"
        if articles is None:
            ids = db.uj(story_row["article_ids"], [])
            articles = [con.execute(
                "SELECT title, summary, source FROM articles WHERE id=?", (i,)).fetchone()
                for i in ids]
            articles = [a for a in articles if a]
        by_source = {}
        for a in articles:
            src = (a["source"] or "").strip()
            if src and src not in by_source:
                by_source[src] = a
        if len(by_source) < 2:
            # Not a failure and not worth an LLM call: one outlet cannot
            # disagree with itself. Cached as a real answer so re-opening the
            # panel doesn't retry forever.
            self._store(con, story_row["id"], {"sources": len(by_source)})
            return None, "single_source"
        blocks = []
        for src, a in list(by_source.items())[:self.MAX_SOURCES]:
            blocks.append(f"[{src}]\nHeadline: {a['title'] or ''}\n"
                          f"Report: {(a['summary'] or '')[:self.EXCERPT]}")
        out = llm.complete_json("framing", prompt("framing", coverage="\n\n".join(blocks)))
        if out is None:
            # Don't cache an LLM outage as "no framing" — that would make a
            # transient rate limit permanent for this story. Let the next open
            # try again.
            return None, "llm_unavailable"
        framing = clean_framing(out, by_source.keys())
        # A genuine "they all framed it the same way" is an answer worth
        # caching; so is a malformed reply, which we record as no-spread rather
        # than re-asking on every open.
        self._store(con, story_row["id"], framing or {"sources": len(by_source)})
        return framing, "ok" if framing else "no_spread"

    @staticmethod
    def _store(con, story_id, payload):
        con.execute("UPDATE stories SET framing=? WHERE id=?",
                    (db.j(payload), story_id))
        con.commit()


class Storyteller:
    # Configurable via MAX_STORIES_PER_RUN env var; the rest roll to next run.
    MAX_PER_RUN = config.MAX_STORIES_PER_RUN

    @staticmethod
    def _record_history(con, story_id, event_id, score, source_count,
                        verdicts, bstats):
        """Append to story_history — but only when something actually moved.

        Writing a row per story per run regardless would make the table grow by
        the whole catalogue every cycle to record that nothing happened, which
        on a 512MB box with a 90-day retention is exactly the kind of unbounded
        write we have been bitten by before. A quiet story writes nothing, so
        the table is a log of CHANGES and "68 -> 82" reads straight off two
        adjacent rows.

        No commit here: this runs inside the Storyteller's per-story write, and
        the caller owns the transaction boundary."""
        vc = verdict_counts(verdicts)
        conflicts = int((bstats or {}).get("conflicts") or 0)
        prev = con.execute(
            "SELECT credibility, source_count, verified, disputed, unverified, "
            "conflicts FROM story_history WHERE story_id=? "
            "ORDER BY created_at DESC LIMIT 1", (story_id,)).fetchone()
        now_row = (round(float(score or 0), 1), int(source_count or 0),
                   vc["verified"], vc["disputed"], vc["unverified"], conflicts)
        if prev is not None:
            was = (round(float(prev["credibility"] or 0), 1),
                   int(prev["source_count"] or 0), int(prev["verified"] or 0),
                   int(prev["disputed"] or 0), int(prev["unverified"] or 0),
                   int(prev["conflicts"] or 0))
            if was == now_row:
                return False
        con.execute(
            "INSERT INTO story_history (id, story_id, event_id, credibility, "
            "source_count, verified, disputed, unverified, conflicts, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (db.new_id(), story_id, event_id or "") + now_row + (db.now(),))
        return True

    def run(self, con, verifier: Verifier):
        """Turn each macro trend cluster (and orphan articles) into a story.
        A trend that already has a story gets that story UPDATED in place when new
        articles arrive (same id, refreshed narrative) — never a second, near-
        duplicate story for the same trend."""
        # Bounded, not "every story ever": a story untouched in
        # STORYTELLER_HISTORY_DAYS has no realistic new coverage to receive
        # (the feed itself only shows 7 days, and a candidate article is never
        # older than 7 days either) — see config.STORYTELLER_HISTORY_DAYS for
        # why this must not go back to an unbounded SELECT.
        prior = [{"id": r["id"], "aids": set(db.uj(r["article_ids"], [])),
                  "tids": set(db.uj(r["trend_ids"], [])), "eid": r["event_id"]}
                 for r in con.execute(
                     "SELECT id, article_ids, trend_ids, event_id FROM stories "
                     "WHERE updated_at > ?",
                     (db.now() - config.STORYTELLER_HISTORY_DAYS * 86400,)).fetchall()]
        done_ids = set()
        for p in prior:
            done_ids.update(p["aids"])
        new_ct = upd_ct = absorbed = 0
        # Live trends only — a retired trend must not spawn or refresh stories.
        trends = con.execute(
            "SELECT * FROM trends WHERE kind='macro' AND retired_at IS NULL").fetchall()
        live_tids = {t["id"] for t in trends}
        # A macro trend is a THEME, not an event: it deliberately spans many
        # separate happenings. Telling one story per trend therefore merged
        # unrelated reporting into a single item — production had an "OpenAI
        # sandbox escape" story holding 234 articles that this pipeline's own
        # same-event clusterer scores as 225 distinct events, among them a
        # humanoid-robot import ban and a $5.7bn bond-market acquisition.
        # So split each trend by the group_id the Deduper already assigned —
        # exactly what the orphan path below has always done — making a story one
        # EVENT, with the trend demoted to a label linked via trend_ids.
        # Keyed by event across ALL trends, not per trend: one event can belong to
        # two themes, and emitting it once per theme would write two stories with
        # the same event_id (they would never match each other, since `prior` is
        # read once up front). Consolidating first also gives the story the whole
        # event, not one trend's slice of it.
        # EVERY theme an event belongs to, not just the first one to claim it.
        # This used to be `setdefault(key, t)` — "first theme wins the label" —
        # which wrote one trend id onto the story and silently dropped the rest.
        # `trend_ids` is a list precisely because an event can sit in two themes,
        # and /trends counts a trend's stories by looking for its id in there:
        # every theme that lost the race ended up reporting `story_count: 0`
        # while holding a dozen articles, so its card said "no scored story this
        # week" and its "See all N stories" page came back empty. 26 of 40 live
        # macro trends were in that state.
        by_event, event_trends = {}, {}
        for t in trends:
            tids = db.uj(t["article_ids"], [])
            if not tids:
                continue
            for r in con.execute(
                    "SELECT id, group_id FROM articles WHERE id IN (%s)"
                    % ",".join("?" * len(tids)), tids).fetchall():
                key = r["group_id"] or r["id"]
                by_event.setdefault(key, set()).add(r["id"])
                seen = event_trends.setdefault(key, [])
                if not any(x["id"] == t["id"] for x in seen):
                    seen.append(t)
        # Orphan articles (not in any trend) become stories too — but near-duplicates
        # from different sources are merged into ONE story per group_id, so the same
        # event isn't storied (and LLM-called) once per source.
        in_trend = {i for ids in by_event.values() for i in ids}
        for o in con.execute(
                "SELECT id, group_id FROM articles WHERE fetched_at > ?",
                (db.now() - 7 * 86400,)).fetchall():
            if o["id"] not in in_trend:
                # An event can be part in a trend and part not; keying into the
                # same dict rejoins those halves instead of emitting a second
                # group under an event_id a trend group already owns.
                by_event.setdefault(o["group_id"] or o["id"], set()).add(o["id"])
        # Repair pass for stories written under the old "first theme wins" rule.
        # It cannot heal itself in the loop below: a story with no new articles
        # is skipped before it is ever rewritten, so a trend that lost the race
        # would report story_count 0 for as long as its coverage stayed quiet.
        # This is a pure label fix — no LLM, no re-telling, and it only ever adds
        # ids, so a story never loses a theme it already carried.
        relabelled = 0
        for p in prior:
            if not p["eid"]:
                continue
            want = {t["id"] for t in event_trends.get(p["eid"], [])}
            if want and not want <= p["tids"]:
                p["tids"] |= want
                con.execute("UPDATE stories SET trend_ids=? WHERE id=?",
                            (db.j(sorted(p["tids"])), p["id"]))
                relabelled += 1
        if relabelled:
            con.commit()

        groups = [(event_trends.get(k, []), sorted(v), k) for k, v in by_event.items()]
        for evt_trends, ids, event_id in groups:
            # The first theme still names the story's topic; all of them label it.
            trend = evt_trends[0] if evt_trends else None
            evt_tids = {t["id"] for t in evt_trends}
            if new_ct + upd_ct >= self.MAX_PER_RUN:
                break
            if not ids:
                continue
            # The story already telling this EVENT. Identity is the event, never
            # the trend: matching on trend id would hand every event of a theme
            # to the same row and rebuild exactly the blob this split undoes.
            mine = next((p for p in prior if p["eid"] and p["eid"] == event_id), None)
            if mine is None:
                # Legacy rows carry no event_id, so they still match on article
                # overlap — but only when the row is itself event-sized. Without
                # that guard an existing 234-article blob contains every small
                # event group outright (containment 1.0) and would swallow them.
                mine = next(
                    (p for p in prior if not p["eid"]
                     and _containment(ids, p["aids"]) >= 0.5
                     and len(p["aids"]) <= max(len(ids) * 3, 12)), None)
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
            # Merge the group's articles into ONE attributed storyline: corroborated
            # facts, details only a single outlet carried, and numeric disagreements.
            # Fuller article text (transient, never stored) gives the merge real
            # material — RSS summaries alone average ~150 characters.
            texts = fulltext.fetch_for_articles(arts) if len(arts) > 1 else {}
            items, bstats = textmerge.build_brief(
                arts, texts=texts, tier_of=verifier.source_tier)
            # bstats = {facts, core, unique, conflicts, sources}. `conflicts` is
            # the count of points where outlets report different numbers — the
            # design's "framing spread" / "where they differ". It was computed
            # here and thrown away on every run since the merge was written.
            bstats = dict(bstats or {})
            bstats["kinds"] = verifier.source_breakdown(a["source"] for a in arts)
            if not items:       # nothing usable survived cleaning — keep the old shape
                items = "\n".join(
                    f"- [{a['source']}] {a['title']}: {(a['summary'] or '')[:200]}"
                    for a in arts)
            verified = verifier.run(con, ids, items)
            if verified is None:
                continue  # LLM unavailable — article kept for next run
            claims, verdicts, score, note = verified
            out = llm.complete_json("story", prompt(
                "story", claims=db.j(claims), items=items,
                depth=depth_hint(bstats, len({a["source"] for a in arts}))))
            if out is None:
                continue
            headline = out.get("headline", arts[0]["title"])
            # The prompt returns the storyline and the implications as separate
            # fields. `narrative` keeps its original meaning (the story body), so
            # feed previews / OG / search are unaffected; "why it matters" gets
            # its own column instead of being recovered by splitting paragraphs,
            # which is what previously left "What happened" as just the hook and
            # dumped the whole storyline into "Why it matters".
            # `narrative` is still accepted as a fallback key so a model that
            # answers in the old single-field shape degrades instead of failing.
            narrative = out.get("what_happened") or out.get("narrative") or items
            why_matters = out.get("why_it_matters", "")
            beats = clean_beats(out.get("beats"), narrative)
            anchors = clean_anchors(out.get("anchors"), claims, beats)
            # When beats came back well-formed they ARE the story, so `narrative`
            # becomes their joined text: everything that reads `narrative` today
            # (OG cards, SEO descriptions, the feed dek, iOS) keeps working and
            # simply sees the fuller piece, with no client change required.
            if beats:
                narrative = "\n\n".join(b["text"] for b in beats)
            conn_ids = [r["id"] for r in con.execute(
                "SELECT id FROM connections WHERE confidence >= 0.6 AND "
                "(article_a IN (%s) OR article_b IN (%s))"
                % (",".join("?" * len(ids)), ",".join("?" * len(ids))),
                ids + ids).fetchall()]
            if mine:  # developing story: refresh content, stamp the retell
                # updated_at, NOT created_at: stamping created_at here reset the
                # story's age to zero every run, so a storyline that accreted one
                # article per cycle never aged out of the top of the feed. Ranking
                # gives a retell bounded credit instead (see ranking.STALE_FLOOR).
                # `trend` is None for an orphan event, which can now match a prior
                # story by event_id — so guard the union instead of indexing None.
                tids = mine["tids"] | evt_tids
                # A cached framing spectrum describes the outlets that had
                # covered the event when it was classified. If a NEW OUTLET has
                # since joined, that picture is stale — drop it so the next
                # reader who opens the panel gets one that includes them. A
                # retell that only adds another piece from an outlet already
                # placed leaves it alone, so a developing story doesn't re-pay
                # for the same classification every run. (No extra query: `arts`
                # already covers both the old and the new article ids.)
                old_srcs = {a["source"] for a in arts if a["id"] in mine["aids"]}
                new_srcs = {a["source"] for a in arts}
                drop_framing = bool(new_srcs - old_srcs)
                # Re-pick on every retell: a later outlet joining the event may
                # carry a better frame than whatever the first telling had.
                con.execute(
                    "UPDATE stories SET headline=?,narrative=?,why_matters=?,credibility=?,"
                    "credibility_note=?,claims=?,article_ids=?,trend_ids=?,"
                    "connection_ids=?,updated_at=?,event_id=?,image_url=?,"
                    "merge_stats=?,beats=?,anchors=?,place=?"
                    + (",framing=NULL" if drop_framing else "") + " WHERE id=?",
                    (headline, narrative, why_matters, score, note,
                     db.j({"claims": claims, "verdicts": verdicts}), db.j(ids),
                     db.j(sorted(tids)), db.j(conn_ids),
                     db.now(), event_id, images.best_of(arts), db.j(bstats),
                     db.j(beats) if beats else None,
                     db.j(anchors) if anchors else None,
                     _place_of(arts),
                     mine["id"]))
                self._record_history(con, mine["id"], event_id, score, len(ids),
                                     verdicts, bstats)
                mine["aids"] = set(ids)
                mine["eid"] = event_id   # legacy row adopts its event identity
                upd_ct += 1
            else:
                sid = db.new_id()
                con.execute(
                    "INSERT INTO stories (id,headline,narrative,why_matters,credibility,"
                    "credibility_note,claims,topic,article_ids,trend_ids,connection_ids,"
                    "created_at,updated_at,event_id,image_url,merge_stats,beats,anchors,"
                    "place) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, headline, narrative, why_matters, score, note,
                     db.j({"claims": claims, "verdicts": verdicts}), arts[0]["topic"],
                     db.j(ids), db.j(sorted(evt_tids)), db.j(conn_ids),
                     db.now(), db.now(), event_id, images.best_of(arts), db.j(bstats),
                     db.j(beats) if beats else None,
                     db.j(anchors) if anchors else None,
                     _place_of(arts)))
                self._record_history(con, sid, event_id, score, len(ids),
                                     verdicts, bstats)
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
            # Commit each storyline as it is finished, NOT once at the end.
            # SQLite has one write lock. The first write above opens a
            # transaction, and a commit only at the end of the loop would hold
            # that lock across every remaining group's full-text fetches and
            # three LLM calls — minutes. GET /story/{id} writes too when the
            # reader is signed in (read_stories), so every signed-in story open
            # in that window queued behind this, waiting out busy_timeout.
            # Per-group commits also mean an OOM kill mid-run keeps the
            # storylines already told instead of discarding the whole pass.
            con.commit()
        con.commit()
        db.log_run(con, "storyteller", "ok",
                   f"{new_ct} new + {upd_ct} updated stories, {absorbed} absorbed"
                   + (f", {relabelled} relabelled" if relabelled else ""))
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
                ents += (entity_terms(a["entities"], "entities")[:4]
                         + entity_terms(a["entities"], "sectors")[:2])
        claims = db.uj(s["claims"]).get("claims") or []
        claim_txt = " ; ".join(str(c) for c in claims[:2])[:180]
        cred = s["credibility"] if "credibility" in s.keys() and s["credibility"] else 0
        narr = (s["narrative"] if "narrative" in s.keys() else "") or ""
        return (f"{s['id']} | {s['topic']} | {s['headline']} | cred {int(cred)} | "
                f"{', '.join(dict.fromkeys(ents))[:120]} | {claim_txt} | {narr[:160]}")

    def run(self, con):
        # Windows run from the last retell (COALESCE), so a storyline or forecast
        # still being developed stays in scope past WINDOW_DAYS from its first
        # telling — it just no longer gets to reset its rank age to zero.
        stories = con.execute(
            "SELECT id, headline, topic, narrative, credibility, claims, article_ids "
            "FROM stories WHERE updated_at > ? "
            "ORDER BY updated_at DESC",
            (db.now() - self.WINDOW_DAYS * 86400,)).fetchall()
        # Age out stale forecasts every run (instead of wiping all of them), so
        # a run that produces nothing new still leaves recent forecasts intact.
        #
        # RETIRED, NOT DELETED. This used to be `DELETE FROM signals`, which
        # destroyed every forecast a week after its last update — and with it any
        # possibility of ever saying "we said 26 things, 19 happened". A forecast
        # whose horizon is "18 months" was being thrown away 17 months before
        # anyone could tell whether it was right. Retirement takes it off
        # /signals (same rule trends already use) and keeps the row, so the
        # record accumulates from today forward instead of being erased daily.
        pruned = con.execute(
            "UPDATE signals SET retired_at = ? "
            "WHERE retired_at IS NULL AND updated_at < ?",
            (db.now(), db.now() - self.WINDOW_DAYS * 86400)).rowcount
        if len(stories) < 4:
            con.commit()
            db.log_run(con, "foresight", "ok",
                       f"too few stories to synthesize; pruned {pruned} stale")
            return 0
        by_topic = _by_topic(stories)
        # Show the LLM what is already forecast so it reuses titles verbatim
        # instead of re-wording the same forecast (which defeats dedupe).
        existing_titles = [r["title"] for r in con.execute(
            "SELECT title FROM signals WHERE retired_at IS NULL "
            "ORDER BY confidence DESC, created_at DESC LIMIT 30").fetchall()]
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
                        "SELECT id, title, story_ids FROM signals "
                        "WHERE retired_at IS NULL").fetchall()]
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
                    "falsifier=?, affected=?, horizon=?, confidence=?, story_ids=?, "
                    "updated_at=?, retired_at=NULL WHERE id=?",
                    (sig.get("title", match["title"]), sig.get("prediction", ""),
                     sig.get("chain", ""), sig.get("watch", ""),
                     sig.get("falsifier", ""),
                     db.j(sig.get("affected", [])), sig.get("horizon", ""), conf,
                     db.j(union), db.now(), match["id"]))
                match["story_ids"] = union
                match["vec"] = vec
                match["title"] = sig.get("title", match["title"])
                upd_ct += 1
            else:
                nid = db.new_id()
                # Explicit column list: a bare VALUES(...) is positional over
                # every column, so it broke the moment updated_at was added.
                con.execute(
                    "INSERT INTO signals (id,title,prediction,chain,watch,falsifier,"
                    "affected,horizon,confidence,story_ids,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, sig.get("title", "Signal"), sig.get("prediction", ""),
                     sig.get("chain", ""), sig.get("watch", ""),
                     sig.get("falsifier", ""),
                     db.j(sig.get("affected", [])), sig.get("horizon", ""), conf,
                     db.j(sids), db.now(), db.now()))
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


def personalization_relevant(ctx, story):
    """Free (no LLM) relevance test: does this story touch the reader's
    interests or location? `story` needs headline/narrative/topic keys — a
    stories row or the dict /feed already builds both satisfy that.

    Drives /feed's `sort=foryou` ordering directly now. The LLM itself is
    only called by Personalizer.personalize(), on demand, when a signed-in
    reader actually opens "What this means for you" on a specific story —
    see that method and POST /story/{id}/personalize."""
    interests = {s.lower() for s in ctx.get("interests", [])}
    # Location VALUES only — matching on str(dict) leaked the keys
    # ("city", "region", "country") and tagged nearly every story.
    loc_words = {w for v in ctx.get("location", {}).values()
                 for w in re.findall(r"[a-z]{3,}", str(v).lower())}
    text = (story["headline"] + " " + story["narrative"] + " " + story["topic"]).lower()
    words = set(re.findall(r"[a-z0-9]{2,}", text))
    return bool(
        story["topic"].lower() in interests
        or any(_phrase_in(i, words) for i in interests)
        or (loc_words & words))


class Personalizer:
    def personalize(self, con, user_row, story_row):
        """Return (impact_text, impact_score) for this (user, story) pair,
        computing and caching it on first request. Cached in `feed_items` —
        a repeat call (re-opening the module, or /feed's LEFT JOIN) never
        calls the LLM again for the same pair. Not relevant → cached as
        ("", 0) without ever touching the LLM."""
        cached = con.execute(
            "SELECT impact_text, impact_score FROM feed_items "
            "WHERE user_id=? AND story_id=?",
            (user_row["id"], story_row["id"])).fetchone()
        if cached:
            return cached["impact_text"], cached["impact_score"]
        ctx = db.uj(user_row["context"])
        impact_text, impact = "", 0
        if personalization_relevant(ctx, story_row):
            trend_ids = db.uj(story_row["trend_ids"], [])
            trends = [t["name"] for t in con.execute(
                "SELECT name FROM trends WHERE id IN (%s)" % ",".join("?" * len(trend_ids)),
                trend_ids).fetchall()] if trend_ids else []
            out = llm.complete_json("personalize", prompt(
                "personalize", context=db.j(ctx), headline=story_row["headline"],
                narrative=story_row["narrative"][:600], trends=db.j(trends)))
            if out is None:
                # LLM unavailable — don't cache a failure as "not relevant";
                # let the next open retry instead of permanently showing nothing.
                return "", 0
            impact_text = out.get("impact_text", "")
            impact = int(out.get("impact_score", 1))
        con.execute(
            "INSERT OR IGNORE INTO feed_items (id,user_id,story_id,impact_text,"
            "impact_score,created_at) VALUES (?,?,?,?,?,?)",
            (db.new_id(), user_row["id"], story_row["id"], impact_text, impact, db.now()))
        con.commit()
        return impact_text, impact


# ------------------------------------------------------- breaking detection
def detect_breaking(con, limit=8):
    """Scan recent stories with the no-LLM heuristic and return breaking-card dicts
    (highest urgency first). Source count = distinct sources across a story's
    member articles; age from the last retell. Pure read — caller writes cards."""
    window = config.BREAKING_WINDOW_HOURS
    min_src = config.BREAKING_MIN_SOURCES
    # Retell time, not first telling: unlike the ranked lists, "breaking" SHOULD
    # follow a developing story — a long-running storyline that just took a wave
    # of fresh coverage is breaking news. The short window plus the age decay in
    # _breaking_score keep that bounded without needing ranking's stale floor.
    rows = con.execute(
        "SELECT id, headline, topic, article_ids, credibility, "
        "updated_at AS created_at "
        "FROM stories WHERE updated_at > ? "
        "ORDER BY updated_at DESC LIMIT 60",
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
