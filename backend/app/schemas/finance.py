"""Pydantic schemas for the finance pipeline.

These are the contract between the three finance agents and SQLite. Every model
here is parsed from LLM output, so the rules are written for that reality:

  * `extra="ignore"` everywhere — a model that invents one extra key must not
    void an otherwise good extraction.
  * every number carries `verbatim`, the exact span it was read from. A figure
    with no span in the source text is a hallucination we can catch cheaply,
    without a second LLM call.
  * a ticker is either RESOLVED against the ticker map or it is None. The org
    name is always kept, so "unresolved" degrades to "we know the company, not
    the symbol" rather than to a plausible-looking wrong symbol.
  * forecast prose runs through `contains_advice()`. The no-financial-advice
    rule is a constraint, and constraints that live only in a system prompt
    leak — this one is enforced in code.

Storage: these serialise with `.model_dump(mode="json")` into the JSON columns
of the fin_* tables, exactly like the existing pipeline stores dicts via db.j().
SCHEMA_VERSION is written on every row so rows produced by an older shape stay
readable after the schema moves. Nothing here touches the `stories`, `trends`
or `signals` tables the current app reads.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Optional

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field,
                      field_validator, model_validator)

# Bumped whenever a stored shape changes incompatibly. Readers switch on it
# rather than guessing from which keys happen to be present.
SCHEMA_VERSION = 1

_MODEL_CONFIG = ConfigDict(
    extra="ignore",            # tolerate invented keys, drop them
    str_strip_whitespace=True,
    use_enum_values=False,
    validate_assignment=True,
)


# --------------------------------------------------------------- vocabularies
class EventType(str, Enum):
    """The financial event schemas the pipeline is tuned for. OTHER is a real
    answer, not a failure — forcing a mislabel is worse than admitting one."""
    MERGER_ACQUISITION = "merger_acquisition"
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    REGULATORY_ACTION = "regulatory_action"
    LEADERSHIP_CHANGE = "leadership_change"
    SUPPLY_CHAIN = "supply_chain"
    FUNDING_ROUND = "funding_round"
    DEBT_RATINGS = "debt_ratings"
    LEGAL_ACTION = "legal_action"
    RESTRUCTURING = "restructuring"
    PRODUCT_LAUNCH = "product_launch"
    MACRO_POLICY = "macro_policy"
    OTHER = "other"


class EntityType(str, Enum):
    ORGANIZATION = "organization"
    PERSON = "person"
    TICKER = "ticker"
    GEOGRAPHY = "geography"
    SECTOR = "sector"
    REGULATOR = "regulator"
    INDEX = "index"
    INSTRUMENT = "instrument"
    CURRENCY = "currency"
    COMMODITY = "commodity"


class ActorRole(str, Enum):
    """SimTom personas. Sentiment is only meaningful per-actor: a fine is bad
    news to the executive and a win to the regulator, and collapsing those to
    one score is what makes single-axis sentiment useless on finance copy."""
    REGULATOR = "regulator"
    EXECUTIVE = "executive"
    BOARD = "board"
    SELL_SIDE_ANALYST = "sell_side_analyst"
    INSTITUTIONAL_INVESTOR = "institutional_investor"
    RETAIL_INVESTOR = "retail_investor"
    CREDITOR = "creditor"
    EMPLOYEE = "employee"
    CUSTOMER = "customer"
    COMPETITOR = "competitor"
    SUPPLIER = "supplier"
    GOVERNMENT = "government"


class Basis(str, Enum):
    """What kind of number this is. Mixing a guided figure with a reported one
    is the single most common way financial extraction goes quietly wrong."""
    REPORTED = "reported"
    GUIDANCE = "guidance"
    CONSENSUS = "consensus"
    ESTIMATE = "estimate"
    PRIOR_PERIOD = "prior_period"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNCLEAR = "unclear"


class Horizon(str, Enum):
    SHORT_TERM = "short_term"   # 1-30 days
    LONG_TERM = "long_term"     # 1-12 months


class ScenarioKind(str, Enum):
    BASE = "base"
    BULL = "bull"
    BEAR = "bear"


# ------------------------------------------------------------- advice guard
# Recommendation *constructions*, not finance vocabulary. Bare "buy" or "sell"
# would fire on "buyback", "sell-side", "the company sold its stake" — all of
# which are reporting, not advice. What is banned is telling a reader what to
# do with money, and naming a price a security is worth.
#
# The modal verb alone is not enough either: "the lender must hold more capital
# under the new rules" is a regulatory fact, and "the company must sell its
# stake" is an obligation being reported. What makes it advice is WHO is being
# told to act, so the subject has to be a reader or an investor.
ADVICE_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\b(?:investors?|traders?|shareholders?|readers?|buyers?|you|one)\s+"
    r"(?:should|must|ought\s+to|need\s+to|may\s+want\s+to|would\s+do\s+well\s+to)\s+"
    r"(?:consider\s+)?"
    r"(?:buy|sell|short|hold|invest|divest|exit|enter|accumulate|book|add|"
    r"reduce|avoid|switch)\b",
    # Subjectless imperative aimed at a security rather than at a company.
    r"\b(?:should|must)\s+(?:consider\s+)?"
    r"(?:buy|sell|short|accumulate|book\s+profits\s+in)\s+"
    r"(?:the\s+|these\s+|its\s+)?(?:stock|shares|scrip|dip|rally)\b",
    r"\b(?:we|i|our\s+\w+)\s+(?:recommend|advise|suggest\s+(?:buying|selling))\b",
    r"\brecommend(?:s|ed|ation)?\s+(?:to\s+)?(?:buy|sell|short|hold|avoid)\b",
    r"\b(?:strong\s+buy|strong\s+sell|table\s+pounding)\b",
    r"\b(?:price|target)\s+target\b",
    r"\bfair\s+value\s+of\s+(?:rs\.?|₹|\$|usd|inr)\s?[\d,]",
    r"\b(?:guaranteed|assured|risk[-\s]free)\s+(?:returns?|profits?|gains?)\b",
    r"\b(?:buy|sell)\s+(?:the\s+)?(?:dip|rally|stock|shares|scrip)\b",
    r"\bgood\s+time\s+to\s+(?:buy|sell|enter|exit)\b",
    r"\b(?:add|book)\s+(?:it\s+)?(?:to|in)\s+your\s+portfolio\b",
))


def contains_advice(text: str) -> Optional[str]:
    """Return the offending phrase, or None. Exposed (not just used as a
    validator) so an agent can repair-and-retry a single scenario instead of
    discarding a whole forecast that was fine apart from one sentence."""
    for pat in ADVICE_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(0)
    return None


def _reject_advice(v: str) -> str:
    hit = contains_advice(v)
    if hit:
        raise ValueError(f"reads as financial advice: {hit!r}")
    return v


# Prose that a reader could mistake for a recommendation.
Analysis = Annotated[str, AfterValidator(_reject_advice)]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Score = Annotated[float, Field(ge=-1.0, le=1.0)]


class _Base(BaseModel):
    model_config = _MODEL_CONFIG


# ------------------------------------------------------------------ evidence
class Evidence(_Base):
    """A pointer back to text that a human can check. Agent 2 and 3 chain these
    so a trend or a forecast can always be walked back to sentences."""
    article_id: str = Field(min_length=1)
    source: str = ""
    quote: str = Field(default="", max_length=600)
    url: str = ""


class Ticker(_Base):
    """`symbol` is populated ONLY from the ticker map — never from the model.
    The LLM proposes a company name; resolution is a deterministic lookup."""
    name: str = Field(min_length=1)           # as written in the article
    symbol: Optional[str] = None
    exchange: str = ""                        # NSE, BSE, NASDAQ, NYSE, ...
    resolved: bool = False
    resolver: str = ""                        # which map/rule resolved it

    @field_validator("symbol")
    @classmethod
    def _symbol_shape(cls, v):
        if v is None:
            return None
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-&]{0,11}", v):
            raise ValueError(f"implausible ticker symbol: {v!r}")
        return v

    @model_validator(mode="after")
    def _no_unresolved_symbols(self):
        # An unresolved ticker must carry no symbol at all. This is the rule
        # that keeps a confident-sounding wrong symbol out of the database.
        if self.symbol and not self.resolved:
            raise ValueError("symbol set on an unresolved ticker")
        if self.resolved and not self.symbol:
            raise ValueError("resolved ticker with no symbol")
        return self


# --------------------------------------------- Agent 1: tabular CoT entities
class EntityRow(_Base):
    """One row of the Tabular Chain-of-Thought table. The table shape is the
    point: the model fills cells it can defend and leaves the rest empty,
    instead of narrating entities into existence in free prose."""
    name: str = Field(min_length=1)
    type: EntityType
    ticker: Optional[Ticker] = None
    geography: str = ""
    role_in_story: str = ""      # "acquirer", "target", "regulator", ...
    verbatim: str = Field(default="", max_length=400)
    mentions: int = Field(default=1, ge=0)
    confidence: Confidence = 0.5

    @model_validator(mode="after")
    def _ticker_only_for_orgs(self):
        if self.ticker and self.type not in (EntityType.ORGANIZATION,
                                             EntityType.TICKER,
                                             EntityType.INDEX):
            raise ValueError(f"ticker attached to a {self.type.value}")
        return self


class EntityTable(_Base):
    rows: list[EntityRow] = Field(default_factory=list)
    # Names the model surfaced but the ticker map could not resolve. Kept
    # visible rather than dropped — it is the work queue for tickers.yaml.
    unresolved: list[str] = Field(default_factory=list)

    @field_validator("rows")
    @classmethod
    def _dedupe(cls, rows):
        seen, out = set(), []
        for r in rows:
            key = (r.name.casefold(), r.type)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def tickers(self) -> list[str]:
        return sorted({r.ticker.symbol for r in self.rows
                       if r.ticker and r.ticker.symbol})


class Metric(_Base):
    """A hard number. `verbatim` is required and must contain a digit: if the
    model cannot quote the span the figure came from, the figure does not get
    stored. Cheapest possible defence against invented financials."""
    name: str = Field(min_length=1)           # "revenue", "eps", "deal_value"
    value: Optional[float] = None
    unit: str = ""                            # "crore", "million", "percent"
    currency: str = ""                        # "INR", "USD"
    period: str = ""                          # "Q1 FY26", "CY2025"
    basis: Basis = Basis.UNKNOWN
    entity: str = ""                          # who the number belongs to
    yoy_change_pct: Optional[float] = None
    direction: Direction = Direction.UNCLEAR
    verbatim: str = Field(min_length=1, max_length=400)
    article_id: str = ""
    confidence: Confidence = 0.5

    @model_validator(mode="after")
    def _number_must_be_quoted(self):
        if self.value is not None and not re.search(r"\d", self.verbatim):
            raise ValueError(f"metric {self.name!r} has a value but no digit "
                             f"in its verbatim span")
        return self


# ------------------------------------------------ Agent 1: SimTom sentiment
class ActorView(_Base):
    """One simulated perspective. `incentive` is what makes this SimTom rather
    than sentiment-with-extra-steps: the model has to state why this actor
    would read the event this way before it scores it."""
    actor: str = Field(min_length=1)          # "RBI", "the CFO", "bondholders"
    role: ActorRole
    stated_position: str = ""                 # what they actually said, if any
    inferred_incentive: str = ""              # why they would see it this way
    sentiment: Score = 0.0
    confidence: Confidence = 0.5
    evidence: list[Evidence] = Field(default_factory=list)


class SentimentProfile(_Base):
    actors: list[ActorView] = Field(default_factory=list)
    rationale: str = ""

    @property
    def net(self) -> float:
        """Confidence-weighted mean across actors. A convenience for ranking —
        the per-actor detail is the product, this is just the sort key."""
        if not self.actors:
            return 0.0
        wsum = sum(a.confidence for a in self.actors) or float(len(self.actors))
        return sum(a.sentiment * a.confidence for a in self.actors) / wsum

    @property
    def dispersion(self) -> float:
        """How far apart the actors are. High dispersion is the interesting
        signal — it means the event is contested, not that it is neutral."""
        if len(self.actors) < 2:
            return 0.0
        vals = [a.sentiment for a in self.actors]
        mean = sum(vals) / len(vals)
        return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


# ------------------------------------------------------- Agent 1: KG triples
class KGRelationship(_Base):
    """A directed edge, stored in a namespace so finance edges never collide
    with anything the general pipeline writes."""
    subject: str = Field(min_length=1)
    subject_type: EntityType = EntityType.ORGANIZATION
    predicate: str = Field(min_length=1)      # "acquires", "fines", "supplies"
    object: str = Field(min_length=1)
    object_type: EntityType = EntityType.ORGANIZATION
    namespace: str = "finance"
    event_type: EventType = EventType.OTHER
    value: Optional[Metric] = None            # deal size, fine amount
    occurred_at: Optional[float] = None       # epoch seconds
    confidence: Confidence = 0.5
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("predicate")
    @classmethod
    def _normalise(cls, v):
        # snake_case so "acquires", "Acquires" and "acquires " are one edge.
        return re.sub(r"[^a-z0-9]+", "_", v.casefold()).strip("_")

    @model_validator(mode="after")
    def _no_self_loops(self):
        if self.subject.casefold() == self.object.casefold():
            raise ValueError(f"self-loop on {self.subject!r}")
        return self

    def key(self) -> tuple:
        """Identity for upsert and for path-walking in Agent 2."""
        return (self.namespace, self.subject.casefold(), self.predicate,
                self.object.casefold())


# -------------------------------------------------------- Agent 1: the story
class FinancialStory(_Base):
    id: str = ""
    headline: str = Field(min_length=1, max_length=300)
    narrative: str = Field(default="", max_length=8000)
    event_type: EventType = EventType.OTHER
    economic_drivers: list[str] = Field(default_factory=list)

    entities: EntityTable = Field(default_factory=EntityTable)
    metrics: list[Metric] = Field(default_factory=list)
    sentiment: SentimentProfile = Field(default_factory=SentimentProfile)
    relationships: list[KGRelationship] = Field(default_factory=list)

    # Provenance. article_ids is required: a story with no articles is not a
    # story, and the multi-source citation requirement is checked here rather
    # than trusted to the prompt.
    article_ids: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)

    credibility: float = Field(default=0.0, ge=0.0, le=100.0)
    created_at: Optional[float] = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_corroborated(self) -> bool:
        return len(set(self.sources)) > 1

    @property
    def tickers(self) -> list[str]:
        return self.entities.tickers()


# ------------------------------------------------------ Agent 2: trends
class ImpactLink(_Base):
    """One hop of a cascade. `order` is 1 for a direct effect, 2 for a
    second-order effect through a supplier/competitor/creditor, and so on —
    the spec's second-order mapping is this field, walked over the KG."""
    from_entity: str = Field(min_length=1)
    to_entity: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)      # why the effect transmits
    order: int = Field(default=1, ge=1, le=4)
    direction: Direction = Direction.UNCLEAR
    confidence: Confidence = 0.5
    via_relationship: Optional[KGRelationship] = None


