"""The orbit: today's stories arranged by how close they land to the reader.

The iOS home draws the reader ("You") at the centre with three rings around it:

  * direct — the story touches something the reader told us about their own
    work or life: their profession, their line of business, or one of the
    free-form `micro` details ("supplies: distributors carry imported generics").
  * near   — it touches their city or one of the topics they follow.
  * wider  — it touched nothing in the lens, but it is one of the day's biggest
    stories, so it earns a place at the edge.

Each story is drawn as ONE word ("Metro", "Rates", "Monsoon"). Stories that
share a word share a node, and tapping a node lists them.

Everything here is FREE — no LLM call happens on this path. Rings come from the
same whole-word matching `personalization_relevant` uses, extended to the lens
fields that test ignores; the word comes from `stories.orbit_word` (written by
the Storyteller in the call it already makes) with a headline heuristic for
rows written before that column existed and for finance-pipeline stories.

Nothing in this module reads the database. main.py hands it rows it has
already fetched, bounded, so the orbit can never become an unbounded scan on
the 512MB instance.
"""
import re

# How many nodes each ring may hold. The phone draws the rings at roughly 90,
# 150 and 205pt radii; beyond these counts the labels start to collide.
RING_CAPS = {"direct": 4, "near": 5, "wider": 4}
RING_ORDER = ("direct", "near", "wider")
# Several stories may share one word, but a node is a filter, not a digest.
MAX_STORIES_PER_NODE = 4

# The four filter chips above the orbit. A story can sit under more than one.
LENSES = ("work", "money", "city", "family")

# --------------------------------------------------------------- vocabulary
_STOP = set("""
a an the of on in for to and or but with as at by from after before over under into onto
amid about against between during without within across through up down out off
is are was were be been being has have had do does did will would may might can could
should shall must not no nor so than then there this that these those it its it's his her
their our your my we you they he she them us who whom whose which what when where why how
new says said say report reports reported via per just also still yet again more most less
least very too all any some each every both few many much such own same other another
first last next top big key major latest live update updates news today tonight yesterday
tomorrow week weeks month months year years day days daily weekly amid despite while
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
mr mrs ms dr vs set sets get gets got make makes made take takes took see sees seen
here know one two three four five six seven eight nine ten eleven twelve
never ever always now then
""".split())

# Words that end up as a node label but say nothing about the subject: verbs of
# reporting and motion that headlines lean on.
_WEAK = set("""
rises rise rising falls fall falling hits hit slams slam eyes eye faces face calls call
warns warn plans plan seeks seek wins win loses lose backs back moves move looks look
opens open closes close proposed proposes propose trials trial begins begin ends end
amid could would should announces announced announce launches launched launch
reveals reveal revealed matters matter changed change changes shift shifts
curbs curb ban bans banned limits limit cuts cut raises raise hikes hike
billion million crore lakh percent tiny huge
""".split())

# Profession/role words that match far too much to mean anything on their own.
_GENERIC_ROLE = set("""
owner owners manager managers worker workers employee employees senior junior lead head
chief officer executive staff professional consultant specialist assistant associate
director founder cofounder partner self employed freelance freelancer student retired
""".split())

# Interests and micro keys → which chip they belong under. Anything not listed
# still places a story in the orbit; it just only shows under "All".
_MONEY = set("""
finance financial money markets market stocks stock shares investing investment investments
economy economic business crypto banking bank loan loans emi mortgage rent lease savings
salary tax taxes insurance pension portfolio mutual funds fund realestate property
""".split())
_FAMILY = set("""
family kids kid children child school schools education parents parenting health healthcare
medical elderly spouse wife husband son daughter baby pregnancy food nutrition fitness
""".split())
_CITY = set("""
local city commute commuting drive driving neighbourhood neighborhood area route traffic
metro transport housing weather
""".split())
_WORK = set("""
work job jobs career office business shop store stores clients customers suppliers supply
distributors distributor vendors industry employer company startup trade inventory
""".split())


def _tokens(text):
    return re.findall(r"[a-z0-9]{2,}", (text or "").lower())


