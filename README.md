# Pizzaville Google Reviews Scraper

Scrapes Google reviews for **Pizzaville, Liberty Village, Toronto** and writes them
to JSON and/or CSV — ready to feed into sentiment analysis.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
# Default: all reviews Google Maps will load, newest first -> pizzaville_reviews.json
python scrape_pizzaville_reviews.py

# JSON + CSV, newest first
python scrape_pizzaville_reviews.py --sort newest --output reviews.json --csv reviews.csv

# Watch the browser work (best way to debug a broken selector)
python scrape_pizzaville_reviews.py --no-headless --max-reviews 50

# A different location, or a Maps URL you already have
python scrape_pizzaville_reviews.py --query "Pizzaville Etobicoke"
python scrape_pizzaville_reviews.py --url "https://www.google.com/maps/place/..."
```

### Options

| Flag | Description |
| --- | --- |
| `--query` | Maps search text (default: `Pizzaville Liberty Village Toronto`) |
| `--url` | Skip the search, scrape this place URL directly |
| `--backend` | `playwright` (default, all reviews) or `places-api` (official, max 5) |
| `--api-key` | Places API key, or set `GOOGLE_MAPS_API_KEY` |
| `--sort` | `newest` (default), `relevant`, `highest`, `lowest` |
| `--max-reviews` | Stop after roughly N reviews |
| `--output` / `--csv` | Output paths |
| `--no-headless` | Show the browser window |
| `--browser-path` | Chromium binary to use instead of the bundled one (or `CHROMIUM_PATH`) |
| `--scroll-pause` | Seconds between scrolls (raise it if reviews stop loading) |
| `--quiet` | Suppress progress logging |

## Output

```json
{
  "place": {
    "name": "Pizzaville",
    "address": "60 Atlantic Ave, Toronto, ON M6K 1X9",
    "rating": 4.1,
    "review_count": 382
  },
  "scraped_at": "2026-08-15T13:25:32+0000",
  "review_count_scraped": 3,
  "reviews": [
    {
      "review_id": "ChZDSUhNMG9nS0VJQ0FnSUMx...",
      "author": "Dana Whitfield",
      "author_meta": "Local Guide · 214 reviews · 1,032 photos",
      "rating": 5.0,
      "relative_date": "3 months ago",
      "estimated_date": "2026-05-16",
      "text": "Best pizza in Liberty Village, hands down...",
      "likes": 12,
      "photo_count": 2,
      "owner_response": "Thanks so much for the kind words, Dana!",
      "owner_response_date": "2 months ago"
    }
  ]
}
```

`estimated_date` is derived from Google's coarse relative timestamp, so it is an
approximation (±~15 days for month-granularity reviews). `relative_date` is the
verbatim string if you need the original.

## Two backends

**`playwright` (default)** drives real Chromium against Google Maps: opens the
place, sorts, scrolls the review pane until no new reviews appear, clicks every
"More" link so no text is truncated, then extracts. Gets everything Maps will
serve. No API key.

**`places-api`** uses the official [Places API (New)](https://developers.google.com/maps/documentation/places/web-service/text-search):

```bash
python scrape_pizzaville_reviews.py --backend places-api --api-key "$GOOGLE_MAPS_API_KEY"
```

Google caps this at **five reviews per place** — that is an API limit, not a
script limitation. It is the sanctioned, stable route; use it if you only need a
sample or you need something that won't break.

## Caveats

- Scraping Google Maps is against Google's Terms of Service. For production or
  commercial use, use `--backend places-api` or a licensed provider (SerpAPI,
  Outscraper, Apify).
- Google's markup uses obfuscated, rotating class names. Selectors here anchor
  on `aria-label` and `data-*` attributes with several fallbacks each, but they
  will need maintenance eventually. If output goes empty, run `--no-headless`
  and check the review pane.
- Maps stops serving more reviews past a few hundred for a given sort order. To
  get past that, run multiple sorts (`newest`, `highest`, `lowest`) and merge on
  `review_id`.
- Scrape gently — a rate-limited or blocked IP looks like an empty result.

## Tests

```bash
python tests/test_scraper.py        # or: pytest tests/
```

13 tests cover the date/rating/count parsers plus the in-page extraction JS, run
by Chromium against `tests/fixtures/mock_maps_reviews.html` — a fixture that
mirrors the real Maps review markup (owner responses, rating-only reviews,
third-party `4/5` ratings, duplicate nodes, non-review chrome). This means the
extraction logic is testable without hitting Google. The browser tests skip
themselves if Chromium isn't available.
