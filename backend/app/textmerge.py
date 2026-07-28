"""Text cleaning, same-story grouping, and cross-source fact merging.

Three jobs, all pure-Python (no numpy/sklearn — the Render free tier stays light)
and all free of network and DB access so they can be unit-tested directly:

1. CLEANING  — strip HTML entities/tags, agency datelines and publisher boilerplate
   so fluff never reaches (and never gets billed by) the LLM.

2. GROUPING  — decide which articles are the SAME real-world event. The previous
   approach compared titles with raw term-frequency cosine and missed almost
   everything: two reports of one event written by different newsrooms share few
   literal words. Here each article becomes a TF-IDF vector (title weighted, plus
   title bigrams) PLUS an "anchor" set of proper nouns/numbers, and a pair must
   clear BOTH a text floor and an anchor floor inside a time window before it can
   merge. Scores in the ambiguous middle band are handed to an LLM verifier.

3. FACT MERGE — once grouped, articles are decomposed into sentence-level facts,
   near-duplicate facts across outlets are collapsed (giving a corroboration
   count), details only ONE outlet reported are surfaced explicitly, and numeric
   disagreements are flagged. The result is a compact, attributed storyline —
   more complete than any single article, and usually shorter than the raw
   concatenation it replaces.
"""
import html
import math
import re
from collections import Counter

from . import config

# ------------------------------------------------------------------ cleaning

_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_WS_RE = re.compile(r"\s+")

# Wire-service datelines: "(Reuters) - ", "NEW DELHI, Jan 5 (AP) — ", "LONDON — "
_DATELINE_RE = re.compile(
    r"^\s*(?:[A-Z][A-Za-z.]*(?:[ ,][A-Z][A-Za-z.]*){0,3},?\s*)?"
    r"(?:\((?:Reuters|AP|AFP|PTI|IANS|Bloomberg)\)\s*)?[-–—]\s+")
_AGENCY_RE = re.compile(r"\((?:Reuters|AP|AFP|PTI|IANS|Bloomberg)\)\s*[-–—]?\s*")

# Publisher furniture that carries no news value.
_BOILER_RES = [re.compile(p, re.I) for p in (
    r"\bread (?:more|the full story)\b.*",
    r"\bcontinue reading\b.*",
    r"\bclick here\b.*",
    r"\b(?:sign up|subscribe)(?: now| today)?\b[^.]*\.",
    r"\badvertisement\b",
    r"\bsponsored content\b",
    r"the post .+ appeared first on .+",
    r"\ball rights reserved\b.*",
    r"\b(?:photo|image|picture credit|credit)\s*:\s*[^.]*",
    r"\bgetty images\b",
    r"\bfollow us on\b[^.]*",
    r"\bshare this (?:article|story)\b[^.]*",
    r"\bthis article (?:was )?(?:originally )?(?:published|appeared)\b[^.]*",
    r"\bcopyright\s*©?\s*\d{4}[^.]*",
)]


def clean_text(s):
    """Remove HTML entities/tags, URLs, datelines and publisher boilerplate.

    17% of ingested summaries carry raw entities such as `&#039;` — unescaping is
    not cosmetic, it stops the model reading (and being billed for) markup noise.
    """
    if not s:
        return ""
    s = html.unescape(str(s))
    s = html.unescape(s)            # double-encoded feeds are common
    s = _TAG_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = _AGENCY_RE.sub("", s)
    s = _DATELINE_RE.sub("", s)
    for rx in _BOILER_RES:
        s = rx.sub(" ", s)
    s = s.replace(" ", " ")
    return _WS_RE.sub(" ", s).strip(" -–—|·•\t")


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
# Don't split after these — they end in a period but not a sentence.
_ABBREV = ("mr.", "mrs.", "ms.", "dr.", "prof.", "sen.", "gov.", "st.", "inc.",
           "ltd.", "co.", "vs.", "u.s.", "u.k.", "e.u.", "no.", "jan.", "feb.",
           "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.",
           "nov.", "dec.")


