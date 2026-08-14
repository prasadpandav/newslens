"""Semantic Cause & Effect and Causal Cascade Prediction Engine.

Multi-hop transmission chains, dampeners, and counterfactual "what-if"
simulation, behind the Predictions page.

WHERE A CHAIN COMES FROM
------------------------
A chain is a PATH through the knowledge graph, not a thing a model invented.
`build()` walks out from a well-connected catalyst entity, taking the strongest
relationship at each hop, and that walk IS the chain: its steps are real nodes,
its `channel` per step is the real predicate, its evidence is the story ids on
the edges it crossed. Only the plain-English framing — title, the one-sentence
gloss per step, the precedent — is asked of a model, and only when the
structure underneath actually moved (see `_prose_hash`).

That division is the same one FinancialTrendAgent uses, for the same reason: a
language model cannot tell you who supplies whom, but the graph built from the
reporting can, and it can be walked.

Until Aug 2026 this module served CANONICAL_CAUSAL_CHAINS — three hand-written
dicts — from every request. Nothing regenerated them because nothing could:
there was no table and no pipeline stage. They survive below as the fallback
for a graph too thin to walk, and are marked `curated: true` on the wire so a
reader is never shown seed content as though it were today's reporting.
"""
import hashlib
import math
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .. import config, db, llm
from ..agents import prompt
from . import kg
from . import tickers as tk

NAMESPACE = "finance"
SCHEMA_VERSION = 1

# Per-step timeline, derived from POSITION in the chain rather than measured:
# an effect two relationships out from the catalyst has not been observed to
# take three months, it is simply further along a chain that takes months to
# run. Labelled in plain words so the page never implies a precision we do not
# have — there is no "14 days" here because we cannot support one.
_STEP_HORIZON = ["Immediate (days)", "Weeks", "1-3 months", "3-6 months",
                 "6-12 months"]


class TransmissionStep(BaseModel):
    step_order: int
    stage_name: str  # "Catalyst Event", "Primary Transmission", "Intermediate Friction", "Terminal Impact"
    entity: str
    action_or_friction: str
    channel: str     # "Monetary Policy Transmission", "Wafer Allocation Bottleneck", etc.
    elasticity_score: float = Field(ge=0.0, le=1.0)
    propagation_horizon: str  # "1-3 weeks", "1-2 quarters", "12-24 months"
    affected_tickers: List[str] = Field(default_factory=list)
    evidence_quote: str = ""


class CounterDampener(BaseModel):
    name: str
    mechanism: str
    absorption_capacity_pct: int
    sponsor_entity: str


class CausalChain(BaseModel):
    id: str
    title: str
    catalyst_event: str
    catalyst_entity: str
    catalyst_domain: str
    terminal_outcome: str
    transmission_channel: str
    overall_confidence: float
    base_probability: float
    time_horizon: str
    steps: List[TransmissionStep]
    dampeners: List[CounterDampener]
    affected_sectors: List[str]
    sensitive_tickers: List[str]
    historical_precedent: str
    corroborating_story_ids: List[str] = Field(default_factory=list)