class FinancialTrend(_Base):
    id: str = ""
    name: str = Field(min_length=1, max_length=200)
    narrative: Analysis = ""
    # The narrative arc as ordered steps: ["rising rates", "CRE defaults",
    # "regional bank stress"]. Ordered, so the arc reads as a chain.
    arc: list[str] = Field(default_factory=list)
    cascade: list[ImpactLink] = Field(default_factory=list)

    story_ids: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    macro_factors: list[str] = Field(default_factory=list)

    window_days: int = Field(default=7, ge=1, le=90)
    velocity: float = 0.0
    confidence: Confidence = 0.5
    evidence: list[Evidence] = Field(default_factory=list)
    created_at: Optional[float] = None
    schema_version: int = SCHEMA_VERSION

    @model_validator(mode="after")
    def _evidence_backed(self):
        # A trend with neither stories nor evidence is an assertion. The
        # general pipeline already learned this the hard way with trends whose
        # member stories didn't exist; don't repeat it here.
        if not self.story_ids and not self.evidence:
            raise ValueError("trend has no story_ids and no evidence")
        return self


# --------------------------------------------------- Agent 3: forecasting
class Invalidator(_Base):
    """What would prove this wrong. Required, and required to be observable —
    "sentiment worsens" is not a test, "CPI prints above 6% in the next
    release" is."""
    observable: str = Field(min_length=1)
    threshold: str = ""
    source: str = ""                          # where that data comes from
    by_date: str = ""
    invalidates: list[ScenarioKind] = Field(default_factory=list)


