# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gasto Súper: phone-first (iPhone Safari / home-screen) supermarket spending tracker for Spain, plus a price comparison across chains. FastAPI + SQLModel on plain Postgres (psycopg 3), vanilla JS frontend with no build step. Production: Vercel (Python runtime + CDN for `public/`) + Neon Postgres; nightly price collection runs in GitHub Actions. Docker is for local dev only.

## Commands

uv is the only dependency manager (no pip, no requirements.txt; Vercel installs from `pyproject.toml` + `uv.lock`).

```bash
docker compose up -d                  # Postgres 18 (host port 5433) + app with hot reload on :8000
docker compose run --rm migrate       # alembic upgrade head
docker compose run --rm collect run --chain mercadona --limit 50   # collector, small run
docker compose run --rm collect report                              # DB size / rows per table

uv run pytest                         # needs `docker compose up -d db` (uses gasto_test DB)
uv run pytest tests/test_compare_api.py::test_search_card_lists_every_chain   # single test
docker compose exec app pytest        # same, inside the container
for f in public/js/*.js; do node --check "$f"; done   # the only JS check (CI runs it)

uv run alembic revision --autogenerate -m "..."   # then review; tests/test_migrations.py fails if models and migrations differ
uv run python -m collectors list | run (--chain X | --source S | --all) | report
uv run python -m collectors import-xlsx FILE... [--dry-run]   # Carrefour/Dia catalogue exports (source "xlsx"; openpyxl is a dev dep)
uv run python -m collectors retype                # re-apply app/product_types.toml (also runs after every `run`)
uv run python -m app.tools.hash_password          # value for APP_PASSWORD_HASH
```

`.env` (gitignored) holds local `DATABASE_URL`, `APP_PASSWORD_HASH`, `SESSION_SECRET`, `COOKIE_SECURE=false`. The password hash uses `:` separators deliberately (a `$` would be interpolated by Compose/shells).

## Hard constraints (from the deployment design)

- **Serverless:** no startup work, no background tasks, no in-app scheduler. `app/main.py` does not create tables or seed; migrations run only via `alembic` (locally or `.github/workflows/migrate.yml`). Any outbound call in a request needs a strict timeout (~5 s) and a graceful fallback (see `refresh_listing` and `OpenFoodFactsClient`).
- **Portable Postgres:** configure only through `DATABASE_URL`; no Neon-specific features. `app/db.py` uses `NullPool` and `prepare_threshold=None` (for the Neon pooler). Web app uses the pooled URL; collectors and migrations use the direct URL (`DATABASE_URL_DIRECT` secret).
- **Static files:** `public/` is served by Vercel's CDN; FastAPI mounts it only when `VERCEL` is unset (local). Don't add auth middleware for static files. Auth is a per-router dependency (`require_session`), which also keeps CDN promotion working.
- **Auth:** every router except `app/routers/auth.py` is included with `Depends(require_session)` in `create_app()`. New routers must be added to that list. Login rate limiting is stored in Postgres (`loginattempt`), not memory.
- Seed data (chains, default stores, postal code 28020) lives in migration `0001` and is hard-coded there. `tests/conftest.py` re-seeds from that migration's `CHAINS` after truncating tables.

## Architecture

**Two domains share one database:**

1. **Spending tracker:** `Store`, `Trip`, `Purchase`, and `Product`. `Product` is a cache of scanned products keyed by EAN, or `vw:<prefix>:<item>` for variable-weight in-store labels (EAN-13 prefixes 20–29, parsed per store by `services/barcode_parser.py` + `app/store_rules.json`, never sent to Open Food Facts).
   - Scan lookup order (`routers/products.py`): tracker cache → price catalogue (`product_from_catalog`, via the canonical EAN) → Open Food Facts → the user names it manually.
   - Money is always integer cents. User input accepts `1,25` and `1.25` (`services/money.py`, mirrored in `public/js/format.js`).
   - Timestamps are `timestamptz` UTC (SQLModel `UTCDateTime`, which requires aware datetimes; use `models.utcnow()`). They are converted to Europe/Madrid only in stats/CSV and in the browser.
2. **Price comparison:** the canonical product is kept separate from each chain's listing.
   - `CanonicalProduct` (EAN optional, department food/drink/household, normalized quantity) ← `Listing` (source, chain, chain product id, `postal_code` or `""` for non-location-specific sources) ← `ListingPrice`.
   - **Price history is change-only:** `ListingPrice` rows are price *periods* (`first_seen_at`/`last_seen_at`). `services/prices.record_observation` inserts only when the price or unit price changes, otherwise it moves `last_seen_at`; observations older than the latest row are ignored. "Stale" means `last_seen_at` is more than 7 days old.
   - **Linking** (`services/catalog.link_listing`): a shared EAN means the same canonical product. A listing without an EAN gets its own canonical product, which is upgraded in place or merged (`merge_canonical`, which moves shopping-list items) once an EAN arrives. `link_source="manual"` links are never touched by collectors.
   - **Similar matching** (`services/matching.py`) is computed at read time. It requires the same department and unit, pack sizes at most 2× apart and keyword overlap on cleaned names (`services/names.clean_name`: no size, packaging or store brand). `ProductMatch` confirm/reject rows always override it.
   - **Product types** (`app/product_types.toml`, `services/product_types.py`): generic names ("Pechuga de pollo") grouping products across chains. Rules are head-anchored patterns on the cleaned name plus exclude words/categories; first match in file order wins. `CanonicalProduct.product_type` holds the slug, set in `_canonical_from_listing` and by `catalog.retype` (one read + one batched update). Types are searched through their es/en names with the synonym groups, so "chicken" lists every chicken type; `/api/compare/types` and `/api/compare/types/{slug}` compare the cheapest unit price per chain. A `ShoppingListItem` is either a product (`quantity` packs) or a type (`amount` in `amount_unit`, DB check constraint); list lines are keyed `p:<id>` / `t:<slug>`, and a type line costs each chain's cheapest way to cover the amount (`compare.cheapest_for_amount`: whole packs, or unit price × amount for "aprox"/"granel" items).
   - The read side is `services/compare.py`: cards per chain in `COMPARED_CHAINS`, "cheapest" by unit price only when there are at least 2 prices, "precio pagado" rows from tracker purchases, shopping-list totals, and a 2-store split that is returned only if it uses both stores and beats the best single store.

