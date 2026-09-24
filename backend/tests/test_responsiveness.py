"""Tests for the fixes behind "the app is slow and finance never runs".

    cd backend && python -m unittest tests.test_responsiveness -v

Each is written against the failure it prevents:

  * an LLM call made while a write transaction is open holds SQLite's only
    write lock for the length of the call, and every request queued behind it;
  * a request that writes (the traffic counter) waits on that lock, on the
    event loop, so one stuck write stalled the whole server;
  * a throttled provider that is the ONLY one left must be waited for, not
    given up on — that was thousands of dropped calls per production run;
  * "out of quota" is not a rate limit and must not be retried every 15 min;
  * a stage that can reach no provider must not start;
  * N open live streams must not mean N database reads per tick.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agents, analytics, config, db, live, llm, orchestrator  # noqa: E402
from tests.test_costcontrol import _DBCase, _patched                     # noqa: E402


class NoLLMCallInsideAWriteTest(_DBCase):
    def test_foresight_releases_the_write_lock_before_calling_the_llm(self):
        now = db.now()
        for i in range(5):
            self.con.execute(
                "INSERT INTO stories (id, headline, narrative, credibility, topic, "
                "claims, article_ids, trend_ids, connection_ids, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"s{i}", f"Headline {i}", "Narrative.", 60, "business", "{}",
                 "[]", "[]", "[]", now, now))
        # A stale forecast, so the prune UPDATE really writes.
        self.con.execute(
            "INSERT INTO signals (id, title, prediction, chain, watch, affected, "
            "horizon, confidence, story_ids, created_at, updated_at) "
            "VALUES ('old','t','p','c','w','[]','',0.5,'[]',?,?)",
            (now - 30 * 86400, now - 30 * 86400))
        self.con.commit()
        seen = []

        def spy(task, *a, **k):
            seen.append(self.con.in_transaction)
            return None

        with _patched(llm, "complete_json", spy):
            agents.Foresight().run(self.con)
        self.assertTrue(seen, "the stage should have asked the LLM something")
        self.assertFalse(any(seen), "an LLM call was made holding the write lock")


class TrafficCounterTest(_DBCase):
    def setUp(self):
        super().setUp()
        analytics._pending.clear()

    def test_requests_are_counted_in_memory_and_written_in_one_batch(self):
        for _ in range(3):
            analytics.note_traffic("/feed")
        analytics.note_traffic("/story/{story_id}")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM traffic").fetchone()[0], 0,
                         "noting a request must not touch the database")
        self.assertEqual(analytics.flush_traffic(self.con), 2)
        hits = dict(self.con.execute("SELECT route, hits FROM traffic").fetchall())
        self.assertEqual(hits, {"/feed": 3, "/story/{story_id}": 1})
        analytics.note_traffic("/feed")
        analytics.flush_traffic(self.con)
        self.assertEqual(self.con.execute(
            "SELECT hits FROM traffic WHERE route='/feed'").fetchone()[0], 4,
            "a second flush must add to the row, not replace it")
        self.assertEqual(analytics.flush_traffic(self.con), 0)

    def test_a_failed_flush_keeps_the_counts(self):
        analytics.note_traffic("/feed")
        dead = sqlite3.connect(":memory:")
        dead.close()
        self.assertEqual(analytics.flush_traffic(dead), 0)
        self.assertEqual(sum(analytics._pending.values()), 1)
        analytics.flush_traffic(self.con)
        self.assertEqual(self.con.execute(
            "SELECT hits FROM traffic WHERE route='/feed'").fetchone()[0], 1)


class ThrottledLastProviderTest(_DBCase):
    """groq's model is retired; gemini is live but its minute window is full."""

    def setUp(self):
        super().setUp()
        config.LLM_PROVIDER = "auto"
        llm._dead_models.clear()
        llm._dead_models[("groq", "m")] = "retired"

    def tearDown(self):
        llm._dead_models.clear()
        super().tearDown()

    def test_waits_for_the_slot_instead_of_giving_up(self):
        waits = iter([4.0, 0.0])        # full once, then free
        slept = []
        with _patched(llm, "_order_for", lambda t: ["groq", "gemini"]), \
                _patched(llm, "_model_for", lambda p, t: "m"), \
                _patched(llm, "_try_reserve", lambda p: next(waits)), \
                _patched(llm, "_pace", lambda p, m: None), \
                _patched(llm, "_call", lambda p, prompt, task=None: '{"ok": 1}'), \
                _patched(llm.time, "sleep", slept.append):
            out = llm.complete_json("story", "prompt-for-throttle-test")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(slept, [4.0], "should have waited for gemini's slot once")

    def test_availability_does_not_count_a_retired_model(self):
        with _patched(llm, "_order_for", lambda t: ["groq"]), \
                _patched(llm, "_model_for", lambda p, t: "m"):
            gate = llm.availability("story")
        self.assertFalse(gate["ready"])
        self.assertIn("retired", gate["detail"])


class QuotaIsNotARateLimitTest(unittest.TestCase):
    def test_exhausted_quota_is_recognised(self):
        self.assertTrue(llm._quota_exhausted(
            '{"error": {"message": "You exceeded your current quota", '
            '"type": "insufficient_quota"}}'))
        self.assertTrue(llm._quota_exhausted(
            '{"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"quotaId": '
            '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'))

    def test_an_ordinary_rate_limit_is_not(self):
        self.assertFalse(llm._quota_exhausted(
            '{"error": {"message": "Rate limit reached for requests per minute"}}'))
        self.assertFalse(llm._quota_exhausted(""))


class StageGateTest(_DBCase):
    def test_a_stage_with_no_reachable_provider_is_skipped(self):
        gate = {"ready": False, "wait_seconds": 900.0, "detail": "all benched"}
        ran = []
        with _patched(llm, "availability", lambda task: gate), \
                _patched(agents.Foresight, "run", lambda self, con: ran.append(1)):
            out = orchestrator.run_pipeline("signals")
        self.assertEqual(ran, [], "the stage must not start")
        self.assertTrue(str(out["signals"]).startswith("skipped"))


class SharedLiveSnapshotTest(unittest.TestCase):
    def setUp(self):
        live._shared.clear()

    def test_open_streams_share_one_read_per_interval(self):
        reads = []
        with _patched(live, "snapshot", lambda con, cats: reads.append(1) or []), \
                _patched(live, "latest_story_marker", lambda con: {"count": 0}), \
                _patched(db, "connect", lambda: sqlite3.connect(":memory:")):
            for _ in range(50):          # fifty clients on the same tick
                live._shared_read(["breaking"], max_age=9)
            live._shared_read(["sports"], max_age=9)
        self.assertEqual(len(reads), 2, "one read per category set, not per client")


if __name__ == "__main__":
    unittest.main()