# Curated cause-and-effect chains explained in plain English
CANONICAL_CAUSAL_CHAINS: List[Dict[str, Any]] = [
    {
        "id": "chain-monetary-policy-lending",
        "title": "How RBI Interest Rate Changes Affect Business Loans & Growth",
        "catalyst_event": "RBI changes the key interest rate (repo rate) that banks pay to borrow money",
        "catalyst_entity": "Reserve Bank of India",
        "catalyst_domain": "Monetary Policy & Central Banking",
        "terminal_outcome": "Companies delay expansion plans as borrowing becomes more expensive across the economy",
        "transmission_channel": "Interest rates flow from RBI → banks → businesses → consumers",
        "overall_confidence": 0.89,
        "base_probability": 0.82,
        "time_horizon": "3-6 months",
        "affected_sectors": ["Banking & Loans", "Infrastructure", "Auto & Manufacturing"],
        "sensitive_tickers": ["HDFCBANK", "RELIANCE", "TATAMOTORS"],
        "historical_precedent": "In 2022-23, global central banks raised rates sharply — corporate borrowing costs spiked, slowing business expansion worldwide",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "Reserve Bank of India",
                "action_or_friction": "RBI raises or lowers the repo rate — this is the base rate at which all banks borrow money from the central bank.",
                "channel": "Central Bank Rate Decision",
                "elasticity_score": 1.0,
                "propagation_horizon": "Immediate (1-3 days)",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "When RBI changes rates, every bank in India must adjust what it costs them to lend money."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "HDFC Bank & Major Banks",
                "action_or_friction": "Banks pass on the rate change to customers — home loans, business loans, and credit lines all get repriced within weeks.",
                "channel": "Bank Loan Repricing",
                "elasticity_score": 0.88,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "Banks adjust their lending rates, making loans cheaper or more expensive for everyone."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "Reliance, Tata & Large Companies",
                "action_or_friction": "Big companies find it costlier to borrow for new projects — factory expansions, new plants, and large deals get reconsidered.",
                "channel": "Corporate Borrowing Costs",
                "elasticity_score": 0.74,
                "propagation_horizon": "3-6 months",
                "affected_tickers": ["RELIANCE", "TATAMOTORS"],
                "evidence_quote": "Large companies rethink how much debt they take on and may postpone expensive projects."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Manufacturing & Auto Sector",
                "action_or_friction": "Smaller suppliers and manufacturers also face higher costs — hiring slows, new orders dip, and overall industrial growth moderates.",
                "channel": "Real Economy Slowdown",
                "elasticity_score": 0.65,
                "propagation_horizon": "6-12 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "The ripple reaches everyday factories and workshops — the real economy feels the squeeze."
            }
        ],
        "dampeners": [
            {
                "name": "Fixed-Rate Loans Already Locked In",
                "mechanism": "Many companies already have loans at fixed rates — they don't feel the impact immediately, buying time to adjust.",
                "absorption_capacity_pct": 28,
                "sponsor_entity": "HDFC Bank & Reliance"
            },
            {
                "name": "Strong Bank Deposits as a Buffer",
                "mechanism": "Banks with large savings deposits don't need to borrow as much at higher rates — this cushions the blow.",
                "absorption_capacity_pct": 22,
                "sponsor_entity": "HDFC Bank"
            }
        ]
    },
    {
        "id": "chain-semiconductor-fab-cascade",
        "title": "How Chip Shortages at TSMC Delay AI Infrastructure in India",
        "catalyst_event": "TSMC's advanced chip packaging lines hit capacity limits due to surging AI demand",
        "catalyst_entity": "TSMC",
        "catalyst_domain": "Semiconductor Fabrication & Packaging",
        "terminal_outcome": "AI server deliveries get delayed, pushing India to accelerate its own chip manufacturing",
        "transmission_channel": "Chip shortage at TSMC → GPU delays at NVIDIA → India builds local alternatives",
        "overall_confidence": 0.93,
        "base_probability": 0.86,
        "time_horizon": "6-12 months",
        "affected_sectors": ["Chip Manufacturing", "AI & Cloud Computing", "Electronics Assembly"],
        "sensitive_tickers": ["TSM", "NVDA", "INFY"],
        "historical_precedent": "In 2021-22, a global chip shortage caused 52+ week wait times for car manufacturers worldwide",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "TSMC",
                "action_or_friction": "TSMC's factories that package advanced AI chips can't keep up with demand from big tech companies like Google, Microsoft, and Meta.",
                "channel": "Chip Factory Capacity",
                "elasticity_score": 0.95,
                "propagation_horizon": "1-2 weeks",
                "affected_tickers": ["TSM", "NVDA"],
                "evidence_quote": "When TSMC's packaging lines max out, fewer AI chips can be assembled for the entire world."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "NVIDIA",
                "action_or_friction": "NVIDIA's latest AI chips (Blackwell, Hopper) face delivery delays of 30-38 weeks — companies have to wait months to get their orders.",
                "channel": "AI Chip Delivery Delays",
                "elasticity_score": 0.91,
                "propagation_horizon": "1-2 months",
                "affected_tickers": ["NVDA"],
                "evidence_quote": "Big cloud companies get priority, while smaller buyers and countries like India face longer waits."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "Foxconn & Gujarat Chip Hub",
                "action_or_friction": "India fast-tracks its own chip packaging and testing facilities in Gujarat (Dholera & Sanand) to reduce dependence on TSMC.",
                "channel": "Building Local Alternatives",
                "elasticity_score": 0.82,
                "propagation_horizon": "6-9 months",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Government-backed projects and joint ventures rush to set up domestic chip infrastructure."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Infosys & IT Services Companies",
                "action_or_friction": "Companies switch from buying expensive hardware to using cloud-based AI services — IT firms like Infosys benefit as they help manage this transition.",
                "channel": "Shift to Cloud AI Services",
                "elasticity_score": 0.70,
                "propagation_horizon": "9-12 months",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Businesses adapt by using optimized, cloud-hosted AI instead of waiting for physical hardware."
            }
        ],
        "dampeners": [
            {
                "name": "Government Subsidies (MeitY PLI Scheme)",
                "mechanism": "The Indian government covers up to 50% of setup costs for domestic chip factories — making it viable to build locally.",
                "absorption_capacity_pct": 35,
                "sponsor_entity": "Ministry of Electronics & IT"
            },
            {
                "name": "Multiple Supplier Strategy",
                "mechanism": "Companies source chip components from multiple factories across Asia — not just TSMC — reducing single-point risk.",
                "absorption_capacity_pct": 20,
                "sponsor_entity": "Foxconn Consortium"
            }
        ]
    },
    {
        "id": "chain-auto-ev-raw-materials",
        "title": "How Rising Lithium Prices Make Electric Vehicles More Expensive",
        "catalyst_event": "Countries that mine lithium and battery materials impose export restrictions, driving up prices",
        "catalyst_entity": "Global Mineral Suppliers",
        "catalyst_domain": "Clean Energy & Mineral Supply Chains",
        "terminal_outcome": "EV prices rise in the short term, but India accelerates building its own battery factories",
        "transmission_channel": "Raw material costs rise → battery packs get expensive → EVs cost more → India builds local supply",
        "overall_confidence": 0.85,
        "base_probability": 0.78,
        "time_horizon": "6-9 months",
        "affected_sectors": ["Auto & Electric Vehicles", "Batteries & Clean Energy", "Electronics Manufacturing"],
        "sensitive_tickers": ["TATAMOTORS", "RELIANCE"],
        "historical_precedent": "In 2022, nickel and lithium prices spiked suddenly — EV makers worldwide saw their margins squeezed overnight",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "Global Mineral Suppliers",
                "action_or_friction": "Countries that control lithium, cobalt, and nickel impose export quotas or tariffs — the price of raw battery materials jumps.",
                "channel": "Raw Material Prices",
                "elasticity_score": 0.90,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["TATAMOTORS", "RELIANCE"],
                "evidence_quote": "When mining countries restrict exports, battery material costs shoot up globally."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "Tata Motors & EV Makers",
                "action_or_friction": "The cost to build each battery pack rises 8-12% — car companies must decide whether to absorb the cost or raise vehicle prices.",
                "channel": "Battery Cost Increase",
                "elasticity_score": 0.84,
                "propagation_horizon": "3-6 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "Automakers face a tough choice: eat into profits or pass higher costs to buyers."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "EV Buyers & Fleet Operators",
                "action_or_friction": "EVs become less attractive vs. petrol/diesel cars — buyers delay purchases, and fleet operators slow their switch to electric.",
                "channel": "Consumer Demand Softens",
                "elasticity_score": 0.72,
                "propagation_horizon": "6-9 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "The cost advantage of owning an EV shrinks, and some buyers wait for prices to stabilize."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Reliance & Domestic Battery Projects",
                "action_or_friction": "India doubles down on building its own battery factories using alternative chemistries (like sodium-ion) to avoid depending on imported lithium.",
                "channel": "Building Local Battery Supply",
                "elasticity_score": 0.68,
                "propagation_horizon": "12-18 months",
                "affected_tickers": ["RELIANCE"],
                "evidence_quote": "Indian companies invest in homegrown gigafactories to insulate the market from global supply shocks."
            }
        ],
        "dampeners": [
            {
                "name": "Government EV Subsidies (FAME / EMPS)",
                "mechanism": "Government incentives for EV buyers offset some of the battery cost increase — keeping EVs competitive for fleet operators.",
                "absorption_capacity_pct": 25,
                "sponsor_entity": "Ministry of Heavy Industries"
            },
            {
                "name": "Long-Term Supply Contracts",
                "mechanism": "Smart automakers lock in multi-year deals at fixed prices — protecting them from sudden price spikes in raw materials.",
                "absorption_capacity_pct": 30,
                "sponsor_entity": "Tata Motors"
            }
        ]
    }
]


