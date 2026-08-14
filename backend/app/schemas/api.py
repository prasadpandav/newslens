"""Public wire schemas — what third-party consumers of the HTTP API decode.

These exist to make `/openapi.json` describe real response shapes. Before this
module every 200 was documented as a bare `{}`, because the handlers return
plain dicts and there was nothing for FastAPI to introspect.

WHY THESE ARE DOCUMENTATION-ONLY
--------------------------------
They are attached with `responses={200: {"model": X}}`, deliberately NOT with
`response_model=X`. `response_model` would make FastAPI *filter* every response
through the model, and two properties of this API make that unsafe:

  * Several keys are omitted rather than nulled, and the omission carries
    meaning. `_evidence` sends no `claims_disputed` when nothing was checked,
    so a client can tell "no disputed claims" from "we never looked"; the
    `correction` key is present only on stories that have one. Validating a
    plain dict into a model and serialising it back would turn those absences
    into explicit nulls.
  * Any field this module failed to declare would be silently dropped from the
    live response — a regression in the iOS and web clients, caused by a
    documentation change.

So the schemas describe; they do not enforce. The cost of that choice is that
they can drift from the handlers, which is what test_openapi.py exists to
catch: it validates real responses against these models.

Leaf types (Metric, EntityRow, ActorView, ImpactLink, Scenario, ...) are
imported from schemas/finance.py rather than restated, so the documented shape
and the pipeline's own contract cannot disagree.
"""
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .finance import (ActorView, EntityRow, Evidence, ImpactLink, Invalidator,
                      Metric, Scenario, Ticker)


class _Out(BaseModel):
    """Wire models are open: a handler may send keys newer than this file, and
    a consumer must tolerate that rather than reject the payload."""
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------- shared bits
class SourceRef(_Out):
    """One article behind a story."""
    title: str = ""
    url: str = ""
    source: str = Field(default="", description="Publisher host, e.g. 'reuters.com'.")


class ClaimVerdict(_Out):
    claim: str = ""
    verdict: str = Field(default="", description="corroborated | disputed | unverified")
    note: str = ""


class Claims(_Out):
    claims: list[str] = Field(default_factory=list)
    verdicts: list[ClaimVerdict] = Field(default_factory=list)


class Beat(_Out):
    """One section of the narrative. `beats` is null — not [] — on stories
    written before the split existed; fall back to `narrative` when it is."""
    label: str = ""
    text: str = ""


class Anchor(_Out):
    """Ties a claim (by index into `claims.claims`) to the sentence it came from."""
    claim: int = 0
    quote: str = ""


class Correction(_Out):
    """Present ONLY when a story's corroboration has fallen or its claims have
    started to be argued over. The key's absence means "nothing to report" —
    never "stable". Never a publisher retraction: this is our own measurement
    moving, and `note` is the plain-words phrasing meant to be printed as-is."""
    kind: str = Field(default="", description="weakened | contested | conflicting")
    from_: float = Field(default=0.0, alias="from",
                         description="Peak credibility this story reached.")
    to: float = 0.0
    delta: float = 0.0
    disputed_added: int = 0
    conflicts_added: int = 0
    since: Optional[float] = None
    note: str = ""


class HistoryPoint(_Out):
    """Corroboration over time, oldest first."""
    credibility: Optional[float] = None
    source_count: Optional[int] = None
    verified: Optional[int] = None
    disputed: Optional[int] = None
    unverified: Optional[int] = None
    conflicts: Optional[int] = None
    created_at: Optional[float] = None


class FramingPosition(_Out):
    source: str = ""
    pos: float = Field(default=0.0, ge=-1.0, le=1.0)
    note: str = ""


class FramingAxis(_Out):
    left: str = ""
    right: str = ""


class Framing(_Out):
    """Null until a reader opens the framing panel on this story. An object
    WITHOUT `positions` means it was computed and found no usable spread."""
    axis: FramingAxis = Field(default_factory=FramingAxis)
    positions: list[FramingPosition] = Field(default_factory=list)
    spread: str = Field(default="", description="wide | moderate | narrow")
    span: float = 0.0
    missing: str = ""


class _Evidenced(_Out):
    """The evidence-panel counts. EVERY field here is omitted when the
    underlying data was never computed, so absent != zero."""
    claims_verified: Optional[int] = None
    claims_disputed: Optional[int] = None
    claims_unverified: Optional[int] = None
    claims_total: Optional[int] = None
    source_count: Optional[int] = None
    conflicts: Optional[int] = None
    source_kinds: Optional[dict[str, int]] = Field(
        default=None, description="Outlet mix, e.g. {'newsroom': 3, 'wire': 1}.")
    source_primary: Optional[int] = None


