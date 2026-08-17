"""Tests for the finance pipeline.

Stdlib unittest, no new dependency:

    cd backend && python -m unittest discover -s tests -v

The pipeline tests drive the three agents against a stub LLM that returns
realistic finance JSON, so what is being tested is this code's handling of a
model's answer — including the answers we WANT rejected (a figure that is not
in the source text, a forecast that reads as advice) — rather than the quality
of any particular model.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError                                # noqa: E402

from app import config, db, fulltext, llm                           # noqa: E402
from app.agents import Verifier                                     # noqa: E402
from app.finance import kg                                          # noqa: E402
from app.finance import tickers as tk                               # noqa: E402
from app.finance.agents import (FinancialForecastingAgent,          # noqa: E402
                                FinancialStoryAgent, FinancialTrendAgent)
from app.finance.orchestrator import unresolved_report              # noqa: E402
from app.schemas.finance import (Basis, EntityRow, EntityType,      # noqa: E402
                                 Evidence, EventType, FinancialStory,
                                 FinancialTrend, Invalidator, KGRelationship,
                                 Metric, Scenario, ScenarioForecast,
                                 ScenarioKind, SentimentProfile, ActorView,
                                 ActorRole, Ticker, contains_advice)


# --------------------------------------------------------------- schemas
class TickerRules(unittest.TestCase):
    def test_unresolved_carries_no_symbol(self):
        """The rule that keeps a confident wrong symbol out of the database."""
        with self.assertRaises(ValidationError):
            Ticker(name="Some Startup", symbol="SSTP")          # resolved=False
        with self.assertRaises(ValidationError):
            Ticker(name="X", resolved=True)                     # no symbol

    def test_implausible_symbol_rejected(self):
        with self.assertRaises(ValidationError):
            Ticker(name="X", symbol="not a ticker!", resolved=True)


class MetricRules(unittest.TestCase):
    def test_value_requires_a_quoted_digit(self):
        """A number with no digit in its verbatim span is not in the source."""
        with self.assertRaises(ValidationError):
            Metric(name="eps", value=12.0, verbatim="beat expectations")
        Metric(name="eps", value=12.0, verbatim="EPS of Rs 12 a share")

    def test_value_free_metric_needs_no_digit(self):
        Metric(name="target_date", verbatim="by the end of the year")


class ForecastRules(unittest.TestCase):
    def _scenarios(self, probs=(0.5, 0.3, 0.2)):
        return [Scenario(kind=k, probability=p, thesis=f"{k.value} case holds.")
                for k, p in zip(ScenarioKind, probs)]

    def test_requires_all_three_cases(self):
        with self.assertRaises(ValidationError):
            ScenarioForecast(title="t", scenarios=self._scenarios()[:2],
                             invalidation=[Invalidator(observable="x")])

    def test_probabilities_normalised_within_tolerance(self):
        f = ScenarioForecast(title="t", scenarios=self._scenarios((0.5, 0.3, 0.25)),
                             invalidation=[Invalidator(observable="x")])
        self.assertAlmostEqual(sum(s.probability for s in f.scenarios), 1.0, places=3)

    def test_probabilities_far_off_rejected(self):
        with self.assertRaises(ValidationError):
            ScenarioForecast(title="t", scenarios=self._scenarios((0.6, 0.6, 0.6)),
                             invalidation=[Invalidator(observable="x")])

    def test_invalidation_required(self):
        with self.assertRaises(ValidationError):
            ScenarioForecast(title="t", scenarios=self._scenarios(), invalidation=[])


class AdviceGuard(unittest.TestCase):
    ADVICE = [
        "Investors should buy the dip in regional banks.",
        "We recommend selling ahead of the print.",
        "Our price target for the stock is 2,400.",
        "This is a good time to enter the sector.",
        "The trade offers guaranteed returns over six months.",
    ]
    # Ordinary finance reporting that must NOT trip the guard. Every one of
    # these contains a word a naive keyword filter would fire on.
    REPORTING = [
        "The board approved a Rs 10,000 crore share buyback.",
        "Sell-side analysts cut their estimates after the print.",
        "The company sold its stake in the logistics arm.",
        "Bondholders may exit at the next reset window.",
        "Retail investors bought heavily during the rally.",
        "The lender must hold more capital under the new rules.",
        "Management said it would consider divesting the unit.",
    ]

    def test_advice_detected(self):
        for text in self.ADVICE:
            self.assertIsNotNone(contains_advice(text), text)

    def test_reporting_passes(self):
        for text in self.REPORTING:
            self.assertIsNone(contains_advice(text), text)

    def test_enforced_on_forecast_prose(self):
        with self.assertRaises(ValidationError):
            Scenario(kind=ScenarioKind.BULL, probability=0.3,
                     thesis="Investors should buy the dip.")


class SentimentMath(unittest.TestCase):
    def test_dispersion_flags_disagreement(self):
        """The signal SimTom exists to produce: a regulator and an executive
        reading the same event in opposite directions."""
        p = SentimentProfile(actors=[
            ActorView(actor="SEBI", role=ActorRole.REGULATOR, sentiment=0.6,
                      confidence=0.9),
            ActorView(actor="CFO", role=ActorRole.EXECUTIVE, sentiment=-0.7,
                      confidence=0.8)])
        self.assertGreater(p.dispersion, 0.5)
        self.assertLess(abs(p.net), 0.2)      # nets out; the split is the story
        self.assertEqual(SentimentProfile().dispersion, 0.0)


class EntityTableRules(unittest.TestCase):
    def test_ticker_cannot_attach_to_a_person(self):
        t = Ticker(name="Infosys", symbol="INFY", resolved=True, resolver="test")
        with self.assertRaises(ValidationError):
            EntityRow(name="Someone", type=EntityType.PERSON, ticker=t)

    def test_self_loop_rejected(self):
        with self.assertRaises(ValidationError):
            KGRelationship(subject="Acme", predicate="acquires", object="acme")

    def test_predicate_normalised(self):
        r = KGRelationship(subject="A", predicate="  Acquires ", object="B")
        self.assertEqual(r.predicate, "acquires")


# -------------------------------------------------------------- resolver
class TickerResolution(unittest.TestCase):
    def test_legal_suffixes_stripped(self):
        for name in ("Reliance Industries", "Reliance Industries Ltd",
                     "Reliance Industries Limited", "RELIANCE INDUSTRIES LTD."):
            self.assertEqual(tk.resolve(name).symbol, "RELIANCE", name)

    def test_aliases(self):
        self.assertEqual(tk.resolve("RIL").symbol, "RELIANCE")
        self.assertEqual(tk.resolve("HUL").symbol, "HINDUNILVR")
        self.assertEqual(tk.resolve("Larsen & Toubro").symbol, "LT")

    def test_unknown_name_stays_unresolved(self):
        t = tk.resolve("Some Startup Nobody Has Heard Of")
        self.assertFalse(t.resolved)
        self.assertIsNone(t.symbol)
        self.assertEqual(t.name, "Some Startup Nobody Has Heard Of")

    def test_regulators_typed_not_tickered(self):
        self.assertEqual(tk.type_of("Reserve Bank of India"), EntityType.REGULATOR)
        self.assertEqual(tk.type_of("SEBI"), EntityType.REGULATOR)
        self.assertFalse(tk.resolve("Reserve Bank of India").resolved)

    def test_indices_typed_as_indices(self):
        self.assertEqual(tk.type_of("Nifty 50"), EntityType.INDEX)

    def test_model_proposal_loses_to_the_map(self):
        """A model calling HDFC Bank a 'person' must not make it one."""
        self.assertEqual(tk.type_of("HDFC Bank", "person"), EntityType.ORGANIZATION)

    def test_canonical_collapses_spellings(self):
        self.assertEqual(tk.canonical("RIL"), tk.canonical("Reliance Industries Ltd"))

    def test_map_has_no_ambiguity(self):
        """A name two entries claim is dropped rather than guessed. If this ever
        fails, tickers.yaml has picked up a genuine collision."""
        self.assertEqual(tk.stats()["ambiguous_dropped"], [])


# ------------------------------------------------------------------- db
class _DBCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_path, self._old_ready = config.DB_PATH, db._schema_ready
        config.DB_PATH, db._schema_ready = self._tmp.name, False
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        config.DB_PATH, db._schema_ready = self._old_path, self._old_ready
        os.unlink(self._tmp.name)


class KnowledgeGraph(_DBCase):
    def _edge(self, s, p, o, conf=0.8):
        kg.upsert_edge(self.con, KGRelationship(
            subject=s, predicate=p, object=o, confidence=conf,
            evidence=[Evidence(article_id="a1", quote=f"{s} {p} {o}")]), "story1")

    def test_edge_merges_instead_of_duplicating(self):
        self._edge("Acme Corp", "supplies", "Beta Inc", 0.5)
        self._edge("Acme Corp", "supplies", "Beta Inc", 0.9)
        rows = self.con.execute("SELECT * FROM fin_kg_edges").fetchall()
        self.assertEqual(len(rows), 1)
        # MAX, not latest: a second report must not lower confidence.
        self.assertAlmostEqual(rows[0]["confidence"], 0.9)

    def test_spellings_converge_on_one_node(self):
        self._edge("RIL", "supplies", "Beta Inc")
        self._edge("Reliance Industries Ltd", "supplies", "Gamma Inc")
        ids = [r["id"] for r in self.con.execute(
            "SELECT id FROM fin_kg_nodes WHERE id='RELIANCE'").fetchall()]
        self.assertEqual(ids, ["RELIANCE"])

    def test_cascade_orders_by_hop(self):
        self._edge("Acme Corp", "supplies", "Beta Inc")
        self._edge("Beta Inc", "supplies", "Gamma Inc")
        self._edge("Gamma Inc", "supplies", "Delta Inc")
        links = kg.cascade(self.con, ["Acme Corp"], max_hops=3)
        by_pair = {(l.from_entity, l.to_entity): l for l in links}
        self.assertEqual(by_pair[("Acme Corp", "Beta Inc")].order, 1)
        self.assertEqual(by_pair[("Beta Inc", "Gamma Inc")].order, 2)
        self.assertIn(("Gamma Inc", "Delta Inc"), by_pair)

    def test_cascade_respects_hop_limit(self):
        self._edge("Acme Corp", "supplies", "Beta Inc")
        self._edge("Beta Inc", "supplies", "Gamma Inc")
        links = kg.cascade(self.con, ["Acme Corp"], max_hops=1)
        self.assertEqual({(l.from_entity, l.to_entity) for l in links},
                         {("Acme Corp", "Beta Inc")})

    def test_confidence_decays_with_distance(self):
        self._edge("Acme Corp", "supplies", "Beta Inc", 0.8)
        self._edge("Beta Inc", "supplies", "Gamma Inc", 0.8)
        links = {(l.from_entity, l.to_entity): l.confidence
                 for l in kg.cascade(self.con, ["Acme Corp"], max_hops=2)}
        self.assertGreater(links[("Acme Corp", "Beta Inc")],
                           links[("Beta Inc", "Gamma Inc")])

    def test_hub_is_not_used_as_a_bridge(self):
        """A regulator touching two companies must not make them connected."""
        for i in range(kg.HUB_DEGREE + 5):
            self._edge("Reserve Bank of India", "regulates", f"Bank {i} Corp")
        links = kg.cascade(self.con, ["Acme Corp"], max_hops=3)
        self.assertEqual(links, [])                      # seed is isolated
        out = kg.cascade(self.con, ["RBI"], max_hops=3)
        self.assertTrue(all(l.order == 1 for l in out))  # never expanded through

    def test_walk_is_bounded_on_a_dense_graph(self):
        for i in range(60):
            self._edge(f"Node {i} Corp", "supplies", f"Node {i+1} Corp")
        links = kg.cascade(self.con, ["Node 0 Corp"], max_hops=4, max_links=10)
        self.assertLessEqual(len(links), 10)

    def test_prune_drops_old_edges(self):
        self._edge("Acme Corp", "supplies", "Beta Inc")
        self.con.execute("UPDATE fin_kg_edges SET updated_at = ?",
                         (db.now() - 999 * 86400,))
        self.con.execute("UPDATE fin_kg_nodes SET last_seen = ?",
                         (db.now() - 999 * 86400,))
        edges, nodes = kg.prune(self.con)
        self.assertEqual(edges, 1)
        self.assertEqual(nodes, 2)


# ------------------------------------------------------------- pipeline
# One realistic answer per finance task. The extract deliberately contains a
# figure that is NOT in the article text, and the forecast deliberately opens
# with advice — both are the interesting cases.
STUB = {
    "claims": {"claims": ["RBI fined HDFC Bank Rs 75 crore.",
                          "The bank said it will comply."]},
    "verify": {"verdicts": [{"claim": "RBI fined HDFC Bank Rs 75 crore.",
                             "verdict": "corroborated", "note": "two outlets"}],
               "score": 74, "note": "2 independent sources"},
    "fin_extract": {
        "event_type": "regulatory_action",
        "entities": [
            {"name": "Reserve Bank of India", "type": "organization",
             "role": "regulator", "verbatim": "The Reserve Bank of India"},
            {"name": "HDFC Bank", "type": "organization", "role": "target",
             "verbatim": "on HDFC Bank"},
            {"name": "banking", "type": "sector", "role": "none",
             "verbatim": "lending"},
            {"name": "India", "type": "geography", "role": "none",
             "verbatim": "India"}],
        "metrics": [
            {"name": "fine_amount", "value": 75, "unit": "crore",
             "currency": "INR", "period": "", "basis": "reported",
             "entity": "HDFC Bank", "yoy_change": None,
             "verbatim": "a penalty of Rs 75 crore"},
            # Not in the source text — must be dropped by the verbatim rule.
            {"name": "revenue", "value": 41000, "unit": "crore",
             "currency": "INR", "period": "FY25", "basis": "reported",
             "entity": "HDFC Bank", "yoy_change": 9.1,
             "verbatim": "the bank is large"}],
        "relationships": [
            {"subject": "Reserve Bank of India", "predicate": "fines",
             "object": "HDFC Bank", "event_type": "regulatory_action",
             "verbatim": "imposed a penalty of Rs 75 crore on HDFC Bank"},
            # One end was never extracted — must be dropped.
            {"subject": "HDFC Bank", "predicate": "acquires",
             "object": "Ghost Corp", "event_type": "merger_acquisition",
             "verbatim": ""}]},
    "fin_sentiment": {
        "actors": [
            {"actor": "Reserve Bank of India", "role": "regulator",
             "knows": "its inspection found loan classification lapses",
             "wants": "compliance across the sector", "position": "",
             "sentiment": 0.5, "confidence": 0.8},
            {"actor": "HDFC Bank", "role": "executive",
             "knows": "the penalty is Rs 75 crore",
             "wants": "to close the matter quietly", "position": "it will comply",
             "sentiment": -0.6, "confidence": 0.8}],
        "rationale": "The regulator and the bank read the same fine oppositely."},
    "fin_story": {
        "headline": "RBI fines HDFC Bank Rs 75 crore over lending lapses",
        "what_happened": "The Reserve Bank of India has penalised HDFC Bank "
                         "Rs 75 crore after an inspection found lapses in how "
                         "loans were classified. The bank said it will comply.",
        "why_it_matters": "The fine is small against the bank's size, but the "
                          "finding points at process rather than at one loan.",
        "economic_drivers": ["regulatory tightening", "credit quality"],
        "beats": [
            {"label": "What the regulator found",
             "text": "The Reserve Bank of India has penalised HDFC Bank Rs 75 "
                     "crore after an inspection found lapses in how loans were "
                     "classified across several branches."},
            {"label": "What the bank says",
             "text": "HDFC Bank said it will comply with the order and has "
                     "already begun reviewing the affected accounts."}],
        "anchors": [{"claim": 0,
                     "quote": "The Reserve Bank of India has penalised HDFC Bank "
                              "Rs 75 crore after an inspection found lapses in how "
                              "loans were classified across several branches."}]},
    "fin_trend": None,      # filled per-test, needs real story ids
    "fin_forecast": None,
}


def _forecast_payload(advice=False):
    thesis = ("Investors should buy the dip in mid-sized lenders." if advice
              else "Supervisory attention widens to peers with similar loan "
                   "classification processes, and provisioning rises modestly.")
    return {
        "title": "Supervisory attention may widen across mid-sized lenders",
        "scenarios": [
            {"kind": "base", "probability": 0.55, "thesis": thesis,
             "horizon": "short_term", "direction": "down", "magnitude": "modest",
             "drivers": ["further inspections"], "precedent": "",
             "affected_sectors": ["banking"], "affected_tickers": ["HDFCBANK"],
             "confidence": 0.6},
            {"kind": "bull", "probability": 0.2,
             "thesis": "The finding stays confined to one bank and the sector "
                       "sees no further action.",
             "horizon": "long_term", "direction": "up", "magnitude": "modest",
             "drivers": ["no further orders"], "precedent": "",
             "affected_sectors": ["banking"], "affected_tickers": [],
             "confidence": 0.4},
            {"kind": "bear", "probability": 0.25,
             "thesis": "Inspections broaden and several lenders restate loan "
                       "classifications, raising provisions across the sector.",
             "horizon": "long_term", "direction": "down", "magnitude": "material",
             "drivers": ["sector-wide review"], "precedent": "",
             "affected_sectors": ["banking"], "affected_tickers": [],
             "confidence": 0.4}],
        "short_term": "Little visible effect beyond the named bank in the first weeks.",
        "long_term": "Whether this widens depends on the next inspection cycle.",
        "risks": ["The finding may be specific to one process."],
        "dependencies": ["The regulator publishing further orders."],
        "invalidation": [{"observable": "RBI enforcement orders published in the "
                                        "next quarterly list",
                          "threshold": "no further lender named",
                          "source": "rbi.org.in press releases",
                          "invalidates": ["bear"]}],
        "confidence": 0.5}


class _StubLLM:
    """Records every task called and answers from STUB."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def __call__(self, task, prompt_text, retries=1, want="object"):
        self.calls.append((task, prompt_text))
        if task in self.overrides:
            value = self.overrides[task]
            return value(prompt_text) if callable(value) else value
        return STUB.get(task)