def _norm(w):
    """Crude singular: 'generics' → 'generic', 'taxes' → 'tax'. Enough for
    matching a reader's own phrasing against a headline; not a stemmer."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _terms(text):
    """Content words of a lens value — what a story has to mention to match."""
    return [_norm(t) for t in _tokens(text)
            if len(t) >= 3 and t not in _STOP and t not in _GENERIC_ROLE
            and not t.isdigit()]


def _bucket(words):
    """Which chip a lens value belongs under, from its own words."""
    ws = set(words) | {_norm(w) for w in words}
    for name, vocab in (("money", _MONEY), ("family", _FAMILY),
                        ("city", _CITY), ("work", _WORK)):
        if ws & vocab:
            return name
    return None


# -------------------------------------------------------------------- lens
def lens_facets(ctx):
    """The reader's lens as a list of matchable facets.

    Each facet is {id, kind, lens, label, detail, terms, ring}:
      kind   — where it came from: profession | business | micro | city | interest
      lens   — which chip it files a matching story under (may be None)
      label  — short text for the "Lens used" chips
      detail — the reader's own words, for "You told Descry …"
      ring   — the innermost ring a match on this facet earns
    """
    ctx = ctx or {}
    out = []

    def add(kind, lens, label, detail, terms, ring, key=""):
        terms = [t for t in dict.fromkeys(terms) if t]
        if not terms:
            return
        out.append({"id": f"{kind}:{key or label}".lower()[:60], "kind": kind,
                    "lens": lens, "label": label.strip()[:40], "detail": detail.strip(),
                    "terms": terms, "ring": ring})

    prof = str(ctx.get("profession") or "").strip()
    if prof:
        add("profession", "work", prof, prof, _terms(prof), "direct")
    lob = str(ctx.get("line_of_business") or "").strip()
    if lob:
        add("business", "work", lob, lob, _terms(lob), "direct")
    for key, val in (ctx.get("micro") or {}).items():
        val = str(val or "").strip()
        if not val:
            continue
        words = _tokens(f"{key} {val}")
        add("micro", _bucket(words) or "work", val, val, _terms(val), "direct",
            key=str(key))
    loc = ctx.get("location") or {}
    city = str(loc.get("city") or "").strip()
    if city:
        add("city", "city", city, city, _terms(city), "near")
    region = str(loc.get("region") or "").strip()
    if region and region.lower() != city.lower():
        add("city", "city", region, region, _terms(region), "near", key="region")
    for interest in ctx.get("interests") or []:
        interest = str(interest or "").strip()
        if not interest or interest.lower() == "all":
            continue
        words = _tokens(interest)
        add("interest", _bucket(words), interest.title() if interest.islower() else interest,
            interest, _terms(interest) or words, "near")
    return out


def lens_label(ctx):
    """"Pharmacy owner, Pune" — the lens as the masthead prints it."""
    ctx = ctx or {}
    parts = [str(ctx.get("profession") or "").strip(),
             str((ctx.get("location") or {}).get("city") or "").strip()]
    return ", ".join(p for p in parts if p)


def _story_words(story):
    text = " ".join(str(story.get(k) or "") for k in ("headline", "place"))
    # The topic with its parent beat, so "You follow technology" still lands
    # on a story filed under ai. Inline: orbit imports nothing from the app.
    t = str(story.get("topic") or "").lower()
    text += " " + t + (" technology" if t == "ai" else "")
    # The first part of the narrative only: a lens term buried in paragraph six
    # is a passing mention, not the story landing on the reader.
    text += " " + str(story.get("narrative") or "")[:700]
    return {_norm(t) for t in _tokens(text)}


def _hits(terms, words):
    """True when the facet's terms appear in the story.

    One term is enough for a short facet ("Pune", "pharmacy"). A long,
    free-form micro value ("drives 40 minutes daily through Hinjewadi") needs
    two, because any single common word in it would match half the feed."""
    found = 0
    for t in terms:
        if t in words or (len(t) >= 7 and any(w.startswith(t[:6]) for w in words)):
            found += 1
    need = 1 if len(terms) <= 2 else 2
    return found >= min(need, len(terms))


_TOPIC_LENS = {
    "business": "money", "finance": "money", "economy": "money", "markets": "money",
    "health": "family", "education": "family", "local": "city",
}


def classify(story, facets):
    """Ring, chip lenses and the matched facets for one story."""
    words = _story_words(story)
    matched = [f for f in facets if _hits(f["terms"], words)]
    # A story set in the reader's own city is near even if the text never says
    # the city's name — the feed it came from already told us where it is.
    place = str(story.get("place") or "").lower()
    if place:
        for f in facets:
            if f["kind"] == "city" and f not in matched and place in f["detail"].lower():
                matched.append(f)
    ring = "wider"
    if any(f["ring"] == "direct" for f in matched):
        ring = "direct"
    elif matched:
        ring = "near"
    # A cached personal angle is the strongest evidence there is: the
    # personalizer read the whole story against the whole lens and scored it.
    score = int(story.get("impact_score") or 0)
    if score >= 2:
        ring = "direct"
    elif score == 1 and ring == "wider":
        ring = "near"
    lenses = []
    for f in matched:
        if f["lens"] and f["lens"] not in lenses:
            lenses.append(f["lens"])
    topic_lens = _TOPIC_LENS.get(str(story.get("topic") or "").lower())
    if story.get("kind") == "finance":
        topic_lens = "money"
    if topic_lens and topic_lens not in lenses and ring != "wider":
        lenses.append(topic_lens)
    return ring, lenses, matched


def touches_lens(ctx, story):
    """True when the story lands on the direct or near ring for this reader,
    on the lens alone (ignoring any cached impact score)."""
    story = dict(story, impact_score=0)
    return classify(story, lens_facets(ctx))[0] != "wider"


def exposure(matched, ring, impact_text=""):
    """"You told Descry your distributors carry imported generics." — the
    reader's own words, quoted back, for the strongest facet that matched.
    None for a wider-ring story: nothing in the lens put it there."""
    if not matched:
        if ring != "wider" and impact_text:
            return {"text": "Descry found a personal angle for you in this story.",
                    "label": "From your lens", "facets": []}
        return None
    order = {"micro": 0, "business": 1, "profession": 2, "city": 3, "interest": 4}
    f = sorted(matched, key=lambda f: order.get(f["kind"], 9))[0]
    d = f["detail"].rstrip(".")
    text = {
        "micro": f"You told Descry: {d[0].lower() + d[1:] if d[:1].isupper() and not d[:2].isupper() else d}.",
        "business": f"You told Descry you work in {d}.",
        "profession": f"You told Descry you work as {_article(d)} {d.lower()}.",
        "city": f"You told Descry you're in {d}.",
        "interest": f"You follow {d}.",
    }[f["kind"]]
    kind_label = {"micro": _key_label(f["id"]), "business": "Line of business",
                  "profession": "Work", "city": "Location",
                  "interest": "Interests"}[f["kind"]]
    return {"text": text, "label": f"From your lens · {kind_label}",
            "facets": [m["id"] for m in matched]}


def _article(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def _key_label(facet_id):
    key = facet_id.split(":", 1)[-1].replace("_", " ").strip()
    return key[:1].upper() + key[1:] if key else "Your details"


# -------------------------------------------------------------------- words
def clean_word(raw):
    """Validate an LLM-proposed orbit word. One token, 2-14 characters,
    letters/digits/hyphen only. Anything else is dropped rather than repaired:
    the heuristic below is a better fallback than a mangled label."""
    if not isinstance(raw, str):
        return None
    w = raw.strip().strip(".,;:!?\"'“”‘’")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{1,13}", w):
        return None
    if w.lower() in _STOP:
        return None
    # Keep acronyms as written (RBI, GST); otherwise capitalise the first letter.
    return w if w.isupper() else w[:1].upper() + w[1:]


def fallback_word(headline, topic="", sectors=None):
    """A one-word label from the headline, for rows with no orbit_word.

    Proper nouns win when the headline is in sentence case (a capital
    mid-sentence is a name: "Hinjewadi", "RBI"); otherwise the earliest
    content word that is not a reporting verb. Falls back to a single-word
    sector, then the topic."""
    raw = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", headline or "")
    words = []
    for w in raw:
        words.extend(p for p in w.split("-") if p)
    caps = sum(1 for w in words if w[:1].isupper())
    sentence_case = bool(words) and caps / len(words) < 0.6
    best, best_score = None, -1.0
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _STOP or lw in _WEAK or len(w) < 3 or len(w) > 14 or lw.isdigit():
            continue
        score = 1.0 - i * 0.06
        if w.isupper() and len(w) >= 2:
            score += 1.5                     # acronym: RBI, GST, NATO
        elif sentence_case and i > 0 and w[:1].isupper():
            score += 2.0                     # a name mid-sentence
        elif sentence_case and i == 0:
            score += 0.8                     # sentence case leads with its subject
        if len(w) >= 5:
            score += 0.3
        if lw.endswith(("ing", "ed")):
            score -= 0.8                     # probably a verb
        if score > best_score:
            best, best_score = w, score
    if best:
        return clean_word(best) or best[:1].upper() + best[1:]
    for s in sectors or []:
        w = clean_word(str(s))
        if w:
            return w
    t = str(topic or "News")
    return t[:1].upper() + t[1:]


# --------------------------------------------------------------- the orbit
def build(items, ctx, meta, connections, hidden=frozenset()):
    """Arrange ranked feed items into rings.

    items       — /feed items, already ranked (best first)
    ctx         — the reader's context blob
    meta        — {story_id: {"orbit_word", "article_ids", "connection_ids"}}
    connections — {connection_id: {"article_a","article_b","chain","confidence",
                                   "titles": {article_id: title}}}
    hidden      — story ids the reader said are not relevant to them

    Returns {"lens", "nodes", "links", "stories"}. `stories` is keyed by id and
    only covers stories that made it into the orbit.
    """
    facets = lens_facets(ctx)
    by_id = {it["id"]: it for it in items}
    placed, nodes, node_of_word = {}, [], {}
    counts = {r: 0 for r in RING_CAPS}
    rank = {it["id"]: i for i, it in enumerate(items)}

    # Ring each story, innermost first, then walk them in rank order so that a
    # ring's slots go to its best stories.
    classified = []
    for it in items:
        if it["id"] in hidden:
            continue
        ring, lenses, matched = classify(it, facets)
        classified.append((it, ring, lenses, matched))
    order = {"direct": 0, "near": 1, "wider": 2}
    classified.sort(key=lambda c: (order[c[1]], rank[c[0]["id"]]))

    for it, ring, lenses, matched in classified:
        m = meta.get(it["id"]) or {}
        word = clean_word(m.get("orbit_word")) or fallback_word(
            it.get("headline"), it.get("topic"), it.get("sectors"))
        key = word.lower()
        node = node_of_word.get(key)
        if node is not None:
            # Same word, same node. Stories are walked innermost first, so the
            # node is always on a ring at least as close as this story's own.
            if len(node["story_ids"]) >= MAX_STORIES_PER_NODE:
                continue
            node["story_ids"].append(it["id"])
            for l in lenses:
                if l not in node["lenses"]:
                    node["lenses"].append(l)
        else:
            # A full ring spills its story to the next ring out rather than
            # dropping it: a sixth direct hit still belongs closer than any
            # wider story. The story keeps its true ring (the card says
            # "Direct"); only where it is drawn moves.
            drawn = next((r for r in RING_ORDER[RING_ORDER.index(ring):]
                          if counts[r] < RING_CAPS[r]), None)
            if drawn is None:
                continue
            counts[drawn] += 1
            node = {"id": f"n{len(nodes) + 1}", "word": word, "ring": drawn,
                    "lenses": list(lenses), "story_ids": [it["id"]]}
            nodes.append(node)
            node_of_word[key] = node
        placed[it["id"]] = {
            "word": node["word"], "ring": ring, "node": node["id"],
            "lenses": lenses,
            "exposure": exposure(matched, ring, it.get("impact_text") or ""),
            "lens_used": [{"id": f["id"], "label": f["label"]} for f in matched],
            "hidden_links": [],
        }

    # Hidden links: the ConnectionFinder's AI-inferred chains between stories.
    # A link to another orbit story becomes a dashed line between the nodes;
    # a link to a story elsewhere in the feed is still listed on the story.
    story_of_article = {}
    for sid, m in meta.items():
        for aid in m.get("article_ids") or []:
            story_of_article.setdefault(aid, sid)
    links, seen = [], set()
    for sid, info in placed.items():
        m = meta.get(sid) or {}
        own = set(m.get("article_ids") or [])
        for cid in m.get("connection_ids") or []:
            c = connections.get(cid)
            if not c:
                continue
            other_art = c["article_b"] if c["article_a"] in own else c["article_a"]
            other = story_of_article.get(other_art)
            if other == sid:
                continue
            conf = float(c.get("confidence") or 0)
            entry = {"chain": c.get("chain") or "", "confidence": round(conf, 2),
                     "confidence_label": confidence_label(conf),
                     "title": (c.get("titles") or {}).get(other_art) or ""}
            if other and other in by_id:
                entry["story_id"] = other
                entry["title"] = by_id[other].get("headline") or entry["title"]
                if other in placed:
                    entry["word"] = placed[other]["word"]
            info["hidden_links"].append(entry)
            if other in placed:
                a, b = info["node"], placed[other]["node"]
                pair = tuple(sorted((a, b)))
                if a != b and pair not in seen:
                    seen.add(pair)
                    links.append({"a": pair[0], "b": pair[1],
                                  "confidence": round(conf, 2)})
    for info in placed.values():
        info["hidden_links"].sort(key=lambda h: -h["confidence"])
        info["hidden_links"] = info["hidden_links"][:3]

    return {
        "lens": {"label": lens_label(ctx), "set": bool(facets),
                 "facets": [{"id": f["id"], "kind": f["kind"], "lens": f["lens"],
                             "label": f["label"]} for f in facets]},
        "nodes": nodes,
        "links": links,
        "stories": placed,
    }


def confidence_label(conf):
    return "high" if conf >= 0.8 else "medium" if conf >= 0.6 else "low"
