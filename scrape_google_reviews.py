#!/usr/bin/env python3
"""Scrape Google reviews for any restaurant, pinned by Google Place ID.

Targets are named in ``places.json``, a hand-maintained registry mapping a short
slug to a Place ID plus the name and address that ID is expected to resolve to::

    {
      "pizzaville-liberty-village": {
        "place_id": "ChIJ...",
        "name": "Pizzaville",
        "address": "60 Atlantic Ave, Toronto, ON M6K 1X9"
      }
    }

Then scrape by slug. The name/address in the record are not decoration: every
run checks the resolved place against them and aborts on a mismatch, so a stale
or rebranded listing fails loudly instead of quietly polluting your dataset.

Two backends are available:

* ``playwright`` (default) drives a real Chromium browser against Google Maps,
  scrolls the review pane until every review is loaded, and extracts the full
  set. No API key needed.
* ``places-api`` uses the official Google Places API (New). It needs an API key
  and only ever returns up to five reviews, but it is the sanctioned route and
  is far more stable.

Examples
--------
    # Find a place and print a pasteable registry record
    python scrape_google_reviews.py --find "Pizzaville Liberty Village Toronto"

    # Scrape a registered slug (the normal case)
    python scrape_google_reviews.py pizzaville-liberty-village --csv reviews.csv

    # Ad-hoc targets: a raw Place ID, a Maps URL, or a fuzzy text search
    python scrape_google_reviews.py ChIJ0SPBaLM0K4gRuFqEbODjSDs
    python scrape_google_reviews.py "https://www.google.com/maps/place/..."
    python scrape_google_reviews.py "Pizzaville Etobicoke"     # fuzzy, warns

    # Re-check every registry entry for drift
    python scrape_google_reviews.py --verify-registry

    # Official API
    python scrape_google_reviews.py pizzaville-liberty-village \
        --backend places-api --api-key "$GOOGLE_MAPS_API_KEY"

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
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

DEFAULT_REGISTRY = "places.json"

# Google's canonical place identifier, e.g. ChIJ0SPBaLM0K4gRuFqEbODjSDs.
PLACE_ID_RE = re.compile(r"^ChI[Ja-zA-Z0-9_-]{10,}$")
# The older feature id ("ftid") pair still embedded in Maps URLs.
FTID_RE = re.compile(r"^0x[0-9a-f]+:0x[0-9a-f]+$", re.IGNORECASE)
# Used to pull a place id out of a rendered Maps page.
PLACE_ID_IN_PAGE_RE = re.compile(r"\"(ChI[Ja-zA-Z0-9_-]{20,})\"")

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
    place_id: str = ""
    slug: str = ""
    business_status: str = "OPERATIONAL"
    verified: bool = False       # did the resolved place match its registry record?
    reviews: list[Review] = field(default_factory=list)


@dataclass
class Target:
    """A resolved scrape target and, when registered, what it should look like."""

    raw: str = ""
    kind: str = "query"          # registry | place_id | url | query
    slug: str = ""
    place_id: str = ""
    url: str = ""
    query: str = ""
    expected_name: str = ""
    expected_address: str = ""

    @property
    def is_registered(self) -> bool:
        return self.kind == "registry"

    @property
    def is_precise(self) -> bool:
        """True when the target names one specific place rather than a search."""
        return self.kind in ("registry", "place_id", "url")

    def label(self) -> str:
        return self.slug or self.expected_name or self.place_id or self.query or self.raw


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


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "place"


def street_number(address: str) -> str:
    """Leading street number of an address, used as a cheap identity check."""
    match = re.search(r"\b(\d+[A-Za-z]?)\b", address or "")
    return match.group(1).lower() if match else ""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def load_registry(path: str = DEFAULT_REGISTRY) -> dict[str, dict]:
    """Load places.json. A missing file is fine -- ad-hoc targets still work."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must be a JSON object mapping slug -> record")

    for slug, record in data.items():
        if not isinstance(record, dict) or not record.get("place_id"):
            raise SystemExit(f"{path}: entry {slug!r} is missing a 'place_id'")
    return data


