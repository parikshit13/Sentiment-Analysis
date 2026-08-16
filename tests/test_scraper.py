#!/usr/bin/env python3
"""Tests for scrape_google_reviews.

Three layers:
  * pure-python parsing helpers (always run)
  * target resolution and the registry verification guard (always run)
  * the in-page extraction JS, run by Chromium against local HTML fixtures
    that mimic the Google Maps review pane (skipped if Playwright/Chromium
    are unavailable)

Run with:  python tests/test_scraper.py      (or: pytest tests/)
Set CHROMIUM_PATH if Playwright's bundled browser is not installed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrape_google_reviews import (  # noqa: E402
    JS_EXTRACT_REVIEWS,
    JS_FIND_SCROLLER,
    JS_PLACE_DETAILS,
    GoogleMapsReviewScraper,
    Place,
    load_registry,
    parse_int,
    parse_rating,
    parse_relative_date,
    place_id_url,
    resolve_target,
    slugify,
    street_number,
    verify_place,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mock_maps_reviews.html"
CLOSED_FIXTURE = Path(__file__).parent / "fixtures" / "mock_maps_closed.html"
TODAY = date(2026, 8, 15)

REGISTRY = {
    "sample-pizza-liberty-village": {
        "place_id": "ChIJ0SPBaLM0K4gRuFqEbODjSDs",
        "name": "Sample Pizza Co",
        "address": "60 Atlantic Ave, Toronto, ON M6K 1X9",
    }
}


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

    def test_slugify(self):
        self.assertEqual(slugify("Sample Pizza Co, Toronto!"), "sample-pizza-co-toronto")
        self.assertEqual(slugify(""), "place")

    def test_street_number(self):
        self.assertEqual(street_number("60 Atlantic Ave, Toronto"), "60")
        self.assertEqual(street_number("2116B Queen St E"), "2116b")
        self.assertEqual(street_number("Atlantic Ave"), "")


class TestTargetResolution(unittest.TestCase):
    def test_registry_slug_wins(self):
        target = resolve_target("sample-pizza-liberty-village", REGISTRY)
        self.assertEqual(target.kind, "registry")
        self.assertTrue(target.is_registered)
        self.assertTrue(target.is_precise)
        self.assertEqual(target.place_id, "ChIJ0SPBaLM0K4gRuFqEbODjSDs")
        self.assertEqual(target.expected_name, "Sample Pizza Co")
        self.assertIn("place_id:ChIJ", target.url)

    def test_raw_place_id(self):
        target = resolve_target("ChIJ0SPBaLM0K4gRuFqEbODjSDs", REGISTRY)
        self.assertEqual(target.kind, "place_id")
        self.assertTrue(target.is_precise)
        self.assertFalse(target.is_registered)

    def test_ftid_uses_ftid_url_form(self):
        target = resolve_target("0x882b34b368c12312:0x3b48e3e06e845ab8", REGISTRY)
        self.assertEqual(target.kind, "place_id")
        self.assertIn("ftid=", target.url)
        self.assertNotIn("place_id:", target.url)

    def test_maps_url(self):
        url = "https://www.google.com/maps/place/Sample/@43.6,-79.4,17z"
        target = resolve_target(url, REGISTRY)
        self.assertEqual(target.kind, "url")
        self.assertEqual(target.url, url)

    def test_free_text_is_a_fuzzy_query(self):
        target = resolve_target("Sample Pizza Etobicoke", REGISTRY)
        self.assertEqual(target.kind, "query")
        self.assertFalse(target.is_precise)  # the chain hazard
        self.assertIn("/maps/search/", target.url)

    def test_near_biases_the_search_url(self):
        target = resolve_target("Sample Pizza", REGISTRY, near="43.6389,-79.4200")
        self.assertIn("@43.6389,-79.4200", target.url)

    def test_empty_target_rejected(self):
        with self.assertRaises(ValueError):
            resolve_target("   ", REGISTRY)

    def test_place_id_url_forms(self):
        self.assertIn("place_id:ChIJabc", place_id_url("ChIJabc"))
        self.assertIn("ftid=0x1%3A0x2", place_id_url("0x1:0x2"))


class TestRegistryLoading(unittest.TestCase):
    def _write(self, payload: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(payload)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_registry("/nonexistent/places.json"), {})

    def test_valid_registry(self):
        path = self._write(json.dumps(REGISTRY))
        self.assertEqual(load_registry(path), REGISTRY)

    def test_entry_without_place_id_is_rejected(self):
        path = self._write(json.dumps({"broken": {"name": "No id here"}}))
        with self.assertRaises(SystemExit):
            load_registry(path)

    def test_malformed_json_is_rejected(self):
        path = self._write("{not json")
        with self.assertRaises(SystemExit):
            load_registry(path)


class TestVerificationGuard(unittest.TestCase):
    def setUp(self):
        self.target = resolve_target("sample-pizza-liberty-village", REGISTRY)

    def test_exact_match_verifies(self):
        place = Place(name="Sample Pizza Co", address="60 Atlantic Ave, Toronto, ON M6K 1X9")
        self.assertEqual(verify_place(place, self.target), [])
        self.assertTrue(place.verified)

    def test_name_superset_still_verifies(self):
        # Google often appends the neighbourhood to a listing name.
        place = Place(name="Sample Pizza Co Liberty Village", address="60 Atlantic Ave")
        self.assertEqual(verify_place(place, self.target), [])
        self.assertTrue(place.verified)

    def test_rebrand_is_blocked(self):
        # Same place id, new business: the case a pinned id cannot protect against.
        place = Place(name="Napoli Slice House", address="60 Atlantic Ave, Toronto")
        with self.assertRaises(RuntimeError) as ctx:
            verify_place(place, self.target)
        self.assertIn("name mismatch", str(ctx.exception))
        self.assertFalse(place.verified)

    def test_wrong_branch_is_blocked(self):
        place = Place(name="Sample Pizza Co", address="2116 Queen St E, Toronto")
        with self.assertRaises(RuntimeError) as ctx:
            verify_place(place, self.target)
        self.assertIn("address mismatch", str(ctx.exception))

    def test_closed_listing_is_blocked(self):
        place = Place(
            name="Sample Pizza Co",
            address="60 Atlantic Ave",
            business_status="CLOSED_PERMANENTLY",
        )
        with self.assertRaises(RuntimeError) as ctx:
            verify_place(place, self.target)
        self.assertIn("closed permanently", str(ctx.exception))

    def test_non_strict_reports_without_raising(self):
        place = Place(name="Napoli Slice House", address="60 Atlantic Ave")
        problems = verify_place(place, self.target, strict=False)
        self.assertEqual(len(problems), 1)
        self.assertFalse(place.verified)

    def test_unregistered_target_is_never_marked_verified(self):
        target = resolve_target("Some Cafe", REGISTRY)
        place = Place(name="Anything At All", address="1 Main St")
        self.assertEqual(verify_place(place, target), [])
        self.assertFalse(place.verified)

    def test_unregistered_target_still_blocks_on_closed(self):
        target = resolve_target("Some Cafe", REGISTRY)
        place = Place(name="Some Cafe", business_status="CLOSED_PERMANENTLY")
        with self.assertRaises(RuntimeError):
            verify_place(place, target)


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
        self.assertIn("Best pizza in the neighbourhood", review["text"])
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
        self.assertEqual(details["name"], "Sample Pizza Co")
        self.assertEqual(parse_rating(details["rating"]), 4.1)
        self.assertEqual(parse_int(details["review_count"]), 382)
        self.assertIn("60 Atlantic Ave", details["address"])
        self.assertEqual(details["category"], "Pizza restaurant")
        self.assertEqual(details["business_status"], "OPERATIONAL")

    def test_closed_listing_is_detected(self):
        page = self.browser.new_page()
        page.goto(CLOSED_FIXTURE.as_uri())
        details = page.evaluate(JS_PLACE_DETAILS)
        # The banner sets the status, but the review whose text is exactly
        # "Permanently closed" must not be what triggered it.
        self.assertEqual(details["business_status"], "CLOSED_PERMANENTLY")
        reviews = page.evaluate(JS_EXTRACT_REVIEWS)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["text"], "Permanently closed")
        page.close()

    def test_closed_fixture_blocks_a_registry_scrape(self):
        """The end-to-end guard: closed page -> details -> verify -> refusal."""
        page = self.browser.new_page()
        page.goto(CLOSED_FIXTURE.as_uri())
        scraper = GoogleMapsReviewScraper(verbose=False)
        place = scraper._read_place_details(page)
        page.close()
        target = resolve_target("sample-pizza-liberty-village", REGISTRY)
        with self.assertRaises(RuntimeError):
            verify_place(place, target)

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
