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

from app import analytics, config, db, gazetteer, llm, llmcache, llmcost  # noqa: E402
from app import agents, main                                       # noqa: E402


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


class ReportWindowTest(_DBCase):
    """Every "last N days" report summed N+1 calendar days: the SQL window used
    `now - days*86400` while the daily chart beside it used `(days - 1)`. The
    oldest day was counted in the total and never plotted. Harmless-looking at
    30, but it DOUBLES a 24-hour view — which is what made it worth fixing
    before adding one."""

    def _spend_on(self, day_key, usd_tokens=1_000_000):
        config.LLM_PRICE_DEFAULTS = {"p/m": [1.0, 0.0, 0.0]}
        llmcost.record("p", "m", "t", prompt_tokens=usd_tokens,
                       total_tokens=usd_tokens, con=self.con)
        self.con.execute("UPDATE llm_usage SET day = ? WHERE day = ?",
                         (day_key, analytics.day_key()))
        self.con.commit()

    def test_window_of_one_day_is_today_only(self):
        _, start = analytics.window_start(1)
        self.assertEqual(start, analytics.day_key())

    def test_window_reaches_back_days_minus_one(self):
        for days in (7, 30, 90):
            _, start = analytics.window_start(days)
            self.assertEqual(start,
                             analytics.day_key(db.now() - (days - 1) * 86400),
                             "a %d-day window must span exactly %d days" % (days, days))

    def test_yesterdays_spend_is_excluded_from_a_one_day_report(self):
        self._spend_on(analytics.day_key(db.now() - 86400))     # yesterday
        rep = llmcost.report(self.con, days=1)
        self.assertEqual(rep["cost_usd"], 0.0,
                         "a 24-hour report must not include yesterday")

    def test_todays_spend_is_included_in_a_one_day_report(self):
        self._spend_on(analytics.day_key())                      # today
        rep = llmcost.report(self.con, days=1)
        self.assertEqual(rep["cost_usd"], 1.0)
        self.assertEqual(len(rep["daily"]), 1, "one bucket for a one-day window")

    def test_totals_and_chart_cover_the_same_span(self):
        """The actual defect: chart and total disagreed by one day."""
        for back in range(9):
            self._spend_on(analytics.day_key(db.now() - back * 86400))
        rep = llmcost.report(self.con, days=7)
        self.assertEqual(len(rep["daily"]), 7)
        self.assertEqual(rep["cost_usd"], 7.0,
                         "7 days of $1/day must total $7, not $8")