def place_id_url(place_id: str) -> str:
    """Documented URL form for opening a place by id -- no API key required."""
    from urllib.parse import quote

    if FTID_RE.match(place_id):
        return f"https://www.google.com/maps/place/?ftid={quote(place_id)}&hl=en"
    return f"https://www.google.com/maps/place/?q=place_id:{quote(place_id)}&hl=en"


def search_url(query: str, near: str | None = None, zoom: int = 14) -> str:
    """Maps search URL, optionally biased to a 'lat,lng' centre."""
    from urllib.parse import quote_plus

    url = f"https://www.google.com/maps/search/{quote_plus(query)}"
    if near:
        url += f"/@{near.strip()},{zoom}z"
    return url + "?hl=en&gl=ca"


def resolve_target(
    raw: str,
    registry: dict[str, dict] | None = None,
    near: str | None = None,
) -> Target:
    """Work out what the user meant by their positional argument.

    Precedence: registry slug > raw Place ID / FTID > Maps URL > text query.
    """
    registry = registry or {}
    text = (raw or "").strip()
    if not text:
        raise ValueError("No target given")

    record = registry.get(text)
    if record:
        place_id = record["place_id"]
        return Target(
            raw=text,
            kind="registry",
            slug=text,
            place_id=place_id,
            url=place_id_url(place_id),
            expected_name=record.get("name", ""),
            expected_address=record.get("address", ""),
        )

    if PLACE_ID_RE.match(text) or FTID_RE.match(text):
        return Target(
            raw=text,
            kind="place_id",
            slug=slugify(text[:24]),
            place_id=text,
            url=place_id_url(text),
        )

    if text.startswith("http://") or text.startswith("https://"):
        return Target(raw=text, kind="url", slug="", url=text)

    return Target(
        raw=text,
        kind="query",
        slug=slugify(text),
        query=text,
        url=search_url(text, near=near),
    )


