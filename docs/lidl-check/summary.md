# Lidl: three ways of reading the catalogue

## C: catalogue, sold in shops only (store=1)

- Requests: 10 (0 retries), time: 19 s
- Items read: 556, unique: 554
- Top-level categories: [('Food', 217), ('Moda', 105), ('Hogar y cocina', 60), ('NonFood', 38), ('Vivir y amueblar', 34), ('Deporte y ocio', 27), ('Tienda de bricolaje', 24), ('Multimedia', 13), ('P+F', 13), ('Vida salvaje', 9), ('F+V', 8), ('El mundo de los niños', 4)]
- Sold in shops (store=true): 327; with packaging: 200; with base price: 11

## B: 127 grocery term searches

- Requests: 246 (0 retries), time: 390 s
- Items read: 7567, unique: 1793
- Top-level categories: [('Food', 4378), ('Hogar y cocina', 1282), ('Moda', 672), ('Tienda de bricolaje', 279), ('El mundo de los niños', 246), ('Vivir y amueblar', 211), ('F+V', 185), ('Deporte y ocio', 100), ('P+F', 72), ('Salud y cuidados', 59), ('Vida salvaje', 36), ('Multimedia', 25)]
- Sold in shops (store=true): 4505; with packaging: 3944; with base price: 203

## A: whole catalogue (q=*)

- Requests: 101 (0 retries), time: 223 s
- Items read: 5813, unique: 5803
- Top-level categories: [('Moda', 1943), ('Tienda de bricolaje', 962), ('Vivir y amueblar', 845), ('Hogar y cocina', 759), ('El mundo de los niños', 379), ('Deporte y ocio', 313), ('Food', 220), ('Salud y cuidados', 162), ('Multimedia', 113), ('Vida salvaje', 45), ('NonFood', 38), ('P+F', 13)]
- Sold in shops (store=true): 327; with packaging: 203; with base price: 67

