# Category tree (design sketch)

Status: idea, not implemented.

## The idea

Put the products people buy most into a browsable tree, so a search lands on a node and every
chain's version of "the same thing" is already grouped:

```
Carne y pescado                      (section)
└─ Carne
   └─ Pollo                          (family)
      ├─ Pechuga de pollo            (type = leaf, compared by €/kg)
      │    Mercadona  Pechuga de pollo fileteada 500 g   6,90 €/kg
      │    Dia        Filetes pechuga pollo 600 g        7,15 €/kg
      │    Carrefour  Pechuga entera de pollo 1 kg       6,49 €/kg
      ├─ Muslos de pollo
      ├─ Alas de pollo
      └─ Pollo entero
```

Searching "chicken" opens *Pollo* (children with their cheapest €/kg per chain); searching
"pechuga" opens the leaf directly. You can also browse down from the top without typing.

## Build on product types, don't add a second system

`app/product_types.toml` already does the hard part: rules that put a product from any chain
into a generic type (`CanonicalProduct.product_type`), and `/api/compare/types/{slug}` already
compares a type's cheapest unit price per chain. The tree is those types as **leaves**, plus
parent nodes above them.

- **Leaves = product types.** One unit per leaf (kg, l or units), so their prices are always
  comparable. This is the only level where prices are compared directly.
- **Inner nodes = navigation.** *Carne › Pollo* never shows "the cheapest chicken"; it shows a
  table of its children (rows) × chains (columns). Comparing wings against breast per kg would
  mislead.
- **Variants are facets, not nodes.** Fileteada / entera, ecológico, bandeja familiar, halal:
  stored as tags on the listing (from the name) and offered as filters on the leaf, instead of
  splitting into ever-smaller leaves with two products each.

### "5000 products" means about 400–600 leaves

The 5000 most-bought products are the *products hanging under* the leaves, not 5000 nodes.
A Spanish basket is concentrated: roughly 400–600 generic types (leche semidesnatada, huevos L,
pechuga de pollo, papel higiénico...) cover the bulk of it, with each leaf holding a handful of
products per chain. Three levels are enough:

| Level   | Example               | Count  | Source                                         |
|---------|-----------------------|--------|------------------------------------------------|
| Section | Carne y pescado       | ~13    | `app/categories.py` (already used by tracker) |
| Family  | Pollo                 | ~80    | new, hand-written                              |
| Type    | Pechuga de pollo      | ~500   | `product_types.toml` (59 today)                |

Reusing the tracker's `CATEGORIES` as sections means a purchase in the tracker and a node in the
price tree share a vocabulary (and the stats screen could later drill down by family).

### Where does "most bought" come from?

No chain publishes sales. Proxies, combined:

1. **Spread across chains**: a type all four chains stock is a staple. Cheap to compute from
   `Listing`.
2. **Our own purchases**: `Purchase` rows linked to a canonical product (via EAN) say what this
   household actually buys; those leaves go first.
3. **MAPA *Panel de consumo alimentario*** (public, yearly volumes per food category in Spain)
   to decide which families deserve leaves at all.
4. **Mercadona's catalogue** is curated (~4–5k SKUs, roughly "what a Spanish household buys"),
   so its category list is a good checklist for missing families.

The target is a coverage number, not a list: *% of food/drink listings with a type* and
*% of tracked purchases with a type*, shown by `collectors report`.

## Data model

The tree lives in the TOML, loaded in memory with `lru_cache` like today. No new table, no
migration: `CanonicalProduct.product_type` keeps holding a leaf slug, and a node's products are
`product_type IN (descendant leaves)`, which is already indexed.

```toml
[nodes.carne]
es = "Carne"
en = "Meat"
parent = "carne-y-pescado"          # a section key

[nodes.pollo]
es = "Pollo"
en = "Chicken"
parent = "carne"
# Optional family-level rule: a product that is clearly chicken but fits no leaf
# lands in "pollo › otros" instead of being untyped.
match = ["pollo*"]
exclude = ["@prepared"]

[types.pechuga-de-pollo]
parent = "pollo"                    # the only new key on existing types
es = "Pechuga de pollo"
...
```

Loader checks (a unit test, like the existing TOML ones): every `parent` exists, no cycles,
every type has a parent, and all products classified under a leaf share one unit.

## Classification

Same first-match rule engine, extended in two small ways:

- **Family fallback.** If no leaf matches, try the family rules; the product is stored with the
  family slug and shown under *Pollo › Otros*. Untyped products shrink a lot without writing
  hundreds of narrow rules.
- **Chain category hint.** Map chain categories to families (Mercadona *"Aves y pollo"* →
  `pollo`) in `sources.toml`, next to the existing department allowlist. Used as a filter, not a
  classifier: a leaf only matches if the product's chain category maps to one of the leaf's
  ancestors (or has no mapping). This kills false positives like "pechuga de pavo" in
  *charcutería*.

Both stay in memory and run in `catalog.retype` (one read + one batched update), so the
collector path stays free of per-product queries.

## Search and comparison

- `types_for_query` searches nodes as well as leaves (es/en names + synonym groups). Results
  rank leaves over families when both match ("pechuga de pollo" → the leaf; "pollo" → the
  family).
- `GET /api/compare/tree/{slug}` (`slug` optional = root) returns the node, its breadcrumb and
  its children, each child with product count, chains present and cheapest unit price per chain.
  For a leaf it returns what `/compare/types/{slug}` returns today (that endpoint can become an
  alias).
- Frontend: a breadcrumb (*Carne › Pollo › Pechuga*) on the type screen, a "Browse" entry on the
  compare screen, and facet chips on leaves. Child rows reuse `typeListHtml`.
- Shopping list: type lines stay leaf-only (they need one unit to price an amount). A
  family line ("any chicken, 1 kg") is possible later but not needed.

## Building the tree without hand-writing 500 rules from scratch

1. New CLI: `python -m collectors untyped --top 300` lists untyped food/drink canonical products
   grouped by the first two words of the cleaned name (Spanish names lead with the head noun, so
   "pechuga pollo", "filete merluza" cluster well), ranked by the popularity proxies above.
2. Each cluster becomes a candidate leaf; candidates are drafted offline (by hand or with an LLM
   over the cluster's names), reviewed, and committed to the TOML. Nothing runs at request time.
3. `collectors retype` + the coverage report show the gain; repeat until coverage plateaus.

## Phases

1. Add `[nodes]` + `parent` to the TOML for the 59 existing types; breadcrumb on the type screen;
   search returns families. Small, no schema change.
2. Browse screen and `/compare/tree`; coverage report and `untyped` CLI.
3. Grow to ~500 leaves family by family (fresh meat, dairy, pantry first: highest spend).
4. Family fallback, chain-category hints, facets.

## Open questions

- Fresh vs frozen: a separate section (*Congelados*, as in the tracker) or a facet on the same
  leaf? Proposal: facet, since people compare "pechuga" regardless of where it sits.
- Store-brand vs branded: same leaf with a "marca blanca" facet, or split? Proposal: same leaf;
  the comparison is about the cheapest way to buy the thing.
- Household products (detergent, paper) are excluded from typing today; they'd need leaves with
  their own units (lavados, rollos, metros).