class Scenario(_Base):
    kind: ScenarioKind
    probability: Probability
    thesis: Analysis = Field(min_length=1)
    horizon: Horizon = Horizon.SHORT_TERM
    direction: Direction = Direction.UNCLEAR
    magnitude: str = ""                       # qualitative, never a price
    affected_sectors: list[str] = Field(default_factory=list)
    affected_tickers: list[str] = Field(default_factory=list)
    drivers: list[str] = Field(default_factory=list)
    precedent: str = ""                       # the historical analogue used
    confidence: Confidence = 0.5


DISCLAIMER = ("Directional scenario analysis derived from published reporting "
              "and historical precedent. Not investment advice.")


class ScenarioForecast(_Base):
    id: str = ""
    title: str = Field(min_length=1, max_length=300)
    trend_ids: list[str] = Field(default_factory=list)
    story_ids: list[str] = Field(default_factory=list)

    scenarios: list[Scenario] = Field(min_length=3, max_length=3)
    short_term: Analysis = ""                 # 1-30 days
    long_term: Analysis = ""                  # 1-12 months

    risks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    invalidation: list[Invalidator] = Field(min_length=1)

    confidence: Confidence = 0.5
    disclaimer: str = DISCLAIMER
    created_at: Optional[float] = None
    schema_version: int = SCHEMA_VERSION

    @model_validator(mode="after")
    def _three_distinct_cases(self):
        kinds = [s.kind for s in self.scenarios]
        if set(kinds) != set(ScenarioKind):
            raise ValueError(f"expected base/bull/bear, got {[k.value for k in kinds]}")
        total = sum(s.probability for s in self.scenarios)
        if not 0.9 <= total <= 1.1:
            raise ValueError(f"scenario probabilities sum to {total:.2f}")
        # Within tolerance, normalise rather than reject — models land on 0.95
        # or 1.05 constantly and that is not a reason to lose the forecast.
        if total and abs(total - 1.0) > 1e-9:
            for s in self.scenarios:
                s.probability = round(s.probability / total, 4)
        return self

    def scenario(self, kind: ScenarioKind) -> Scenario:
        return next(s for s in self.scenarios if s.kind == kind)