def verify_place(place: Place, target: Target, strict: bool = True) -> list[str]:
    """Check a resolved place against its registry record.

    Returns a list of problems. Raises when ``strict`` and anything mismatched --
    a wrong or rebranded listing must not slip silently into the dataset.
    """
    problems: list[str] = []

    if place.business_status and place.business_status != "OPERATIONAL":
        problems.append(
            f"listing is {place.business_status.replace('_', ' ').lower()}; "
            "its reviews describe a business that is no longer trading"
        )

    if target.is_registered:
        expected_name = target.expected_name.strip().lower()
        actual_name = place.name.strip().lower()
        if expected_name and expected_name not in actual_name and actual_name not in expected_name:
            problems.append(
                f"name mismatch: registry says {target.expected_name!r}, "
                f"Google returned {place.name!r} -- the listing may have been "
                "rebranded, which means its reviews now mix two businesses"
            )

        expected_number = street_number(target.expected_address)
        actual_number = street_number(place.address)
        if expected_number and actual_number and expected_number != actual_number:
            problems.append(
                f"address mismatch: registry says {target.expected_address!r}, "
                f"Google returned {place.address!r}"
            )

    place.verified = target.is_registered and not problems

    if problems and strict:
        detail = "\n  - ".join(problems)
        raise RuntimeError(
            f"Refusing to scrape {target.label()!r}:\n  - {detail}\n"
            f"Re-pin it with:  --find {target.expected_name or target.label()!r}\n"
            "Or pass --no-verify to scrape anyway."
        )
    return problems


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
  // "Permanently closed" / "Temporarily closed" banner. Matched on exact text
  // outside any review node so a review mentioning closure cannot trigger it.
  let status = 'OPERATIONAL';
  for (const el of document.querySelectorAll('span, div')) {
    const label = el.textContent.trim();
    if (label !== 'Permanently closed' && label !== 'Temporarily closed') continue;
    if (el.closest('[data-review-id]')) continue;
    if (el.children.length) continue;
    status = label === 'Permanently closed' ? 'CLOSED_PERMANENTLY' : 'CLOSED_TEMPORARILY';
    break;
  }

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
    business_status: status,
  };
}
"""

# Search-results list, used by --find to build registry records.
JS_SEARCH_RESULTS = r"""
() => {
  const out = [];
  const links = document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]');
  for (const link of links) {
    const card = link.closest('div[jsaction]') || link.parentElement;
    const name = link.getAttribute('aria-label')
      || (card ? (card.querySelector('div.qBF1Pd, div.fontHeadlineSmall') || {}).textContent : '')
      || '';
    const lines = card
      ? Array.from(card.querySelectorAll('div.W4Efsd span, div.UaQhfb span'))
          .map((s) => s.textContent.trim())
          .filter(Boolean)
      : [];
    out.push({
      name: name.trim(),
      url: link.href,
      rating: card ? ((card.querySelector('span.MW4etd') || {}).textContent || '') : '',
      review_count: card ? ((card.querySelector('span.UY7F9') || {}).textContent || '') : '',
      detail_lines: lines,
    });
  }
  return out;
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

    # -- browser ---------------------------------------------------------- #
    @contextmanager
    def _page(self):
        """Launch Chromium and yield a configured page."""
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
                yield page
            finally:
                context.close()
                browser.close()

    # -- public ----------------------------------------------------------- #
    def scrape(self, target: Target, strict_verify: bool = True) -> Place:
        with self._page() as page:
            self.log(f"target: {target.label()} (resolved as {target.kind})")
            self._goto_place(page, target)

            place = self._read_place_details(page)
            place.url = page.url
            place.place_id = target.place_id or self._read_place_id(page)
            place.slug = target.slug or slugify(place.name)

            for problem in verify_place(place, target, strict=strict_verify):
                self.log(f"WARNING: {problem}")

            self._open_reviews(page)
            self._set_sort(page, self.sort)
            self._load_all_reviews(page)
            self._expand_long_reviews(page)

            place.reviews = self._extract_reviews(page, place.url)
            self.log(f"collected {len(place.reviews)} reviews")
            return place

    def find(self, query: str, limit: int = 5, near: str | None = None) -> list[dict]:
        """Search Maps and return candidate places with their Place IDs."""
        with self._page() as page:
            page.goto(search_url(query, near=near), wait_until="domcontentloaded")
            self._dismiss_consent(page)

            # A precise query can skip the list and land straight on a place.
            if page.locator("h1.DUwDvf").count() > 0 and not page.locator(
                'div[role="feed"] a[href*="/maps/place/"]'
            ).count():
                place = self._read_place_details(page)
                return [{
                    "name": place.name,
                    "address": place.address,
                    "rating": place.rating,
                    "review_count": place.review_count,
                    "place_id": self._read_place_id(page),
                    "url": page.url,
                }]

            try:
                page.wait_for_selector('div[role="feed"] a[href*="/maps/place/"]', timeout=15_000)
            except Exception:
                return []

            results = page.evaluate(JS_SEARCH_RESULTS)[:limit]
            self.log(f"{len(results)} candidate(s); opening each to read its place id")

            candidates: list[dict] = []
            for result in results:
                entry = {
                    "name": result.get("name", ""),
                    "address": " · ".join(result.get("detail_lines", [])[:2]),
                    "rating": parse_rating(result.get("rating", "")),
                    "review_count": parse_int(result.get("review_count", "")),
                    "place_id": "",
                    "url": result.get("url", ""),
                }
                try:
                    page.goto(entry["url"], wait_until="domcontentloaded")
                    page.wait_for_selector("h1.DUwDvf", timeout=10_000)
                    details = page.evaluate(JS_PLACE_DETAILS)
                    entry["name"] = details.get("name") or entry["name"]
                    entry["address"] = (
                        re.sub(r"^Address:\s*", "", details.get("address", "")).strip()
                        or entry["address"]
                    )
                    entry["place_id"] = self._read_place_id(page)
                    entry["url"] = page.url
                except Exception as exc:
                    self.log(f"could not open {entry['name']!r}: {exc}")
                candidates.append(entry)
            return candidates

    # -- steps ------------------------------------------------------------ #
    def _read_place_id(self, page: Any) -> str:
        """Best-effort Place ID from the rendered page.

        Maps embeds the id in the page payload rather than exposing it in the
        DOM, so this is a regex over the HTML with an ftid fallback from the URL.
        """
        try:
            match = PLACE_ID_IN_PAGE_RE.search(page.content())
            if match:
                return match.group(1)
        except Exception:
            pass
        ftid = re.search(r"[?&]ftid=([^&]+)", page.url)
        if ftid:
            return ftid.group(1)
        hexid = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", page.url)
        return hexid.group(1) if hexid else ""

    def _goto_place(self, page: Any, target: Target) -> None:
        self.log(f"opening {target.url}")
        page.goto(target.url, wait_until="domcontentloaded")
        self._dismiss_consent(page)

        if target.is_precise:
            # A Place ID / place URL resolves straight to the place pane.
            try:
                page.wait_for_selector("h1.DUwDvf", timeout=20_000)
                return
            except Exception:
                pass
            if page.locator('div[role="feed"] a[href*="/maps/place/"]').count() == 0:
                raise RuntimeError(
                    f"{target.place_id or target.url} did not resolve to a place. "
                    "The Place ID may be retired -- re-pin it with --find."
                )
        self._ensure_place_page(page, target)

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

    def _ensure_place_page(self, page: Any, target: Target) -> None:
        """A text search can land on a result list; open the first hit if so."""
        try:
            page.wait_for_selector('h1.DUwDvf, div[role="feed"] a[href*="/maps/place/"]')
        except Exception:
            pass

        if page.locator("h1.DUwDvf").count() > 0:
            return

        results = page.locator('div[role="feed"] a[href*="/maps/place/"]')
        count = results.count()
        if count == 0:
            raise RuntimeError(
                "Could not find a place page. Run with --no-headless to see what "
                "Google returned, or pin the place with --find."
            )

        if count > 1:
            # The chain problem: several branches match, and picking the first is
            # a guess. Say so loudly rather than letting a wrong branch through.
            self.log(
                f"WARNING: {count} places match {target.query!r}; taking the first. "
                f"Pin the one you want with:  --find {target.query!r}"
            )
        else:
            self.log("search returned a list; opening the only result")

        results.first.click()
        page.wait_for_selector("h1.DUwDvf")
        page.wait_for_timeout(1500)

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
            business_status=raw.get("business_status", "OPERATIONAL"),
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
PLACE_FIELDS = [
    "id",
    "displayName",
    "formattedAddress",
    "rating",
    "userRatingCount",
    "primaryTypeDisplayName",
    "nationalPhoneNumber",
    "websiteUri",
    "googleMapsUri",
    "businessStatus",
    "reviews",
]


