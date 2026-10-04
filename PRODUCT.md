# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

Phone-first web app, used mainly from iPhone Safari and as a home-screen app (`display: standalone`, portrait). Mobile web, not native.

## Users

People who buy groceries in Spain and want to know where their basket is cheapest. Today the app is private (one shared password). The plan is to open it to a wider audience later, so product decisions should not assume a single user forever.

Users are bilingual: Spanish and English are both used for real. Neither language is a secondary nicety.

## Product Purpose

Gasto Súper answers "where should I buy this, and what am I really spending?" for Spanish supermarkets.

Price comparison comes first. Users search a product by text or barcode and see its price at each chain near their postal code, with a unit price (€/kg, €/L, €/ud) and when that price was last seen. A shopping list shows the total per chain and the best split across two stores.

Spending tracking supports the comparison. Users scan barcodes in the aisle, confirm the price, and log it against the current trip. History, stats and CSV export follow from that log, and the prices people actually paid show up in comparisons as "precio pagado".

Success means a user can decide quickly which store to go to and trust that the numbers are honest and recent.

## Positioning

- One comparison across Mercadona, Carrefour, Dia, Lidl, Alcampo and Consum, keyed to the user's postal code.
- Unit-price-first: "cheapest" is decided by unit price, and only when at least two prices exist.
- Price history is recorded only when prices change, and the app shows how recent each price is. Prices not seen for more than 7 days are marked as stale, not hidden.
- Real paid prices from the user's own scans sit next to prices collected from the chains.
- Similar-product matching is transparent, and users can confirm or reject a match.

## Operating Context

- In the store: standing in the aisle, often using one hand (the other on the cart or basket), sometimes with poor signal. Scanning uses the phone camera (iOS needs HTTPS, `playsinline`).
- Planning at home: searching products, building the shopping list, choosing a store or a two-store split.
- Prices are collected nightly by GitHub Actions. The web app's "refresh now" makes a live fetch with a 5 s timeout and falls back gracefully.
- Money is shown in Spanish format (`1.234,56 €`, DD/MM/YYYY) in both UI languages.

## Capabilities and Constraints

- Tabs: Scan, Compare, History, Stats, More. Language setting is stored server-side.
- Barcode scanning via `@zxing/browser` (iOS Safari has no `BarcodeDetector`). Variable-weight in-store labels (EAN prefixes 20–29) are parsed per store.
- Product lookup order: tracker cache → price catalogue → Open Food Facts → user names it manually.
- Stack: vanilla HTML/CSS/ES modules in `public/`, no build step. FastAPI + Postgres behind it. Served by Vercel's CDN, serverless, with no background work in requests.
- Charts are hand-written SVG. Chain colours are fixed per chain and never assigned by rank.
- Every UI string is in `public/js/i18n.js` with matching `es`/`en` keys. Categories are stored in Spanish.
- Price coverage differs by chain and source (for example, Alcampo and EasyCompra have no EANs and are not location-specific). The UI must be honest about missing or stale data.
- Undecided: multi-user accounts, sign-up and data separation for a wider audience.

## Brand Commitments

- Name: **Gasto Súper**. Existing icon at `public/icons/`.
- Spanish is the default language (`lang="es"`).

## Evidence on Hand

- Real price data from the nightly collectors, plus catalogue exports in `SupermaketData/` (Carrefour, Dia, Mercadona xlsx).
- No testimonials, user counts, savings figures or press. Do not make them up.

## Product Principles

1. **Decision first.** Every comparison screen should help answer "where do I buy this?" quickly. Unit price and freshness are what make that answer possible.
2. **Honest numbers.** Show how old a price is, when coverage is missing and how confident a match is. Never imply a price is current or comparable when it isn't.
3. **Usable in the aisle.** Main actions work one-handed, on a phone, with a weak connection. Scanning and logging must never block on the network.
4. **Two languages, equal footing.** Spanish and English copy get the same care. Money and date formats stay Spanish in both.
5. **Built to grow.** Keep today's single-user shortcuts out of the product's shape so it can open to more people later.

## Accessibility & Inclusion

- Main flows must work one-handed: primary actions within thumb reach and large tap targets.
- Full parity between Spanish and English. Layouts must handle the length differences between the two languages.
