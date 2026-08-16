# Google Reviews Scraper

Scrapes Google reviews for any restaurant and writes them to JSON and/or CSV —
ready to feed into sentiment analysis.

Restaurants are named in `places.json` and pinned by **Google Place ID**, so a
run always targets one exact location rather than whatever a text search happens
to match first.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Workflow

### 1. Find the place once

```bash
python scrape_google_reviews.py --find "Pizzaville Liberty Village Toronto"
```

Prints the matches with their Place IDs and a ready-to-paste registry record:

```json
{
  "pizzaville-liberty-village": {
    "place_id": "ChIJ...",
    "name": "Pizzaville",
    "address": "60 Atlantic Ave, Toronto, ON M6K 1X9",
    "last_verified": "2026-08-16"
  }
}
```

With `GOOGLE_MAPS_API_KEY` set this uses the official Places API and gets exact
IDs. Without a key it drives the browser and reads the ID out of each place
page — best-effort, and it may fall back to the older `ftid` form, which the
scraper accepts too. Google's
[Place ID Finder](https://developers.google.com/maps/documentation/places/web-service/place-id)
is the zero-code alternative.

### 2. Paste it into `places.json`

```json
{
  "pizzaville-liberty-village": { "place_id": "ChIJ...", "name": "Pizzaville", "address": "60 Atlantic Ave, Toronto, ON" },
  "pizzaville-etobicoke":       { "place_id": "ChIJ...", "name": "Pizzaville", "address": "1244 Islington Ave, Etobicoke, ON" }
}
```

`name` and `address` are not decoration — they are checked on every run (see
[Why the guard exists](#why-the-guard-exists)).

### 3. Scrape by name

```bash
python scrape_google_reviews.py pizzaville-liberty-village
python scrape_google_reviews.py pizzaville-liberty-village --sort newest --csv reviews.csv
```

Output defaults to `reviews_<slug>.json`.

## Targets

The positional argument is auto-detected, so you are never forced through the
registry for a one-off:

| What you pass | Treated as | Precise? |
| --- | --- | --- |
| `pizzaville-liberty-village` | a `places.json` entry | yes — and verified |
| `ChIJ0SPBaLM0K4gRuFqEbODjSDs` | a raw Place ID | yes |
| `0x882b...:0x3b48...` | a legacy FTID | yes |
| `https://www.google.com/maps/place/...` | a Maps URL | yes |
| `"Pizzaville Etobicoke"` | a text search | **no — warns** |

A text search is the only fuzzy option. If several places match, the scraper
says so and takes the first — fine for exploring, not for a dataset. Use
`--near "43.6389,-79.4200"` to bias a search toward a neighbourhood.

## Why the guard exists

A Place ID identifies a **business**, not an address. That gives you three
distinct failure modes, and the registry's `name`/`address` catch all of them:

- **A new restaurant replaces the old one.** It gets its own Place ID. Your
  pinned ID reports the old business as permanently closed — detected, and the
  run refuses rather than collecting a dead listing's stale reviews.
- **The ID goes stale on its own.** Google re-issues IDs when it merges
  duplicate listings or a business relocates. On the API backend the canonical
  ID comes back in the response, so drift is reported; refresh IDs older than
  about a year.
- **The listing is rebranded.** An owner renames an existing listing instead of
  creating a new one, so the Place ID *and the whole review history* carry over.
  This is the dangerous one — a pinned ID alone cannot catch it, and you would
  end up with one dataset blending two different restaurants. The name check is
  what catches it.

Any mismatch aborts the run and tells you what changed. `--no-verify` overrides.
To sweep every entry without scraping:

```bash
python scrape_google_reviews.py --verify-registry
```

## Options

| Flag | Description |
| --- | --- |
| `--find QUERY` | Search and print a pasteable registry record, then exit |
| `--verify-registry` | Re-resolve every entry and report drift |
| `--registry` | Registry path (default: `places.json`) |
| `--near LAT,LNG` | Bias a text search toward coordinates |
| `--no-verify` | Scrape even when the place stopped matching its record |
| `--backend` | `playwright` (default, all reviews) or `places-api` (official, max 5) |
| `--api-key` | Places API key, or set `GOOGLE_MAPS_API_KEY` |
| `--sort` | `newest` (default), `relevant`, `highest`, `lowest` |
| `--max-reviews` | Stop after roughly N reviews |
| `--output` / `--csv` | Output paths (JSON defaults to `reviews_<slug>.json`) |
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
    "review_count": 382,
    "place_id": "ChIJ...",
    "slug": "pizzaville-liberty-village",
    "business_status": "OPERATIONAL",
    "verified": true
  },
  "scraped_at": "2026-08-16T13:25:32+0000",
  "review_count_scraped": 3,
  "reviews": [
    {
      "review_id": "ChZDSUhNMG9nS0VJQ0FnSUMx...",
      "author": "Dana Whitfield",
      "author_meta": "Local Guide · 214 reviews · 1,032 photos",
      "rating": 5.0,
      "relative_date": "3 months ago",
      "estimated_date": "2026-05-16",
      "text": "Best pizza in the neighbourhood...",
      "likes": 12,
      "photo_count": 2,
      "owner_response": "Thanks so much for the kind words, Dana!",
      "owner_response_date": "2 months ago"
    }
  ]
}
```

`verified: true` means the resolved place still matched its registry record.
`estimated_date` is derived from Google's coarse relative timestamp, so it is an
approximation (±~15 days at month granularity); `relative_date` keeps the
verbatim string.

## Two backends

**`playwright` (default)** drives real Chromium against Google Maps: opens the
place, sorts, scrolls the review pane until no new reviews appear, clicks every
"More" link so no text is truncated, then extracts. Gets everything Maps will
serve. No API key.

**`places-api`** uses the official
[Places API (New)](https://developers.google.com/maps/documentation/places/web-service/place-details).
Google caps it at **five reviews per place** — an API limit, not a script
limitation — but it is the sanctioned, stable route, and with a pinned Place ID
it uses Place Details for an exact lookup:

```bash
python scrape_google_reviews.py pizzaville-liberty-village \
    --backend places-api --api-key "$GOOGLE_MAPS_API_KEY"
```

## Caveats

- Scraping Google Maps is against Google's Terms of Service. For production or
  commercial use, use `--backend places-api` or a licensed provider (SerpAPI,
  Outscraper, Apify).
- Google's markup uses obfuscated, rotating class names. Selectors anchor on
  `aria-label` and `data-*` attributes with fallbacks, but they will need
  maintenance eventually. If output goes empty, run `--no-headless` and look at
  the review pane.
- Maps stops serving more reviews past a few hundred for a given sort order. To
  get past that, run several sorts and merge on `review_id`.
- Scrape gently — a rate-limited or blocked IP looks like an empty result.

## Tests

```bash
python tests/test_scraper.py        # or: pytest tests/
```

37 tests covering the parsers, target resolution, registry loading, the
verification guard, and the in-page extraction JS — the last run by Chromium
against fixtures in `tests/fixtures/` that mirror the real Maps markup (owner
responses, rating-only reviews, third-party `4/5` ratings, duplicate nodes,
non-review chrome, and a permanently-closed listing). No network needed. The
browser tests skip themselves if Chromium is unavailable.
