"""Tests for persistent LLM spend accounting (app/llmcost.py).

    cd backend && python -m unittest discover -s tests -v

What is under test is the arithmetic and the storage guarantees, not any
provider's price: the rate card is injected per test. The two properties that
matter most to a cost report are here as their own cases — that an unpriced
model reads as "unknown" rather than "free", and that the numbers survive the
restart which empties llm.usage.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, llm, llmcost                            # noqa: E402


class _CostCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = (config.DB_PATH, db._schema_ready, config.LLM_PRICES_ENV,
                     config.LLM_PRICE_DEFAULTS, config.LLM_PROVIDER)
        config.DB_PATH, db._schema_ready = self._tmp.name, False
        # A rate card with round numbers, so an expected cost can be read off
        # the test rather than trusted from a fixture.
        config.LLM_PRICE_DEFAULTS = {"mock/mock": [0.0, 0.0, 0.0]}
        config.LLM_PRICES_ENV = {"acme/fast": [1.0, 10.0, 0.1]}
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        (config.DB_PATH, db._schema_ready, config.LLM_PRICES_ENV,
         config.LLM_PRICE_DEFAULTS, config.LLM_PROVIDER) = self._old
        os.unlink(self._tmp.name)


class Pricing(_CostCase):
    def test_input_and_output_are_priced_apart(self):
        """Output is the expensive half; a single blended rate over total tokens
        would understate any call that answers at length."""
        # 1M input at $1 + 0.5M output at $10 = $6.
        self.assertAlmostEqual(
            llmcost.cost_of("acme", "fast", 1_000_000, 500_000), 6.0)

    def test_cached_prompt_tokens_are_a_subset_not_an_extra(self):
        """Providers report cache hits as part of prompt_tokens. Counting them
        on top would bill the same tokens twice."""
        # 1M prompt of which 600k were cache hits: 400k at $1 + 600k at $0.10.
        self.assertAlmostEqual(
            llmcost.cost_of("acme", "fast", 1_000_000, 0, cached_tokens=600_000),
            0.4 + 0.06)
        # A cached count larger than the prompt is a provider quirk, not a credit.
        self.assertGreaterEqual(
            llmcost.cost_of("acme", "fast", 1000, 0, cached_tokens=99999), 0)

    def test_peak_doubles_every_billing_item(self):
        off = llmcost.cost_of("acme", "fast", 10_000, 1_000)
        self.assertAlmostEqual(
            llmcost.cost_of("acme", "fast", 10_000, 1_000, peak=True), off * 2)

    def test_unknown_model_is_unpriced_not_free(self):
        """The distinction the whole report rests on: $0 is a claim that a model
        costs nothing, and a wrong $0 in a monthly total never gets questioned."""
        self.assertIsNone(llmcost.cost_of("acme", "never-heard-of-it", 1e6, 1e6))
        self.assertEqual(llmcost.price_for("acme", "never-heard-of-it")[1], "unpriced")

    def test_configured_rate_beats_the_built_in_default(self):
        config.LLM_PRICE_DEFAULTS = {"acme/fast": [99.0, 99.0, 99.0]}
        rate, source = llmcost.price_for("acme", "fast")
        self.assertEqual((rate[0], source), (1.0, "configured"))

    def test_malformed_price_entry_is_skipped_not_read_as_zero(self):
        parsed = config._parse_prices('{"a/b": "free", "c/d": [2], "e/f": [-1, 1]}')
        self.assertNotIn("a/b", parsed)
        self.assertNotIn("e/f", parsed)
        self.assertEqual(parsed["c/d"], [2.0, 2.0, 2.0])   # one number = flat rate


class Recording(_CostCase):
    def test_counters_accumulate_in_place(self):
        for _ in range(3):
            llmcost.record("acme", "fast", "trend", prompt_tokens=1000,
                           completion_tokens=100, total_tokens=1100, latency_ms=500)
        rows = self.con.execute("SELECT * FROM llm_usage").fetchall()
        self.assertEqual(len(rows), 1, "one row per day/provider/model/task")
        self.assertEqual((rows[0]["calls"], rows[0]["total_tokens"]), (3, 3300))

    def test_peak_and_offpeak_are_separate_rows(self):
        """So a repricing can reapply the right multiplier, and the report can
        say how much was billed at the doubled rate."""
        llmcost.record("acme", "fast", "t", prompt_tokens=100, total_tokens=100)
        llmcost.record("acme", "fast", "t", prompt_tokens=100, total_tokens=100,
                       peak=True)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) c FROM llm_usage").fetchone()["c"], 2)
        r = llmcost.report(self.con, days=7)
        self.assertAlmostEqual(r["peak_cost_usd"], r["cost_usd"] * 2 / 3)

    def test_failed_call_counts_as_a_failure_not_a_call(self):
        llmcost.record("acme", "fast", "t", prompt_tokens=100, total_tokens=100)
        llmcost.record("acme", "fast", "t", failed=True)
        row = self.con.execute("SELECT * FROM llm_usage").fetchone()
        self.assertEqual((row["calls"], row["failures"], row["total_tokens"]),
                         (1, 1, 100))
        self.assertEqual(llmcost.report(self.con)["by_model"][0]["failure_rate"], 50.0)

    def test_survives_the_restart_that_empties_the_session_counters(self):
        """The whole reason this table exists."""
        llmcost.record("acme", "fast", "t", prompt_tokens=1_000_000,
                       completion_tokens=0, total_tokens=1_000_000)
        before = llmcost.totals(self.con)
        # A process restart: new connection, module state reset. Nothing else.
        self.con.close()
        db._schema_ready = False
        llm.usage.update({"calls": 0, "tokens": 0, "cost_usd": 0.0})
        self.con = db.connect()
        self.assertEqual(llmcost.totals(self.con), before)
        self.assertAlmostEqual(before["cost_usd"], 1.0)


class Reporting(_CostCase):
    def _seed(self):
        llmcost.record("acme", "fast", "trend", prompt_tokens=2_000_000,
                       completion_tokens=0, total_tokens=2_000_000, latency_ms=1000)
        llmcost.record("acme", "fast", "trend", prompt_tokens=0,
                       completion_tokens=0, total_tokens=0, latency_ms=3000)
        llmcost.record("acme", "slow", "story", prompt_tokens=5_000,
                       completion_tokens=500, total_tokens=5_500)

    def test_averages_are_per_call_and_per_model(self):
        self._seed()
        by_model = {m["model"]: m for m in llmcost.report(self.con)["by_model"]}
        self.assertEqual(by_model["fast"]["calls"], 2)
        self.assertEqual(by_model["fast"]["avg_tokens_per_call"], 1_000_000.0)
        self.assertAlmostEqual(by_model["fast"]["avg_cost_per_call"], 1.0)
        self.assertEqual(by_model["fast"]["avg_latency_ms"], 2000)

    def test_unpriced_model_is_named_and_excluded_from_cost(self):
        self._seed()
        rep = llmcost.report(self.con)
        self.assertEqual([u["model"] for u in rep["unpriced"]], ["slow"])
        # 'slow' has real tokens and contributes nothing to the money.
        self.assertAlmostEqual(rep["cost_usd"], 2.0)
        self.assertEqual(rep["total_tokens"], 2_005_500)
        self.assertFalse({m["model"]: m for m in rep["by_model"]}["slow"]["priced"])

    def test_task_breakdown_splits_the_same_model_by_stage(self):
        self._seed()
        tasks = {t["task"]: t for t in llmcost.report(self.con)["by_task"]}
        self.assertAlmostEqual(tasks["trend"]["cost_usd"], 2.0)
        self.assertEqual(tasks["story"]["calls"], 1)

    def test_daily_series_is_dense(self):
        """A sparse series plots a handful of gaps as continuous activity."""
        self._seed()
        self.assertEqual(len(llmcost.report(self.con, days=14)["daily"]), 14)

    def test_reprice_rebuilds_cost_from_the_stored_tokens(self):
        """The recovery path for a rate that was missing or wrong — which it is
        by default for any model with no seeded price."""
        self._seed()
        self.assertAlmostEqual(llmcost.report(self.con)["cost_usd"], 2.0)
        config.LLM_PRICES_ENV["acme/slow"] = [2.0, 4.0, 2.0]
        self.assertEqual(llmcost.reprice(self.con), 1)
        rep = llmcost.report(self.con)
        self.assertEqual(rep["unpriced"], [])
        # 5000 in at $2/1M + 500 out at $4/1M, on top of the unchanged $2.
        self.assertAlmostEqual(rep["cost_usd"], 2.0 + 0.01 + 0.002)

    def test_reprice_leaves_a_still_unpriced_model_alone(self):
        self._seed()
        self.assertEqual(llmcost.reprice(self.con), 0)
        self.assertAlmostEqual(llmcost.report(self.con)["cost_usd"], 2.0)

    def test_empty_database_reports_zeroes_not_an_error(self):
        rep = llmcost.report(self.con, days=7)
        self.assertEqual((rep["calls"], rep["cost_usd"], rep["by_model"]), (0, 0, []))
        self.assertEqual(llmcost.totals(self.con)["calls"], 0)


class LLMIntegration(_CostCase):
    """The wiring from a real complete_json call through to the table."""

    def test_mock_call_is_recorded_with_its_model_and_task(self):
        config.LLM_PROVIDER = "mock"
        llm.complete_json("entities", "Reliance posts a record 12000 crore profit")
        row = self.con.execute("SELECT * FROM llm_usage").fetchone()
        self.assertEqual((row["provider"], row["model"], row["task"], row["calls"]),
                         ("mock", "mock", "entities", 1))

    def test_gemini_usage_counts_thinking_tokens_as_output(self):
        """candidatesTokenCount excludes them, but they are billed — leaving them
        out reports a thinking model's bill as a fraction of what it was."""
        p, c, cached, total = llm._gemini_usage({"usageMetadata": {
            "promptTokenCount": 900, "candidatesTokenCount": 100,
            "thoughtsTokenCount": 4000, "totalTokenCount": 5000}})
        self.assertEqual((p, c, cached, total), (900, 4100, 0, 5000))

    def test_openai_usage_reads_both_cache_shapes(self):
        self.assertEqual(llm._openai_usage({"usage": {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_cache_hit_tokens": 60, "total_tokens": 110}}),
            (100, 10, 60, 110))
        self.assertEqual(llm._openai_usage({"usage": {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 40}}}),
            (100, 10, 40, 110))

    def test_recording_never_raises_when_the_database_is_gone(self):
        """Accounting must not be the reason a pipeline stage fails."""
        config.DB_PATH = "/nonexistent-directory/nope.db"
        db._schema_ready = False
        self.assertFalse(llmcost.record("acme", "fast", "t", total_tokens=10))


if __name__ == "__main__":
    unittest.main()
