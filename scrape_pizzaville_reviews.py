#!/usr/bin/env python3
"""Scrape Google reviews for Pizzaville (Liberty Village, Toronto).

Two backends are available:

* ``playwright`` (default) drives a real Chromium browser against Google Maps,
  scrolls the review pane until every review is loaded, and extracts the full
  set. No API key needed.
* ``places-api`` uses the official Google Places API (New). It needs an API key
  and only ever returns up to five reviews, but it is the sanctioned route and
  is far more stable.

Examples
--------
    # Everything Google Maps will hand out, newest first, as JSON + CSV
    python scrape_pizzaville_reviews.py --sort newest --output reviews.json --csv reviews.csv

    # Watch it work
    python scrape_pizzaville_reviews.py --no-headless --max-reviews 50

    # A different restaurant / a known Maps URL
    python scrape_pizzaville_reviews.py --query "Pizzaville Etobicoke"
    python scrape_pizzaville_reviews.py --url "https://www.google.com/maps/place/..."

    # Official API
    python scrape_pizzaville_reviews.py --backend places-api --api-key "$GOOGLE_MAPS_API_KEY"

Note: scraping Google Maps is against Google's Terms of Service, and the page
markup changes without warning. The selectors below are defensive (several
fallbacks each), but expect to revisit them. For anything production-facing,
use ``--backend places-api`` or a licensed data provider.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

DEFAULT_QUERY = "Pizzaville Liberty Village Toronto"

SORT_OPTIONS = {
    "relevant": 0,
    "newest": 1,
    "highest": 2,
    "lowest": 3,
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Review:
    review_id: str = ""
    author: str = ""
    author_url: str = ""
    author_meta: str = ""          # e.g. "Local Guide · 42 reviews · 10 photos"
    rating: float | None = None
    relative_date: str = ""        # e.g. "3 months ago"
    estimated_date: str = ""       # ISO date approximated from relative_date
    text: str = ""
    likes: int = 0
    photo_count: int = 0
    owner_response: str = ""
    owner_response_date: str = ""
    source_url: str = ""


@dataclass
class Place:
    name: str = ""
    address: str = ""
    rating: float | None = None
    review_count: int | None = None
    category: str = ""
    phone: str = ""
    website: str = ""
    url: str = ""
    reviews: list[Review] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_REL_UNITS = {
    "second": 1 / 86400,
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1,
    "week": 7,
    "month": 30.44,
    "year": 365.25,
}


def parse_relative_date(relative: str, today: date | None = None) -> str:
    """Turn "3 months ago" into an approximate ISO date.

    Google only exposes coarse relative timestamps, so this is an estimate --
    "3 months ago" could be anywhere in a ~30 day window. Returns "" if the
    string cannot be parsed.
    """
    if not relative:
        return ""
    today = today or date.today()
    text = relative.lower().strip()

    match = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
    else:
        # "a month ago", "an hour ago", "yesterday"
        if "yesterday" in text:
            amount, unit = 1, "day"
        else:
            match = re.search(r"\b(a|an)\s+(second|minute|hour|day|week|month|year)", text)
            if not match:
                return ""
            amount, unit = 1, match.group(2)

    days = _REL_UNITS[unit] * amount
    return (today - timedelta(days=days)).isoformat()


def parse_rating(text: str) -> float | None:
    """Pull a rating out of "5 stars", "4.0 stars", "3/5" or a bare "4.5"."""
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", " "))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if 0 <= value <= 5 else None


def parse_int(text: str) -> int:
    if not text:
        return 0
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


# --------------------------------------------------------------------------- #
# Browser-side extraction
# --------------------------------------------------------------------------- #
# Runs inside the page. Every field has a few fallback selectors because Google
# ships obfuscated, frequently-rotated class names; aria-labels and data-*
# attributes are the most durable anchors.
JS_EXTRACT_REVIEWS = r"""
() => {
  const pick = (root, selectors) => {
    for (const sel of selectors) {
      const el = root.querySelector(sel);
      if (el) return el;
    }
    return null;
  };
  const textOf = (root, selectors) => {
    const el = pick(root, selectors);
    return el ? el.textContent.trim() : "";
  };

  const nodes = document.querySelectorAll(
    'div[data-review-id][jsaction], div.jftiEf, div[data-review-id][jslog]'
  );

  const seen = new Set();
  const out = [];

  for (const node of nodes) {
    const id = node.getAttribute('data-review-id') || '';
    if (id && seen.has(id)) continue;

    // The owner-response block also carries a data-review-id; skip nested ones.
    if (node.parentElement && node.parentElement.closest('[data-review-id]')) continue;

    const ratingEl = pick(node, [
      'span[role="img"][aria-label*="star"]',
      'span[role="img"][aria-label*="Star"]',
      'span.kvMYJc',
    ]);
    const ratingText = ratingEl
      ? (ratingEl.getAttribute('aria-label') || ratingEl.textContent || '')
      : textOf(node, ['span.fzvQIb']);  // "3/5" style used by imported reviews

    const body = textOf(node, [
      'div.MyEned span.wiI7pd',
      'span.wiI7pd',
      'div[class*="MyEned"]',
      'div[id$=".text"]',
    ]);

    // A node with neither a rating nor text is chrome, not a review.
    if (!ratingText && !body) continue;
    if (id) seen.add(id);

    const authorLink = pick(node, [
      'a[href*="/maps/contrib/"]',
      'button[jsaction*="reviewerLink"]',
    ]);
    let author = textOf(node, ['div.d4r55', 'div[class*="d4r55"]', 'div.TSUbDb a']);
    if (!author && authorLink) {
      author = (authorLink.getAttribute('aria-label') || authorLink.textContent || '')
        .replace(/^Photo of\s+/i, '')
        .trim();
    }

    const ownerBlock = pick(node, ['div.CDe7pd', 'div[class*="CDe7pd"]']);
    let ownerResponse = '';
    let ownerDate = '';
    if (ownerBlock) {
      ownerResponse = textOf(ownerBlock, ['div.wiI7pd', 'span.wiI7pd', 'div[class*="wiI7pd"]']);
      ownerDate = textOf(ownerBlock, ['span.DZSIDd', 'span.rsqaWe', 'span[class*="DZSIDd"]']);
    }

    const likesEl = pick(node, [
      'button[aria-label*="helpful"]',
      'span.pkWtMe',
      'button[jsaction*="thumbUp"] span',
    ]);
    const likes = likesEl
      ? (likesEl.getAttribute('aria-label') || likesEl.textContent || '')
      : '';

    const photos = node.querySelectorAll(
      'button[data-photo-index], button[jsaction*="review.openPhoto"]'
    ).length;

    out.push({
      review_id: id,
      author: author,
      author_url: authorLink && authorLink.href ? authorLink.href : '',
      author_meta: textOf(node, ['div.RfnDt', 'div[class*="RfnDt"]']),
      rating_text: ratingText,
      relative_date: textOf(node, [
        'span.rsqaWe',
        'span.xRkPPb',
        'span[class*="rsqaWe"]',
      ]),
      text: body,
      likes_text: likes,
      photo_count: photos,
      owner_response: ownerResponse,
      owner_response_date: ownerDate,
    });
  }
  return out;
}
"""

# Find the scrollable review pane by walking up from a review node rather than
# trusting a class name.
JS_FIND_SCROLLER = r"""
() => {
  const node = document.querySelector('div[data-review-id][jsaction], div.jftiEf');
  if (!node) return document.querySelector('div[role="feed"]');
  let el = node.parentElement;
  while (el && el !== document.body) {
    const style = window.getComputedStyle(el);
    const scrolls = /(auto|scroll)/.test(style.overflowY);
    if (scrolls && el.scrollHeight > el.clientHeight + 50) return el;
    el = el.parentElement;
  }
  return document.querySelector('div[role="feed"]');
}
"""

JS_PLACE_DETAILS = r"""
() => {
  const text = (sel) => {
    const el = document.querySelector(sel);
    return el ? el.textContent.trim() : '';
  };
  const attr = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute(name) || '') : '';
  };
  return {
    name: text('h1.DUwDvf') || text('h1'),
    rating: text('div.F7nice span[aria-hidden="true"]')
            || attr('div.F7nice span[role="img"]', 'aria-label'),
    review_count: text('div.F7nice span[aria-label*="review"]')
                  || attr('div.F7nice button', 'aria-label'),
    category: text('button[jsaction*="category"]'),
    address: attr('button[data-item-id="address"]', 'aria-label'),
    phone: attr('button[data-item-id^="phone"]', 'aria-label'),
    website: attr('a[data-item-id="authority"]', 'href'),
  };
}
"""


# --------------------------------------------------------------------------- #
# Playwright backend
# --------------------------------------------------------------------------- #
class GoogleMapsReviewScraper:
    def __init__(
        self,
        headless: bool = True,
        max_reviews: int | None = None,
        sort: str = "newest",
        timeout: int = 30_000,
        scroll_pause: float = 1.1,
        max_stalls: int = 6,
        verbose: bool = True,
        browser_path: str | None = None,
    ) -> None:
        self.headless = headless
        self.max_reviews = max_reviews
        self.sort = sort
        self.timeout = timeout
        self.scroll_pause = scroll_pause
        self.max_stalls = max_stalls
        self.verbose = verbose
        self.browser_path = browser_path or os.environ.get("CHROMIUM_PATH")

    # -- logging ---------------------------------------------------------- #
    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[scraper] {message}", file=sys.stderr, flush=True)

    # -- public ----------------------------------------------------------- #
    def scrape(self, query: str | None = None, url: str | None = None) -> Place:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "Playwright is not installed. Run:\n"
                "    pip install -r requirements.txt\n"
                "    playwright install chromium"
            ) from exc

        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if self.browser_path:
            launch_kwargs["executable_path"] = self.browser_path
            self.log(f"using browser at {self.browser_path}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                locale="en-US",
                timezone_id="America/Toronto",
                viewport={"width": 1360, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout)

            try:
                target = url or self._search_url(query or DEFAULT_QUERY)
                self.log(f"opening {target}")
                page.goto(target, wait_until="domcontentloaded")
                self._dismiss_consent(page)
                self._ensure_place_page(page)

                place = self._read_place_details(page)
                place.url = page.url

                self._open_reviews(page)
                self._set_sort(page, self.sort)
                self._load_all_reviews(page)
                self._expand_long_reviews(page)

                place.reviews = self._extract_reviews(page, place.url)
                self.log(f"collected {len(place.reviews)} reviews")
                return place
            finally:
                context.close()
                browser.close()

    # -- steps ------------------------------------------------------------ #
    @staticmethod
    def _search_url(query: str) -> str:
        from urllib.parse import quote_plus

        return f"https://www.google.com/maps/search/{quote_plus(query)}?hl=en&gl=ca"

    def _dismiss_consent(self, page: Any) -> None:
        """Click through the EU/consent interstitial if Google shows one."""
        selectors = [
            'button[aria-label*="Accept all"]',
            'button[aria-label*="Reject all"]',
            'form[action*="consent"] button',
            'button:has-text("Accept all")',
            'button:has-text("Reject all")',
            'button:has-text("I agree")',
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1500):
                    self.log("dismissing consent dialog")
                    button.click()
                    page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

    def _ensure_place_page(self, page: Any) -> None:
        """A search can land on a result list; open the first hit if so."""
        try:
            page.wait_for_selector('h1.DUwDvf, div[role="feed"] a[href*="/maps/place/"]')
        except Exception:
            pass

        if page.locator("h1.DUwDvf").count() > 0:
            return

        results = page.locator('div[role="feed"] a[href*="/maps/place/"]')
        if results.count() > 0:
            self.log("search returned a list; opening the first result")
            results.first.click()
            page.wait_for_selector("h1.DUwDvf")
            page.wait_for_timeout(1500)
        else:
            raise RuntimeError(
                "Could not find a place page. Try passing an explicit --url, "
                "or run with --no-headless to see what Google returned."
            )

    def _read_place_details(self, page: Any) -> Place:
        raw = page.evaluate(JS_PLACE_DETAILS)
        place = Place(
            name=raw.get("name", ""),
            address=re.sub(r"^Address:\s*", "", raw.get("address", "")).strip(),
            rating=parse_rating(raw.get("rating", "")),
            review_count=parse_int(raw.get("review_count", "")) or None,
            category=raw.get("category", ""),
            phone=re.sub(r"^Phone:\s*", "", raw.get("phone", "")).strip(),
            website=raw.get("website", ""),
        )
        self.log(f"place: {place.name or '(unknown)'} — {place.address or 'no address'}")
        return place

    def _open_reviews(self, page: Any) -> None:
        selectors = [
            'button[jsaction*="pane.reviewChart.moreReviews"]',
            'button[aria-label*="Reviews for"]',
            'button[role="tab"][aria-label*="Reviews"]',
            'div[role="tablist"] button:has-text("Reviews")',
            'button:has-text("reviews")',
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible(timeout=2000):
                    button.click()
                    page.wait_for_selector(
                        'div[data-review-id][jsaction], div.jftiEf', timeout=15_000
                    )
                    page.wait_for_timeout(1200)
                    self.log("reviews tab open")
                    return
            except Exception:
                continue

        # Some layouts already show reviews without a tab click.
        if page.locator("div[data-review-id][jsaction], div.jftiEf").count() > 0:
            self.log("reviews already visible")
            return
        raise RuntimeError("Could not open the reviews tab for this place.")

    def _set_sort(self, page: Any, sort: str) -> None:
        index = SORT_OPTIONS.get(sort)
        if index is None or sort == "relevant":
            return
        selectors = [
            'button[aria-label*="Sort reviews"]',
            'button[data-value="Sort"]',
            'button[aria-label="Sort"]',
            'button:has-text("Most relevant")',
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible(timeout=2000):
                    button.click()
                    page.wait_for_timeout(900)
                    options = page.locator('div[role="menuitemradio"], li[role="menuitemradio"]')
                    if options.count() > index:
                        options.nth(index).click()
                        page.wait_for_timeout(2500)
                        self.log(f"sorted by {sort}")
                    return
            except Exception:
                continue
        self.log(f"could not apply sort '{sort}'; continuing with default order")

    def _count_reviews(self, page: Any) -> int:
        return page.locator("div[data-review-id][jsaction], div.jftiEf").count()

    def _load_all_reviews(self, page: Any) -> None:
        """Scroll the review pane until the count stops growing."""
        scroller = page.evaluate_handle(JS_FIND_SCROLLER)
        if not scroller:
            self.log("no scrollable review pane found")
            return

        previous = self._count_reviews(page)
        stalls = 0
        while True:
            if self.max_reviews and previous >= self.max_reviews:
                self.log(f"reached --max-reviews ({self.max_reviews})")
                break

            page.evaluate("(el) => el.scrollBy(0, el.scrollHeight)", scroller)
            page.wait_for_timeout(int(self.scroll_pause * 1000 * random.uniform(0.85, 1.3)))

            current = self._count_reviews(page)
            if current == previous:
                stalls += 1
                if stalls >= self.max_stalls:
                    self.log(f"no new reviews after {stalls} scrolls; done at {current}")
                    break
                # Nudge with keyboard too; Maps sometimes needs a real scroll event.
                try:
                    page.mouse.wheel(0, 4000)
                except Exception:
                    pass
            else:
                stalls = 0
                self.log(f"loaded {current} reviews")
            previous = current

    def _expand_long_reviews(self, page: Any) -> None:
        """Click every "More" link so truncated review bodies are complete."""
        selectors = [
            'button[aria-label="See more"]',
            "button.w8nwRe",
            'button[jsaction*="review.expandReview"]',
        ]
        clicked = 0
        for selector in selectors:
            buttons = page.locator(selector)
            for i in range(buttons.count()):
                try:
                    button = buttons.nth(i)
                    if button.is_visible():
                        button.click(timeout=1500)
                        clicked += 1
                except Exception:
                    continue
        if clicked:
            page.wait_for_timeout(800)
            self.log(f"expanded {clicked} truncated reviews")

    def _extract_reviews(self, page: Any, source_url: str) -> list[Review]:
        raw_reviews = page.evaluate(JS_EXTRACT_REVIEWS)
        reviews: list[Review] = []
        seen: set[str] = set()

        for raw in raw_reviews:
            key = raw.get("review_id") or f"{raw.get('author')}|{raw.get('text')[:80]}"
            if key in seen:
                continue
            seen.add(key)

            relative = raw.get("relative_date", "")
            reviews.append(
                Review(
                    review_id=raw.get("review_id", ""),
                    author=raw.get("author", ""),
                    author_url=raw.get("author_url", ""),
                    author_meta=raw.get("author_meta", ""),
                    rating=parse_rating(raw.get("rating_text", "")),
                    relative_date=relative,
                    estimated_date=parse_relative_date(relative),
                    text=raw.get("text", ""),
                    likes=parse_int(raw.get("likes_text", "")),
                    photo_count=raw.get("photo_count", 0) or 0,
                    owner_response=raw.get("owner_response", ""),
                    owner_response_date=raw.get("owner_response_date", ""),
                    source_url=source_url,
                )
            )

        if self.max_reviews:
            reviews = reviews[: self.max_reviews]
        return reviews


# --------------------------------------------------------------------------- #
# Places API backend
# --------------------------------------------------------------------------- #
def scrape_with_places_api(query: str, api_key: str, verbose: bool = True) -> Place:
    """Official route. Returns at most five reviews -- that is an API limit."""
    import requests

    endpoint = "https://places.googleapis.com/v1/places:searchText"
    fields = [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "places.primaryTypeDisplayName",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.reviews",
    ]
    response = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": ",".join(fields),
        },
        json={"textQuery": query, "languageCode": "en", "maxResultCount": 1},
        timeout=30,
    )
    response.raise_for_status()
    places = response.json().get("places", [])
    if not places:
        raise RuntimeError(f"Places API returned no results for {query!r}")

    raw = places[0]
    if verbose:
        print(f"[scraper] Places API matched: {raw.get('displayName', {}).get('text', '')}",
              file=sys.stderr)

    place = Place(
        name=raw.get("displayName", {}).get("text", ""),
        address=raw.get("formattedAddress", ""),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount"),
        category=raw.get("primaryTypeDisplayName", {}).get("text", ""),
        phone=raw.get("nationalPhoneNumber", ""),
        website=raw.get("websiteUri", ""),
        url=raw.get("googleMapsUri", ""),
    )

    for item in raw.get("reviews", []):
        published = item.get("publishTime", "")
        place.reviews.append(
            Review(
                review_id=item.get("name", "").split("/")[-1],
                author=item.get("authorAttribution", {}).get("displayName", ""),
                author_url=item.get("authorAttribution", {}).get("uri", ""),
                rating=item.get("rating"),
                relative_date=item.get("relativePublishTimeDescription", ""),
                estimated_date=published[:10],
                text=item.get("originalText", {}).get("text", "")
                or item.get("text", {}).get("text", ""),
                source_url=item.get("googleMapsUri", place.url),
            )
        )
    return place


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CSV_COLUMNS = [
    "review_id",
    "author",
    "rating",
    "relative_date",
    "estimated_date",
    "text",
    "likes",
    "photo_count",
    "author_meta",
    "owner_response",
    "owner_response_date",
    "author_url",
    "source_url",
]


def write_json(place: Place, path: str) -> None:
    payload = {
        "place": {k: v for k, v in asdict(place).items() if k != "reviews"},
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "review_count_scraped": len(place.reviews),
        "reviews": [asdict(r) for r in place.reviews],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"Wrote {len(place.reviews)} reviews to {path}")


def write_csv(reviews: Iterable[Review], path: str) -> None:
    rows = [asdict(r) for r in reviews]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} reviews to {path}")


def print_summary(place: Place) -> None:
    reviews = place.reviews
    print("\n" + "=" * 62)
    print(f"  {place.name or 'Unknown place'}")
    if place.address:
        print(f"  {place.address}")
    if place.rating:
        total = f" from {place.review_count} ratings" if place.review_count else ""
        print(f"  Google rating: {place.rating}{total}")
    print(f"  Reviews scraped: {len(reviews)}")

    rated = [r.rating for r in reviews if r.rating is not None]
    if rated:
        print(f"  Mean of scraped ratings: {sum(rated) / len(rated):.2f}")
        print("  Distribution:")
        for star in range(5, 0, -1):
            count = sum(1 for r in rated if round(r) == star)
            bar = "#" * int(40 * count / max(len(rated), 1))
            print(f"    {star}* {count:4d}  {bar}")
    with_text = sum(1 for r in reviews if r.text)
    print(f"  With written text: {with_text}")
    print(f"  With owner replies: {sum(1 for r in reviews if r.owner_response)}")
    print("=" * 62 + "\n")

    for review in reviews[:3]:
        stars = f"{review.rating:g}*" if review.rating else "n/a"
        snippet = (review.text[:160] + "...") if len(review.text) > 160 else review.text
        print(f"  {stars}  {review.author or 'Anonymous'}  ({review.relative_date})")
        if snippet:
            print(f"      {snippet}")
    if reviews:
        print()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Google reviews for Pizzaville in Liberty Village, Toronto.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help=f"Search text for Google Maps (default: {DEFAULT_QUERY!r})")
    parser.add_argument("--url", default=None,
                        help="Skip the search and scrape this Google Maps place URL directly")
    parser.add_argument("--backend", choices=["playwright", "places-api"], default="playwright",
                        help="playwright scrapes all reviews; places-api is official but caps at 5")
    parser.add_argument("--api-key", default=os.environ.get("GOOGLE_MAPS_API_KEY"),
                        help="Google Places API key (or set GOOGLE_MAPS_API_KEY)")
    parser.add_argument("--sort", choices=list(SORT_OPTIONS), default="newest",
                        help="Review sort order (default: newest)")
    parser.add_argument("--max-reviews", type=int, default=None,
                        help="Stop after roughly this many reviews")
    parser.add_argument("--output", default="pizzaville_reviews.json",
                        help="JSON output path (default: pizzaville_reviews.json)")
    parser.add_argument("--csv", default=None, help="Also write a CSV to this path")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show the browser window (useful for debugging)")
    parser.add_argument("--browser-path", default=None,
                        help="Chromium executable to use instead of Playwright's bundled "
                             "build (or set CHROMIUM_PATH)")
    parser.add_argument("--scroll-pause", type=float, default=1.1,
                        help="Seconds to wait between scrolls (default: 1.1)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Per-action timeout in seconds (default: 30)")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.backend == "places-api":
            if not args.api_key:
                print("--backend places-api needs --api-key or GOOGLE_MAPS_API_KEY",
                      file=sys.stderr)
                return 2
            place = scrape_with_places_api(args.query, args.api_key, verbose=not args.quiet)
        else:
            scraper = GoogleMapsReviewScraper(
                headless=args.headless,
                max_reviews=args.max_reviews,
                sort=args.sort,
                timeout=args.timeout * 1000,
                scroll_pause=args.scroll_pause,
                verbose=not args.quiet,
                browser_path=args.browser_path,
            )
            place = scraper.scrape(query=args.query, url=args.url)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not place.reviews:
        print("No reviews found. Re-run with --no-headless to see what the page showed.",
              file=sys.stderr)

    write_json(place, args.output)
    if args.csv:
        write_csv(place.reviews, args.csv)
    if not args.quiet:
        print_summary(place)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
