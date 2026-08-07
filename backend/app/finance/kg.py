"""The financial knowledge graph: storage and traversal.

Edges are written by Agent 1 as it reads each story, and walked by Agent 2 to
find second-order exposure — "who else is on the other end of a relationship
with the company this happened to". SQLite holds it: two indexed tables, no
extra service, and a walk is a handful of indexed lookups.

Every traversal here is bounded by node budget AND hop depth, deliberately.
The general pipeline's ConnectionFinder once OOM-killed production with an
unbounded pair scan, and a graph walk is the same shape of risk with a worse
constant — a well-connected node like "Reserve Bank of India" is adjacent to a
large fraction of the graph, so an unbounded BFS from it visits everything.
"""
import heapq
import json

from .. import config, db
from ..schemas.finance import (Direction, EntityType, Evidence, ImpactLink,
                               KGRelationship)
from . import tickers as tk

NAMESPACE = "finance"

# A hub this well connected carries no information about a specific cascade:
# "RBI" touching two companies does not make them exposed to each other. Nodes
# above this degree are traversable as endpoints but never used as a bridge.
HUB_DEGREE = 40
# Ceiling on nodes visited in one walk, whatever the depth allows.
MAX_VISITED = 400


def upsert_node(con, name, type_, ticker=None):
    """Add or touch a node. Returns its canonical id.

    The id is the canonical name from tickers.canonical() — a symbol when one
    resolved — so the three ways an article can spell a company collapse to one
    node. Without this the graph looks populated and every walk comes back
    empty, because the edges were written against names that never match."""
    node_id = tk.canonical(name)
    if not node_id:
        return ""
    now = db.now()
    type_val = type_.value if isinstance(type_, EntityType) else str(type_)
    sym = ticker.symbol if (ticker and ticker.resolved) else None
    exch = ticker.exchange if (ticker and ticker.resolved) else ""
    con.execute(
        "INSERT INTO fin_kg_nodes (namespace,id,name,type,ticker,exchange,"
        "mentions,first_seen,last_seen) VALUES (?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(namespace,id) DO UPDATE SET "
        "mentions = mentions + 1, last_seen = excluded.last_seen, "
        # Keep the first non-empty ticker/type rather than letting a later,
        # vaguer mention blank out what an earlier one established.
        "ticker = COALESCE(fin_kg_nodes.ticker, excluded.ticker), "
        "type = CASE WHEN fin_kg_nodes.type IN ('', 'organization') "
        "            THEN excluded.type ELSE fin_kg_nodes.type END",
        (NAMESPACE, node_id, name, type_val, sym, exch, now, now))
    return node_id


def upsert_edge(con, rel: KGRelationship, story_id=""):
    """Write one relationship, merging onto the existing edge if this pair and
    predicate are already known. Returns the edge id.

    Confidence takes the MAX of the two reports, not the latest: a second
    outlet confirming a relationship should never be able to lower our
    confidence in it below what the first one supported."""
    subj = upsert_node(con, rel.subject, rel.subject_type)
    obj = upsert_node(con, rel.object, rel.object_type)
    if not subj or not obj or subj == obj:
        return ""      # canonicalisation can collapse two names onto one node
    now = db.now()
    row = con.execute(
        "SELECT id, confidence, story_ids, evidence FROM fin_kg_edges "
        "WHERE namespace=? AND subject=? AND predicate=? AND object=?",
        (rel.namespace, subj, rel.predicate, obj)).fetchone()
    ev = [e.model_dump(mode="json") for e in rel.evidence]
    if row:
        sids = sorted(set(db.uj(row["story_ids"], [])) | ({story_id} if story_id else set()))
        prior_ev = db.uj(row["evidence"], [])
        # Evidence is capped: an edge reported by 40 outlets does not need 40
        # quotes stored to be believable, and this table is append-mostly.
        merged_ev = (prior_ev + [e for e in ev if e not in prior_ev])[:8]
        con.execute(
            "UPDATE fin_kg_edges SET confidence=?, story_ids=?, evidence=?, "
            "updated_at=?, event_type=COALESCE(NULLIF(event_type,'other'),?) "
            "WHERE id=?",
            (max(float(row["confidence"] or 0), rel.confidence), db.j(sids),
             db.j(merged_ev), now, rel.event_type.value, row["id"]))
        return row["id"]
    eid = db.new_id()
    con.execute(
        "INSERT INTO fin_kg_edges (id,namespace,subject,predicate,object,"
        "subject_type,object_type,event_type,value,confidence,evidence,"
        "story_ids,occurred_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, rel.namespace, subj, rel.predicate, obj,
         rel.subject_type.value, rel.object_type.value, rel.event_type.value,
         db.j(rel.value.model_dump(mode="json")) if rel.value else None,
         rel.confidence, db.j(ev), db.j([story_id] if story_id else []),
         rel.occurred_at, now, now))
    return eid


