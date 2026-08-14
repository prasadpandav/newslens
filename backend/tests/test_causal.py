"""Cause-and-effect chains: the Predictions page.

The bug these cover: until Aug 2026 the page served three hand-written dicts
from a module-level constant. Nothing regenerated them because nothing could —
no table, no pipeline stage — so "the data never updates" was not a staleness
problem, it was the absence of a generator. `causal.build()` is that generator,
and what matters about it is that a chain stays GROUNDED: every step a real
graph node, every link a relationship the reporting established, and the
evidence read off the edges actually walked.

    cd backend && python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("LLM_PROVIDER", "mock")

from app import config, db, llm                                     # noqa: E402
from app.finance import causal                                      # noqa: E402


class _Graph(unittest.TestCase):
    """A small, deliberately directional graph:

        RBI --regulates--> HDFCBANK --lends_to--> TATAMOTORS --sources_from--> EXIDEIND

    Causality runs left to right. Any walk that reports it otherwise is wrong.
    """

    def setUp(self):
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "causal.db")
        os.environ["DB_PATH"] = config.DB_PATH
        # Schema creation is once-per-process, so a fresh file in the same
        # process comes back empty unless the flag is cleared first.
        db._schema_ready = False
        self.con = db.connect()
        self.now = db.now()
        self._seed()

    def tearDown(self):
        self.con.close()

    def node(self, nid, name, ticker=None, typ="organization"):
        self.con.execute(
            "INSERT INTO fin_kg_nodes (namespace,id,name,type,ticker,exchange,"
            "mentions,first_seen,last_seen) VALUES ('finance',?,?,?,?,'NSE',5,?,?)",
            (nid, name, typ, ticker, self.now, self.now))

    def edge(self, s, p, o, conf, story_ids):
        self.con.execute(
            "INSERT INTO fin_kg_edges (id,namespace,subject,predicate,object,"
            "subject_type,object_type,event_type,confidence,evidence,story_ids,"
            "created_at,updated_at) VALUES (?,'finance',?,?,?,'organization',"
            "'organization','regulation',?,'[]',?,?,?)",
            (db.new_id(), s, p, o, conf, json.dumps(story_ids), self.now, self.now))

    def story(self, sid, headline, sectors):
        self.con.execute(
            "INSERT INTO fin_stories (id,headline,sectors,tickers,metrics,"
            "narrative,updated_at,created_at) VALUES (?,?,?,'[]','[]','n',?,?)",
            (sid, headline, json.dumps(sectors), self.now, self.now))

    def _seed(self):
        self.node("RBI", "Reserve Bank of India", typ="government")
        self.node("HDFCBANK", "HDFC Bank", "HDFCBANK")
        self.node("TATAMOTORS", "Tata Motors", "TATAMOTORS")
        self.node("EXIDEIND", "Exide Industries", "EXIDEIND")
        self.story("s1", "RBI tightens lending norms", ["banking"])
        self.story("s2", "HDFC Bank raises auto loan rates", ["banking", "auto"])
        self.story("s3", "Tata Motors flags weaker demand", ["auto"])
        self.edge("RBI", "regulates", "HDFCBANK", 0.9, ["s1"])
        self.edge("HDFCBANK", "lends_to", "TATAMOTORS", 0.8, ["s2"])
        self.edge("TATAMOTORS", "sources_from", "EXIDEIND", 0.7, ["s3"])
        self.con.commit()


class BuildTest(_Graph):
    def test_a_chain_is_built_from_the_graph(self):
        self.assertEqual(causal.build(self.con), 1)
        chains = causal.list_causal_chains(self.con)
        self.assertEqual(len(chains), 1)
        self.assertFalse(chains[0]["curated"],
                         "a generated chain must not be flagged as seed content")

    def test_the_chain_runs_in_the_direction_of_causation(self):
        """The regression that made the first build unusable: kg.cascade walks
        edges BOTH ways (correct for exposure), so an undirected walk produced
        'Tata Motors -> HDFC Bank -> RBI' — the actual causality reversed, and
        about to be published as a prediction."""
        causal.build(self.con)
        chain = causal.list_causal_chains(self.con)[0]
        entities = [s["entity"] for s in chain["steps"]]
        self.assertEqual(entities[0], "Reserve Bank of India",
                         "the catalyst must be the entity that ACTS")
        self.assertEqual(entities[-1], "Exide Industries")
        self.assertEqual(chain["signature"],
                         "RBI>HDFCBANK>TATAMOTORS>EXIDEIND")

    def test_steps_carry_the_real_predicate_and_are_ordered(self):
        causal.build(self.con)
        steps = causal.list_causal_chains(self.con)[0]["steps"]
        self.assertEqual([s["step_order"] for s in steps], [1, 2, 3, 4])
        self.assertEqual(steps[0]["channel"], "catalyst")
        self.assertEqual([s["channel"] for s in steps[1:]],
                         ["regulates", "lends to", "sources from"])

    def test_elasticity_is_the_confidence_of_the_link_that_reached_the_step(self):
        causal.build(self.con)
        steps = causal.list_causal_chains(self.con)[0]["steps"]
        self.assertEqual(steps[0]["elasticity_score"], 1.0)   # catalyst acts alone
        self.assertAlmostEqual(steps[1]["elasticity_score"], 0.9)
        self.assertAlmostEqual(steps[2]["elasticity_score"], 0.8)
        self.assertAlmostEqual(steps[3]["elasticity_score"], 0.7)

    def test_evidence_is_the_stories_behind_the_edges_walked(self):
        """The old implementation LIKE-matched ticker symbols against article
        prose — articles write 'HDFC Bank', never 'HDFCBANK', so it mostly
        matched nothing and never matched the specific relationship."""
        causal.build(self.con)
        chain = causal.list_causal_chains(self.con)[0]
        self.assertEqual(chain["corroborating_story_ids"], ["s1", "s2", "s3"])

    def test_sectors_come_from_the_corroborating_stories(self):
        causal.build(self.con)
        chain = causal.list_causal_chains(self.con)[0]
        self.assertEqual(sorted(chain["affected_sectors"]), ["auto", "banking"])

    def test_probability_is_joint_and_confidence_is_per_link(self):
        causal.build(self.con)
        c = causal.list_causal_chains(self.con)[0]
        # Joint: every hop has to hold, so the product.
        self.assertAlmostEqual(c["base_probability"], 0.9 * 0.8 * 0.7, places=3)
        # Per-link quality sits above it, and between the weakest and strongest.
        self.assertGreater(c["overall_confidence"], c["base_probability"])
        self.assertTrue(0.7 <= c["overall_confidence"] <= 0.9)

    def test_a_walk_too_short_to_be_a_chain_is_not_published(self):
        con2 = db.connect()
        con2.execute("DELETE FROM fin_kg_edges WHERE subject IN ('HDFCBANK','TATAMOTORS')")
        con2.commit()
        self.assertEqual(causal.build(con2), 0)
        # ...and the page falls back to seed content rather than going blank.
        chains = causal.list_causal_chains(con2)
        self.assertTrue(chains and all(c["curated"] for c in chains))
        con2.close()

    def test_weak_edges_are_not_chained(self):
        con2 = db.connect()
        con2.execute("UPDATE fin_kg_edges SET confidence=? WHERE subject='HDFCBANK'",
                     (config.FIN_CAUSAL_MIN_EDGE_CONF - 0.05,))
        con2.commit()
        causal.build(con2)
        for c in causal.list_causal_chains(con2):
            if not c["curated"]:
                self.assertNotIn("TATAMOTORS", c["signature"].split(">")[2:],
                                 "a chain must not cross a link below the floor")
        con2.close()

    def test_overlapping_walks_collapse_to_one_chain(self):
        """A greedy walk entering the same cascade one hop later traces the same
        edges from then on. Publishing both puts one story on the page twice."""
        self.node("MARUTI", "Maruti Suzuki", "MARUTI")
        self.edge("EXIDEIND", "supplies", "MARUTI", 0.75, ["s3"])
        self.con.commit()
        causal.build(self.con)
        sigs = [c["signature"] for c in causal.list_causal_chains(self.con)]
        self.assertEqual(len(sigs), 1, f"expected one cascade, got {sigs}")


class IncrementalTest(_Graph):
    """The cost argument for the stage: structure is free, prose is not, so
    prose is bought only when the structure it describes actually moved."""

    def _calls(self, fn):
        before = llm.usage["calls"]
        fn()
        return llm.usage["calls"] - before

    def test_first_build_narrates_then_reruns_are_free(self):
        self.assertEqual(self._calls(lambda: causal.build(self.con)), 1)
        self.assertEqual(self._calls(lambda: causal.build(self.con)), 0,
                         "an unchanged graph must not buy the same words twice")

    def test_confidence_drift_below_the_rounding_threshold_is_free(self):
        causal.build(self.con)
        self.con.execute("UPDATE fin_kg_edges SET confidence=0.92 WHERE subject='RBI'")
        self.con.commit()
        self.assertEqual(self._calls(lambda: causal.build(self.con)), 0)

    def test_a_new_hop_re_narrates(self):
        causal.build(self.con)
        # Re-point the last hop at a different company: a real structural change.
        self.node("AMARAJABAT", "Amara Raja", "AMARAJABAT")
        self.con.execute("UPDATE fin_kg_edges SET object='AMARAJABAT' "
                         "WHERE subject='TATAMOTORS'")
        self.con.commit()
        self.assertEqual(self._calls(lambda: causal.build(self.con)), 1)

    def test_reused_prose_survives_the_rebuild(self):
        causal.build(self.con)
        first = causal.list_causal_chains(self.con)[0]
        causal.build(self.con)
        again = causal.list_causal_chains(self.con)[0]
        self.assertEqual(first["title"], again["title"])
        self.assertEqual([s["action_or_friction"] for s in first["steps"]],
                         [s["action_or_friction"] for s in again["steps"]])
        self.assertEqual(first["id"], again["id"],
                         "the same path must update in place, not duplicate")

    def test_prose_calls_are_capped_per_run(self):
        """A fresh graph where everything is new at once must not narrate
        without bound — the first run after a deploy is exactly that case."""
        for i in range(8):
            a, b, c = f"A{i}", f"B{i}", f"C{i}"
            for n in (a, b, c):
                self.node(n, f"Company {n}", n)
            self.edge(a, "supplies", b, 0.8, ["s1"])
            self.edge(b, "supplies", c, 0.8, ["s2"])
        self.con.commit()
        self.assertLessEqual(self._calls(lambda: causal.build(self.con)),
                             config.FIN_MAX_CAUSAL_PROSE_CALLS)


class CuratedFallbackTest(unittest.TestCase):
    def setUp(self):
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "empty.db")
        os.environ["DB_PATH"] = config.DB_PATH
        db._schema_ready = False
        self.con = db.connect()

    def tearDown(self):
        self.con.close()

    def test_an_empty_graph_serves_flagged_seed_content(self):
        self.assertEqual(causal.build(self.con), 0)
        chains = causal.list_causal_chains(self.con)
        self.assertTrue(chains)
        self.assertTrue(all(c["curated"] for c in chains),
                        "seed chains must be flagged so a reader is never shown "
                        "them as though they were today's reporting")

    def test_listing_never_mutates_the_module_constant(self):
        """It used to: `list(CANONICAL_CAUSAL_CHAINS)` is a shallow copy, so
        writing corroborating_story_ids onto a 'copy' wrote into shared state
        that every later request read."""
        before = json.dumps(causal.CANONICAL_CAUSAL_CHAINS, sort_keys=True)
        causal.list_causal_chains(self.con)
        for c in causal.list_causal_chains(self.con):
            c["title"] = "mutated by a caller"
            c["corroborating_story_ids"].append("injected")
        after = json.dumps(causal.CANONICAL_CAUSAL_CHAINS, sort_keys=True)
        self.assertEqual(before, after)


class SimulationTest(_Graph):
    def setUp(self):
        super().setUp()
        causal.build(self.con)
        self.chain = causal.list_causal_chains(self.con)[0]

    def test_an_unknown_shock_id_errors_instead_of_answering(self):
        """It used to fall through to CANONICAL_CAUSAL_CHAINS[0] — a full,
        confident simulation of a completely different chain, with no signal
        that the requested one was never found."""
        out = causal.simulate_counterfactual_shock("no-such-chain", 25, con=self.con)
        self.assertIn("error", out)
        self.assertNotIn("simulated_steps", out)
        self.assertIn(self.chain["id"], out["available"])

    def test_a_known_chain_simulates(self):
        out = causal.simulate_counterfactual_shock(self.chain["id"], 25, con=self.con)
        self.assertNotIn("error", out)
        self.assertEqual(out["shock_id"], self.chain["id"])
        self.assertEqual(len(out["simulated_steps"]), len(self.chain["steps"]))
        self.assertFalse(out["curated"])

    def test_exposure_tier_follows_position_in_this_chain(self):
        """Not a hard-coded list of four symbols, which is what it was — every
        other company in the graph was 'MODERATE' forever."""
        out = causal.simulate_counterfactual_shock(self.chain["id"], 50, con=self.con)
        impacts = out["ticker_impacts"]
        self.assertEqual(impacts["HDFCBANK"]["exposure_tier"], "HIGH")
        self.assertLess(impacts["EXIDEIND"]["sensitivity_beta"],
                        impacts["HDFCBANK"]["sensitivity_beta"],
                        "a company four hops out cannot be as exposed as one "
                        "the catalyst reaches directly")
        self.assertEqual(impacts["HDFCBANK"]["reached_at_step"], 2)

    def test_generated_dampeners_carry_no_invented_number(self):
        for d in self.chain["dampeners"]:
            self.assertIsNone(d["absorption_capacity_pct"])
        out = causal.simulate_counterfactual_shock(self.chain["id"], 25, con=self.con)
        self.assertEqual(out["dampener_absorption_pct"], 0,
                         "absorption is not measurable from this data, so the "
                         "simulation must not credit the chain with a cushion")

    def test_intensity_moves_the_probability_in_the_right_direction(self):
        base = causal.simulate_counterfactual_shock(self.chain["id"], 0, con=self.con)
        harder = causal.simulate_counterfactual_shock(self.chain["id"], 75, con=self.con)
        easier = causal.simulate_counterfactual_shock(self.chain["id"], -75, con=self.con)
        self.assertGreater(harder["simulated_ripple_probability"],
                           base["simulated_ripple_probability"])
        self.assertLess(easier["simulated_ripple_probability"],
                        base["simulated_ripple_probability"])


class StageTest(_Graph):
    def test_fin_causal_is_a_pipeline_stage(self):
        """The whole defect was that no stage existed, so the page could not
        update however often the pipeline ran."""
        from app.finance.orchestrator import STAGES
        self.assertIn("fin_causal", STAGES)
        self.assertEqual(STAGES[-1], "fin_causal",
                         "chains must be walked AFTER fin_stories has written "
                         "this cycle's edges")

    def test_the_stage_logs_a_run_row(self):
        causal.build(self.con)
        r = self.con.execute(
            "SELECT status, detail FROM runs WHERE stage='fin_causal' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertIsNotNone(r, "the stage must be visible in recent_runs")
        self.assertEqual(r["status"], "ok")

    def test_health_names_an_empty_chain_table(self):
        from app.finance.orchestrator import health
        db.log_run(self.con, "finance_pipeline", "done", "{}")
        self.con.commit()
        out = health(self.con)
        self.assertIn("fin_causal_chains", out["content"])
        self.assertTrue(any("curated" in p.lower() for p in out["problems"]),
                        "an empty chain table is the exact cause of 'the "
                        "Predictions page never changes' and must be named")


if __name__ == "__main__":
    unittest.main()
