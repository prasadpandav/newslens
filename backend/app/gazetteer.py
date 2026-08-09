"""A memory of every entity the extractor has already resolved.

EntityTagger asked an LLM to read a headline and name the organisations, people,
sectors and places in it — once per article group, ~1,200 times over a few days.
Most of those questions had been answered before: a news beat's vocabulary turns
over slowly, so the same banks, ministries, companies and countries recur for
weeks while the events around them change. Nothing remembered any of it, so every
recurrence was bought again.

This module is that memory. It is built entirely from extractions already paid
for — `learn()` is called with the LLM's own output — so it introduces no new
source of truth and no new failure mode: worst case it matches nothing and every
article takes the LLM path exactly as before.

## Why it is allowed to answer at all

The decision is deliberately asymmetric. A MISS costs one twentieth of a batched
call. A false HIT writes a wrong entity list into `articles.entities`, which
ConnectionFinder and TrendLinker then read as fact — a quality regression that is
invisible until it has propagated. So `match()` answers only when it can account
for the WHOLE article:

  * every capitalised proper noun in the text is either part of a known entity or
    a known place, and
  * at least one organisation/person was recognised, and
  * a sector is known for it (directly or by association).

An unrecognised proper noun is the exact signal that something new has appeared,
which is the one case that must never be guessed. That article goes to the LLM,
and whatever it returns is learned — so the vocabulary grows precisely where it
was thin.

## Bounded on purpose

`load()` reads at most GAZETTEER_MAX_TERMS rows, most-seen first. This runs on a
512MB box where every unbounded read has eventually OOM-killed the process, and a
table that grows with the news is exactly that kind of read. The cap costs
nothing in quality: the terms it drops are the long tail seen once, which are the
least likely to recur.
"""
import json
import sqlite3
import time
from . import config, db, textmerge

KINDS = ("entities", "sectors", "regions")

#: Terms whose match tells us nothing. `anchors()` already drops generic
#: capitalised nouns, but an LLM extraction can return a term that is legitimate
#: as an entity and useless as evidence that we understood the article.
_WEAK = {"reuters", "ap", "afp", "pti", "ians", "bloomberg", "the", "inc", "ltd",
         "limited", "corp", "co", "plc", "group", "company", "government"}


def normalise(term):
    """Comparison key for one term: lowercased, punctuation-stripped, collapsed."""
    return " ".join(textmerge.tokens(str(term or "")))


def _key_tokens(term):
    """The words a term must show in the text to count as present."""
    return [t for t in textmerge.tokens(str(term or ""))
            if len(t) >= 2 and t not in _WEAK]


# --------------------------------------------------------------------- writing
def learn(con, extraction, commit=True):
    """Record one extraction (the shape clean_entities returns) into the table.

    Entities also accumulate the sectors/regions they appeared with, which is how
    a later match can supply a sector that is nowhere in the article's own words.
    Best-effort: a gazetteer that raises would fail a stage that had otherwise
    succeeded, and it is only ever an optimisation."""
    doc = extraction if isinstance(extraction, dict) else {}
    sectors = [str(s) for s in (doc.get("sectors") or [])][:8]
    regions = [str(r) for r in (doc.get("regions") or [])][:8]
    now = db.now()
    try:
        for kind in KINDS:
            for term in (doc.get(kind) or [])[:24]:
                norm = normalise(term)
                toks = _key_tokens(term)
                if not norm or not toks:
                    continue
                assoc = ""
                if kind == "entities" and (sectors or regions):
                    assoc = json.dumps({"sectors": sectors, "regions": regions})
                con.execute(
                    "INSERT INTO entity_gazetteer "
                    "  (term, kind, norm, tokens, assoc, seen_count, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,1,?,?) "
                    "ON CONFLICT(norm, kind) DO UPDATE SET "
                    "  seen_count = seen_count + 1, last_seen = excluded.last_seen, "
                    # Keep the earlier spelling: the first form the extractor chose
                    # is the one already written into stored article blobs.
                    "  assoc = CASE WHEN excluded.assoc != '' THEN excluded.assoc "
                    "               ELSE assoc END",
                    (str(term)[:120], kind, norm, " ".join(toks), assoc, now, now))
        if commit:
            con.commit()
        return True
    except sqlite3.Error:
        return False