# ------------------------------------------------------------------ walking
def _node_names(con):
    """{canonical id: (display name, ticker, type)} for the whole namespace.

    One read, because a walk touches most of these and a per-node lookup inside
    the loop is the shape that has stalled this box before."""
    return {r["id"]: (r["name"] or r["id"], r["ticker"], r["type"] or "")
            for r in con.execute(
                "SELECT id, name, ticker, type FROM fin_kg_nodes WHERE namespace=?",
                (NAMESPACE,)).fetchall()}


def _path_from(con, seed, deg, min_conf, max_steps):
    """Greedy strongest-first walk from `seed`, as a list of edge rows.

    DIRECTED, and that is not a detail. kg.cascade walks edges both ways —
    correct for "who is exposed to this", since exposure travels back down a
    supply relationship as readily as forward. A causal chain is the other
    question: it asks what CAUSES what, so it may only follow subject -> object.
    Walking undirected here produced chains like "Tata Motors -> HDFC Bank ->
    RBI", which reads the actual causality exactly backwards and would have
    been published as a prediction.

    Greedy rather than exhaustive on purpose. The best-supported relationship
    out of an entity is the one the reporting actually established, and a
    search over every path would find chains whose links no single story
    connects — precisely the invented cascade this design exists to avoid. It
    also keeps the walk linear in chain length on a 512MB box.
    """
    path, node, visited = [], seed, {seed}
    while len(path) < max_steps:
        # A hub is a fine place to START (a central bank is the catalyst of half
        # the chains worth drawing) but routing THROUGH one connects two
        # companies that merely share a regulator. Same rule kg.cascade applies.
        if path and deg.get(node, 0) > kg.HUB_DEGREE:
            break
        best = None
        for other, row, forward in kg.neighbours(con, node, NAMESPACE, min_conf):
            if not forward:
                continue        # effects run subject -> object, never back up
            if not other or other in visited:
                continue        # a chain that revisits a node is a loop, not a chain
            conf = float(row["confidence"] or 0)
            if best is None or conf > best[0]:
                best = (conf, other, row, forward)
        if best is None:
            break
        _, other, row, forward = best
        path.append((row, forward, node, other))
        visited.add(other)
        node = other
    return path


def _signature(seed, path):
    """The chain's identity: the canonical node path it walks.

    Exact rather than fuzzy (fin_trends matches arcs by name cosine) because a
    path through canonical node ids is already unambiguous — two walks that
    cross the same nodes in the same order ARE the same chain."""
    return ">".join([seed] + [to for _, _, _, to in path])


