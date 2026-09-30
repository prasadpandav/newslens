"""Server-rendered pages and sitemaps, the parts of the site search engines read.

    cd backend && python -m unittest tests.test_seo_pages -v

Each test is written against a way the page could fail in search:

  * a page with the home page's title, or two titles or canonicals;
  * a canonical or sitemap entry that points at a redirecting host;
  * a missing story answered with 200 (a "soft 404");
  * the API host's blanket noindex header leaking onto the real article;
  * structured data that a headline can break out of.
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_PROVIDER", "mock")

from fastapi.testclient import TestClient                           # noqa: E402

from app import config, db, pages                                   # noqa: E402
from tests.test_openapi import _insert                              # noqa: E402

# Just enough of web/index.html for assemble() to work on: the tags it must
# replace, and the empty #view it fills.
SHELL = """<!DOCTYPE html>
<html lang="en">

<head>
  <meta charset="UTF-8">
  <title>Descry — Understand the news, not just read it</title>
  <meta name="description"
    content="Descry helps you understand the news.">
  <meta property="og:type" content="website">
  <meta property="og:title" content="Descry — Understand the news, not just read it">
  <meta property="og:image" content="https://descry.in/og.png">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="/icon.svg">
</head>
<body>
  <main id="view"></main>
  <script>/* app */</script>