def sentences(s):
    """Split cleaned text into sentences, re-joining common abbreviation splits."""
    if not s:
        return []
    parts, out = _SENT_SPLIT_RE.split(s), []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and out[-1].lower().endswith(_ABBREV):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


_FLUFF_SENT_RE = re.compile(
    r"^\s*(?:here'?s what|here is what|read on|find out|learn more|watch\b|"
    r"see (?:also|more)|related\b|more on this|follow\b|scroll\b|"
    r"what to know|in pictures|live updates?)\b", re.I)


def is_fluff_sentence(s):
    """True for navigational/promotional filler with no reportable content."""
    if len(s) < 25:
        return True
    if _FLUFF_SENT_RE.match(s):
        return True
    letters = sum(c.isalpha() for c in s)
    if letters < len(s) * 0.5:      # mostly punctuation/markup residue
        return True
    return False


def drop_fluff_sentences(sents):
    return [s for s in sents if not is_fluff_sentence(s)]


# ------------------------------------------------------------------ features

STOP = set("""the a an of to in on for and or is are was were with from by at as it its this
that new says said will would could more amid has have had be been but not you your we our they
their he she his her them get make made just like about into over after before than then when
what why how who which where all can may might must should also only other some such no nor too
very who's it's don't per via amid across among between during while since until
""".split())

# Words that are capitalised for grammar, not because they're names.
_NOT_ANCHORS = set("""the a an this that these those he she it they we you i and but or if then
when what why how who which where there here his her its their our your my one two three new
first last next after before over under about into from with for
""".split())