def _places_api_get(place_id: str, api_key: str) -> dict:
    """Place Details -- the precise lookup when a Place ID is already pinned."""
    import requests

    response = requests.get(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": ",".join(PLACE_FIELDS),
        },
        params={"languageCode": "en"},
        timeout=30,
    )
    if response.status_code == 404:
        raise RuntimeError(
            f"Places API does not recognise {place_id!r}. Place IDs are re-issued "
            "when Google merges or relocates a listing -- re-pin it with --find."
        )
    response.raise_for_status()
    return response.json()


def _places_api_search(query: str, api_key: str, limit: int = 1) -> list[dict]:
    """Text Search -- the fuzzy lookup used by --find."""
    import requests

    response = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": ",".join(f"places.{f}" for f in PLACE_FIELDS),
        },
        json={"textQuery": query, "languageCode": "en", "maxResultCount": limit},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("places", [])


def scrape_with_places_api(
    target: Target, api_key: str, verbose: bool = True, strict_verify: bool = True
) -> Place:
    """Official route. Returns at most five reviews -- that is an API limit."""
    if target.place_id:
        raw = _places_api_get(target.place_id, api_key)
    else:
        query = target.query or target.raw
        results = _places_api_search(query, api_key, limit=1)
        if not results:
            raise RuntimeError(f"Places API returned no results for {query!r}")
        raw = results[0]

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
        place_id=raw.get("id", target.place_id),
        business_status=raw.get("businessStatus", "OPERATIONAL"),
    )
    place.slug = target.slug or slugify(place.name)

    # The API returns the canonical id, so a pinned id that has been superseded
    # shows up here as a mismatch worth reporting.
    if target.place_id and place.place_id and place.place_id != target.place_id:
        print(
            f"[scraper] NOTE: Google now calls this place {place.place_id!r}, "
            f"not {target.place_id!r}. Update places.json.",
            file=sys.stderr,
        )

    for problem in verify_place(place, target, strict=strict_verify):
        print(f"[scraper] WARNING: {problem}", file=sys.stderr)

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


