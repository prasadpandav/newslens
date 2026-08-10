"""Tests for the LLM cost controls: task tiers, spend ceilings, the answer
cache, the entity gazetteer and incremental trend synthesis.

    cd backend && python -m unittest discover -s tests -v

Each of these exists because a specific way of overspending was possible, so the
tests are written as "this must not be able to happen again" rather than as
coverage of the happy path:

  * a mechanical task must not be answerable by a frontier model, whatever the
    provider order does;
  * an UNPRICED model must not read as a free one, or it escapes every ceiling;
  * a per-minute cap on one provider must not stall the others;
  * the gazetteer must refuse to answer an article containing a name it does not
    know, because a wrong entity list propagates into the knowledge graph;
  * an incremental trend run must GROW a trend, never shrink it to its delta.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, gazetteer, llm, llmcache, llmcost      # noqa: E402
from app import agents                                             # noqa: E402


class _DBCase(unittest.TestCase):
    """A throwaway database plus the config knobs these tests move."""

    KNOBS = ("DB_PATH", "LLM_PROVIDER", "LLM_DAILY_BUDGET_USD", "FREE_PROVIDERS",
             "PROVIDER_DAILY_CALL_LIMITS", "PROVIDER_MAX_CALLS_PER_MIN",
             "LLM_MAX_CALLS_PER_MIN", "LLM_CACHE_TTL_SECONDS",
             "LLM_PRICE_DEFAULTS", "LLM_PRICES_ENV", "GAZETTEER_SHADOW",
             "GAZETTEER_MAX_UNKNOWN", "ENTITIES_BATCH_SIZE",
             "TRENDS_FULL_PASS_HOURS", "OPENAI_CHEAP_MODEL", "OPENAI_MODEL",
             "OPENAI_REASONING_MODEL", "CHEAP_TASKS", "REASONING_TASKS")

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._saved = {k: getattr(config, k) for k in self.KNOBS}
        self._ready = db._schema_ready
        config.DB_PATH, db._schema_ready = self._tmp.name, False
        config.LLM_PROVIDER = "mock"
        config.GAZETTEER_SHADOW = False
        llm.usage["calls"] = 0
        llm._benched_until.clear()
        llm._bench_strikes.clear()
        llm._call_times.clear()
        llm._provider_call_times.clear()
        llmcost.invalidate_today()
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        for k, v in self._saved.items():
            setattr(config, k, v)
        db._schema_ready = self._ready
        llmcost.invalidate_today()
        os.unlink(self._tmp.name)

    def add_article(self, aid, title, summary, topic="finance", ents=(),
                    sectors=("banking",), regions=("India",), age_h=1):
        blob = json.dumps({"entities": list(ents), "sectors": list(sectors),
                           "regions": list(regions)}) if ents else ""
        at = db.now() - age_h * 3600
        self.con.execute(
            "INSERT INTO articles (id,url,title,summary,source,topic,entities,"
            "fetched_at,published,group_id) VALUES (?,?,?,?,?,?,?,?,?,'')",
            (aid, "http://x/" + aid, title, summary, "src.com", topic, blob, at, at))
        self.con.commit()


# ------------------------------------------------------------------- tiers
class TierRoutingTest(_DBCase):
    def test_cheap_task_never_reaches_the_base_model(self):
        """The whole point of the cheap tier. `entities` is not a reasoning task,
        so before tiers it fell through to the ordinary path and was served by
        whatever OPENAI_MODEL happened to be — a frontier model."""
        config.OPENAI_MODEL = "expensive-frontier-model"
        config.OPENAI_CHEAP_MODEL = "cheap-small-model"
        self.assertEqual(llm._model_for("openai", "entities"), "cheap-small-model")
        self.assertEqual(llm._model_for("openai", "story"), "expensive-frontier-model")

    def test_reasoning_and_standard_are_unchanged(self):
        config.OPENAI_MODEL = "base"
        config.OPENAI_REASONING_MODEL = "thinker"
        self.assertEqual(llm._model_for("openai", "trend"), "thinker")
        self.assertEqual(llm._model_for("openai", "story"), "base")

    def test_reasoning_falls_back_to_base_when_unset(self):
        config.OPENAI_MODEL = "base"
        config.OPENAI_REASONING_MODEL = ""
        self.assertEqual(llm._model_for("openai", "trend"), "base")

    def test_unclassified_task_is_standard(self):
        """A task nobody has tiered keeps today's behaviour rather than being
        silently downgraded to a small model."""
        self.assertEqual(config.tier_of("some_brand_new_task"), "standard")


# ---------------------------------------------------------------- ceilings
class SpendCeilingTest(_DBCase):
    def setUp(self):
        super().setUp()
        config.LLM_PRICE_DEFAULTS = {"paid/m": [1.0, 1.0, 1.0],
                                     "free/m": [0.0, 0.0, 0.0]}
        config.LLM_PRICES_ENV = {}

    def _model_for(self, provider, task):
        return "m"

    def test_unpriced_model_counts_as_paid(self):
        """An unknown price is not a free one. Treating it as free is how a model
        with no rate card entry slips past every ceiling below."""
        with _patched(llm, "_model_for", lambda p, t: "not-in-the-rate-card"):
            self.assertTrue(llm._is_paid("openai", "story"))

    def test_budget_exhausted_drops_paid_but_keeps_free(self):
        config.LLM_DAILY_BUDGET_USD = 1.0
        llmcost.record("paid", "m", "story", prompt_tokens=2_000_000,
                       total_tokens=2_000_000, con=self.con)
        llmcost.invalidate_today()
        with _patched(llm, "_model_for", self._model_for):
            order, notes = llm._affordable(["free", "paid"], "story")
        self.assertEqual(order, ["free"])
        self.assertTrue(any("budget" in n for n in notes))

    def test_under_budget_keeps_everything(self):
        config.LLM_DAILY_BUDGET_USD = 100.0
        with _patched(llm, "_model_for", self._model_for):
            order, _ = llm._affordable(["free", "paid"], "story")
        self.assertEqual(order, ["free", "paid"])

    def test_daily_call_limit_drops_a_provider_even_when_free(self):
        config.PROVIDER_DAILY_CALL_LIMITS = {"free": 2}
        for _ in range(2):
            llmcost.record("free", "m", "story", total_tokens=1, con=self.con)
        llmcost.invalidate_today()
        with _patched(llm, "_model_for", self._model_for):
            order, notes = llm._affordable(["free", "paid"], "story")
        self.assertEqual(order, ["paid"])
        self.assertTrue(any("daily call limit" in n for n in notes))

    def test_budget_blocks_the_call_entirely_when_nothing_is_left(self):
        """Returning None puts the item on the existing retry path rather than
        spending past the ceiling.

        The ceiling has to be reached with a PRICED model: an unpriced one
        records $0 however many tokens it burns, so the budget would never
        engage and this call would go out over the network for real."""
        config.LLM_PROVIDER = "openai"
        config.LLM_PRICE_DEFAULTS = {"openai/m": [1.0, 1.0, 1.0]}
        config.LLM_DAILY_BUDGET_USD = 0.01
        llmcost.record("openai", "m", "story", prompt_tokens=1_000_000,
                       total_tokens=1_000_000, con=self.con)
        llmcost.invalidate_today()
        self.assertGreater(llmcost.today_usage(max_age=0)[0], 0.01,
                           "the fixture must actually record spend")
        blocked = llm.usage["budget_blocked"]
        with _patched(llm, "_model_for", self._model_for):
            self.assertIsNone(llm.complete_json("story", "hello"))
        self.assertEqual(llm.usage["budget_blocked"], blocked + 1,
                         "must be refused by the ceiling, not by a failed call")

    def test_free_tier_spend_does_not_consume_the_budget(self):
        """Groq/Gemini are billed at $0 up to a daily quota, but they DO have a
        rate card entry (it answers "what would the paid plan have cost"). If
        that notional figure counted against the ceiling, free traffic would
        exhaust the budget and lock out the providers actually being billed —
        exactly backwards."""
        config.FREE_PROVIDERS = {"groq"}
        config.LLM_PRICE_DEFAULTS = {"groq/m": [10.0, 10.0, 10.0]}
        llmcost.record("groq", "m", "story", prompt_tokens=1_000_000,
                       total_tokens=1_000_000, con=self.con)
        llmcost.invalidate_today()
        spend, calls = llmcost.today_usage(max_age=0)
        self.assertEqual(spend, 0.0, "free-tier spend is not money")
        self.assertEqual(calls["groq"], 1, "but its CALLS still count, for quota")

    def test_a_free_provider_is_never_priced_out(self):
        config.FREE_PROVIDERS = {"groq"}
        config.LLM_PRICE_DEFAULTS = {"groq/m": [99.0, 99.0, 99.0]}
        with _patched(llm, "_model_for", self._model_for):
            self.assertFalse(llm._is_paid("groq", "story"))

    def test_today_usage_ignores_other_days(self):
        llmcost.record("paid", "m", "story", prompt_tokens=1_000_000,
                       total_tokens=1_000_000, con=self.con)
        self.con.execute("UPDATE llm_usage SET day = '1999-01-01'")
        self.con.commit()
        llmcost.invalidate_today()
        spend, calls = llmcost.today_usage(max_age=0)
        self.assertEqual(spend, 0.0)
        self.assertEqual(calls, {})


class DotenvQuotingTest(unittest.TestCase):
    """A quoted value in .env kept its quotes, so LLM_PRICES arrived as
    `'{"model": [...]}'`, json.loads rejected it, and the ENTIRE configured rate
    card was discarded — leaving every model priced at $0.00 while the cost
    report went on printing that as if it were a measurement. A JSON value has to
    be quoted to survive a shell, so this is the normal way to write one."""

    def test_single_and_double_quotes_are_stripped(self):
        self.assertEqual(config._unquote("'{\"a\": 1}'"), '{"a": 1}')
        self.assertEqual(config._unquote('"hello"'), "hello")

    def test_unquoted_and_mismatched_values_are_left_alone(self):
        self.assertEqual(config._unquote("plain"), "plain")
        self.assertEqual(config._unquote("'mismatched\""), "'mismatched\"")
        self.assertEqual(config._unquote("it's"), "it's")
        self.assertEqual(config._unquote(""), "")
        self.assertEqual(config._unquote("'"), "'")

    def test_a_quoted_rate_card_parses(self):
        card = '\'{"openai/m": [1.0, 2.0]}\''
        self.assertEqual(config._parse_prices(config._unquote(card)),
                         {"openai/m": [1.0, 2.0, 2.0]})

    def test_malformed_json_is_recorded_not_swallowed(self):
        before = len(config._price_errors)
        self.assertEqual(config._parse_prices("{not json"), {})
        self.assertGreater(len(config._price_errors), before,
                           "a discarded rate card must not be silent")
        config._price_errors.pop()


class PacingTest(_DBCase):
    """The pacing table is keyed by exact model name, and production ran two
    Gemini models that were not in it — `gemini-3.1-flash-lite` when the table
    listed `gemini-3.5-flash-lite`. There was no ("gemini", None) fallback, so
    the lookup returned 0 and Gemini was sent at the global cap into a 15 RPM
    free tier: guaranteed 429, guaranteed bench, and the run handed to whoever
    was last in the provider order. One character, and the fallback provider
    silently became the primary one."""

    def test_exact_model_wins(self):
        self.assertEqual(llm._interval_for("gemini", "gemini-2.5-flash"), 6.7)

    def test_unlisted_model_inherits_its_family(self):
        self.assertEqual(llm._interval_for("gemini", "gemini-3.1-flash-lite"), 4.3)
        self.assertEqual(llm._interval_for("gemini", "gemini-9.9-flash"), 6.7)

    def test_no_configured_model_is_ever_unpaced(self):
        """The property that actually matters — not any particular number."""
        for provider in ("groq", "gemini", "deepseek", "openai"):
            for task in ("entities", "story", "trend"):
                model = llm._model_for(provider, task)
                if model:
                    self.assertGreater(
                        llm._interval_for(provider, model), 0,
                        "%s/%s would be sent at the global cap" % (provider, model))

    def test_unknown_provider_has_no_interval(self):
        self.assertEqual(llm._interval_for("nosuchprovider", "m"), 0)


class BenchAndThrottleTest(_DBCase):
    def test_bench_doubles_for_repeat_offenders_and_caps(self):
        """A flat 15-minute bench re-probes an exhausted DAILY quota four times an
        hour, and every probe pushes the run onto the paid provider."""
        config.PROVIDER_BENCH_MAX_SECONDS = 3600
        self.assertEqual(llm._bench("groq"), 900)
        self.assertEqual(llm._bench("groq"), 1800)
        self.assertEqual(llm._bench("groq"), 3600)
        self.assertEqual(llm._bench("groq"), 3600)   # capped

    def test_per_provider_cap_does_not_block_other_providers(self):
        config.LLM_MAX_CALLS_PER_MIN = 0
        config.PROVIDER_MAX_CALLS_PER_MIN = {"groq": 1}
        self.assertEqual(llm._try_reserve("groq"), 0.0)
        self.assertGreater(llm._try_reserve("groq"), 0.0)   # groq full
        self.assertEqual(llm._try_reserve("gemini"), 0.0)   # others unaffected

    def test_global_cap_still_applies(self):
        config.LLM_MAX_CALLS_PER_MIN = 1
        config.PROVIDER_MAX_CALLS_PER_MIN = {}
        self.assertEqual(llm._try_reserve("groq"), 0.0)
        self.assertGreater(llm._try_reserve("gemini"), 0.0)


# ------------------------------------------------------------------- cache
class CacheTest(_DBCase):
    def test_round_trip(self):
        self.assertIsNone(llmcache.get("entities", "p"))
        llmcache.put("entities", "p", {"entities": ["X"]})
        self.assertEqual(llmcache.get("entities", "p"), {"entities": ["X"]})

    def test_task_is_part_of_the_key(self):
        """Same text, different question, different answer."""
        llmcache.put("entities", "p", {"a": 1})
        self.assertIsNone(llmcache.get("story", "p"))

    def test_one_character_difference_is_a_different_prompt(self):
        llmcache.put("entities", "prompt a", {"a": 1})
        self.assertIsNone(llmcache.get("entities", "prompt b"))

    def test_expired_entry_is_not_served(self):
        llmcache.put("entities", "p", {"a": 1})
        config.LLM_CACHE_TTL_SECONDS = 1
        self.con.execute("UPDATE llm_cache SET created_at = ?", (time.time() - 10,))
        self.con.commit()
        self.assertIsNone(llmcache.get("entities", "p"))
        self.assertEqual(llmcache.purge(self.con), 1)

    def test_disabled_cache_never_stores_or_serves(self):
        config.LLM_CACHE_TTL_SECONDS = 0
        self.assertFalse(llmcache.put("entities", "p", {"a": 1}))
        self.assertIsNone(llmcache.get("entities", "p"))


# --------------------------------------------------------------- gazetteer
class GazetteerTest(_DBCase):
    def setUp(self):
        super().setUp()
        gazetteer.learn(self.con, {"entities": ["Reserve Bank of India", "HDFC Bank"],
                                   "sectors": ["banking"], "regions": ["India"]})
        self.index = gazetteer.load(self.con)

    def test_matches_a_fully_known_article(self):
        hit, why = gazetteer.match(
            self.index, "HDFC Bank fined by Reserve Bank of India",
            "The regulator acted over India lending rules.")
        self.assertEqual(why, "matched")
        self.assertIn("HDFC Bank", hit["entities"])
        self.assertEqual(hit["sectors"], ["banking"])

    def test_refuses_when_an_unknown_name_is_present(self):
        """The case that must never be guessed: an unrecognised proper noun is
        the signal that something NEW has appeared."""
        hit, why = gazetteer.match(
            self.index, "HDFC Bank to merge with Zeptolytics", "India deal.")
        self.assertIsNone(hit)
        self.assertTrue(why.startswith("unknown_anchors"))

    def test_refuses_when_no_entity_is_recognised(self):
        hit, why = gazetteer.match(self.index, "Cabinet clears policy", "No names.")
        self.assertIsNone(hit)
        self.assertEqual(why, "no_known_entity")

    def test_sector_comes_from_association_when_absent_from_the_text(self):
        """'banking' is not a word in this headline, and could not be recovered
        by any text match — only by what these entities were extracted with."""
        hit, why = gazetteer.match(
            self.index, "HDFC Bank names new chief", "Reserve Bank of India cleared it.")
        self.assertEqual(why, "matched")
        self.assertEqual(hit["sectors"], ["banking"])

    def test_empty_gazetteer_answers_nothing(self):
        self.assertEqual(gazetteer.match(gazetteer.Index([]), "HDFC Bank", "x"),
                         (None, "empty_gazetteer"))

    def test_backfill_seeds_from_stored_articles(self):
        self.add_article("b1", "t", "s", ents=["Infosys"])
        self.assertEqual(gazetteer.backfill(self.con), 1)
        self.assertIn("infosys",
                      [k[0] for k in gazetteer.load(self.con).terms])

    def test_learn_keeps_the_first_spelling(self):
        gazetteer.learn(self.con, {"entities": ["HDFC bank"], "sectors": ["banking"]})
        idx = gazetteer.load(self.con)
        self.assertEqual(idx.terms[("hdfc bank", "entities")]["term"], "HDFC Bank")


class EntitiesBatchParseTest(unittest.TestCase):
    def test_answers_land_on_the_index_they_name(self):
        got = agents.parse_entities_batch(
            {"items": [{"i": 2, "entities": ["B"]}, {"i": 1, "entities": ["A"]}]},
            [0, 1, 2])
        self.assertEqual(got[0]["entities"], ["A"])
        self.assertEqual(got[1]["entities"], ["B"])

    def test_missing_index_falls_back_to_position(self):
        got = agents.parse_entities_batch(
            {"items": [{"entities": ["X"]}, {"entities": ["Y"]}]}, [0, 1, 2])
        self.assertEqual(got[0]["entities"], ["X"])
        self.assertEqual(got[1]["entities"], ["Y"])

    def test_short_answer_leaves_the_rest_untagged(self):
        """A model that answers 2 of 3 must not cause the third to be stored
        wrong — it stays untagged and is retried."""
        got = agents.parse_entities_batch({"items": [{"i": 1, "entities": ["A"]}]},
                                          [0, 1, 2])
        self.assertEqual(set(got), {0})

    def test_a_bare_array_is_accepted(self):
        """Production failure: the model answered with a top-level `[...]`
        instead of the `{"items": [...]}` envelope the prompt specified.
        _extract_json parses an array as happily as an object, so `out` was a
        list, `out.get("items")` raised AttributeError, and because the
        orchestrator catches per STAGE the whole entities stage aborted —
        `entities: "error: 'list' object has no attribute 'get'"`, zero articles
        tagged for the entire run."""
        got = agents.parse_entities_batch(
            [{"i": 1, "entities": ["A"]}, {"i": 2, "entities": ["B"]}], [0, 1])
        self.assertEqual(got[0]["entities"], ["A"])
        self.assertEqual(got[1]["entities"], ["B"])

    def test_bare_array_without_indices_uses_position(self):
        got = agents.parse_entities_batch(
            [{"entities": ["X"]}, {"entities": ["Y"]}], [0, 1])
        self.assertEqual(got[0]["entities"], ["X"])
        self.assertEqual(got[1]["entities"], ["Y"])

    def test_bare_array_is_not_counted_twice_for_trends(self):
        """A bare array is ONE list. Handing it to both keys would file every
        trend as a micro-trend as well."""
        arr = [{"name": "t", "members": [1, 2]}]
        self.assertEqual(agents._items_of(arr, "trends"), arr)
        self.assertEqual(agents._items_of(arr, "micro_trends", bare=False), [])

    def test_items_of_tolerates_every_junk_shape(self):
        for bad in (None, "text", 5, {"items": "nope"}, {}, {"items": None}):
            self.assertEqual(agents._items_of(bad, "items"), [])

    def test_junk_is_ignored(self):
        for bad in ({}, {"items": "nope"}, {"items": [{"i": 99, "entities": ["Z"]}]},
                    {"items": [{"i": "x"}]}, {"items": ["string"]}):
            self.assertEqual(agents.parse_entities_batch(bad, [0, 1, 2]), {})


class EntityTaggerTest(_DBCase):
    def test_gazetteer_hits_cost_no_calls(self):
        gazetteer.learn(self.con, {"entities": ["Infosys"], "sectors": ["technology"],
                                   "regions": ["India"]})
        for i in range(6):
            self.add_article("a%d" % i, "Infosys wins India contract",
                             "India technology deal.", ents=())
        llm.usage["calls"] = 0
        agents.EntityTagger().run(self.con)
        self.assertEqual(llm.usage["calls"], 0)
        tagged = self.con.execute(
            "SELECT COUNT(*) c FROM articles WHERE entities != ''").fetchone()["c"]
        self.assertEqual(tagged, 6)

    def test_unknown_articles_are_batched_into_one_call(self):
        config.ENTITIES_BATCH_SIZE = 20
        for i in range(12):
            self.add_article("a%d" % i, "Zeptolytics %d launches Qandaru" % i,
                             "A brand new thing happened.", ents=())
        llm.usage["calls"] = 0
        agents.EntityTagger().run(self.con)
        self.assertEqual(llm.usage["calls"], 1, "12 articles must be one batched call")

    def test_shadow_mode_still_pays_for_the_call(self):
        config.GAZETTEER_SHADOW = True
        gazetteer.learn(self.con, {"entities": ["Infosys"], "sectors": ["technology"],
                                   "regions": ["India"]})
        self.add_article("a0", "Infosys wins India contract", "India technology.", ents=())
        llm.usage["calls"] = 0
        agents.EntityTagger().run(self.con)
        self.assertEqual(llm.usage["calls"], 1)
        self.assertIn("SHADOW", self.con.execute(
            "SELECT detail FROM runs WHERE stage='entities' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()["detail"])

    def test_extraction_is_learned_for_next_time(self):
        self.add_article("a0", "Zeptolytics launches Qandaru", "New thing.", ents=())
        agents.EntityTagger().run(self.con)
        self.assertGreater(len(gazetteer.load(self.con)), 0)


# ------------------------------------------------------- incremental trends
class IncrementalTrendTest(_DBCase):
    def _seed_full_pass(self):
        for k in range(8):
            self.add_article(
                "f%d" % k, "Reserve Bank of India tightens lending rules step %d" % k,
                "Banks face new capital norms across India lending markets.",
                ents=["Reserve Bank of India", "HDFC Bank"], age_h=30)
        agents.TrendLinker().run(self.con)

    def test_first_run_is_a_full_pass(self):
        self._seed_full_pass()
        detail = self.con.execute(
            "SELECT detail FROM runs WHERE stage='trends' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()["detail"]
        self.assertTrue(detail.startswith("full:"), detail)

    def test_similar_articles_attach_without_a_call(self):
        self._seed_full_pass()
        for k in range(4):
            self.add_article(
                "n%d" % k, "Reserve Bank of India adds lending rule update %d" % k,
                "HDFC Bank and other banks adjust to India lending norms.",
                ents=["Reserve Bank of India", "HDFC Bank"])
        llm.usage["calls"] = 0
        agents.TrendLinker().run(self.con)
        self.assertEqual(llm.usage["calls"], 0)

    def test_attachment_grows_a_trend_instead_of_replacing_it(self):
        """The failure this guards: an incremental run derives from a DELTA, so
        writing its member list straight over the stored one would shrink a
        running trend to the last few hours of evidence."""
        self._seed_full_pass()
        before = max(len(db.uj(r["article_ids"], [])) for r in self.con.execute(
            "SELECT article_ids FROM trends WHERE kind='macro'").fetchall())
        for k in range(4):
            self.add_article(
                "n%d" % k, "Reserve Bank of India adds lending rule update %d" % k,
                "HDFC Bank and other banks adjust to India lending norms.",
                ents=["Reserve Bank of India", "HDFC Bank"])
        agents.TrendLinker().run(self.con)
        after = max(len(db.uj(r["article_ids"], [])) for r in self.con.execute(
            "SELECT article_ids FROM trends WHERE kind='macro'").fetchall())
        self.assertGreater(after, before)

    def test_unrelated_articles_do_reach_the_llm(self):
        """Attachment must not swallow genuinely new material — that would be a
        saving bought with content."""
        self._seed_full_pass()
        for k in range(5):
            self.add_article(
                "u%d" % k, "Volcano erupts in Iceland region %d" % k,
                "Reykjavik authorities evacuate Grindavik as lava flows.",
                topic="world", ents=["Iceland Met Office", "Grindavik"],
                sectors=("energy",), regions=("Iceland",))
        llm.usage["calls"] = 0
        agents.TrendLinker().run(self.con)
        self.assertGreaterEqual(llm.usage["calls"], 1)

    def test_incremental_never_retires(self):
        """An incremental pass never looked at the full window, so 'absent from
        the fresh set' carries no information about what is still live."""
        self._seed_full_pass()
        live = self.con.execute("SELECT COUNT(*) c FROM trends "
                                "WHERE retired_at IS NULL").fetchone()["c"]
        for k in range(5):
            self.add_article("u%d" % k, "Volcano erupts in Iceland %d" % k,
                             "Reykjavik evacuates Grindavik.", topic="world",
                             ents=["Iceland Met Office", "Grindavik"])
        agents.TrendLinker().run(self.con)
        still = self.con.execute("SELECT COUNT(*) c FROM trends "
                                 "WHERE retired_at IS NULL").fetchone()["c"]
        self.assertGreaterEqual(still, live)

    def test_full_pass_returns_when_due(self):
        self._seed_full_pass()
        config.TRENDS_FULL_PASS_HOURS = 0
        agents.TrendLinker().run(self.con)
        detail = self.con.execute(
            "SELECT detail FROM runs WHERE stage='trends' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()["detail"]
        self.assertTrue(detail.startswith("full:"), detail)

    def test_a_barren_full_pass_still_counts_as_one(self):
        """Otherwise a full pass that produced nothing usable would be re-run
        every cycle — re-paying for the expensive path because it found nothing."""
        self.add_article("a0", "One lonely item", "Nothing to trend on.")
        agents.TrendLinker().run(self.con)
        self.assertFalse(agents.TrendLinker()._due_for_full_pass(self.con))


class _patched:
    """Minimal attribute patcher — the suite has no external test deps."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


if __name__ == "__main__":
    unittest.main()