def _prose_hash(path):
    """Covers only what the prose actually describes: which relationships, in
    which order, at roughly what strength. Confidence is rounded to one decimal
    so an edge drifting 0.71 -> 0.73 does not buy a fresh LLM call — the words
    would come back the same."""
    parts = [f"{f}|{r['predicate']}|{t}|{round(float(r['confidence'] or 0), 1)}"
             for r, _, f, t in path]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _build_one(con, seed, deg, names):
    """One chain's STRUCTURE — everything that comes from the graph. Returns
    None when the walk is too short to be a transmission chain."""
    path = _path_from(con, seed, deg, config.FIN_CAUSAL_MIN_EDGE_CONF,
                      max(1, config.FIN_MAX_CASCADE_HOPS))
    # Steps are NODES, so N edges give N+1 steps.
    if len(path) + 1 < max(2, config.FIN_CAUSAL_MIN_STEPS):
        return None

    nodes = [seed] + [to for _, _, _, to in path]
    confs = [float(r["confidence"] or 0) for r, _, _, _ in path]

    steps, tickers, story_ids = [], [], []
    for i, node in enumerate(nodes):
        name, ticker, _type = names.get(node, (node, None, ""))
        if ticker:
            tickers.append(ticker)
        # Step 1 is the catalyst acting under its own steam, so it transmits at
        # full strength by definition. Every later step transmits only as well
        # as the relationship that reached it.
        elasticity = 1.0 if i == 0 else confs[i - 1]
        channel = ((path[i - 1][0]["predicate"] or "related_to").replace("_", " ")
                   if i else "catalyst")
        steps.append({
            "step_order": i + 1,
            "stage_name": "Trigger" if i == 0 else (
                "End Result" if i == len(nodes) - 1 else f"Ripple {i}"),
            "entity": name,
            # Placeholder prose. Replaced by the model when it narrates this
            # chain; left as the plain relationship if that call never lands, so
            # a card is always readable rather than blank.
            "action_or_friction": (
                f"{name} is where this starts."
                if i == 0 else
                f"{names.get(nodes[i-1], (nodes[i-1],))[0]} {channel} {name}, "
                f"so the effect passes on."),
            "channel": channel,
            "elasticity_score": round(elasticity, 3),
            "propagation_horizon": _STEP_HORIZON[min(i, len(_STEP_HORIZON) - 1)],
            "affected_tickers": [ticker] if ticker else []})
    for r, _, _, _ in path:
        story_ids.extend(db.uj(r["story_ids"], []))

    # Two different questions, so two numbers rather than one doing both jobs:
    # how well evidenced is a typical link here (geometric mean), and what is
    # the chance the WHOLE chain holds (the product — every hop has to).
    overall = (math.exp(sum(math.log(max(c, 0.01)) for c in confs) / len(confs))
               if confs else 0.0)
    joint = 1.0
    for c in confs:
        joint *= c

    sectors = _sectors_for(con, story_ids)
    return {
        "signature": _signature(seed, path),
        "catalyst_entity": names.get(seed, (seed,))[0],
        "steps": steps,
        "affected_sectors": sectors,
        "sensitive_tickers": sorted(set(tickers)),
        # The evidence is EXACT now: the stories that reported the very edges
        # this chain crosses, read off fin_kg_edges.story_ids. The old
        # implementation LIKE-matched ticker symbols against article prose,
        # which mostly matched nothing (articles write "HDFC Bank", never
        # "HDFCBANK") and never matched the specific relationship.
        "corroborating_story_ids": sorted(set(i for i in story_ids if i))[:8],
        "overall_confidence": round(overall, 3),
        "base_probability": round(joint, 3),
        "prose_hash": _prose_hash(path),
        "_path": path, "_nodes": nodes}