def degrees(con, namespace=NAMESPACE):
    """{node: edge count}. One grouped scan, used to identify hubs before a
    walk rather than counting neighbours per node inside the loop."""
    out = {}
    for col in ("subject", "object"):
        for r in con.execute(
                f"SELECT {col} AS n, COUNT(*) c FROM fin_kg_edges "
                f"WHERE namespace=? GROUP BY {col}", (namespace,)).fetchall():
            out[r["n"]] = out.get(r["n"], 0) + r["c"]
    return out


def neighbours(con, node, namespace=NAMESPACE, min_confidence=0.0):
    """Edges touching `node`, in both directions, as (other_node, row, forward)."""
    rows = con.execute(
        "SELECT * FROM fin_kg_edges WHERE namespace=? AND (subject=? OR object=?) "
        "AND confidence >= ?", (namespace, node, node, min_confidence)).fetchall()
    out = []
    for r in rows:
        forward = r["subject"] == node
        out.append(((r["object"] if forward else r["subject"]), r, forward))
    return out


def cascade(con, seeds, max_hops=None, namespace=NAMESPACE, min_confidence=0.35,
            max_links=40):
    """Breadth-first walk from `seeds`, returning ImpactLinks ordered by hop.

    `order` on each link is the hop depth: 1 is a direct relationship with a
    seed, 2 is one step further out (the supplier of the affected company), and
    so on. That is precisely the spec's second-order impact mapping, computed
    over real stored relationships instead of asked of the model — the model
    is not able to tell you who supplies whom, but the graph it built from the
    reporting can.

    Confidence decays with distance. A three-hop chain asserted with the same
    confidence as a direct one would be dishonest: each hop is another
    relationship that has to hold.
    """
    max_hops = max_hops or config.FIN_MAX_CASCADE_HOPS
    deg = degrees(con, namespace)
    seen_nodes = {s for s in seeds if s}
    if not seen_nodes:
        return []
    frontier = [(s, 0) for s in sorted(seen_nodes)]
    links, seen_pairs = [], set()
    while frontier and len(links) < max_links and len(seen_nodes) < MAX_VISITED:
        node, hop = frontier.pop(0)
        if hop >= max_hops:
            continue
        # A hub is a legitimate destination but a meaningless bridge, so we
        # never expand THROUGH one. Without this, every regulator in the graph
        # turns any two companies it has touched into "connected".
        if hop > 0 and deg.get(node, 0) > HUB_DEGREE:
            continue
        for other, row, forward in neighbours(con, node, namespace, min_confidence):
            # Dedupe on the EDGE, not on (node, predicate, other). An edge is
            # reachable from both of its ends, so a direction-sensitive key
            # emits the same relationship twice — once at hop N going out and
            # again at hop N+1 coming back — and the second, weaker copy
            # overwrites the first wherever a caller keys by entity pair.
            if row["id"] in seen_pairs or not other:
                continue
            seen_pairs.add(row["id"])
            decay = 0.75 ** hop
            links.append(ImpactLink(
                from_entity=node if forward else other,
                to_entity=other if forward else node,
                mechanism=_mechanism(row, forward),
                order=min(hop + 1, 4),
                direction=Direction.UNCLEAR,
                confidence=max(0.0, min(1.0, float(row["confidence"] or 0) * decay)),
                via_relationship=_row_to_rel(row)))
            if other not in seen_nodes:
                seen_nodes.add(other)
                frontier.append((other, hop + 1))
    # Strongest links first, then shortest — a weak direct link is usually less
    # interesting than a strong two-hop one, but ties break toward directness.
    links.sort(key=lambda l: (-l.confidence, l.order))
    return links[:max_links]


