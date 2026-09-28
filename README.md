# Gasto Súper

Phone-first web app for supermarket spending in Spain, plus a price comparison across chains.

- **Spending tracker:** scan a barcode with the iPhone camera, confirm the price, and it's logged against the current shopping trip. Includes history, stats and CSV export.
- **Price comparison:** search a product (by text or barcode) and see its price at each chain near your postal code, the unit price (€/kg, €/L, €/ud), and when that price was last seen. It also has a shopping list with a total per chain and the best split across two stores.

## Architecture

```
iPhone (Safari / home-screen app)
   │ HTTPS
   ▼
Vercel (Hobby) ── public/ served by the CDN (HTML/CSS/JS, no build step)
   │             └ app/main.py → FastAPI as one Python function (login required for /api/*)
   │ pooled connection (DATABASE_URL)
   ▼
Neon Postgres (free) ◄── GitHub Actions, nightly: python -m collectors (direct connection)
                          └ Mercadona API · EasyCompra dataset · Open Food Facts Open Prices
```

- **Backend:** Python 3.12, FastAPI and SQLModel on **plain Postgres** through psycopg 3. **Alembic** handles migrations. **uv** is the only dependency manager (Vercel installs straight from `pyproject.toml` + `uv.lock`).
- **Frontend:** plain HTML, CSS and ES modules in `public/`. Barcode scanning uses `@zxing/browser` from jsDelivr (pinned, with SRI), because iOS Safari has no `BarcodeDetector`.
- **Nothing runs in the background inside the app.** Every request finishes on its own: Open Food Facts lookups and "refresh now" have a 5 s timeout and fall back gracefully. Price collection runs in GitHub Actions.
- **Portable database:** the connection is configured only through `DATABASE_URL`, and nothing is Neon-specific. See [Moving to a self-hosted Postgres](#moving-to-a-self-hosted-postgres).

## Local development (Docker)

Docker is for local development only. It runs Postgres 18 (the same major version as Neon) and the app with hot reload.

```bash
cp .env.example .env
uv run python -m app.tools.hash_password        # paste the output into APP_PASSWORD_HASH in .env
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"   # → SESSION_SECRET in .env

docker compose up -d                            # db + app on http://localhost:8000
docker compose run --rm migrate                 # alembic upgrade head
docker compose run --rm collect run --chain mercadona --limit 200   # try a collector
docker compose run --rm collect run --all       # everything (Mercadona takes ~15 min)
docker compose run --rm collect report          # database size / rows per table
```

Postgres is published on `localhost:5433`, so it doesn't clash with a local Postgres on 5432. Each of these commands is one step and uses the same `DATABASE_URL` mechanism as production.

### Without Docker for the app

```bash
docker compose up -d db                         # or any Postgres you have
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload            # http://localhost:8000
uv run python -m collectors run --chain dia
```

`COOKIE_SECURE=false` is needed on plain `http://localhost`; docker-compose sets it for you. In production, leave it `true`.

### Tests

The tests run against a real Postgres database (the `gasto_test` database, created by `docker/postgres-init.sql`):

```bash
docker compose up -d db
uv run pytest                                   # or inside Docker: docker compose exec app pytest
```

They cover:
- barcode and price parsing, and unit-price parsing for many real size formats;
- storing a price only when it changes;
- EAN and "similar" matching, including manual overrides;
- every API route, including login, session protection and rate limiting;
- each source adapter against recorded responses in `tests/fixtures/`, so tests never contact the stores;
- the collector CLI, the SQLite import, and a check that the Alembic migrations match the models.

CI (`.github/workflows/ci.yml`) runs all of this against a Postgres 18 service container on every push.

## Deploying: Neon + Vercel + GitHub Actions

### 1. Create the Neon database

1. At [neon.com](https://neon.com), create a project: **Postgres 18**, region **AWS Europe Central 1 (Frankfurt)**. That region is next to the Vercel region set in `vercel.json` (`fra1`).
2. On the project dashboard, click **Connect** and copy two connection strings:
   - **Pooled** (the host contains `-pooler`): used by the web app on Vercel.
   - **Direct** (turn "Connection pooling" off): used by migrations and the collectors.

   Both look like `postgresql://user:pass@host/neondb?sslmode=require…`, and the app accepts them as they are.

### 2. Create the tables (migrations)

Migrations never run when the app starts. Run them either from your computer:

```bash
DATABASE_URL='<Neon DIRECT url>' uv run alembic upgrade head
```

…or from GitHub: **Actions → Migrate database → Run workflow**, typing `migrate` to confirm (step 4 sets up the secret it needs).

**Existing data from the old SQLite version** can be copied once:

```bash
# if it's still in the old Docker volume:
docker run --rm -v spendingtracker_app-data:/data -v "$PWD":/out alpine cp /data/spending.db /out/
DATABASE_URL='<Neon DIRECT url>' uv run python -m app.tools.sqlite_to_postgres spending.db
```

### 3. Deploy to Vercel

1. Push this repository to GitHub, then in Vercel choose **Add New → Project** and import it. Vercel detects FastAPI from `pyproject.toml` (the entrypoint is set in `[tool.vercel]`). Leave the build settings as they are.
2. Under **Settings → Environment Variables** (Production), add:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Neon **pooled** URL |
   | `APP_PASSWORD_HASH` | output of `uv run python -m app.tools.hash_password` |
   | `SESSION_SECRET` | `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"` |
   | `OFF_USER_AGENT` *(optional)* | e.g. `GastoSuper/0.1 (you@example.com)` |

3. Deploy. `https://<project>.vercel.app/healthz` should return `{"status":"ok"}`. Open the main URL and log in.

What's configured:
- `vercel.json` pins the region, keeps tests, collectors and migrations out of the function bundle, and adds security headers.
- `public/` is served by Vercel's CDN.
- The function limit (`maxDuration`) is 15 s, and every request is designed to finish in well under 6 s.

### 4. GitHub Actions secrets

In the repository, go to **Settings → Secrets and variables → Actions**:

| Name | Type | Value |
|---|---|---|
| `DATABASE_URL_DIRECT` | Secret | the Neon **direct** URL (used by `collect.yml` and `migrate.yml`) |
| `SOURCES_USER_AGENT` | Variable (optional) | User-Agent sent to the price sources |

### 5. Price collection

`collect.yml` runs every night at 01:30 UTC, which is 03:30 in Madrid in summer and 02:30 in winter.
- There is one job per chain (`mercadona`, `dia`, `carrefour`) plus `openprices`, so one failure doesn't stop the others.
- Only one collection runs at a time.
- Each run writes a summary to its page (products checked, prices changed, errors per chain) and finishes with the database size report.

To collect manually, go to **Actions → Collect prices → Run workflow**. Pick one target or `all`; "limit" is for quick tests.

**Minutes budget:**
- Mercadona takes about 15 minutes (about 150 category requests plus up to 400 product-detail requests, at 1 request every 1.5 s). Alcampo takes about 4 minutes (about 80 search pages at 1 every 2 s). The other jobs take about 1 minute each.
- That's roughly 20 minutes a night, or about 600 of the 2,000 free minutes a month for a private repository.
- The first two weeks fill in Mercadona EANs gradually (400 a night).

GitHub disables scheduled workflows after 60 days without repository activity. It emails you first, and you can re-enable the workflow in the Actions tab.

### 6. On the iPhone

Open the Vercel URL in Safari and log in. The session lasts 400 days. Then tap **Share → Add to Home Screen**. Vercel provides HTTPS, so the camera works with no extra setup.

## Price sources

Researched in September 2026. All of these are unofficial and can change or disappear, and each can be switched on or off in `app/sources/sources.toml`.

| Chain | Source | Location-specific | Notes / risks |
|---|---|---|---|
| Mercadona | Mercadona's storefront JSON API (`tienda.mercadona.es/api`) | **Yes**: the postal code maps to a warehouse (28020 → `mad3`) | No login needed. Undocumented, and **`robots.txt` disallows `/api`**. Used for personal use only, politely: 1 request every 1.5 s, never in parallel, food/drink/household categories only, each EAN fetched once. "Refresh now" works for Mercadona. |
| Dia, Carrefour | [EasyCompra-datos](https://github.com/elopositor/EasyCompra-datos), a community dataset updated daily | No (general prices) | Partial catalogues (Carrefour about 900, Dia about 500 with no EANs). Lidl is left out in `sources.toml`: its ~100 items have no pack sizes, so no unit price, and include non-food. Licence: personal, non-commercial use. A chain marked `fresh: false` is skipped, so its prices age and show as stale. |
| Several | [Open Prices](https://prices.openfoodfacts.org) (Open Food Facts) | Yes (10 km around the postal code) | Crowd-sourced and sparse in Spain. Shops are mapped to chains by their OpenStreetMap brand. |
| Alcampo | Alcampo's shop pages (`compraonline.alcampo.es`), read from the product data embedded in the HTML (approach from [grocery-cli](https://github.com/jgalea/grocery-cli)) | No (default region) | The JSON API returns HTTP 403 to non-browser clients, so this source sends a browser User-Agent. The catalogue is whatever the `search_terms` in `sources.toml` return (up to 50 products each), limited to food, drink and household categories. No EANs, so Alcampo only appears as "Similar" matches. Pages are 1–2 MB each; 1 request every 2 s. A run stops after 3 refused requests (HTTP 403/429) in a row. "Refresh now" works for Alcampo. |
| Lidl | Open Prices only | | Lidl Spain has no online grocery shop: its search API returns weekly offers and non-food items without unit prices. |
| Dia/Carrefour direct | not used | | Protected by Akamai (HTTP 403); a browser User-Agent isn't enough. |
| Your purchases | the spending tracker | | Shown on each product as **"precio pagado"**. |

To add a chain, add a module in `app/sources/` that implements `SourceAdapter` (`search`, `fetch_catalog`, `fetch_product`), register it in `registry.py`, and add a fixture-based test.

### How prices are stored

- **Canonical product:** name, brand, EAN, department, and quantity normalised to kg, L or units. It is separate from each chain's **listing** (the chain's product id, the postal code, and the size as the chain describes it).
- **Price history:** `listing_price` stores price *periods* (`first_seen_at` → `last_seen_at`). A new row is written only when the price or unit price changes. An unchanged price just moves `last_seen_at` forward, which keeps the Neon free tier small.
- **Stale prices:** a price is marked stale when its `last_seen_at` is more than 7 days old.
- **Matching:** listings that share an EAN are the *same* product.
  - Without a shared EAN (typical for store brands), the app looks for a *similar* product at each chain: same department and unit, comparable size (±25%), and shared keywords with brands ignored. These are always labelled "Similar".
  - On the product page you can confirm, reject or re-link a match. Your decisions always override the automatic matching.
- **Catalog scope:** only food, drink and household products are collected, for the postal code set under **Más → Código postal**.
- **Size:** `uv run python -m app.tools.db_report` (or `python -m collectors report`) prints the database size and rows per table.

## Moving to a self-hosted Postgres

No code changes are needed:

1. Create the database (Postgres 14 or later; the app uses only standard features).
2. Copy the data with `pg_dump "<Neon direct url>" | psql "<new url>"`, or run `alembic upgrade head` on an empty database.
3. Point `DATABASE_URL` at the new server: in Vercel (or wherever the app runs) and in the GitHub secret `DATABASE_URL_DIRECT`. Any `postgresql://…` or `postgresql+psycopg://…` URL works.

If the app then runs as a long-lived server instead of serverless, you can raise the pool size in `app/db.py` (it uses `NullPool` for serverless).

## Configuration

All settings are environment variables; see [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Docker DB | Postgres URL (pooled for the web app, direct for collectors and migrations) |
| `APP_PASSWORD_HASH` | *(empty: login disabled)* | scrypt hash from `app.tools.hash_password` |
| `SESSION_SECRET` | *(empty: login disabled)* | ≥ 32 chars; changing it logs out every device |
| `COOKIE_SECURE` | `true` | Set `false` only on plain-http localhost |
| `SESSION_MAX_AGE_DAYS` | `400` | Session cookie lifetime |
| `OFF_USER_AGENT`, `SOURCES_USER_AGENT` | app name | User-Agent for Open Food Facts and the price sources |
| `DEFAULT_POSTAL_CODE` | `28020` | Used until you set one in the app |
| `ALLOWED_ORIGINS` | *(empty)* | Extra CORS origins (not needed) |
| `TIMEZONE` | `Europe/Madrid` | Stats buckets and CSV export (times are stored in UTC) |

## Security

- One user. The password is stored only as a scrypt hash in an environment variable.
- The session cookie is signed, HttpOnly, Secure and SameSite=Lax, and lasts 400 days. Changing the password also invalidates old sessions.
- Every `/api/*` route except `/api/auth/*` requires the session. `/healthz` only returns `ok`.
- Login is rate-limited to 5 failures per IP per 15 minutes, with a global cap as well, stored in Postgres because serverless instances share no memory.
- All secrets live in Vercel and GitHub settings. `.env` is gitignored.

## Weighed products (EAN-13 prefixes 20–29)

In-store labels embed a price or a weight, and the layout differs by chain. The layouts are configured in [`app/store_rules.json`](app/store_rules.json):
- positions are 0-based;
- `kind: price` pre-fills the label's price;
- `kind: weight` pre-fills the weight in kg and asks for the price per kg.

These codes are never sent to Open Food Facts, and you can always override the pre-filled value.

## Project layout

```
app/
  main.py              FastAPI app (Vercel entrypoint); no startup work
  auth.py              password hashing, signed session cookie, require_session
  config.py, db.py     settings; psycopg engine from DATABASE_URL (NullPool)
  models.py, schemas.py
  routers/             auth, stores, products (scan), trips, stats (+CSV), compare, shopping_list
  services/            barcode_parser, money, openfoodfacts, quantity, text, prices (change-only history),
                       catalog (upsert + EAN linking), matching, compare (cards, list totals, split), kv
  sources/             source adapters (mercadona, easycompra, openprices), registry, sources.toml
  tools/               hash_password, db_report, sqlite_to_postgres
collectors/            CLI: python -m collectors {list,run,report}
migrations/            Alembic (0001 = initial schema + seed data)
public/                index.html, css/, js/ (app, compare, ui, api, scanner, charts, format, i18n), icons
tests/                 pytest suite + recorded fixtures
.github/workflows/     ci.yml, collect.yml, migrate.yml
vercel.json, Dockerfile (dev), docker-compose.yml (dev), alembic.ini
```

## Languages

The app is available in **Spanish and English**. To switch, go to **Más → Idioma** (or **More → Language**). The choice is saved on the server, so every device follows it, and it applies to the whole app: every screen, messages, category names, month names and the CSV export headers.

- All UI text lives in [`public/js/i18n.js`](public/js/i18n.js), with one table per language. To add a language, add a table and an entry in `LOCALES`.
- Product and store names stay as the chains publish them, which is Spanish. Categories are stored in Spanish and only translated for display.
- Money keeps Spanish formatting (`1.234,56 €`) and dates are `DD/MM/YYYY` in Europe/Madrid time, in both languages.

**Search in English:** both searches (price comparison and saved products) accept English as well as Spanish. For example, `milk` finds "Leche entera", `olive oil` finds "Aceite de oliva" and `semi-skimmed milk` finds "Leche semidesnatada".
- The grocery vocabulary is in [`app/services/synonyms.py`](app/services/synonyms.py): about 160 terms, one line each, easy to extend.
- Words match at the start of a word, and short words (3 letters or fewer) only as whole words. So `tea` doesn't match "tomate" and `ham` doesn't match "hamburguesa".