# ---------------------------------------------------------------------- feed
class FeedItem(_Evidenced):
    """One card in the feed.

    Finance-pipeline stories appear here too, carrying `kind: "finance"` and
    four extra summary fields. They are otherwise ordinary feed items — same
    `topic`, same card, same `/story/{id}` for the detail — so a client that
    ignores `kind` behaves exactly as it did before finance existed.
    """
    id: str
    headline: str = ""
    narrative: str = ""
    credibility: float = Field(default=0.0, ge=0.0, le=100.0,
                               description="How well corroborated, 0-100. The "
                                           "median story scores around 25.")
    credibility_note: str = ""
    topic: str = Field(default="", description="Beat the client's chips filter on, "
                                               "e.g. finance, business, world.")
    place: Optional[str] = None
    image_url: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    trend_ids: list[str] = Field(default_factory=list)
    impact_text: str = Field(default="", description="Personalised; empty until the "
                                                     "reader opens 'what this means "
                                                     "for you' on this story.")
    impact_score: float = 0.0
    correction: Optional[Correction] = None

    # ---- finance-only. Absent on ordinary news items.
    kind: Optional[str] = Field(
        default=None, description="'finance' on finance-pipeline stories; absent "
                                  "otherwise. This is the flag to badge on.")
    event_type: Optional[str] = Field(
        default=None, description="earnings | m_and_a | regulation | ... ")
    sectors: Optional[list[str]] = None
    tickers: Optional[list[str]] = None
    sentiment_net: Optional[float] = Field(
        default=None, ge=-1.0, le=1.0,
        description="Net actor sentiment, -1 to 1.")
    metric_count: Optional[int] = Field(
        default=None, description="How many extracted figures the detail carries.")


class FeedResponse(_Out):
    items: list[FeedItem] = Field(default_factory=list)


# -------------------------------------------------------------- story detail
class TrendRef(_Out):
    id: str = ""
    kind: str = ""
    name: str = ""
    narrative: str = ""
    velocity: float = 0.0


class ConnectionRef(_Out):
    """`locked` is true for signed-out readers: they learn a connection exists
    and to which story, but `chain` — the actual inference — stays empty."""
    chain: str = ""
    confidence: Optional[float] = None
    locked: bool = False
    other_title: str = ""
    other_url: str = ""


class StoryDetail(_Evidenced):
    """GET /story/{id}. Serves BOTH pipelines: a finance story id resolves here
    too, answering with `kind: "finance"` and the finance-only fields appended.
    That is why there is one id space and one detail endpoint."""
    id: str
    headline: str = ""
    narrative: str = ""
    why_matters: str = Field(default="", description="Empty on older stories.")
    credibility: float = 0.0
    credibility_note: str = ""
    claims: Claims = Field(default_factory=Claims)
    topic: str = ""
    beats: Optional[list[Beat]] = None
    anchors: Optional[list[Anchor]] = None
    framing: Optional[Framing] = None
    image_url: str = ""
    sources: list[SourceRef] = Field(default_factory=list)
    trends: list[TrendRef] = Field(default_factory=list)
    connections: list[ConnectionRef] = Field(default_factory=list)
    created_at: float = 0.0
    impact_text: str = ""
    impact_score: float = 0.0
    history: list[HistoryPoint] = Field(default_factory=list)
    correction: Optional[Correction] = None

    # ---- finance-only
    kind: Optional[str] = None
    event_type: Optional[str] = None
    sectors: Optional[list[str]] = None
    tickers: Optional[list[str]] = None
    metrics: Optional[list[Metric]] = None
    entities: Optional[dict[str, Any]] = Field(
        default=None, description="{'rows': [EntityRow], 'unresolved': [str]}.")
    sentiment: Optional[dict[str, Any]] = Field(
        default=None, description="{'actors': [ActorView], 'rationale': str}.")
    sentiment_net: Optional[float] = None
    economic_drivers: Optional[list[str]] = None


# ----------------------------------------------------------- finance stories
class FinanceStorySummary(_Out):
    """List shape. The heavy structures — metric table, entity table, actor
    sentiment — are sent only by GET /finance/story/{id}; `metric_count` is
    here so a list can badge a story without carrying them."""
    id: str
    headline: str = ""
    event_type: str = ""
    topic: str = ""
    credibility: float = 0.0
    sectors: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    sentiment_net: float = Field(default=0.0, ge=-1.0, le=1.0)
    sentiment_dispersion: float = Field(
        default=0.0, description="How far apart the actors are. High means the "
                                 "net figure hides real disagreement.")
    image_url: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    narrative: str = Field(default="", description="Truncated to 280 chars here.")
    metric_count: int = 0


class EntityTableOut(_Out):
    rows: list[EntityRow] = Field(default_factory=list)
    unresolved: list[str] = Field(
        default_factory=list,
        description="Named in the reporting but not resolved to a known entity.")