def _sectors_for(con, story_ids):
    """Sectors the corroborating stories were filed under — real labels from
    fin_stories rather than a guess from the entity names."""
    ids = sorted(set(i for i in story_ids if i))[:20]
    if not ids:
        return []
    out = []
    for r in con.execute(
            "SELECT sectors FROM fin_stories WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall():
        for s in db.uj(r["sectors"], []):
            if s and s not in out:
                out.append(s)
    return out[:6]


# ------------------------------------------------------------------- reading
# Two chains sharing this much of their node set are the same cascade seen from
# different starting points — a greedy walk that enters one hop later traces the
# same edges from then on, so a dense neighbourhood emits a sliding window of
# near-identical paths. Publishing those fills the page with one story told four
# ways, which is the complaint this whole stage exists to answer.
_MAX_NODE_OVERLAP = 0.6


def _dedupe_paths(chains):
    """Keep the strongest chain from each cluster of overlapping walks.

    Ordered by length first, then confidence: the longest walk carries the
    catalyst that the ones joining later are missing, and the catalyst is the
    half of a causal chain a reader cannot reconstruct for themselves."""
    kept = []
    for c in sorted(chains, key=lambda c: (-len(c["_nodes"]),
                                           -c["overall_confidence"])):
        nodes = set(c["_nodes"])
        if any(len(nodes & set(k["_nodes"])) / len(nodes) >= _MAX_NODE_OVERLAP
               for k in kept):
            continue
        kept.append(c)
    return kept


def _row_to_chain(r):
    return {"id": r["id"], "signature": r["signature"], "title": r["title"],
            "catalyst_event": r["catalyst_event"],
            "catalyst_entity": r["catalyst_entity"],
            "catalyst_domain": r["catalyst_domain"],
            "terminal_outcome": r["terminal_outcome"],
            "transmission_channel": r["transmission_channel"],
            "overall_confidence": r["overall_confidence"],
            "base_probability": r["base_probability"],
            "time_horizon": r["time_horizon"],
            "affected_sectors": db.uj(r["affected_sectors"], []),
            "sensitive_tickers": db.uj(r["sensitive_tickers"], []),
            "historical_precedent": r["historical_precedent"] or "",
            "steps": db.uj(r["steps"], []),
            "dampeners": db.uj(r["dampeners"], []),
            "corroborating_story_ids": db.uj(r["corroborating_story_ids"], []),
            "curated": False,
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def _curated(domain=""):
    """Seed content, clearly flagged. Served only when the graph has produced
    nothing yet — a fresh deploy, or a database whose edges have been pruned."""
    out = []
    for c in CANONICAL_CAUSAL_CHAINS:
        c = dict(c)                      # never hand out the module-level dict:
        c["curated"] = True              # callers used to mutate it in place
        c.setdefault("corroborating_story_ids", [])
        out.append(c)
    return _filter_domain(out, domain)


def _filter_domain(chains, domain):
    if not domain:
        return chains
    d = domain.lower()
    hit = [c for c in chains
           if d in (c.get("catalyst_domain") or "").lower()
           or d in (c.get("transmission_channel") or "").lower()
           or d in (c.get("title") or "").lower()
           or any(d in str(s).lower() for s in c.get("affected_sectors") or [])]
    return hit


def list_causal_chains(con=None, domain: str = "") -> List[Dict[str, Any]]:
    """Live chains, strongest first — or the curated seed set if none exist yet.

    A caller can tell which it got: generated chains carry `curated: false` and
    real `corroborating_story_ids`."""
    if con is None:
        return _curated(domain)
    try:
        rows = con.execute(
            "SELECT * FROM fin_causal_chains WHERE retired_at IS NULL "
            "ORDER BY overall_confidence DESC, updated_at DESC LIMIT ?",
            (max(1, config.FIN_MAX_CAUSAL_CHAINS),)).fetchall()
    except Exception:
        # The table is missing (an old database that has not been migrated).
        # The page keeps working on seed content rather than 500ing.
        return _curated(domain)
    if not rows:
        return _curated(domain)
    return _filter_domain([_row_to_chain(r) for r in rows], domain)


def get_chain(con, chain_id):
    """One chain by id, from either source. None when the id is unknown."""
    if con is not None:
        try:
            r = con.execute("SELECT * FROM fin_causal_chains WHERE id=?",
                            (chain_id,)).fetchone()
            if r:
                return _row_to_chain(r)
        except Exception:
            pass
    return next((dict(c, curated=True) for c in CANONICAL_CAUSAL_CHAINS
                 if c["id"] == chain_id), None)


# ------------------------------------------------------------------ building
def _out_degree(con):
    """{node: outgoing edge count}. Distinct from kg.degrees, which counts both
    directions — a catalyst is defined by what it ACTS ON, and a company named
    as the object of forty relationships causes none of them."""
    return {r["n"]: r["c"] for r in con.execute(
        "SELECT subject AS n, COUNT(*) c FROM fin_kg_edges WHERE namespace=? "
        "GROUP BY subject", (NAMESPACE,)).fetchall()}


def _seeds(con, names, deg, limit):
    """Catalyst candidates, best first.

    Ranked by OUT-degree: a catalyst is an entity that acts on others, and the
    thing that starts a chain is the thing with somewhere to send an effect.
    Recency gates the pool via top_entities, so chains are built out of what is
    being reported now rather than out of whatever is oldest in the graph.

    No minimum degree. An entity with a single outgoing relationship can still
    begin a real chain if that relationship leads somewhere — requiring two
    excluded exactly the regulators and central banks that make the best
    catalysts, because they tend to act on one named party at a time."""
    recent = [e["id"] for e in kg.top_entities(con, limit=limit * 6)]
    out = _out_degree(con)
    ranked = sorted((n for n in recent if out.get(n, 0) >= 1),
                    key=lambda n: (-out.get(n, 0), -deg.get(n, 0), n))
    return ranked[:limit]


def _narrate(con, chain, names):
    """Ask the model for the plain-English framing of an already-built chain.

    Returns True if the chain was narrated. A failure is not fatal: the chain
    keeps the deterministic step text from _build_one, which is plain but
    accurate, so the page shows a real chain rather than nothing."""
    steps_text = "\n".join(
        f"{s['step_order']}. {s['entity']}"
        + (f"  (reached because: {s['channel']})" if s["step_order"] > 1 else "  (the catalyst)")
        for s in chain["steps"])
    edges_text = "\n".join(
        f"- {names.get(f, (f,))[0]} {(r['predicate'] or '').replace('_',' ')} "
        f"{names.get(t, (t,))[0]}  (confidence {float(r['confidence'] or 0):.2f})"
        for r, _, f, t in chain["_path"])
    heads = [h["headline"] for h in con.execute(
        "SELECT headline FROM fin_stories WHERE id IN (%s) LIMIT 8"
        % ",".join("?" * len(chain["corroborating_story_ids"])),
        chain["corroborating_story_ids"]).fetchall()
    ] if chain["corroborating_story_ids"] else []

    out = llm.complete_json("fin_causal", prompt(
        "fin_causal", catalyst=chain["catalyst_entity"], steps=steps_text,
        edges=edges_text,
        headlines="\n".join(f"- {h}" for h in heads) or "(none on file)"))
    if not isinstance(out, dict):
        return False

    chain["title"] = str(out.get("title") or "").strip()[:200]
    chain["catalyst_event"] = str(out.get("catalyst_event") or "").strip()[:400]
    chain["transmission_channel"] = str(out.get("transmission_channel") or "").strip()[:300]
    chain["terminal_outcome"] = str(out.get("terminal_outcome") or "").strip()[:400]
    chain["catalyst_domain"] = str(out.get("catalyst_domain") or "").strip()[:60]
    chain["time_horizon"] = str(out.get("time_horizon") or "").strip()[:40]
    chain["historical_precedent"] = str(out.get("historical_precedent") or "").strip()[:400]
    # Prose only. The model was given the steps and told to keep their order and
    # count; anything else it returns is discarded rather than trusted, because
    # the entity list is the part that has to stay grounded in the graph.
    said = [s for s in (out.get("steps") or []) if isinstance(s, dict)]
    for i, s in enumerate(chain["steps"]):
        if i < len(said):
            stage = str(said[i].get("stage_name") or "").strip()[:40]
            act = str(said[i].get("action_or_friction") or "").strip()[:400]
            if stage:
                s["stage_name"] = stage
            if act:
                s["action_or_friction"] = act
    # Dampeners are named by the model but carry NO number: absorption capacity
    # is not something this data can measure, and a plausible-looking percentage
    # under a real company's name is exactly the kind of invention this pipeline
    # refuses elsewhere. The clients render "—" when it is null.
    chain["dampeners"] = [
        {"name": str(d.get("name") or "").strip()[:120],
         "mechanism": str(d.get("mechanism") or "").strip()[:300],
         "sponsor_entity": str(d.get("sponsor_entity") or "").strip()[:120],
         "absorption_capacity_pct": None}
        for d in (out.get("dampeners") or [])[:2]
        if isinstance(d, dict) and str(d.get("name") or "").strip()]
    return bool(chain["title"])


def _fallback_prose(chain):
    """Enough framing for a readable card when the model never answered."""
    first = chain["steps"][0]["entity"]
    last = chain["steps"][-1]["entity"]
    chain.setdefault("title", f"How {first} reaches {last}")
    chain.setdefault("catalyst_event", f"Something changes at {first}.")
    chain.setdefault("transmission_channel",
                     " → ".join(s["entity"] for s in chain["steps"]))
    chain.setdefault("terminal_outcome", f"The effect reaches {last}.")
    chain.setdefault("catalyst_domain", "")
    chain.setdefault("time_horizon", _STEP_HORIZON[
        min(len(chain["steps"]) - 1, len(_STEP_HORIZON) - 1)])
    chain.setdefault("historical_precedent", "")
    chain.setdefault("dampeners", [])
    return chain


def _persist(con, chain):
    """Upsert on `signature`. Returns 'new' | 'updated'."""
    now = db.now()
    row = con.execute(
        "SELECT id, created_at FROM fin_causal_chains WHERE signature=?",
        (chain["signature"],)).fetchone()
    vals = (chain["title"], chain["catalyst_event"], chain["catalyst_entity"],
            chain["catalyst_domain"], chain["terminal_outcome"],
            chain["transmission_channel"], chain["overall_confidence"],
            chain["base_probability"], chain["time_horizon"],
            db.j(chain["affected_sectors"]), db.j(chain["sensitive_tickers"]),
            chain["historical_precedent"], db.j(chain["steps"]),
            db.j(chain["dampeners"]), db.j(chain["corroborating_story_ids"]),
            chain["prose_hash"], SCHEMA_VERSION, now)
    if row:
        con.execute(
            "UPDATE fin_causal_chains SET title=?, catalyst_event=?, "
            "catalyst_entity=?, catalyst_domain=?, terminal_outcome=?, "
            "transmission_channel=?, overall_confidence=?, base_probability=?, "
            "time_horizon=?, affected_sectors=?, sensitive_tickers=?, "
            "historical_precedent=?, steps=?, dampeners=?, "
            "corroborating_story_ids=?, prose_hash=?, schema_version=?, "
            "updated_at=?, retired_at=NULL WHERE id=?",
            vals + (row["id"],))
        chain["id"] = row["id"]
        return "updated"
    chain["id"] = db.new_id()
    con.execute(
        "INSERT INTO fin_causal_chains (id, signature, title, catalyst_event, "
        "catalyst_entity, catalyst_domain, terminal_outcome, "
        "transmission_channel, overall_confidence, base_probability, "
        "time_horizon, affected_sectors, sensitive_tickers, "
        "historical_precedent, steps, dampeners, corroborating_story_ids, "
        "prose_hash, schema_version, updated_at, created_at) "
        "VALUES (?,?," + ",".join("?" * 19) + ")",
        (chain["id"], chain["signature"]) + vals + (now,))
    return "new"


def build(con):
    """The `fin_causal` stage: rebuild the chain set from the current graph.

    Cost is bounded twice over. The structure — which is all of the substance —
    costs no LLM calls at all. Narration is asked for only when a chain is new
    or its `prose_hash` moved, and never more than
    FIN_MAX_CAUSAL_PROSE_CALLS times in one run, so a steady graph settles to
    zero calls per run while a fresh one fills in over a few cycles."""
    deg = kg.degrees(con, NAMESPACE)
    if not deg:
        db.log_run(con, "fin_causal", "ok",
                   "knowledge graph is empty; serving curated chains")
        return 0
    names = _node_names(con)
    candidates, seen_sigs = [], set()
    for seed in _seeds(con, names, deg, config.FIN_MAX_CAUSAL_CHAINS * 3):
        chain = _build_one(con, seed, deg, names)
        if not chain or chain["signature"] in seen_sigs:
            continue
        seen_sigs.add(chain["signature"])
        candidates.append(chain)
    # Prune BEFORE narrating: a sub-path that will be discarded must not spend
    # one of the run's prose calls on its way to being discarded.
    built = _dedupe_paths(candidates)
    built.sort(key=lambda c: -c["overall_confidence"])
    built = built[:config.FIN_MAX_CAUSAL_CHAINS]

    if not built:
        db.log_run(con, "fin_causal", "ok",
                   f"no path of {config.FIN_CAUSAL_MIN_STEPS}+ steps in a graph "
                   f"of {len(deg)} nodes; serving curated chains")
        return 0

    known = {r["signature"]: r for r in con.execute(
        "SELECT signature, prose_hash, title, catalyst_event, catalyst_domain, "
        "terminal_outcome, transmission_channel, time_horizon, "
        "historical_precedent, steps, dampeners FROM fin_causal_chains")}
    narrated = reused = 0
    for chain in built:
        prior = known.get(chain["signature"])
        if prior and prior["prose_hash"] == chain["prose_hash"] and prior["title"]:
            # Structure unchanged — the words would come back the same, so keep
            # them. This is the whole cost argument for the stage.
            for k in ("title", "catalyst_event", "catalyst_domain",
                      "terminal_outcome", "transmission_channel",
                      "time_horizon", "historical_precedent"):
                chain[k] = prior[k] or ""
            said = db.uj(prior["steps"], [])
            for i, s in enumerate(chain["steps"]):
                if i < len(said) and isinstance(said[i], dict):
                    s["stage_name"] = said[i].get("stage_name") or s["stage_name"]
                    s["action_or_friction"] = (said[i].get("action_or_friction")
                                               or s["action_or_friction"])
            chain["dampeners"] = db.uj(prior["dampeners"], [])
            reused += 1
        elif narrated < config.FIN_MAX_CAUSAL_PROSE_CALLS:
            try:
                if _narrate(con, chain, names):
                    narrated += 1
            except Exception as e:                       # noqa: BLE001
                llm._note("chain_prose_failed", "finance", "fin_causal", str(e)[:200])
        _fallback_prose(chain)

    new_ct = upd_ct = 0
    for chain in built:
        if _persist(con, chain) == "new":
            new_ct += 1
        else:
            upd_ct += 1

    live = {c["signature"] for c in built}
    placeholders = ",".join("?" * len(live))
    retired = con.execute(
        f"UPDATE fin_causal_chains SET retired_at=? WHERE retired_at IS NULL "
        f"AND signature NOT IN ({placeholders}) AND updated_at < ?",
        (db.now(), *live, db.now() - config.FIN_CAUSAL_RETIRE_DAYS * 86400)
    ).rowcount
    # Migrations must commit: an uncommitted write is invisible to the API's
    # own connection, so the page would keep serving curated chains.
    con.commit()
    db.log_run(con, "fin_causal", "ok",
               f"{new_ct} new + {upd_ct} updated chains from {len(deg)} graph "
               f"nodes; {narrated} narrated, {reused} prose reused, "
               f"{retired} retired")
    return new_ct + upd_ct


def simulate_counterfactual_shock(
    shock_id: str,
    intensity_pct: float = 0.0,
    horizon_override: str = "",
    con=None,
) -> Dict[str, Any]:
    """Calculate the downstream ripple effects when an exogenous catalyst shock is simulated.

    Args:
        shock_id: ID of a chain from list_causal_chains().
        intensity_pct: Magnitude of the shock (-100% to +100%).
        horizon_override: Optional custom time horizon.
        con: Optional DB handle; without it only curated chains resolve.

    An unknown `shock_id` returns {"error": ...} and nothing else. It used to
    fall through to CANONICAL_CAUSAL_CHAINS[0], which answered a question
    nobody asked with a full, confident-looking simulation of a different
    chain — the worst available failure mode for a page about consequences.
    """
    chain = get_chain(con, shock_id)
    if chain is None:
        return {"error": f"unknown shock_id '{shock_id}'",
                "shock_id": shock_id,
                "available": [c["id"] for c in list_causal_chains(con)][:10]}

    # Base transmission calculations
    normalized_intensity = max(-1.0, min(1.0, float(intensity_pct or 0.0) / 100.0))
    base_prob = float(chain.get("base_probability") or 0.0)

    # Dampener absorption. Generated chains carry no percentage — absorption
    # capacity is not measurable from this data (see _narrate) — so they damp
    # nothing and the simulation says so via dampener_absorption_pct=0 rather
    # than by inventing a cushion.
    pcts = [d.get("absorption_capacity_pct") for d in chain.get("dampeners", [])]
    total_absorption = sum(p for p in pcts if isinstance(p, (int, float))) / 100.0
    effective_dampener = min(0.65, total_absorption)

    # Elasticity-adjusted ripple probability
    # Shock amplitude scales the probability non-linearly
    if normalized_intensity >= 0:
        simulated_prob = base_prob + (1.0 - base_prob) * (normalized_intensity * 0.4)
    else:
        simulated_prob = base_prob * (1.0 + normalized_intensity * 0.5)

    # Net terminal probability accounting for dampeners
    net_terminal_prob = simulated_prob * (1.0 - (effective_dampener * 0.4))
    net_terminal_prob = max(0.1, min(0.98, net_terminal_prob))

    # Calculate step-by-step impact values
    steps_output = []
    for idx, step in enumerate(chain["steps"]):
        # Downstream steps experience decay based on elasticity
        step_elasticity = step["elasticity_score"]
        step_impact = abs(normalized_intensity) * step_elasticity * (1.0 - (idx * 0.08))
        direction = "EXPANSION / SURPLUS" if normalized_intensity < 0 else "FRICTION / CONTRACTION"
        
        steps_output.append({
            "step_order": step["step_order"],
            "stage_name": step["stage_name"],
            "entity": step["entity"],
            "action_or_friction": step["action_or_friction"],
            "channel": step["channel"],
            "elasticity_score": step_elasticity,
            "computed_impact_pct": round(step_impact * 100, 1),
            "direction": direction,
            "propagation_horizon": step["propagation_horizon"],
            "affected_tickers": step["affected_tickers"]
        })

    # Sensitive ticker impact matrix. Exposure is read off THIS chain — how far
    # along it the ticker appears and how well the relationship reaching it is
    # supported — rather than from a hard-coded list of four symbols, which is
    # what this was and which made every other company "MODERATE" forever.
    step_of = {}
    for s in steps_output:
        for t in s.get("affected_tickers") or []:
            # Earliest appearance wins: a company hit at step 2 is more exposed
            # than one the effect only reaches at step 5.
            if t not in step_of or s["step_order"] < step_of[t]["step_order"]:
                step_of[t] = s
    ticker_impacts = {}
    for ticker in chain.get("sensitive_tickers", []):
        s = step_of.get(ticker)
        elasticity = float(s["elasticity_score"]) if s else 0.5
        depth = int(s["step_order"]) if s else len(steps_output)
        # Near the catalyst AND on a well-supported link is the only combination
        # that earns HIGH.
        tier = ("HIGH" if elasticity >= 0.7 and depth <= 2 else
                "LOW" if elasticity < 0.5 or depth >= 4 else "MODERATE")
        ticker_impacts[ticker] = {
            "exposure_tier": tier,
            "sensitivity_beta": round(elasticity * (0.85 ** max(0, depth - 1)) * 1.5, 2),
            "expected_transmission": (
                f"{'+' if normalized_intensity < 0 else '-'}"
                f"{round(abs(normalized_intensity) * elasticity * 4.2, 1)}% "
                f"directional pressure"),
            "status": ("Vulnerable to Pass-Through" if normalized_intensity > 0
                       else "Liquidity & Supply Relieved"),
            "reached_at_step": depth,
        }

    return {
        "shock_id": chain["id"],
        "shock_title": chain["title"],
        "catalyst_entity": chain["catalyst_entity"],
        "applied_intensity_pct": round(intensity_pct, 1),
        "base_probability": round(base_prob, 3),
        "simulated_ripple_probability": round(net_terminal_prob, 3),
        "dampener_absorption_pct": int(effective_dampener * 100),
        "time_horizon": horizon_override or chain["time_horizon"],
        "transmission_channel": chain["transmission_channel"],
        "terminal_outcome": chain["terminal_outcome"],
        "simulated_steps": steps_output,
        "ticker_impacts": ticker_impacts,
        "dampeners": chain.get("dampeners") or [],
        "historical_precedent": chain.get("historical_precedent") or "",
        # So a caller can tell a simulation of today's graph from one of the
        # seed content, without a second request.
        "curated": bool(chain.get("curated")),
    }