def _mechanism(row, forward):
    verb = (row["predicate"] or "related_to").replace("_", " ")
    return (f"{row['subject']} {verb} {row['object']}" if forward
            else f"{row['subject']} {verb} {row['object']} (reverse exposure)")


def _row_to_rel(row):
    try:
        return KGRelationship(
            subject=row["subject"], predicate=row["predicate"], object=row["object"],
            subject_type=row["subject_type"] or EntityType.ORGANIZATION,
            object_type=row["object_type"] or EntityType.ORGANIZATION,
            namespace=row["namespace"], event_type=row["event_type"] or "other",
            confidence=float(row["confidence"] or 0),
            occurred_at=row["occurred_at"],
            evidence=[Evidence(**e) for e in db.uj(row["evidence"], [])
                      if isinstance(e, dict) and e.get("article_id")])
    except (ValueError, TypeError, json.JSONDecodeError):
        # A stored edge that no longer validates (older schema, hand-edited
        # row) must not take down a walk that has nothing to do with it.
        return None


def top_entities(con, namespace=NAMESPACE, limit=20, since=None):
    """Most-mentioned nodes in the window — the seeds Agent 2 starts from."""
    since = since if since is not None else db.now() - config.FIN_TREND_WINDOW_DAYS * 86400
    return [dict(r) for r in con.execute(
        "SELECT id, name, type, ticker, mentions FROM fin_kg_nodes "
        "WHERE namespace=? AND last_seen > ? "
        "ORDER BY mentions DESC, last_seen DESC LIMIT ?",
        (namespace, since, limit)).fetchall()]


def prune(con, days=None, namespace=NAMESPACE):
    """Drop edges nobody has re-reported in `days`, then orphaned nodes.

    Written at birth rather than added after the table grew: story_history
    needed exactly this and only got it after it had become a problem."""
    days = days if days is not None else config.FIN_KG_RETAIN_DAYS
    cutoff = db.now() - days * 86400
    edges = con.execute("DELETE FROM fin_kg_edges WHERE namespace=? AND updated_at < ?",
                        (namespace, cutoff)).rowcount
    nodes = con.execute(
        "DELETE FROM fin_kg_nodes WHERE namespace=? AND last_seen < ? AND id NOT IN "
        "(SELECT subject FROM fin_kg_edges WHERE namespace=? "
        " UNION SELECT object FROM fin_kg_edges WHERE namespace=?)",
        (namespace, cutoff, namespace, namespace)).rowcount
    return edges, nodes


def stats(con, namespace=NAMESPACE):
    n = con.execute("SELECT COUNT(*) c FROM fin_kg_nodes WHERE namespace=?",
                    (namespace,)).fetchone()["c"]
    e = con.execute("SELECT COUNT(*) c FROM fin_kg_edges WHERE namespace=?",
                    (namespace,)).fetchone()["c"]
    deg = degrees(con, namespace)
    hubs = heapq.nlargest(5, deg.items(), key=lambda kv: kv[1])
    return {"nodes": n, "edges": e,
            "hubs": [{"node": k, "degree": v} for k, v in hubs]}