class SentimentProfileOut(_Out):
    actors: list[ActorView] = Field(default_factory=list)
    rationale: str = ""


class StoryRelationship(_Out):
    """A graph edge this story established. `subject`/`object` are canonical
    ids (a ticker where one resolved) and are what joins across stories;
    `subject_name`/`object_name` are as the article wrote them — print those."""
    subject: str = ""
    predicate: str = Field(default="", description="acquires | fines | supplies | ...")
    object: str = ""
    subject_name: str = ""
    object_name: str = ""
    subject_type: str = ""
    object_type: str = ""
    event_type: str = ""
    confidence: float = 0.0


class FinanceStoryDetail(FinanceStorySummary):
    narrative: str = Field(default="", description="Full text here, unlike the list.")
    why_matters: str = ""
    credibility_note: str = ""
    claims: Claims = Field(default_factory=Claims)
    article_ids: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list,
                               description="Publisher hosts, not objects.")
    geographies: list[str] = Field(default_factory=list)
    entities: EntityTableOut = Field(default_factory=EntityTableOut)
    metrics: list[Metric] = Field(default_factory=list)
    sentiment: SentimentProfileOut = Field(default_factory=SentimentProfileOut)
    economic_drivers: list[str] = Field(default_factory=list)
    beats: Optional[list[Beat]] = None
    anchors: Optional[list[Anchor]] = None
    merge_stats: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)
    schema_version: int = 1
    relationships: list[StoryRelationship] = Field(default_factory=list)


class FinanceStoryList(_Out):
    stories: list[FinanceStorySummary] = Field(default_factory=list)
    count: int = 0


# ------------------------------------------------------------ finance trends
class FinanceTrendOut(_Out):
    id: str
    name: str = ""
    narrative: str = ""
    arc: list[str] = Field(default_factory=list,
                           description="The stages this force has moved through.")
    cascade: list[ImpactLink] = Field(default_factory=list)
    story_ids: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    macro_factors: list[str] = Field(default_factory=list)
    window_days: int = 7
    velocity: float = Field(default=0.0, description="How fast evidence is accruing.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class FinanceTrendList(_Out):
    trends: list[FinanceTrendOut] = Field(default_factory=list)
    count: int = 0


# --------------------------------------------------------- finance forecasts
class FinanceForecastOut(_Out):
    """Directional scenario analysis. `scenarios` is always exactly three —
    base, bull, bear — with probabilities summing to 1."""
    id: str
    title: str = ""
    trend_ids: list[str] = Field(default_factory=list)
    story_ids: list[str] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    short_term: str = Field(default="", description="1-30 days.")
    long_term: str = Field(default="", description="1-12 months.")
    risks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    invalidation: list[Invalidator] = Field(
        default_factory=list,
        description="Observables that would falsify this. Render them.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    disclaimer: str = Field(
        default="", description="Not-investment-advice framing. Sent on every "
                                "forecast because it must travel with the "
                                "content wherever it is rendered — display it.")
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class FinanceForecastList(_Out):
    forecasts: list[FinanceForecastOut] = Field(default_factory=list)
    count: int = 0


# ------------------------------------------------------------- finance graph
class GraphHub(_Out):
    node: str = ""
    degree: int = 0


class GraphStats(_Out):
    nodes: int = 0
    edges: int = 0
    hubs: list[GraphHub] = Field(default_factory=list)


class GraphEntity(_Out):
    id: str = Field(default="", description="Canonical id — a ticker where one resolved.")
    name: str = ""
    type: str = ""
    ticker: Optional[str] = None
    mentions: int = 0


class GraphEdge(_Out):
    from_entity: str = ""
    to_entity: str = ""
    mechanism: str = Field(default="", description="Why the effect transmits.")
    confidence: float = 0.0
    order: int = Field(default=1, description="Hop distance from the seed.")


class FinanceGraphResponse(_Out):
    """Two shapes behind one path. Without `?entity=` you get the overview —
    `stats`, `top`, `links`. With `?entity=` you get the cascade around that
    entity — `entity`, `resolved_to`, `links`, `count` — where `links` are
    ImpactLinks whose `order` is hop depth and whose confidence decays with it."""
    stats: Optional[GraphStats] = None
    top: Optional[list[GraphEntity]] = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    entity: Optional[str] = None
    resolved_to: Optional[str] = Field(
        default=None, description="What `entity` resolved to, e.g. 'HDFCBANK'.")
    count: Optional[int] = None


class EntityStoryRef(_Out):
    id: str
    headline: str = ""
    narrative: str = ""
    credibility: float = 0.0
    credibility_note: str = ""
    created_at: float = 0.0


class FinanceEntityStories(_Out):
    entity: str = ""
    resolved_to: Optional[str] = None
    stories: list[EntityStoryRef] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = Field(
        default=None, description="Present instead of results if the lookup failed. "
                                  "The status is still 200 — check for this key.")


# ------------------------------------------------------------ causal chains
class CausalStep(_Out):
    step_order: int = 0
    stage_name: str = ""
    entity: str = Field(default="", description="A real graph node, never invented.")
    action_or_friction: str = ""
    channel: str = Field(
        default="", description="The relationship that reached this step, e.g. "
                                "'regulates', 'supplies'. 'catalyst' on step 1.")
    elasticity_score: float = Field(
        default=0.0, description="How strongly this step transmits — the "
                                 "confidence of the link that reached it. 1.0 on "
                                 "the catalyst, which acts under its own steam.")
    propagation_horizon: str = Field(
        default="", description="Derived from POSITION in the chain, not measured. "
                                "Deliberately vague ('Weeks', '1-3 months').")
    affected_tickers: list[str] = Field(default_factory=list)
    evidence_quote: str = ""


class Dampener(_Out):
    """A force that absorbs the shock before it reaches the terminal outcome."""
    name: str = ""
    mechanism: str = ""
    absorption_capacity_pct: Optional[int] = Field(
        default=None, description="Null on generated chains: absorption capacity "
                                  "is not measurable from this data, and a "
                                  "plausible number under a real organisation's "
                                  "name would be invented. Render '—' for null.")
    sponsor_entity: str = ""


class CausalChain(_Out):
    """A path through the knowledge graph: each step a real entity, each
    `channel` a relationship the reporting established."""
    id: str
    signature: Optional[str] = Field(
        default=None, description="The canonical node path ('RBI>HDFCBANK>...'). "
                                  "Stable identity across rebuilds.")
    curated: bool = Field(
        default=False, description="True on the hand-written seed chains served "
                                   "only while the graph has produced none. "
                                   "Those carry no corroborating stories.")
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    title: str = ""
    catalyst_event: str = ""
    catalyst_entity: str = ""
    catalyst_domain: str = ""
    terminal_outcome: str = ""
    transmission_channel: str = ""
    overall_confidence: float = Field(
        default=0.0, description="Geometric mean of the confidence of each link "
                                 "— how well evidenced a typical hop is.")
    base_probability: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Joint confidence that EVERY link holds (the product), so it "
                    "falls as chains lengthen. A long chain scoring low is "
                    "honest, not broken.")
    time_horizon: str = ""
    affected_sectors: list[str] = Field(default_factory=list)
    sensitive_tickers: list[str] = Field(default_factory=list)
    historical_precedent: str = ""
    steps: list[CausalStep] = Field(default_factory=list)
    dampeners: list[Dampener] = Field(default_factory=list)
    corroborating_story_ids: list[str] = Field(
        default_factory=list,
        description="Live stories supporting this chain. Resolve via GET /story/{id}.")


