"""AI coverage filed under `ai`, without losing it for Technology followers.

    cd backend && python -m unittest tests.test_topics -v

Production on 2026-10-01: 25 AI stories, 2 filed under ai, so there was no AI
chip. The headlines below are real ones from that day.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agents, db, topics                                   # noqa: E402
from tests.test_costcontrol import _DBCase                          # noqa: E402
from tests.test_openapi import _insert                              # noqa: E402

AI = [
    "Meta Debuts Muse, a New AI Agent That Handles Everything From Emails to Plane Tickets",
    "Seattle Times and Newsday Sue OpenAI and Microsoft Over Copyright Infringement",
    "Can a New Chatbot Finally Decode Tattered Ancient Greek Records?",
    "How Three Researchers Used Rival Anthropic's Claude to Breach OpenAI in Under 72 Hours",
    "OpenAI's GPT-6 Astra Cuts Hallucinations and Blocks Most Direct Attacks",
    "Disney Hires Former Character.AI CEO as Its First-Ever CTO",
]
NOT_AI = [
    "Microsoft's Next Surface Mouse Brings Haptic Feedback and Copilot Shortcuts",
    "Apple Enters the Fold With $2,000 iPhone Duo Alongside iPhone 18 Pro",
    "XPENG's Iron Humanoid Robot Walks Off Assembly Line Ready for Work",
    "Supercomputer Simulations Reveal Secret Behind Webb's Little Red Dots",
    "Ai Weiwei Opens a New Exhibition in Berlin",       # a surname, not the acronym
    "Rain Brings Relief to Chennai",
]


class ClassifierTest(unittest.TestCase):
    def test_ai_titles(self):
        for t in AI:
            self.assertTrue(topics.is_ai(t), t)

    def test_not_ai_titles(self):
        for t in NOT_AI:
            self.assertFalse(topics.is_ai(t), t)

    def test_only_technology_and_science_are_refined(self):
        t = "SoftBank Taps Bond Markets for $11.1 Billion to Fuel OpenAI Ambitions"
        self.assertEqual(topics.refine("technology", t), "ai")
        self.assertEqual(topics.refine("finance", t), "finance",
                         "an AI-spending story keeps its money beat")
        self.assertEqual(topics.refine("technology", NOT_AI[1]), "technology")

    def test_a_technology_follower_still_counts_ai_stories(self):
        self.assertTrue(topics.matches("ai", {"technology"}))
        self.assertTrue(topics.matches("ai", {"ai"}))
        self.assertFalse(topics.matches("technology", {"ai"}),
                         "following AI is not following all of technology")
        self.assertFalse(topics.matches("", {"technology"}))

    def test_personalization_relevance_uses_the_parent(self):
        story = {"headline": "New model released", "narrative": "Details.", "topic": "ai"}
        self.assertTrue(agents.personalization_relevant({"interests": ["Technology"]}, story))


class BackfillTest(_DBCase):
    def test_recent_ai_rows_are_refiled_once(self):
        now = time.time()
        for i, (title, topic) in enumerate([(AI[0], "technology"), (NOT_AI[1], "technology"),
                                            (AI[1], "business"), (AI[2], "science")]):
            _insert(self.con, "articles", {"id": f"a{i}", "title": title, "url": f"https://x/{i}",
                                           "source": "x.com", "topic": topic, "fetched_at": now})
            _insert(self.con, "stories", {
                "id": f"s{i}", "headline": title, "narrative": "n", "credibility": 50,
                "topic": topic, "claims": "{}", "article_ids": "[]", "trend_ids": "[]",
                "connection_ids": "[]", "created_at": now, "updated_at": now})
        self.con.commit()
        self.assertEqual(topics.backfill(self.con), (2, 2))
        got = dict(self.con.execute("SELECT id, topic FROM stories").fetchall())
        self.assertEqual(got, {"s0": "ai", "s1": "technology", "s2": "business", "s3": "ai"})
        self.assertEqual(topics.backfill(self.con), (0, 0), "idempotent")


if __name__ == "__main__":
    unittest.main()