class PipelineCase(_DBCase):
    def setUp(self):
        super().setUp()
        self._real_llm, self._real_fetch = llm.complete_json, fulltext.fetch_for_articles
        # No network in tests, and no full-text fetch: build_brief works from
        # the RSS summaries alone.
        fulltext.fetch_for_articles = lambda arts, limit=None: {}
        self._seed_articles()

    def tearDown(self):
        llm.complete_json, fulltext.fetch_for_articles = self._real_llm, self._real_fetch
        super().tearDown()

    def _seed_articles(self, groups=2):
        now = db.now()
        for g in range(groups):
            for i, src in enumerate(("rbi.org.in", "livemint.com")):
                aid = f"g{g}art{i}".ljust(12, "0")
                self.con.execute(
                    "INSERT INTO articles (id,url,title,summary,source,topic,"
                    "published,entities,fetched_at,group_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, f"https://{src}/a{g}{i}",
                     "RBI fines HDFC Bank Rs 75 crore over lending lapses",
                     "The Reserve Bank of India imposed a penalty of Rs 75 crore "
                     "on HDFC Bank after an inspection found lapses in loan "
                     "classification. The bank said it will comply.",
                     src, "finance", now - 3600, "", now - 3600, f"grp{g}"))
        self.con.commit()

    def _run_stories(self, overrides=None):
        stub = _StubLLM(overrides)
        llm.complete_json = stub
        n = FinancialStoryAgent().run(self.con, Verifier())
        return n, stub