def find_with_places_api(query: str, api_key: str, limit: int = 5) -> list[dict]:
    """Candidate places with real Place IDs, straight from the official API."""
    return [
        {
            "name": raw.get("displayName", {}).get("text", ""),
            "address": raw.get("formattedAddress", ""),
            "rating": raw.get("rating"),
            "review_count": raw.get("userRatingCount"),
            "place_id": raw.get("id", ""),
            "url": raw.get("googleMapsUri", ""),
            "business_status": raw.get("businessStatus", "OPERATIONAL"),
        }
        for raw in _places_api_search(query, api_key, limit=limit)
    ]


def print_candidates(candidates: list[dict], query: str) -> None:
    """Print --find results as records ready to paste into places.json."""
    if not candidates:
        print(f"No places matched {query!r}.")
        return

    print(f"\n{len(candidates)} match(es) for {query!r}:\n")
    for i, candidate in enumerate(candidates, 1):
        rating = candidate.get("rating")
        count = candidate.get("review_count")
        summary = f"{rating} ({count} ratings)" if rating else "no rating"
        status = candidate.get("business_status", "OPERATIONAL")
        flag = "" if status == "OPERATIONAL" else f"  [{status}]"
        print(f"  {i}. {candidate['name']} — {candidate['address']}")
        print(f"     {summary}{flag}")
        if not candidate.get("place_id"):
            print("     place_id: (not found — open the URL and copy it manually)")
            print(f"     {candidate.get('url', '')}")
        print()

    print("Paste the one you want into places.json:\n")
    best = candidates[0]
    slug = slugify(f"{best['name']} {best['address'].split(',')[0]}")
    record = {
        slug: {
            "place_id": best.get("place_id", ""),
            "name": best.get("name", ""),
            "address": best.get("address", ""),
            "last_verified": date.today().isoformat(),
        }
    }
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print()


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
    if place.place_id:
        print(f"  Place ID: {place.place_id}" + ("  (verified)" if place.verified else ""))
    if place.business_status != "OPERATIONAL":
        print(f"  Status: {place.business_status}")
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
        prog="scrape_google_reviews.py",
        description="Scrape Google reviews for a restaurant, pinned by Place ID.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="A places.json slug (preferred), a Place ID, a Maps URL, "
                             "or free text to search for")
    parser.add_argument("--find", metavar="QUERY", default=None,
                        help="Search for a place and print a pasteable places.json "
                             "record instead of scraping")
    parser.add_argument("--verify-registry", action="store_true",
                        help="Re-resolve every registry entry and report drift")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help=f"Registry file (default: {DEFAULT_REGISTRY})")
    parser.add_argument("--near", default=None, metavar="LAT,LNG",
                        help="Bias a text search toward these coordinates")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="Scrape even when the place no longer matches its record")
    parser.add_argument("--backend", choices=["playwright", "places-api"], default="playwright",
                        help="playwright scrapes all reviews; places-api is official but caps at 5")
    parser.add_argument("--api-key", default=os.environ.get("GOOGLE_MAPS_API_KEY"),
                        help="Google Places API key (or set GOOGLE_MAPS_API_KEY)")
    parser.add_argument("--sort", choices=list(SORT_OPTIONS), default="newest",
                        help="Review sort order (default: newest)")
    parser.add_argument("--max-reviews", type=int, default=None,
                        help="Stop after roughly this many reviews")
    parser.add_argument("--output", default=None,
                        help="JSON output path (default: reviews_<slug>.json)")
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