# Capitalised COMMON nouns — roles, institutions, section furniture. They look
# like names to a regex but identify a beat, not an event: two unrelated papers
# both open "Scientists…", every tournament piece says "World Cup" / "Round".
# Left in, they merge whole topics together; the corpus is often too small for
# rarity weighting alone to suppress them.
_GENERIC_ANCHORS = set("""scientists scientist researchers researcher research study studies
university college professor doctor doctors experts expert analysts analyst officials official
president prime minister government parliament senate congress police court judge company
companies firm report reports survey poll world cup round city state national international
news update updates live exclusive breaking watch video photos pictures opinion analysis
review interview editor staff team teams match game season league final fans fan people
users user customers workers employees group groups market markets industry sector
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_PROPER_RE = re.compile(r"\b[A-Z][A-Za-z0-9'’\-]{2,}\b")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")
_NUM_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")


def tokens(text):
    """Content tokens (2+ chars, stopwords removed). Keeps 'ai'/'eu'/'us'."""
    return [w for w in _TOKEN_RE.findall((text or "").lower())
            if len(w) >= 2 and w not in STOP]


_POSSESSIVE_RE = re.compile(r"['’]s$")


def anchors(text):
    """Proper nouns, acronyms and significant numbers — the identity of an event.

    Anchors are what let two differently-worded reports of one event match, and
    they need no LLM, which matters because entity extraction runs AFTER grouping.
    Short numbers (1-2 digits) are deliberately excluded: "2" or "9" appear
    everywhere and identify nothing, while years and figures like 2026 or 4,000 do.
    """
    t = html.unescape(text or "")
    out = set()
    for w in _PROPER_RE.findall(t):
        lw = _POSSESSIVE_RE.sub("", w.lower())
        if (lw and len(lw) >= 3 and lw not in _NOT_ANCHORS
                and lw not in STOP and lw not in _GENERIC_ANCHORS):
            out.add(lw)
    for w in _ACRONYM_RE.findall(t):
        lw = w.lower()
        if lw not in _NOT_ANCHORS and lw not in _GENERIC_ANCHORS:
            out.add(lw)
    for n in _NUM_RE.findall(t):
        n = n.replace(",", "")
        if len(n.split(".")[0]) >= 3:
            out.add(n)
    return out


def numbers(text):
    """Bare numeric values, for conflict detection (12 dead vs 14 dead)."""
    return {n.replace(",", "") for n in _NUM_RE.findall(text or "")}


def feature_counts(title, summary, title_weight=3):
    """Bag of features: title tokens (weighted) + title bigrams + summary tokens.

    The title carries the event's identity, so it outweighs the body; bigrams keep
    a little word order ("switch 2", "export controls").
    """
    ti, su = tokens(title), tokens(summary)
    c = Counter()
    for w in ti:
        c[w] += title_weight
    for w in su:
        c[w] += 1
    for bg in zip(ti, ti[1:]):
        c[" ".join(bg)] += title_weight
    return c


# ------------------------------------------------------------------- tf-idf

def build_idf(docs):
    """docs: list of Counter. Returns {term: idf} with smoothed inverse doc freq."""
    n = len(docs) or 1
    df = Counter()
    for d in docs:
        df.update(set(d))
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def tfidf(counts, idf):
    """L2-normalised sublinear TF-IDF vector."""
    v = {t: (1 + math.log(n)) * idf.get(t, 1.0) for t, n in counts.items() if n > 0}
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {t: x / norm for t, x in v.items()} if norm else {}


def cosine(a, b):
    """Cosine of two L2-normalised sparse vectors (iterate the smaller one)."""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def overlap(a, b):
    """Overlap relative to the smaller set (containment)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


MIN_SHARED_ANCHORS = 2
# A shared anchor only counts as distinctive if it appears in at most this share
# of the batch (or 3 documents, whichever is larger).
RARE_ANCHOR_FRACTION = 0.02


def anchor_sim(a, b, stats=None):
    """IDF-weighted anchor containment, gated on a shared *distinctive* name.

    Plain containment over-merges badly: two unrelated science stories that both
    open with "Scientists…" share a ubiquitous anchor and score 1.0. Two guards
    fix that:
      1. weight anchors by rarity, so "meta" or "world cup" contribute little
         while "platner" or "jackdaw" dominate;
      2. require at least one shared anchor that is genuinely rare in the batch —
         two reports of the SAME event name at least one distinctive thing in
         common, whereas two stories merely in the same beat share only generic
         role words (Scientists, Researchers, University, President).
    `stats` is {"idf": {...}, "df": {...}, "n": corpus_size}.
    """
    if not a or not b:
        return 0.0
    shared = a & b
    if len(shared) < MIN_SHARED_ANCHORS:
        return 0.0
    if not stats:
        return len(shared) / min(len(a), len(b))
    df, n = stats.get("df", {}), max(stats.get("n", 1), 1)
    rare_max = max(3.0, RARE_ANCHOR_FRACTION * n)
    if not any(df.get(t, 1) <= rare_max for t in shared):
        return 0.0
    idf = stats.get("idf", {})
    w = lambda s: sum(idf.get(t, 1.0) for t in s)  # noqa: E731
    ws, wa, wb = w(shared), w(a), w(b)
    if not (wa and wb):
        return 0.0
    # Containment alone explodes for anchor-poor documents: a vague headline whose
    # only anchors are "meta" and "ai" scores 1.0 against anything mentioning them.
    # Averaging with Dice (symmetric, penalises lopsided sets) damps that while
    # still rewarding a short headline fully contained in a richer one.
    containment = ws / min(wa, wb)
    dice = 2.0 * ws / (wa + wb)
    return 0.5 * containment + 0.5 * dice


# ----------------------------------------------------------------- grouping

class _Doc:
    __slots__ = ("id", "vec", "anchors", "nums", "at", "title", "aidf")

    def __init__(self, id, vec, anchors, nums, at, title, aidf=None):
        self.id, self.vec, self.anchors = id, vec, anchors
        self.nums, self.at, self.title = nums, at, title
        self.aidf = aidf        # shared corpus anchor-IDF (set by _prepare)


def _prepare(rows):
    """rows -> list[_Doc]. Each row needs id/title/summary and a timestamp."""
    cleaned = []
    for r in rows:
        title = clean_text(r["title"] if "title" in r.keys() else "")
        summary = clean_text(r["summary"] if "summary" in r.keys() else "")
        at = 0.0
        for key in ("published", "fetched_at", "created_at"):
            if key in r.keys() and r[key]:
                at = float(r[key])
                break
        cleaned.append((r["id"], title, summary, at))
    idf = build_idf([feature_counts(t, s) for _, t, s, _ in cleaned])
    anchor_sets = [anchors(t) | anchors(s[:200]) for _, t, s, _ in cleaned]
    # How rare each anchor is across this batch — "meta" is everywhere, "jackdaw"
    # is not, and only the rare ones should imply two reports share an event.
    adf = Counter()
    for s_ in anchor_sets:
        adf.update(s_)
    astats = {"idf": build_idf([Counter(a) for a in anchor_sets]),
              "df": adf, "n": len(anchor_sets)}
    docs = []
    for (aid, title, summary, at), anc in zip(cleaned, anchor_sets):
        docs.append(_Doc(
            id=aid,
            vec=tfidf(feature_counts(title, summary), idf),
            anchors=anc,
            nums=numbers(title),
            at=at,
            title=title,
            aidf=astats))
    return docs, idf


def pair_score(a: "_Doc", b: "_Doc", window_hours=None):
    """Combined same-event score, or None if a hard gate rejects the pair.

    Gates (all must hold): text similarity floor, anchor-overlap floor, and time
    proximity. The floors are what separate a genuine differently-worded match
    from an unrelated story that merely shares a common word.
    """
    win = (window_hours if window_hours is not None
           else config.DEDUPE_WINDOW_HOURS) * 3600.0
    dt = abs(a.at - b.at)
    if a.at and b.at and dt > win:
        return None
    cos = cosine(a.vec, b.vec)
    if cos < config.DEDUPE_COS_FLOOR:
        return None
    anc = anchor_sim(a.anchors, b.anchors, a.aidf or b.aidf)
    if anc < config.DEDUPE_ANCHOR_FLOOR:
        return None
    prox = 1.0 - min(dt, win) / win if (a.at and b.at) else 0.5
    # Anchors carry most of the weight: two newsrooms describe one event with
    # very different words, but they name the same people, places and companies.
    score = 0.40 * cos + 0.45 * anc + 0.15 * prox
    # Fully disjoint numbers hint at different events (a 6.2 quake vs a 5.1 quake)
    # — but can equally be one event with an updated figure, so nudge rather than
    # veto and let the borderline band decide.
    if a.nums and b.nums and not (a.nums & b.nums):
        score -= 0.08
    return score


def _candidates(docs, idf):
    """Blocking: only compare articles sharing a distinctive term or anchor.

    Keeps the pass near-linear and suppresses nonsense comparisons.
    """
    posting = {}
    for i, d in enumerate(docs):
        keys = set(d.anchors)
        # the most distinctive text features of this doc
        keys |= {t for t, _ in sorted(d.vec.items(), key=lambda kv: -kv[1])[:12]}
        for k in keys:
            posting.setdefault(k, []).append(i)
    limit = max(4, int(len(docs) * 0.2))    # skip terms that match almost everything
    pairs = set()
    for k, idxs in posting.items():
        if len(idxs) > limit:
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                pairs.add((idxs[x], idxs[y]))
    return pairs


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def group_articles(rows, verify=None):
    """Cluster rows into same-event groups.

    `verify(pairs) -> set of accepted indices` optionally resolves the ambiguous
    middle band (typically one batched LLM call). Returns
    (clusters, stats) where clusters is a list of lists of article ids.
    """
    docs, idf = _prepare(rows)
    n = len(docs)
    if n == 0:
        return [], {"pairs": 0, "merged": 0, "borderline": 0, "verified": 0}

    strong, borderline = [], []
    for i, j in _candidates(docs, idf):
        s = pair_score(docs[i], docs[j])
        if s is None:
            continue
        if s >= config.DEDUPE_HIGH:
            strong.append((i, j, s))
        elif s >= config.DEDUPE_LOW:
            borderline.append((i, j, s))

    verified = 0
    if borderline and verify and config.SAMESTORY_VERIFY:
        borderline.sort(key=lambda t: -t[2])
        batch = borderline[: config.SAMESTORY_MAX_PAIRS]
        try:
            accepted = verify([(docs[i].title, docs[j].title) for i, j, _ in batch]) or set()
        except Exception:  # noqa: BLE001 — verification is best-effort
            accepted = set()
        for k, (i, j, s) in enumerate(batch):
            if k in accepted:
                strong.append((i, j, s))
                verified += 1

    uf = _UF(n)
    for i, j, _ in strong:
        uf.union(i, j)
    clusters = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    # Anti-chaining: a member only stays if it is genuinely similar to the rest of
    # its cluster on average, not merely to the one neighbour that pulled it in.
    final = []
    for members in clusters.values():
        if len(members) <= 2:
            final.append(members)
            continue
        keep, evicted = [], []
        for m in members:
            others = [o for o in members if o != m]
            sims = [pair_score(docs[m], docs[o]) or 0.0 for o in others]
            (keep if (sum(sims) / len(sims)) >= config.DEDUPE_COHESION
             else evicted).append(m)
        final.append(keep or members)
        final.extend([[e] for e in evicted] if keep else [])

    out = [[docs[i].id for i in c] for c in final if c]
    merged = sum(len(c) - 1 for c in out)
    return out, {"pairs": len(strong), "merged": merged,
                 "borderline": len(borderline), "verified": verified}


# -------------------------------------------------------------- fact merging

# Sentence-level near-duplicate threshold. Lower than the article-level bar
# because outlets paraphrase the same fact rather than repeating it verbatim.
FACT_SIM = 0.40
_MIN_FACT_CHARS = 30
# News is written inverted-pyramid: the core facts are in the opening sentences.
# Capping each article to its lead keeps the brief focused, stops one long piece
# flooding it, and makes cross-source corroboration detectable (every outlet's
# lead covers the same core, while their tails wander into unrelated background).
MAX_SENTS_PER_ARTICLE = 8


def _info_score(text):
    """How much reportable substance a sentence carries."""
    return (len(anchors(text)) * 1.0
            + len(numbers(text)) * 0.8
            + min(len(text) / 120.0, 1.5))


def _facts_from(articles, texts, tier_of):
    """Sentence-level facts from article BODIES, plus each outlet's headline kept
    separately. Headlines are framing, not facts — mixing them in made every
    outlet's title look like an exclusive detail."""
    facts, heads = [], []
    for ai, a in enumerate(articles):
        src = (a["source"] if "source" in a.keys() else "") or "unknown"
        tier = tier_of(src) if tier_of else 4
        title = clean_text(a["title"] if "title" in a.keys() else "")
        if title:
            heads.append((src, title))
        body = (texts or {}).get(a["id"]) or (a["summary"] if "summary" in a.keys() else "")
        usable = []
        for s in drop_fluff_sentences(sentences(clean_text(body))):
            # Fetched pages often lead with their own <h1>, so the first "sentence"
            # repeats the headline verbatim — strip that prefix rather than bill for it.
            if title and s.startswith(title):
                s = s[len(title):].lstrip(" -–—:.")
            if len(s) >= _MIN_FACT_CHARS:
                usable.append(s)
        for si, sent in enumerate(usable[:MAX_SENTS_PER_ARTICLE]):
            facts.append({"text": sent, "source": src, "tier": tier,
                          "art": ai, "pos": si})
    return facts, heads


def _cluster_facts(facts):
    """Group near-duplicate sentences across outlets (average-linkage, greedy)."""
    if not facts:
        return []
    idf = build_idf([Counter(tokens(f["text"])) for f in facts])
    vecs = [tfidf(Counter(tokens(f["text"])), idf) for f in facts]
    clusters = []
    for i, f in enumerate(facts):
        best, best_sim = None, 0.0
        for c in clusters:
            sims = [cosine(vecs[i], vecs[m]) for m in c]
            avg = sum(sims) / len(sims)
            if avg > best_sim:
                best, best_sim = c, avg
        if best is not None and best_sim >= FACT_SIM:
            best.append(i)
        else:
            clusters.append([i])
    return clusters


def _conflicts(facts, cluster):
    """Numeric disagreement between outlets reporting the same fact."""
    by_src = {}
    for i in cluster:
        nums = numbers(facts[i]["text"])
        if nums:
            by_src.setdefault(facts[i]["source"], set()).update(nums)
    if len(by_src) < 2:
        return None
    allnums = set().union(*by_src.values())
    if len(allnums) < 2:
        return None
    # Only flag when outlets genuinely differ (one has a value another lacks).
    if all(v == next(iter(by_src.values())) for v in by_src.values()):
        return None
    return {s: sorted(v) for s, v in by_src.items()}


def build_brief(articles, texts=None, tier_of=None, max_chars=None):
    """Merge a group of articles into ONE attributed storyline for the LLM.

    Returns (brief_text, stats). Facts corroborated by several outlets form the
    spine; details only one outlet carries are called out (so the model includes
    rather than drops them); numeric disagreements are listed instead of being
    silently resolved.
    """
    max_chars = max_chars or config.BRIEF_MAX_CHARS
    facts, heads = _facts_from(articles, texts, tier_of)
    if not facts:
        # No usable body text — fall back to headlines so the caller still has input.
        head_lines = "\n".join(f"- [{s}] {t}" for s, t in heads)
        return head_lines, {"facts": 0, "core": 0, "unique": 0,
                            "conflicts": 0, "sources": len(set(s for s, _ in heads))}

    clusters = _cluster_facts(facts)
    entries = []
    for c in clusters:
        srcs, rep = [], None
        for i in c:
            if facts[i]["source"] not in srcs:
                srcs.append(facts[i]["source"])
        # representative = most informative, tie-broken by source tier then position
        rep = max(c, key=lambda i: (_info_score(facts[i]["text"]),
                                    -facts[i]["tier"], -facts[i]["pos"]))
        entries.append({
            "text": facts[rep]["text"],
            "sources": srcs,
            "corr": len(srcs),
            "pos": min(facts[i]["pos"] for i in c),
            "info": _info_score(facts[rep]["text"]),
            "conflict": _conflicts(facts, c),
        })

    # Spine first: corroboration dominates, then substance, then how early it ran.
    entries.sort(key=lambda e: -(e["corr"] * 2.0 + e["info"] - 0.35 * e["pos"]))
    core = [e for e in entries if e["corr"] >= 2]
    unique = [e for e in entries if e["corr"] == 1]
    conflicts = [e for e in entries if e["conflict"]]

    all_sources = []
    for a in articles:
        s = (a["source"] if "source" in a.keys() else "") or "unknown"
        if s not in all_sources:
            all_sources.append(s)

    lines, n = [], 0
    if heads:
        lines.append("HOW EACH OUTLET FRAMED IT")
        for s, t in heads:
            lines.append(f"- [{s}] {t}")
        lines.append("")
    if core:
        lines.append("CORE FACTS (reported by more than one outlet)")
        for e in core:
            n += 1
            lines.append(f"[{n}] ({e['corr']} sources: {', '.join(e['sources'][:4])}) {e['text']}")
    if unique:
        lines.append("")
        # Outlets often report genuinely different details rather than restating
        # each other. When nothing was corroborated, saying "additional detail"
        # would misdescribe the whole brief — these simply are the reported facts.
        lines.append(
            "ADDITIONAL DETAIL (only one outlet reported this — include if relevant)"
            if core else
            "REPORTED FACTS (each carried by a single outlet — attribute in the prose)")
        for e in unique:
            n += 1
            lines.append(f"[{n}] ({e['sources'][0]}) {e['text']}")
    if conflicts:
        lines.append("")
        lines.append("DISCREPANCIES (report the disagreement; do not silently pick one)")
        for e in conflicts:
            detail = "; ".join(f"{s}: {'/'.join(v)}" for s, v in e["conflict"].items())
            lines.append(f"- {e['text'][:90]} → {detail}")
    lines.append("")
    lines.append(f"SOURCES: {', '.join(all_sources)}")

    brief = "\n".join(lines)
    if len(brief) > max_chars:                       # keep the spine, drop the tail
        brief = brief[:max_chars].rsplit("\n", 1)[0]
        brief += f"\n\nSOURCES: {', '.join(all_sources)}"
    return brief, {"facts": len(facts), "core": len(core), "unique": len(unique),
                   "conflicts": len(conflicts), "sources": len(all_sources)}