class Agent1Extraction(PipelineCase):
    def test_builds_stories_and_graph(self):
        n, stub = self._run_stories()
        self.assertEqual(n, 2)
        tasks = [t for t, _ in stub.calls]
        # The five calls per event, and no more.
        self.assertEqual(sorted(set(tasks)),
                         ["claims", "fin_extract", "fin_sentiment", "fin_story", "verify"])
        self.assertEqual(len(tasks), 10)                 # 5 per event, 2 events

        row = self.con.execute("SELECT * FROM fin_stories LIMIT 1").fetchone()
        self.assertEqual(row["event_type"], "regulatory_action")
        self.assertEqual(db.uj(row["tickers"], []), ["HDFCBANK"])
        self.assertIn("banking", db.uj(row["sectors"], []))
        self.assertIn("India", db.uj(row["geographies"], []))

    def test_unquoted_figure_is_dropped(self):
        """The revenue figure in the stub is not in the article text."""
        self._run_stories()
        metrics = db.uj(self.con.execute(
            "SELECT metrics FROM fin_stories LIMIT 1").fetchone()["metrics"], [])
        names = {m["name"] for m in metrics}
        self.assertIn("fine_amount", names)
        self.assertNotIn("revenue", names)

    def test_regulator_typed_and_not_given_a_ticker(self):
        self._run_stories()
        node = self.con.execute(
            "SELECT * FROM fin_kg_nodes WHERE id='RBI'").fetchone()
        self.assertIsNotNone(node)
        self.assertEqual(node["type"], "regulator")
        self.assertIsNone(node["ticker"])

    def test_relationship_with_an_unextracted_end_is_dropped(self):
        self._run_stories()
        preds = [r["predicate"] for r in
                 self.con.execute("SELECT predicate FROM fin_kg_edges").fetchall()]
        self.assertEqual(preds, ["fines"])           # not "acquires Ghost Corp"

    def test_simtom_disagreement_is_recorded(self):
        self._run_stories()
        row = self.con.execute(
            "SELECT sentiment, sentiment_dispersion FROM fin_stories LIMIT 1").fetchone()
        actors = db.uj(row["sentiment"], {}).get("actors") or []
        self.assertEqual(len(actors), 2)
        self.assertGreater(row["sentiment_dispersion"], 0.4)
        # The perspective-taking stage is kept, not just the score.
        self.assertIn("compliance", actors[0]["inferred_incentive"])

    def test_rerun_without_new_articles_makes_no_llm_calls(self):
        self._run_stories()
        _, stub = self._run_stories()
        self.assertEqual(stub.calls, [])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) c FROM fin_stories").fetchone()["c"], 2)

    def test_new_article_updates_in_place(self):
        self._run_stories()
        before = self.con.execute("SELECT id FROM fin_stories").fetchall()
        self.con.execute(
            "INSERT INTO articles (id,url,title,summary,source,topic,published,"
            "entities,fetched_at,group_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("g0art2xxxxx", "https://x/3", "RBI fines HDFC Bank",
             "A third outlet carried the Rs 75 crore penalty.", "thehindu.com",
             "finance", db.now(), "", db.now(), "grp0"))
        self.con.commit()
        self._run_stories()
        after = self.con.execute("SELECT id FROM fin_stories").fetchall()
        self.assertEqual({r["id"] for r in before}, {r["id"] for r in after})

    def test_failed_extract_skips_the_story(self):
        n, _ = self._run_stories({"fin_extract": None})
        self.assertEqual(n, 0)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) c FROM fin_stories").fetchone()["c"], 0)

    def test_failed_sentiment_still_produces_a_story(self):
        """SimTom is enriching, not essential — losing it must not lose the story."""
        n, _ = self._run_stories({"fin_sentiment": None})
        self.assertEqual(n, 2)
        row = self.con.execute("SELECT sentiment FROM fin_stories LIMIT 1").fetchone()
        self.assertEqual(db.uj(row["sentiment"], {}).get("actors"), [])

    def test_unresolved_names_are_reported(self):
        self._run_stories({"fin_extract": dict(
            STUB["fin_extract"],
            entities=STUB["fin_extract"]["entities"] +
            [{"name": "Ghost Holdings", "type": "organization", "role": "none",
              "verbatim": "Ghost Holdings"}])})
        names = [n for n, _ in unresolved_report(self.con)]
        self.assertIn("Ghost Holdings", names)