def _make_scraper(args) -> GoogleMapsReviewScraper:
    return GoogleMapsReviewScraper(
        headless=args.headless,
        max_reviews=args.max_reviews,
        sort=args.sort,
        timeout=args.timeout * 1000,
        scroll_pause=args.scroll_pause,
        verbose=not args.quiet,
        browser_path=args.browser_path,
    )


def run_find(args) -> int:
    if args.api_key:
        candidates = find_with_places_api(args.find, args.api_key)
    else:
        candidates = _make_scraper(args).find(args.find, near=args.near)
    print_candidates(candidates, args.find)
    return 0 if candidates else 1


def run_verify_registry(args) -> int:
    registry = load_registry(args.registry)
    if not registry:
        print(f"{args.registry} is empty or missing — nothing to verify.")
        return 1

    scraper = None if args.api_key else _make_scraper(args)
    drifted = 0

    for slug in registry:
        target = resolve_target(slug, registry)
        try:
            if args.api_key:
                raw = _places_api_get(target.place_id, args.api_key)
                place = Place(
                    name=raw.get("displayName", {}).get("text", ""),
                    address=raw.get("formattedAddress", ""),
                    place_id=raw.get("id", ""),
                    business_status=raw.get("businessStatus", "OPERATIONAL"),
                )
            else:
                with scraper._page() as page:
                    scraper._goto_place(page, target)
                    place = scraper._read_place_details(page)
                    place.place_id = scraper._read_place_id(page)

            problems = verify_place(place, target, strict=False)
            if problems:
                drifted += 1
                print(f"  DRIFT  {slug}")
                for problem in problems:
                    print(f"         {problem}")
            else:
                print(f"  ok     {slug} — {place.name}, {place.address}")
        except Exception as exc:
            drifted += 1
            print(f"  FAIL   {slug}: {exc}")

    print(f"\n{len(registry)} entries checked, {drifted} needing attention.")
    return 1 if drifted else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.find and args.verify_registry:
        print("Use --find or --verify-registry, not both.", file=sys.stderr)
        return 2
    if args.backend == "places-api" and not args.api_key:
        print("--backend places-api needs --api-key or GOOGLE_MAPS_API_KEY", file=sys.stderr)
        return 2

    try:
        if args.find:
            return run_find(args)
        if args.verify_registry:
            return run_verify_registry(args)

        if not args.target:
            registry = load_registry(args.registry)
            known = ", ".join(sorted(registry)) if registry else "(registry is empty)"
            print(
                "No target given.\n\n"
                f"  Registered places: {known}\n\n"
                "  Scrape one:   scrape_google_reviews.py <slug>\n"
                "  Add one:      scrape_google_reviews.py --find \"Name, City\"",
                file=sys.stderr,
            )
            return 2

        registry = load_registry(args.registry)
        target = resolve_target(args.target, registry, near=args.near)

        if target.kind == "query" and not args.quiet:
            print(
                f"[scraper] NOTE: {args.target!r} is not in {args.registry}, so it is "
                "being treated as a text search. Chains have many branches — pin the "
                f"one you want with:  --find {args.target!r}",
                file=sys.stderr,
            )

        if args.backend == "places-api":
            place = scrape_with_places_api(
                target, args.api_key, verbose=not args.quiet, strict_verify=args.verify
            )
        else:
            place = _make_scraper(args).scrape(target, strict_verify=args.verify)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not place.reviews:
        print("No reviews found. Re-run with --no-headless to see what the page showed.",
              file=sys.stderr)

    output = args.output or f"reviews_{place.slug or 'place'}.json"
    write_json(place, output)
    if args.csv:
        write_csv(place.reviews, args.csv)
    if not args.quiet:
        print_summary(place)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