def backfill(con, limit=5000):
    """Seed the table from entity blobs already stored on articles.

    Without this the gazetteer starts empty and takes days of pipeline runs to
    become useful — while the answers it needs are already sitting in the
    articles table, bought and paid for. Returns how many rows were read."""
    n = 0
    try:
        rows = con.execute(
            "SELECT entities FROM articles WHERE entities IS NOT NULL "
            "AND entities != '' ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return 0
    for r in rows:
        doc = db.uj(r["entities"])
        if isinstance(doc, dict):
            learn(con, doc, commit=False)
            n += 1
    try:
        con.commit()
    except sqlite3.Error:
        pass
    return n


# --------------------------------------------------------------------- reading
class Index:
    """An inverted index over the gazetteer, built once per stage run.

    Keyed by first key-token, so an article is only ever compared against terms
    that share a word with it — the alternative (every term against every
    article) is quadratic in exactly the two things that grow."""

    def __init__(self, rows):
        self.by_token = {}
        self.terms = {}
        for r in rows:
            toks = (r["tokens"] or "").split()
            if not toks:
                continue
            key = (r["norm"], r["kind"])
            self.terms[key] = {"term": r["term"], "kind": r["kind"], "tokens": toks,
                               "assoc": db.uj(r["assoc"]) if r["assoc"] else None,
                               "seen": r["seen_count"] or 0}
            self.by_token.setdefault(toks[0], []).append(key)

    def __len__(self):
        return len(self.terms)


def load(con, limit=None):
    """Build the index, most-frequently-seen terms first (see the cap note above)."""
    limit = config.GAZETTEER_MAX_TERMS if limit is None else limit
    if limit <= 0:
        return Index([])
    try:
        rows = con.execute(
            "SELECT term, kind, norm, tokens, assoc, seen_count FROM entity_gazetteer "
            "ORDER BY seen_count DESC, last_seen DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return Index([])
    return Index(rows)


def _found(index, present, kinds):
    """Terms of `kinds` whose every key token is in `present`."""
    hits = []
    for tok in present:
        for key in index.by_token.get(tok, ()):
            entry = index.terms[key]
            if entry["kind"] in kinds and all(t in present for t in entry["tokens"]):
                hits.append(entry)
    # Dedupe by term, longest (most specific) first: "reserve bank of india"
    # should be preferred over a bare "india" that its own tokens also satisfy.
    seen, out = set(), []
    for e in sorted(hits, key=lambda e: -len(e["tokens"])):
        if e["term"] not in seen:
            seen.add(e["term"])
            out.append(e)
    return out


def match(index, title, summary):
    """Resolve one article from memory alone.

    Returns (extraction, reason). `extraction` is None when the gazetteer cannot
    account for the article — `reason` then says which test failed, which is what
    makes the hit rate diagnosable instead of merely low."""
    if not len(index):
        return None, "empty_gazetteer"
    text = f"{title or ''} {summary or ''}"
    anchors = {a for a in textmerge.anchors(text) if not a[:1].isdigit()}
    words = set(textmerge.tokens(text))
    if not anchors:
        return None, "no_anchors"

    ents = _found(index, anchors, ("entities",))
    regions = _found(index, anchors, ("regions",))
    # Sectors are ordinary lowercase words ("banking", "energy"), so they match
    # against the full token set rather than the capitalised anchors.
    sectors = _found(index, words, ("sectors",))

    if not ents:
        return None, "no_known_entity"

    # Everything capitalised in the text must be accounted for. Whatever is left
    # over is a name we have never resolved — the case this must not guess at.
    claimed = set()
    for e in ents + regions:
        claimed.update(e["tokens"])
    unknown = {a for a in anchors if a not in claimed and a not in _WEAK}
    if len(unknown) > config.GAZETTEER_MAX_UNKNOWN:
        return None, f"unknown_anchors:{len(unknown)}"

    sector_names = [s["term"] for s in sectors]
    region_names = [r["term"] for r in regions]
    if not sector_names or not region_names:
        # Fall back to what these entities have been seen with before.
        for e in ents:
            a = e["assoc"] or {}
            if not sector_names:
                sector_names = [str(s) for s in (a.get("sectors") or [])][:3]
            if not region_names:
                region_names = [str(r) for r in (a.get("regions") or [])][:3]
            if sector_names and region_names:
                break
    if not sector_names:
        # A sector is what ConnectionFinder and the feed's topic logic read. An
        # entity list with no sector is a partial answer, and a partial answer
        # stored as a complete one is the failure this module must not produce.
        return None, "no_sector"

    return ({"entities": [e["term"] for e in ents][:12],
             "sectors": list(dict.fromkeys(sector_names))[:4],
             "regions": list(dict.fromkeys(region_names))[:4]},
            "matched")


def stats(con):
    """Size and shape of the vocabulary, for /admin/usage."""
    try:
        rows = con.execute(
            "SELECT kind, COUNT(*) n, SUM(seen_count) seen FROM entity_gazetteer "
            "GROUP BY kind").fetchall()
        return {r["kind"]: {"terms": r["n"], "observations": r["seen"] or 0}
                for r in rows}
    except sqlite3.Error:
        return {}


def prune(con, retain_days=None):
    """Drop terms seen once and not since the retention window — the long tail
    that will never match again but would otherwise crowd the load() cap."""
    days = config.GAZETTEER_RETAIN_DAYS if retain_days is None else retain_days
    if not days or days <= 0:
        return 0
    try:
        n = con.execute(
            "DELETE FROM entity_gazetteer WHERE seen_count <= 1 AND last_seen < ?",
            (time.time() - days * 86400,)).rowcount
        con.commit()
        return n
    except sqlite3.Error:
        return 0