**Price sources** (`app/sources/`, shared by the collectors and the web "refresh now" endpoint):
- Each adapter implements `SourceAdapter` (`search`, `fetch_catalog` async generator, `fetch_product`) and never writes to the DB.
- `PoliteClient` serializes requests (min interval, retries, honors `Retry-After`).
- Sources are enabled and configured in `app/sources/sources.toml`, which includes Mercadona's category→department allowlist.
- Current sources:
  - `mercadona`: unofficial storefront API; postal code → warehouse, cached in `appsetting`; EANs only from product detail, backfilled at `max_detail_fetches` per run.
  - `easycompra`: community dataset for Dia/Carrefour (Lidl left out in `sources.toml`: no pack sizes); not location-specific; chains marked not-fresh are skipped with a warning.
  - `openprices`: postal code geocoded via Nominatim; OSM brand → chain.
  - `alcampo`: server-rendered shop pages (`window.__INITIAL_STATE__` `productEntities`), fetched with a browser User-Agent (`SourceAdapter.user_agent`) because the JSON API answers 403 to other clients. Catalogue = the `search_terms` in `sources.toml`, filtered by top-level category → department, `max_products_per_run` per night with a rotating cursor (`alcampo:cursor` in `appsetting`). Stops after 3 consecutive 403/429. No EANs, not location-specific.
- Dia/Carrefour direct APIs are behind Akamai; a browser User-Agent isn't enough there.
- `collectors/` is only the CLI and runner (`CollectorRun` rows, GitHub step summary). Collectors run on US GitHub runners against Neon in Frankfurt (~150 ms per round trip), so the runner stores products in batches of 200 via `services/catalog.store_listings` (a few statements per batch, same rules as `upsert_listing` + `record_listing_price`); a failed batch is redone per item with savepoints. Don't add per-product queries to that path. It is excluded from the Vercel bundle, so web code must not import from it.

**Search** (`services/synonyms.py`):
- Queries become ordered groups of alternatives (English→Spanish grocery table; the first translation is the primary meaning).
- SQL filtering uses word-start `LIKE` on normalized `search_text`, with whole-word matching for alternatives of 3 characters or fewer.
- Results are ranked in Python by `relevance()`, where a match on the first word dominates because Spanish names lead with the head noun.
- The tracker product search uses the same helpers in Python (`text_matches`).

**Frontend** (`public/js`, ES modules, hash router in `app.js`):
- `compare.js` holds the comparison screens, `ui.js` the shared sheets/toasts, `scanner.js` the camera/barcode flow.
- **Design sources:** `PRODUCT.md` (users, positioning, product principles) and `DESIGN.md` (visual system: off-white canvas, near-black ink, light-weight serif display, pastel gradient orbs) drive UI work. Fonts are self-hosted in `public/fonts/` (Inter body, Spectral 300 as the display substitute), exposed via CSS vars like `--display` in `public/css/app.css`. `.impeccable/config.json` records accepted design-lint exceptions.
- `api.js` dispatches a window `unauthorized` event on a 401, which shows the login screen.
- `@zxing/browser` is loaded from jsDelivr with pinned SRI. iOS needs `playsinline`/`muted`/`autoplay` and HTTPS for the camera.
- **i18n:** every UI string lives in `public/js/i18n.js` with `es` and `en` tables that must keep identical keys. The language is stored server-side (`appsetting.language` via `/api/settings`) and mirrored in localStorage for the login screen.
  - Categories are stored in Spanish and translated only for display (`tCategory`; `app/categories.py` `CATEGORIES_EN` for the CSV export).
  - API errors are machine codes (`detail`) translated as `error.<code>`.
  - Money and dates keep Spanish formatting (`1.234,56 €`, DD/MM/YYYY) in both languages.
- Charts are hand-written SVG in `charts.js`. Chain colours are fixed per chain (`CHAIN_SLOT`, `--series-N` CSS vars), never by rank.

## Tests

- Tests run against real Postgres. `conftest.py` sets env vars *before* importing app modules, runs Alembic once per session, and truncates and re-seeds per test via the `clean_db` fixture.
- Fixtures: `client` is logged in, `anon_client` isn't, and `fake_off` stands in for Open Food Facts.
- `async def` tests are auto-marked for anyio.
- Adapters are tested only against recorded responses in `tests/fixtures/` through `httpx.MockTransport` (`tests/sources_helpers.py`). Tests must never hit the real stores.
- CI (`.github/workflows/ci.yml`) uses a `postgres:18-alpine` service. `collect.yml` builds a dynamic matrix from the `target` input and passes free-text inputs via env vars, not `${{ }}` in `run:`.