class Agent2Linking(PipelineCase):
    def _seed_stories(self, count=4):
        """Finance stories written directly, so the trend agent is tested on its
        own rather than through Agent 1."""
        ids = []
        for i in range(count):
            sid = db.new_id()
            ids.append(sid)
            self.con.execute(
                "INSERT INTO fin_stories (id,event_id,headline,narrative,event_type,"
                "topic,credibility,article_ids,sources,sectors,tickers,metrics,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, f"e{i}", f"Lender {i} raises provisions", "narrative",
                 "regulatory_action", "finance", 70.0, db.j(["a"]),
                 db.j(["livemint.com"]), db.j(["banking"]), db.j(["HDFCBANK"]),
                 db.j([]), db.now(), db.now()))
        self.con.commit()
        return ids

    def test_arc_needs_two_stories(self):
        ids = self._seed_stories()
        llm.complete_json = _StubLLM({"fin_trend": {"trends": [
            {"name": "Thin arc", "narrative": "One story only.", "arc": ["a", "b"],
             "confidence": 0.6, "story_ids": [ids[0]]}]}})
        self.assertEqual(FinancialTrendAgent().run(self.con), 0)

    def test_builds_and_reconciles_arcs(self):
        ids = self._seed_stories()
        payload = {"trends": [
            {"name": "Provisioning pressure spreads", "arc": ["inspections widen",
             "provisions rise", "lending slows"],
             "narrative": "Supervisors are finding the same process problem at "
                          "several lenders.",
             "sectors": ["banking"], "tickers": ["HDFCBANK"],
             "macro_factors": ["credit quality"], "second_order": [],
             "confidence": 0.6, "story_ids": ids[:3]}]}
        llm.complete_json = _StubLLM({"fin_trend": payload})
        self.assertEqual(FinancialTrendAgent().run(self.con), 1)
        # A second run over the same arc updates in place, keeping the id, so a
        # forecast pointing at it does not dangle.
        first = self.con.execute("SELECT id FROM fin_trends").fetchone()["id"]
        FinancialTrendAgent().run(self.con)
        rows = self.con.execute("SELECT id FROM fin_trends").fetchall()
        self.assertEqual([r["id"] for r in rows], [first])

    def test_arc_reading_as_advice_is_rejected(self):
        ids = self._seed_stories()
        llm.complete_json = _StubLLM({"fin_trend": {"trends": [
            {"name": "Buy the lenders", "arc": ["a", "b"],
             "narrative": "Investors should buy the dip in mid-sized lenders.",
             "confidence": 0.6, "story_ids": ids[:2]}]}})
        self.assertEqual(FinancialTrendAgent().run(self.con), 0)

    def test_cascade_uses_the_graph(self):
        ids = self._seed_stories()
        kg.upsert_edge(self.con, KGRelationship(
            subject="HDFC Bank", predicate="supplies", object="Beta Inc",
            confidence=0.8), ids[0])
        self.con.commit()
        payload = {"trends": [
            {"name": "Provisioning pressure spreads", "arc": ["a", "b"],
             "narrative": "Supervisors are finding the same problem at lenders.",
             "sectors": [], "tickers": ["HDFCBANK"], "macro_factors": [],
             "second_order": [], "confidence": 0.6, "story_ids": ids[:2]}]}
        stub = _StubLLM({"fin_trend": payload})
        llm.complete_json = stub
        FinancialTrendAgent().run(self.con)
        # The walk's links reached the prompt...
        self.assertIn("Beta Inc", stub.calls[0][1])
        # ...and were stored on the arc, with a real relationship behind them.
        cascade = db.uj(self.con.execute(
            "SELECT cascade FROM fin_trends").fetchone()["cascade"], [])
        self.assertTrue(any(c["to_entity"] == "Beta Inc" for c in cascade))