class CausalChainList(_Out):
    chains: list[CausalChain] = Field(default_factory=list)


class SimulatedStep(CausalStep):
    computed_impact_pct: float = 0.0
    direction: str = Field(
        default="", description="EXPANSION / SURPLUS | FRICTION / CONTRACTION")


class TickerImpact(_Out):
    exposure_tier: str = Field(
        default="", description="HIGH | MODERATE | LOW, from where this ticker "
                                "sits in the chain: near the catalyst on a "
                                "well-evidenced link is HIGH.")
    sensitivity_beta: float = 0.0
    expected_transmission: str = Field(
        default="", description="Directional pressure only — never a price or a "
                                "target.")
    status: str = ""
    reached_at_step: Optional[int] = None


class CausalSimulation(_Out):
    """A counterfactual: what a shock of `applied_intensity_pct` does to one
    chain. Directional analysis over stored relationships — never a price."""
    shock_id: str = Field(
        default="", description="The chain actually simulated. An unrecognised "
                                "shock_id silently falls back to the first "
                                "chain, so compare this against what you sent.")
    shock_title: str = ""
    catalyst_entity: str = ""
    applied_intensity_pct: float = 0.0
    base_probability: float = 0.0
    simulated_ripple_probability: float = 0.0
    dampener_absorption_pct: int = 0
    time_horizon: str = ""
    transmission_channel: str = ""
    terminal_outcome: str = ""
    simulated_steps: list[SimulatedStep] = Field(default_factory=list)
    ticker_impacts: dict[str, TickerImpact] = Field(default_factory=dict)
    dampeners: list[Dampener] = Field(default_factory=list)
    historical_precedent: str = ""
    curated: bool = False
    error: Optional[str] = Field(
        default=None, description="Present with a 200 when `shock_id` is unknown; "
                                  "`available` then lists valid ids and no "
                                  "simulation fields are sent.")
    available: Optional[list[str]] = None


__all__ = [n for n in dir() if n[0].isupper()]
