#!/usr/bin/env python3
"""Tests for scrape_pizzaville_reviews.

Two layers:
  * pure-python parsing helpers (always run)
  * the in-page extraction JS, run by Chromium against a local HTML fixture
    that mimics the Google Maps review pane (skipped if Playwright/Chromium
    are unavailable)

Run with:  python tests/test_scraper.py      (or: pytest tests/)
Set CHROMIUM_PATH if Playwright's bundled browser is not installed.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrape_pizzaville_reviews import (  # noqa: E402
    JS_EXTRACT_REVIEWS,
    JS_FIND_SCROLLER,
    JS_PLACE_DETAILS,
    GoogleMapsReviewScraper,
    parse_int,
    parse_rating,
    parse_relative_date,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mock_maps_reviews.html"
TODAY = date(2026, 8, 15)


class TestParsingHelpers(unittest.TestCase):
    def test_relative_dates(self):
        cases = {
            "3 months ago": "2026-05-16",
            "a month ago": "2026-07-16",
            "5 days ago": "2026-08-10",
            "a week ago": "2026-08-08",
            "2 years ago": "2024-08-15",
            "an hour ago": "2026-08-15",
            "yesterday": "2026-08-14",
        }
        for text, expected in cases.items():
            self.assertEqual(parse_relative_date(text, TODAY), expected, text)

    def test_unparseable_dates_return_empty(self):
        for text in ["", "recently", "il y a 3 mois"]:
            self.assertEqual(parse_relative_date(text, TODAY), "")

    def test_ratings(self):
        self.assertEqual(parse_rating("5 stars"), 5.0)
        self.assertEqual(parse_rating("4.0 stars"), 4.0)
        self.assertEqual(parse_rating("1 star"), 1.0)
        self.assertEqual(parse_rating("3/5"), 3.0)
        self.assertIsNone(parse_rating(""))
        self.assertIsNone(parse_rating("no digits here"))
        self.assertIsNone(parse_rating("42 stars"))  # out of range

    def test_ints(self):
        self.assertEqual(parse_int("1,234 reviews"), 1234)
        self.assertEqual(parse_int("12 people found this helpful"), 12)
        self.assertEqual(parse_int(""), 0)
        self.assertEqual(parse_int("no digits"), 0)


_BROWSER: tuple | None = None
_LAUNCH_ATTEMPTED = False


def _launch():
    """Return a cached (playwright, browser) pair, or None if one can't start.

    Cached because sync_playwright().start() may only be called once per
    thread -- the skip check and setUpClass both need the same instance.
    """
    global _BROWSER, _LAUNCH_ATTEMPTED
    if _LAUNCH_ATTEMPTED:
        return _BROWSER
    _LAUNCH_ATTEMPTED = True

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    path = os.environ.get("CHROMIUM_PATH")
    if path:
        kwargs["executable_path"] = path
    try:
        pw = sync_playwright().start()
        _BROWSER = (pw, pw.chromium.launch(**kwargs))
    except Exception as exc:
        print(f"[tests] browser unavailable, skipping page tests: {exc}", file=sys.stderr)
        _BROWSER = None
    return _BROWSER


@unittest.skipIf(_launch() is None, "Playwright/Chromium not available")
class TestPageExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw, cls.browser = _launch()
        cls.page = cls.browser.new_page()
        cls.page.goto(FIXTURE.as_uri())
        cls.reviews = cls.page.evaluate(JS_EXTRACT_REVIEWS)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_finds_every_real_review_once(self):
        # 3 unique reviews; the duplicate id and the summary block are dropped.
        self.assertEqual(len(self.reviews), 3)

    def test_skips_non_review_nodes(self):
        ids = [r["review_id"] for r in self.reviews]
        self.assertNotIn("summary-block", ids)

    def test_full_review_fields(self):
        review = self.reviews[0]
        self.assertEqual(review["author"], "Dana Whitfield")
        self.assertEqual(parse_rating(review["rating_text"]), 5.0)
        self.assertEqual(review["relative_date"], "3 months ago")
        self.assertIn("Best pizza in Liberty Village", review["text"])
        self.assertEqual(review["author_meta"], "Local Guide · 214 reviews · 1,032 photos")
        self.assertEqual(review["photo_count"], 2)
        self.assertEqual(parse_int(review["likes_text"]), 12)

    def test_owner_response_is_separated_from_review_text(self):
        review = self.reviews[0]
        self.assertIn("Thanks so much for the kind words", review["owner_response"])
        self.assertNotIn("Thanks so much", review["text"])
        self.assertEqual(review["owner_response_date"], "2 months ago")

    def test_rating_only_review(self):
        review = self.reviews[1]
        self.assertEqual(review["author"], "M. Okonkwo")
        self.assertEqual(parse_rating(review["rating_text"]), 2.0)
        self.assertEqual(review["text"], "")
        self.assertEqual(review["owner_response"], "")
        self.assertIn("/maps/contrib/", review["author_url"])

    def test_third_party_rating_format(self):
        review = self.reviews[2]
        self.assertEqual(review["author"], "Priya Raman")
        self.assertEqual(parse_rating(review["rating_text"]), 4.0)

    def test_place_details(self):
        details = self.page.evaluate(JS_PLACE_DETAILS)
        self.assertEqual(details["name"], "Pizzaville")
        self.assertEqual(parse_rating(details["rating"]), 4.1)
        self.assertEqual(parse_int(details["review_count"]), 382)
        self.assertIn("60 Atlantic Ave", details["address"])
        self.assertEqual(details["category"], "Pizza restaurant")

    def test_scroller_lookup_finds_the_scrollable_pane(self):
        handle = self.page.evaluate_handle(JS_FIND_SCROLLER)
        element_id = self.page.evaluate("(el) => el && el.id", handle)
        self.assertEqual(element_id, "pane")

    def test_extract_reviews_maps_into_dataclasses(self):
        scraper = GoogleMapsReviewScraper(verbose=False)
        parsed = scraper._extract_reviews(self.page, "https://maps.example/place")
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].rating, 5.0)
        self.assertEqual(parsed[0].likes, 12)
        self.assertEqual(parsed[0].estimated_date, parse_relative_date("3 months ago"))
        self.assertEqual(parsed[0].source_url, "https://maps.example/place")
        self.assertTrue(all(r.review_id for r in parsed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