class Agent3Forecasting(PipelineCase):
    def _seed_trend(self):
        sid, tid = db.new_id(), db.new_id()
        self.con.execute(
            "INSERT INTO fin_stories (id,event_id,headline,narrative,event_type,"
            "topic,credibility,article_ids,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "e0", "RBI fines HDFC Bank", "narrative", "regulatory_action",
             "finance", 70.0, db.j(["a"]), db.now(), db.now()))
        self.con.execute(
            "INSERT INTO fin_trends (id,name,narrative,arc,cascade,story_ids,"
            "sectors,tickers,macro_factors,window_days,velocity,confidence,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "Provisioning pressure spreads", "narrative",
             db.j(["inspections widen", "provisions rise"]), db.j([]), db.j([sid]),
             db.j(["banking"]), db.j(["HDFCBANK"]), db.j(["credit quality"]),
             30, 2.0, 0.6, db.now(), db.now()))
        self.con.commit()
        return tid

    def test_produces_three_cases_with_invalidation(self):
        self._seed_trend()
        llm.complete_json = _StubLLM({"fin_forecast": _forecast_payload()})
        self.assertEqual(FinancialForecastingAgent().run(self.con), 1)
        row = self.con.execute("SELECT * FROM fin_forecasts").fetchone()
        kinds = {s["kind"] for s in db.uj(row["scenarios"], [])}
        self.assertEqual(kinds, {"base", "bull", "bear"})
        self.assertTrue(db.uj(row["invalidation"], []))
        self.assertIn("Not investment advice", row["disclaimer"])

    def test_advice_triggers_one_repair_call(self):
        self._seed_trend()
        answers = [_forecast_payload(advice=True), _forecast_payload(advice=False)]
        stub = _StubLLM({"fin_forecast": lambda _p: answers.pop(0)})
        llm.complete_json = stub
        self.assertEqual(FinancialForecastingAgent().run(self.con), 1)
        calls = [t for t, _ in stub.calls]
        self.assertEqual(calls, ["fin_forecast", "fin_forecast"])
        # The repair prompt names the exact phrase that failed.
        self.assertIn("should buy", stub.calls[1][1])
        thesis = db.uj(self.con.execute(
            "SELECT scenarios FROM fin_forecasts").fetchone()["scenarios"], [])[0]["thesis"]
        self.assertIsNone(contains_advice(thesis))

    def test_advice_twice_stores_nothing(self):
        self._seed_trend()
        llm.complete_json = _StubLLM({"fin_forecast": _forecast_payload(advice=True)})
        self.assertEqual(FinancialForecastingAgent().run(self.con), 0)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) c FROM fin_forecasts").fetchone()["c"], 0)


