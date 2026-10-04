# Consum: first live run

- Requests: 92
- Status: ok
- Catalogue size reported by Consum: 9084
- Products kept: 8924 (food 7108, drink 1261, household 555)
- Skipped: 160 ([('non-grocery category', 160)])
- With EAN: 8851; with a unit price: 949
- Pack price: min 0,14 €, median 2,75 €, max 240,00 €
  (a median under 0,10 € would mean the API returns cents, not euros)

Category names and where they end up: `categories.csv`. Kept products: `products.csv`.
