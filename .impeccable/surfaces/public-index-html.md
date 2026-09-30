---
version: 1
slug: "public-index-html"
primary_target: "public/index.html"
related_targets: ["public/css/app.css","public/js/app.js","public/js/compare.js"]
---

# App shell: Gasto Súper (all screens)

Scope: every screen rendered into `public/index.html` (compare, list, product, scan, history, trip, stats, more, login, sheets). Visitor mode: Operate. Redesign into the world pinned by DESIGN.md (user-supplied), adapted from marketing site to a phone app.

Confirmed with user (2026-09-30): restyle + rework layouts, same routes/features; Compare becomes first tab and start screen; dark mode derived from DESIGN.md dark tokens; chain colours = the five gradient hues deepened for ≥3:1 (validated with dataviz validator, light + dark).

## Direction contract

THESIS: A price comparison that reads like a quiet print price-list: editorial serif prices over off-white paper, one ink pill per screen. Refuses the default grocery-app look (green accent, badge soup, heavy cards).

OWN-WORLD: Canvas #f5f5f5, white cards with 1px warm hairlines, ink #0c0a09 / body #4e4e4e / muted #777169. Spectral Light 300 for screen titles and every headline price (tabular), Inter 400/500 for everything else. Ink pill primary, hairline-outline pill secondary, pill badges in surface-strong. Pastel orb (mint/peach/lavender/sky/rose) as atmosphere only, behind screen headers. Chain identity = small deepened-pastel dot + name.

STORY: User sees where a product is cheapest by unit price, how fresh each price is, and adds it to the list; the list shows per-chain totals and the best two-store split.

FIRST VIEWPORT: Compare: large serif "Comparar" title over a soft orb, search pill field with scan button inside, Search/List segmented control, then result cards whose cheapest row leads with a serif unit price. Tab bar bottom, Compare first. Primary actions in thumb zone.

FORM: Pinned by DESIGN.md (user brief); no concept roll. Seed key: none (brief-pinned).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
