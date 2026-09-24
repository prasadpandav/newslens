"""The orbit: rings, words and hidden links. Pure functions, no database.

    cd backend && python -m unittest tests.test_orbit -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import orbit  # noqa: E402

MEERA = {
    "profession": "Pharmacy owner",
    "line_of_business": "Retail pharmacy",
    "location": {"city": "Pune", "region": "", "country": "India"},
    "interests": ["finance", "education"],
    "micro": {"supply dependencies": "My distributors carry imported generics",
              "commute": "Drives 40 minutes daily through Hinjewadi"},
}


def story(sid, headline, narrative="", topic="world", **kw):
    return dict(id=sid, headline=headline, narrative=narrative, topic=topic,
                impact_score=0, impact_text="", **kw)


class WordTest(unittest.TestCase):
    def test_llm_word_is_validated_not_repaired(self):
        self.assertEqual(orbit.clean_word("monsoon"), "Monsoon")
        self.assertEqual(orbit.clean_word(" Rates. "), "Rates")
        self.assertEqual(orbit.clean_word("RBI"), "RBI")
        for bad in ("two words", "", None, 42, "x", "the", "a" * 20):
            self.assertIsNone(orbit.clean_word(bad), bad)

    def test_fallback_prefers_a_name_mid_sentence(self):
        self.assertEqual(orbit.fallback_word(
            "Metro trials close part of the Hinjewadi road until Wednesday"), "Hinjewadi")

    def test_fallback_skips_reporting_verbs(self):
        w = orbit.fallback_word("Curbs proposed on imported generic-drug ingredients")
        self.assertNotIn(w.lower(), ("proposed", "on"))

    def test_fallback_never_returns_empty(self):
        self.assertEqual(orbit.fallback_word("", topic="business"), "Business")
        self.assertEqual(orbit.fallback_word("The", sectors=["banking"]), "Banking")


class LensTest(unittest.TestCase):
    def test_facets_carry_the_readers_own_words(self):
        facets = orbit.lens_facets(MEERA)
        kinds = [f["kind"] for f in facets]
        self.assertIn("profession", kinds)
        self.assertIn("micro", kinds)
        self.assertIn("city", kinds)
        prof = next(f for f in facets if f["kind"] == "profession")
        # "owner" matches nothing useful; "pharmacy" is the lens.
        self.assertEqual(prof["terms"], ["pharmacy"])
        # Country is deliberately not a facet — it would match nearly everything.
        self.assertFalse(any(f["label"] == "India" for f in facets))

    def test_empty_lens(self):
        self.assertEqual(orbit.lens_facets({}), [])
        self.assertEqual(orbit.lens_label({}), "")
        self.assertEqual(orbit.lens_label(MEERA), "Pharmacy owner, Pune")


class RingTest(unittest.TestCase):
    def setUp(self):
        self.facets = orbit.lens_facets(MEERA)

    def ring(self, s):
        return orbit.classify(s, self.facets)[0]

    def test_micro_detail_is_a_direct_hit(self):
        s = story("s1", "Curbs proposed on imported generic-drug ingredients",
                  "The ministry wants limits on imported active ingredients for generics.")
        ring, lenses, matched = orbit.classify(s, self.facets)
        self.assertEqual(ring, "direct")
        self.assertIn("work", lenses)
        exp = orbit.exposure(matched, ring)
        self.assertTrue(exp["text"].startswith("You told Descry"))
        self.assertIn("generics", exp["text"])

    def test_city_is_near(self):
        s = story("s2", "Pune civic body approves new water tariff", topic="local")
        ring, lenses, _ = orbit.classify(s, self.facets)
        self.assertEqual(ring, "near")
        self.assertIn("city", lenses)

    def test_place_counts_even_when_the_text_does_not_name_it(self):
        s = story("s3", "Road closed for metro work", topic="local", place="Pune")
        self.assertEqual(self.ring(s), "near")

    def test_one_common_word_does_not_match_a_long_micro_value(self):
        # "daily" alone is in the commute detail; it must not pull this inward.
        s = story("s4", "Daily briefing on global shipping")
        self.assertEqual(self.ring(s), "wider")

    def test_cached_impact_score_wins(self):
        s = story("s5", "Something unrelated to the lens")
        s["impact_score"] = 3
        self.assertEqual(self.ring(s), "direct")

    def test_no_lens_means_everything_is_wider(self):
        s = story("s6", "Pune pharmacy news")
        self.assertEqual(orbit.classify(s, [])[0], "wider")


class BuildTest(unittest.TestCase):
    def test_rings_caps_nodes_and_links(self):
        items = [
            story("a", "Curbs proposed on imported generic-drug ingredients",
                  "Limits on imported generics."),
            story("b", "Red Sea freight rates jump again", "Shipping costs rise."),
            story("c", "Pune metro closes Hinjewadi road", topic="local"),
        ] + [story(f"w{i}", f"Unrelated headline number {i} about Zanzibar{i}")
             for i in range(10)]
        meta = {
            "a": {"orbit_word": "Generics", "article_ids": ["x1"], "connection_ids": ["c1"]},
            "b": {"orbit_word": "Freight", "article_ids": ["x2"], "connection_ids": ["c1"]},
            "c": {"orbit_word": "Metro", "article_ids": ["x3"], "connection_ids": []},
        }
        conns = {"c1": {"article_a": "x1", "article_b": "x2", "chain": "Freight → costs",
                        "confidence": 0.65, "titles": {"x1": "t1", "x2": "t2"}}}
        out = orbit.build(items, MEERA, meta, conns)
        rings = {n["word"]: n["ring"] for n in out["nodes"]}
        self.assertEqual(rings["Generics"], "direct")
        self.assertEqual(rings["Metro"], "near")
        self.assertLessEqual(sum(1 for n in out["nodes"] if n["ring"] == "wider"),
                             orbit.RING_CAPS["wider"])
        self.assertTrue(out["lens"]["set"])
        # The freight story is on the wider ring but linked to Generics.
        self.assertEqual(len(out["links"]), 1)
        hl = out["stories"]["a"]["hidden_links"][0]
        self.assertEqual(hl["story_id"], "b")
        self.assertEqual(hl["word"], "Freight")
        self.assertEqual(hl["confidence_label"], "medium")
        self.assertIsNone(out["stories"]["b"]["exposure"])

    def test_a_full_ring_spills_outward_instead_of_dropping(self):
        items = [story(f"d{i}", f"Pune pharmacy story {i}", "Pune pharmacy news.")
                 for i in range(orbit.RING_CAPS["direct"] + 1)]
        meta = {it["id"]: {"orbit_word": f"Word{i}"} for i, it in enumerate(items)}
        out = orbit.build(items, MEERA, meta, {})
        self.assertEqual(len(out["nodes"]), len(items))
        spilled = out["nodes"][-1]
        self.assertEqual(spilled["ring"], "near")
        # The card still tells the truth about why it reaches the reader.
        self.assertEqual(out["stories"][spilled["story_ids"][0]]["ring"], "direct")

    def test_shared_word_shares_a_node(self):
        items = [story("a", "Monsoon arrives early"), story("b", "Monsoon floods roads")]
        meta = {"a": {"orbit_word": "Monsoon"}, "b": {"orbit_word": "monsoon"}}
        out = orbit.build(items, {}, meta, {})
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["nodes"][0]["story_ids"], ["a", "b"])

    def test_hidden_stories_are_left_out(self):
        items = [story("a", "Monsoon arrives early")]
        out = orbit.build(items, {}, {}, {}, hidden={"a"})
        self.assertEqual(out["nodes"], [])


if __name__ == "__main__":
    unittest.main()