class FinanceChainingTest(_DBCase):
    """The finance pipeline now runs at the end of a clean news run instead of
    on a clock of its own. Three things must hold, and each has a way of failing
    silently:

      * a FAILED news run must not be followed by a finance run;
      * a single-stage admin trigger must not drag a full finance run with it;
      * a skip must be RECORDED — a gate that blocks quietly is exactly the
        invisibility this whole endpoint was added to fix.
    """

    KNOBS = _DBCase.KNOBS + ("FINANCE_AFTER_PIPELINE", "FINANCE_REQUIRED_STAGES",
                             "FINANCE_TOPICS", "FINANCE_IN_PROCESS",
                             "PIPELINE_INTERVAL_HOURS")

    def setUp(self):
        super().setUp()
        config.FINANCE_AFTER_PIPELINE = True
        config.FINANCE_REQUIRED_STAGES = []
        config.FINANCE_TOPICS = ["finance", "business"]

    def test_clean_run_is_not_blocked(self):
        from app import main
        self.assertEqual(main._failed_stages({"scout": 10, "stories": 4}), [])

    def test_failed_stage_blocks(self):
        from app import main
        blocked = main._failed_stages({"scout": 10, "entities": "error: boom"})
        self.assertEqual(blocked, ["entities"])

    def test_required_stages_narrows_the_gate(self):
        """A failure in `signals` cannot affect what finance reads, so gating on
        the real dependency has to be possible."""
        from app import main
        config.FINANCE_REQUIRED_STAGES = ["scout", "dedupe"]
        results = {"scout": 10, "dedupe": 3, "signals": "error: boom"}
        self.assertEqual(main._failed_stages(results), [])
        results["dedupe"] = "error: nope"
        self.assertEqual(main._failed_stages(results), ["dedupe"])

    def test_a_skip_is_written_to_runs(self):
        from app import main
        out = main._chain_finance({"entities": "error: boom"})
        self.assertIn("skipped", out)
        row = self.con.execute(
            "SELECT status, detail FROM runs WHERE stage='finance_pipeline' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn("entities", row["detail"])

    def test_health_reports_the_chained_runner_and_news_cadence(self):
        """FINANCE_INTERVAL_HOURS is dead while chaining is on; staleness judged
        against it would call a healthy pipeline late every cycle."""
        from app.finance.orchestrator import health
        config.PIPELINE_INTERVAL_HOURS = 6.0
        config.FINANCE_INTERVAL_HOURS = 3.0
        h = health(self.con)
        self.assertTrue(h["scheduling"]["chained_after_news"])
        self.assertEqual(h["scheduling"]["interval_hours"], 6.0)
        self.assertIn("chained", h["scheduling"]["runner"])

    def test_health_flags_a_gated_run(self):
        from app import main
        from app.finance.orchestrator import health
        main._chain_finance({"stories": "error: boom"})
        self.assertEqual(health(self.con)["verdict"], "gated")


class FinanceFeedMergeTest(_DBCase):
    """Finance-pipeline stories share the feed and the topic chips with ordinary
    coverage. Both pipelines read the SAME articles, so the risk this guards is
    the reader seeing one event told twice — once plainly, once enriched."""

    KNOBS = _DBCase.KNOBS + ("FINANCE_TOPICS",)

    def setUp(self):
        super().setUp()
        config.FINANCE_TOPICS = ["finance", "business"]
        self.con.execute("INSERT INTO users (id, token, created_at) VALUES (?,?,?)",
                         ("u1", "t", db.now()))
        self.con.commit()

    def _news(self, sid, event_id, headline="Plain telling"):
        self.con.execute(
            "INSERT INTO stories (id,event_id,headline,narrative,credibility,topic,"
            "article_ids,trend_ids,claims,merge_stats,created_at,updated_at) "
            "VALUES (?,?,?,'n',50,'finance','[\"a1\"]','[]','{}','{}',?,?)",
            (sid, event_id, headline, db.now(), db.now()))
        self.con.commit()

    def _fin(self, sid, event_id, headline="Enriched telling", metrics="[]"):
        self.con.execute(
            "INSERT INTO fin_stories (id,event_id,headline,narrative,credibility,topic,"
            "article_ids,claims,merge_stats,metrics,sectors,tickers,event_type,"
            "created_at,updated_at) VALUES (?,?,?,'n',70,'finance','[\"a1\"]','{}','{}',"
            "?,'[]','[]','earnings',?,?)",
            (sid, event_id, headline, metrics, db.now(), db.now()))
        self.con.commit()

    def _feed(self):
        from app import main
        return main.feed(user_id="u1", authorization="Bearer t")["items"]

    def test_the_enriched_telling_replaces_its_plain_twin(self):
        self._news("s1", "E1")
        self._fin("f1", "E1")
        self.assertEqual({i["id"] for i in self._feed()}, {"f1"})

    def test_a_story_only_the_news_pipeline_told_survives(self):
        self._news("s2", "E2")
        self._fin("f1", "E1")
        self.assertEqual({i["id"] for i in self._feed()}, {"s2", "f1"})

    def test_finance_items_are_marked_and_carry_their_figure_count(self):
        self._fin("f1", "E1", metrics='[{"name":"revenue"},{"name":"pat"}]')
        item = self._feed()[0]
        self.assertEqual(item["kind"], "finance")
        self.assertEqual(item["metric_count"], 2)
        self.assertEqual(item["topic"], "finance")   # lands under the chip

    def test_event_id_never_reaches_the_wire(self):
        """A join key, not part of the API shape."""
        self._news("s1", "E1")
        self._fin("f1", "E2")
        for item in self._feed():
            self.assertNotIn("event_id", item)

    def test_finance_disabled_leaves_the_feed_untouched(self):
        config.FINANCE_TOPICS = []
        self._news("s1", "E1")
        self._fin("f1", "E1")
        self.assertEqual({i["id"] for i in self._feed()}, {"s1"})

    def test_story_detail_serves_a_finance_id_as_a_news_superset(self):
        """The card links to /story/{id}, so that route has to answer for both
        id spaces or every finance tap 404s."""
        from app import main
        self._fin("f1", "E1", metrics='[{"name":"revenue"}]')
        d = main.story(story_id="f1", user_id="u1", authorization="Bearer t")
        self.assertEqual(d["kind"], "finance")
        for key in ("headline", "narrative", "why_matters", "credibility", "claims",
                    "topic", "framing", "sources", "trends", "connections",
                    "impact_text", "impact_score", "created_at"):
            self.assertIn(key, d, "missing news-shape key %r" % key)
        self.assertEqual([m["name"] for m in d["metrics"]], ["revenue"])

    def test_unknown_id_still_404s(self):
        from app import main
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            main.story(story_id="nope", user_id="u1", authorization="Bearer t")


class StoryTopicTest(_DBCase):
    """A story's topic decides which feed chip it filters under. It used to be
    `arts[0]["topic"]` — the topic of whichever article sorted first, and the
    list is sorted by `uuid4().hex[:12]`, so "first" was a coin toss.

    Single-feed stories were right by luck. A cross-beat merge — the thing the
    Deduper exists to create — got an arbitrary label, so filtering by Finance
    returned stories about anything. Reported as "only the top story is relevant
    to the topic"."""

    def _arts(self, topics):
        """Article rows carrying the given topics, in the order given."""
        rows = []
        for i, t in enumerate(topics):
            aid = "a%d" % i
            self.add_article(aid, "t%d" % i, "s", topic=t)
            rows.append(self.con.execute(
                "SELECT * FROM articles WHERE id=?", (aid,)).fetchone())
        return rows

    def test_majority_beat_wins(self):
        arts = self._arts(["world", "finance", "finance"])
        self.assertEqual(agents._topic_of(arts), "finance")

    def test_order_does_not_decide(self):
        """The actual defect: the same set of articles, shuffled, must give the
        same answer. Under the old rule each ordering gave a different topic."""
        base = self._arts(["world", "finance", "finance"])
        for order in ([0, 1, 2], [2, 1, 0], [1, 2, 0], [1, 0, 2]):
            self.assertEqual(agents._topic_of([base[i] for i in order]), "finance")

    def test_single_topic_is_unchanged(self):
        """The case that always worked keeps working."""
        self.assertEqual(agents._topic_of(self._arts(["finance"] * 3)), "finance")

    def test_no_topic_anywhere_returns_empty(self):
        self.assertEqual(agents._topic_of(self._arts(["", ""])), "")

    def test_backfill_repairs_stored_rows(self):
        from app import main
        for i, t in enumerate(["world", "finance", "finance"]):
            self.add_article("a%d" % i, "t", "s", topic=t)
        self.con.execute(
            "INSERT INTO stories (id,headline,narrative,credibility,topic,article_ids,"
            "trend_ids,claims,merge_stats,created_at,updated_at) VALUES "
            "('s1','H','n',50,'world','[\"a0\",\"a1\",\"a2\"]','[]','{}','{}',?,?)",
            (db.now(), db.now()))
        self.con.commit()
        out = main.admin_fix_topics(token=config.ADMIN_TOKEN or "x",
                                    authorization="")
        self.assertEqual(out["changed"], 1)
        self.assertEqual(out["examples"][0]["to"], "finance")
        self.assertEqual(self.con.execute(
            "SELECT topic FROM stories WHERE id='s1'").fetchone()["topic"], "finance")


class WrongShapeTest(_DBCase):
    """Production: `fin_stories` died every run with "'list' object has no
    attribute 'get'".

    `_extract_json` matches `\\{.*\\}|\\[.*\\]`, so a model answering with a bare
    array parsed fine and was handed to a caller doing `out.get(...)`. Because
    the orchestrator catches per STAGE, one malformed answer took the whole
    stage down. And the answer had already been written to llm_cache, so every
    later run replayed the crash straight from cache without calling a provider
    that might have answered correctly.
    """

    def setUp(self):
        super().setUp()
        config.LLM_PROVIDER = "openai"      # not mock: exercise the real path
        config.LLM_CACHE_TTL_SECONDS = 3600
        self.calls = []

    def _answers(self, *texts):
        """Stub _call, returning each text in turn."""
        seq = list(texts)

        def fake(provider, prompt, task):
            self.calls.append(task)
            return seq.pop(0) if seq else texts[-1]
        return _patched(llm, "_call", fake)

    def test_a_bare_array_is_rejected_not_handed_to_the_caller(self):
        with self._answers('[{"a": 1}, {"b": 2}]'):
            self.assertIsNone(llm.complete_json("fin_extract", "p"))

    def test_a_rejected_answer_is_never_cached(self):
        """The half that made it permanent."""
        with self._answers('[{"a": 1}, {"b": 2}]'):
            llm.complete_json("fin_extract", "p")
        self.assertIsNone(llmcache.get("fin_extract", "p"))

    def test_it_retries_and_takes_the_corrected_answer(self):
        with self._answers('[1, 2]', '{"event_type": "earnings"}'):
            out = llm.complete_json("fin_extract", "p")
        self.assertEqual(out, {"event_type": "earnings"})
        self.assertEqual(llmcache.get("fin_extract", "p"), {"event_type": "earnings"})

    def test_a_single_element_wrapper_is_unwrapped_not_thrown_away(self):
        """The commonest model tic. There is one candidate and no ambiguity
        about which object was meant, so recovering it beats a retry."""
        with self._answers('[{"event_type": "m_and_a"}]'):
            out = llm.complete_json("fin_extract", "p")
        self.assertEqual(out, {"event_type": "m_and_a"})
        self.assertEqual(llm.usage["shape_recovered"], 1)

    def test_a_poisoned_cache_row_is_dropped_on_read(self):
        """Rows written before this validation existed are still in production
        holding the exact answers that were crashing stages."""
        llmcache.put("fin_extract", "p", [{"a": 1}, {"b": 2}])
        with self._answers('{"event_type": "other"}'):
            out = llm.complete_json("fin_extract", "p")
        self.assertEqual(out, {"event_type": "other"},
                         "a poisoned row must not be served")
        self.assertEqual(llmcache.get("fin_extract", "p"), {"event_type": "other"},
                         "and must be replaced, not just skipped")

    def test_callers_that_tolerate_a_bare_array_still_get_one(self):
        """entities_batch and trend go through agents._items_of, which is
        written to accept either shape. Enforcing objects on them would break
        the very answers _items_of exists to handle."""
        with self._answers('[{"i": 0, "entities": ["X"]}]'):
            out = llm.complete_json("entities_batch", "p", want="any")
        self.assertEqual(out, [{"i": 0, "entities": ["X"]}])

    def test_the_finance_extract_call_site_enforces_objects(self):
        """The specific call that was crashing. Left at the default `want`, a
        bare array reaches _tables() and raises AttributeError."""
        from app.finance import agents as fin_agents
        self.assertIn("want", llm.complete_json.__doc__ or "")
        with self._answers('["not", "an", "object"]'):
            self.assertIsNone(
                llm.complete_json("fin_extract", "p"),
                "fin_extract must not return a list to _tables()")
        self.assertTrue(hasattr(fin_agents, "FinancialStoryAgent"))


class StarvationDiagnosticTest(_DBCase):
    """Production logged `gave_up · all · fin_forecast — providers [...] all
    unavailable or benched` with no indication of WHY, or of whether the money
    or the clock was the constraint. Those need opposite fixes."""

    def setUp(self):
        super().setUp()
        config.LLM_PROVIDER = "openai"

    def test_the_reason_each_provider_was_skipped_is_recorded(self):
        llm._bench("openai", 900)
        with _patched(llm, "_call", lambda *a, **k: '{"a": 1}'):
            self.assertIsNone(llm.complete_json("story", "p"))
        note = llm.recent_errors[0]
        self.assertEqual(note["kind"], "gave_up")
        self.assertIn("openai", note["detail"])
        self.assertIn("benched", note["detail"])
        self.assertIn("tier=", note["detail"],
                      "the tier decides the provider order, so it belongs in "
                      "the line that says no provider was reachable")

    def test_a_real_error_still_wins_over_the_skip_summary(self):
        """When a provider WAS attempted and failed, its error is the useful
        message — the skip summary would bury it."""
        def boom(*a, **k):
            raise RuntimeError("upstream exploded")
        with _patched(llm, "_call", boom):
            self.assertIsNone(llm.complete_json("story", "p"))
        self.assertIn("upstream exploded", llm.recent_errors[0]["detail"])


class StarvationGateTest(_DBCase):
    """A fully benched pipeline used to grind every item into an identical
    failure and file an `error` row, which reads like a bug in the stage. The
    honest report is "no provider was reachable", and the honest action is to
    stop and let the next run pick the work up.

    Waiting it out is not an option worth building: the first bench is 15
    minutes and escalates to six hours, against a pipeline that runs every six.
    """

    def setUp(self):
        super().setUp()
        config.LLM_PROVIDER = "openai"

    def test_availability_reports_ready_when_nothing_is_benched(self):
        got = llm.availability("fin_trend")
        self.assertTrue(got["ready"])
        self.assertEqual(got["wait_seconds"], 0.0)

    def test_availability_reports_the_soonest_bench_expiry(self):
        llm._bench("openai", 600)
        got = llm.availability("fin_trend")
        self.assertFalse(got["ready"])
        self.assertAlmostEqual(got["wait_seconds"], 600, delta=5)
        self.assertIn("benched", got["detail"])

    def test_waiting_cannot_help_when_the_budget_is_the_constraint(self):
        """None, not a number: a bench expires on its own, an exhausted budget
        does not. They need opposite responses."""
        config.LLM_PRICE_DEFAULTS = {"openai/m": [1.0, 1.0, 1.0]}
        config.LLM_DAILY_BUDGET_USD = 0.01
        config.FREE_PROVIDERS = set()
        llmcost.record("openai", "m", "story", prompt_tokens=1_000_000,
                       total_tokens=1_000_000, con=self.con)
        llmcost.invalidate_today()
        with _patched(llm, "_model_for", lambda p, t: "m"):
            got = llm.availability("story")
        self.assertFalse(got["ready"])
        self.assertIsNone(got["wait_seconds"])

    def test_a_starved_stage_is_skipped_not_failed(self):
        from app.finance import orchestrator
        llm._bench("openai", 1800)
        results = orchestrator.run_finance_pipeline(stage="fin_trends")
        self.assertIn("skipped", results["fin_trends"])
        r = self.con.execute(
            "SELECT status, detail FROM runs WHERE stage='fin_trends' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(r["status"], "skipped",
                         "a stage with no provider to call did not fail — it "
                         "never started")

    def test_health_calls_starvation_by_its_name(self):
        from app.finance.orchestrator import health
        db.log_run(self.con, "finance_pipeline", "done", "{}")
        db.log_run(self.con, "fin_trends", "skipped", "all benched — openai: 25m")
        self.con.commit()
        out = health(self.con)
        self.assertEqual(out["verdict"], "starved")
        self.assertTrue(any("no LLM provider was reachable" in p
                            for p in out["problems"]))
        self.assertTrue(any("402" in p for p in out["problems"]),
                        "the problem text should say how to tell a quota "
                        "exhaustion from an empty account")

    def test_fin_causal_is_not_gated_because_it_needs_no_provider(self):
        """Its structure is a graph walk; only the prose is an LLM call. Gating
        it would stop the one stage that still does useful work while benched."""
        from app.finance import orchestrator
        llm._bench("openai", 1800)
        results = orchestrator.run_finance_pipeline(stage="fin_causal")
        self.assertNotIn("skipped", str(results["fin_causal"]))


class DeadModelTest(_DBCase):
    """Production, 2026-08-17: Groq retired `llama-3.3-70b-versatile` and
    answered every call with a 404. It fell through to the generic error
    handler, so nothing recorded that the model was gone and the next call
    tried it again — one wasted round trip per pacing interval, indefinitely.

    A retired model is permanent in a way a 429 or a 402 is not: no amount of
    waiting brings it back, and only a config change fixes it.
    """

    def setUp(self):
        super().setUp()
        config.LLM_PROVIDER = "groq"
        # GROQ_API_KEY is not one of _DBCase's restored knobs, so save it here
        # rather than leaving a fake key behind for every later test.
        self._key, config.GROQ_API_KEY = config.GROQ_API_KEY, "test-key"
        # Pacing would sleep 2.1s per retry; these tests are about routing, not
        # rate limits, and a 10-second suite tax buys nothing.
        self._interval = llm.MIN_INTERVAL.get(("groq", None))
        llm.MIN_INTERVAL[("groq", None)] = 0.0
        llm._dead_models.clear()

    def tearDown(self):
        llm._dead_models.clear()
        config.GROQ_API_KEY = self._key
        if self._interval is None:
            llm.MIN_INTERVAL.pop(("groq", None), None)
        else:
            llm.MIN_INTERVAL[("groq", None)] = self._interval
        super().tearDown()

    class _Resp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def json(self):
            return {}

    def _404(self):
        body = ('{"error":{"message":"The model `llama-3.3-70b-versatile` does '
                'not exist or you do not have access to it.",'
                '"type":"invalid_request_error","code":"model_not_found"}}')
        return _patched(llm.httpx, "post",
                        lambda *a, **k: self._Resp(404, body))

    def test_a_retired_model_is_recorded_not_just_logged(self):
        with self._404():
            self.assertIsNone(llm.complete_json("entities", "p"))
        self.assertIn(("groq", config.GROQ_CHEAP_MODEL), llm._dead_models)
        self.assertIn("groq/", " ".join(llm.dead_models()))

    def test_it_is_not_attempted_again(self):
        """The whole point. The second call must cost no request at all."""
        with self._404():
            llm.complete_json("entities", "p")
        calls = {"n": 0}

        def counting(*a, **k):
            calls["n"] += 1
            return self._Resp(404, "model not found")

        with _patched(llm.httpx, "post", counting):
            self.assertIsNone(llm.complete_json("entities", "p2"))
        self.assertEqual(calls["n"], 0,
                         "a model the provider has retired must never be "
                         "requested again")

    def test_the_provider_is_not_benched_for_it(self):
        """The account is fine — only the model name is wrong. Benching groq
        would also take out any tier pointing at a model that still exists."""
        with self._404():
            llm.complete_json("entities", "p")
        self.assertNotIn("groq", llm._benched_until)

    def test_the_reason_reaches_the_stage_log(self):
        with self._404():
            llm.complete_json("entities", "p")
        why = llm.why_failed()
        self.assertIn("no longer exists", why)
        self.assertIn("GROQ_MODEL", why)

    def test_a_404_that_is_not_about_a_model_is_not_treated_as_permanent(self):
        with _patched(llm.httpx, "post",
                      lambda *a, **k: self._Resp(404, "endpoint not found")):
            self.assertIsNone(llm.complete_json("entities", "p"))
        self.assertEqual(llm._dead_models, {},
                         "only a model-not-found may disable a model")