</body>
</html>"""


def head_of(doc):
    return doc[:doc.index("</head>")]


class PagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = config.DB_PATH = os.path.join(cls.tmp, "seo.db")
        db._schema_ready = False
        cls._base = config.WEB_BASE_URL
        config.WEB_BASE_URL = "https://descry.in"
        from app import main
        cls.client = TestClient(main.app)
        now = time.time()
        con = db.connect()
        _insert(con, "articles", {
            "id": "a1", "title": "Bank raises rates", "url": "https://x.test/1",
            "source": "reuters.com", "topic": "business", "fetched_at": now})
        for sid, head, created in (
                ("fresh1", 'Bank raises rates </script><b>"x"</b>', now - 3600),
                ("old1", "Last week's story", now - 5 * 86400)):
            _insert(con, "stories", {
                "id": sid, "headline": head,
                "narrative": "A bank raised rates.\nMarkets fell.",
                "why_matters": "Loans get dearer.", "credibility": 60.0,
                "credibility_note": "", "topic": "business", "claims": "{}",
                "article_ids": json.dumps(["a1"]), "trend_ids": "[]",
                "connection_ids": "[]", "image_url": "https://img.test/p.jpg",
                "created_at": created, "updated_at": created})
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        config.WEB_BASE_URL = cls._base

    def setUp(self):
        # Never reach the network for the shell in tests.
        pages._shell.update(html=SHELL, at=time.time())

    # -------------------------------------------------------------- stories
    def test_story_page_is_a_complete_indexable_article(self):
        r = self.client.get("/page/story/fresh1")
        self.assertEqual(r.status_code, 200)
        doc = r.text
        head = head_of(doc)
        self.assertEqual(head.count("<title>"), 1, "exactly one title")
        self.assertIn("Bank raises rates", re.search(r"<title>(.*?)</title>", head).group(1))
        self.assertEqual(len(re.findall(r'rel="canonical"', head)), 1)
        self.assertIn('<link rel="canonical" href="https://descry.in/story/fresh1">', head)
        self.assertEqual(len(re.findall(r'<meta name="description"', head)), 1)
        self.assertIn('content="Loans get dearer."', head)
        self.assertIn('name="robots" content="index,follow', head)
        self.assertEqual(len(re.findall(r'property="og:title"', head)), 1)
        self.assertNotIn("Understand the news, not just read it", head,
                         "the home page's title and og tags must be gone")
        # The article is in the HTML, in the element the app adopts.
        self.assertIn('<main id="view" data-ssr="/story/fresh1">', doc)
        self.assertEqual(doc.count("<h1>"), 1)
        self.assertIn("Markets fell.", doc)
        self.assertIn('href="https://x.test/1"', doc, "sources are linked")
        self.assertIn('alt="Bank raises rates', doc, "the photo has alt text")
        self.assertIn('href="/story/old1"', doc, "links onward to other stories")
        self.assertNotIn('href="/story/fresh1"', doc.split("ssr-more")[-1],
                         "does not list itself under More from Descry")
        self.assertIn("<script>/* app */</script>", doc, "the app still boots")

    def test_structured_data_is_valid_and_cannot_be_broken_out_of(self):
        doc = self.client.get("/page/story/fresh1").text
        self.assertNotIn("</script><b>", doc, "the headline must not close a tag")
        blob = re.search(r'<script type="application/ld\+json" id="ld-route">(.*?)</script>',
                         doc, re.S).group(1)
        data = json.loads(blob)
        kinds = [n["@type"] for n in data["@graph"]]
        self.assertEqual(kinds, ["NewsArticle", "BreadcrumbList"])
        art = data["@graph"][0]
        self.assertEqual(art["mainEntityOfPage"]["@id"], "https://descry.in/story/fresh1")
        self.assertEqual(art["image"], ["https://img.test/p.jpg"])
        self.assertTrue(art["datePublished"].endswith("+00:00"))
        self.assertEqual(art["author"]["@type"], "Organization")
        self.assertEqual(data["@graph"][1]["itemListElement"][-1]["item"],
                         "https://descry.in/story/fresh1")

    def test_missing_story_is_a_real_404_and_noindex(self):
        r = self.client.get("/page/story/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn('name="robots" content="noindex,follow"', head_of(r.text))
        self.assertIn('data-ssr="/story/nope"', r.text)

    def test_missing_trend_and_forecast_are_404(self):
        self.assertEqual(self.client.get("/page/trend/nope").status_code, 404)
        self.assertEqual(self.client.get("/page/signal/nope").status_code, 404)

    def test_pages_are_not_marked_noindex_by_the_api_host_header(self):
        r = self.client.get("/page/story/fresh1")
        self.assertNotIn("x-robots-tag", {k.lower() for k in r.headers})
        # ...while the API's own JSON still is.
        self.assertEqual(self.client.get("/story/fresh1").headers.get("x-robots-tag"),
                         "noindex")

    def test_page_is_compressed_when_the_client_accepts_it(self):
        r = self.client.get("/page/story/fresh1", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertIn("Accept-Encoding", r.headers.get("vary", ""))
        self.assertIn("Markets fell.", r.text)   # the client decoded it

    def test_without_a_shell_the_page_still_stands_alone(self):
        pages._shell.update(html=None, at=time.time())
        doc = self.client.get("/page/story/fresh1").text
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn('<link rel="canonical" href="https://descry.in/story/fresh1">', doc)
        self.assertIn("Markets fell.", doc)

    # ------------------------------------------------------------- sitemaps
    def test_sitemaps_list_the_live_host_only(self):
        for path in ("/sitemap.xml", "/news-sitemap.xml"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("x-robots-tag", {k.lower() for k in r.headers},
                             "Google will not read a noindex sitemap")
            locs = re.findall(r"<loc>(.*?)</loc>", r.text)
            self.assertTrue(locs)
            self.assertTrue(all(l.startswith("https://descry.in/") for l in locs), locs)

    def test_news_sitemap_holds_the_last_two_days_only(self):
        xml = self.client.get("/news-sitemap.xml").text
        self.assertIn("https://descry.in/story/fresh1", xml)
        self.assertNotIn("old1", xml)
        self.assertIn("<news:name>Descry</news:name>", xml)
        self.assertIn("&lt;/script&gt;", xml, "titles are escaped")


class HelpersTest(unittest.TestCase):
    def test_clip_cuts_at_a_word(self):
        self.assertEqual(pages.clip("one two three four", 12), "one two…")
        self.assertEqual(pages.clip("short", 50), "short")

    def test_topic_labels(self):
        self.assertEqual(pages.topic_label("local:pune"), "Local")
        self.assertEqual(pages.topic_label("ai"), "AI")
        self.assertEqual(pages.topic_label("business"), "Business")
        self.assertEqual(pages.topic_label(""), "News")


if __name__ == "__main__":
    unittest.main()