class Isolation(PipelineCase):
    def test_general_tables_are_untouched(self):
        """The standing constraint: existing stories must not break. The finance
        pipeline writes only fin_* tables."""
        self.con.execute(
            "INSERT INTO stories (id,headline,narrative,credibility,topic,"
            "article_ids,trend_ids,connection_ids,created_at) "
            "VALUES ('s1','existing','n',50.0,'finance','[]','[]','[]',?)",
            (db.now(),))
        self.con.commit()
        before = {t: self.con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("stories", "trends", "signals", "feed_items")}
        self._run_stories()
        after = {t: self.con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                 for t in ("stories", "trends", "signals", "feed_items")}
        self.assertEqual(before, after)
        self.assertEqual(self.con.execute(
            "SELECT headline FROM stories WHERE id='s1'").fetchone()["headline"],
            "existing")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StageIsolation(PipelineCase):
    """Production ran for days with `fin_stories error: 'list' object has no
    attribute 'get'` and produced nothing at all.

    Two separate defects stacked. The router handed a bare JSON array to a
    caller doing `out.get(...)` (fixed in llm._shaped), and the stage had no
    per-event try/except — so that one AttributeError aborted `run()` entirely
    and every remaining event in the batch was lost. The second is the one that
    turns any future bad answer into a total outage, so it is pinned here.
    """

    def _seed_articles(self, groups=3):
        super()._seed_articles(groups=groups)

    def test_one_bad_event_does_not_take_down_the_stage(self):
        seen = {"n": 0}

        def explode_once(prompt_text):
            """Fail the first extraction the way production did — a list where
            an object was expected — and answer normally after that."""
            seen["n"] += 1
            if seen["n"] == 1:
                return ["this", "is", "not", "an", "object"]
            return STUB.get("fin_extract")

        n, _ = self._run_stories({"fin_extract": explode_once})
        self.assertEqual(n, 2, "the two healthy events must still be written")
        rows = self.con.execute("SELECT COUNT(*) c FROM fin_stories").fetchone()["c"]
        self.assertEqual(rows, 2)

    def test_the_failure_is_reported_rather_than_swallowed(self):
        n, _ = self._run_stories(
            {"fin_extract": lambda _p: ["not", "an", "object"]})
        self.assertEqual(n, 0)
        r = self.con.execute(
            "SELECT status, detail FROM runs WHERE stage='fin_stories' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(r["status"], "error",
                         "a stage that errored on every event must not report ok")
        self.assertIn("errored", r["detail"])

    def test_the_graph_still_grows_from_the_events_that_worked(self):
        """The downstream cost of the outage: no edges, so fin_causal had
        nothing to walk and the Predictions page stayed on curated chains."""
        seen = {"n": 0}

        def explode_once(prompt_text):
            seen["n"] += 1
            return (["bad"] if seen["n"] == 1 else STUB.get("fin_extract"))

        self._run_stories({"fin_extract": explode_once})
        edges = self.con.execute(
            "SELECT COUNT(*) c FROM fin_kg_edges").fetchone()["c"]
        self.assertGreater(edges, 0)
