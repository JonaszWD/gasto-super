"""English -> Spanish grocery vocabulary, so "milk" finds "Leche entera" and "olive oil" finds
"Aceite de oliva". Product names come from Spanish chains, so searches are expanded, not data.

Each query word (or known multi-word phrase) becomes a *group* of alternatives; a product must
match every group, and any alternative within a group. Spanish words pass through unchanged.
Alternatives are ordered: the typed word, then translations with the primary meaning first
("pepper" -> pimienta before pimiento). `relevance()` uses that order to rank results.
"""

from difflib import get_close_matches
from functools import lru_cache

from app.services.text import normalize

# "english word or phrase: spanish word, ..." — one distinctive Spanish word per alternative.
_TABLE = """
milk: leche
whole: entera, entero
skimmed: desnatada, desnatado
semi skimmed: semidesnatada, semidesnatado
lactose free: lactosa
butter: mantequilla
margarine: margarina
cheese: queso
cream cheese: untable, crema
cream: nata, crema
yogurt: yogur
yoghurt: yogur
greek: griego
egg: huevo
eggs: huevo
free range: camperos, camperas
bread: pan
sliced bread: molde
baguette: barra
toast: tostado, tostadas
flour: harina
sugar: azucar
salt: sal
pepper: pimienta, pimiento
oil: aceite
olive: oliva, aceituna
sunflower: girasol
extra virgin: virgen
vinegar: vinagre
rice: arroz
pasta: pasta
spaghetti: espagueti, espaguetis
macaroni: macarron, macarrones
noodles: fideos
lentils: lenteja
chickpeas: garbanzo
beans: judia, alubia, frijol
green beans: judia
peas: guisante
tuna: atun
sardines: sardina
salmon: salmon
cod: bacalao
hake: merluza
prawns: gamba, langostino
shrimp: gamba, langostino
squid: calamar
mussels: mejillon
fish: pescado
seafood: marisco
chicken: pollo
breast: pechuga
thigh: muslo
turkey: pavo
beef: ternera, vacuno
veal: ternera
pork: cerdo
lamb: cordero
rabbit: conejo
mince: picada, picado
minced meat: picada, picado
burger: hamburguesa
sausage: salchicha
sausages: salchicha
ham: jamon
cured ham: serrano
cooked ham: cocido, york
bacon: bacon, beicon
chorizo: chorizo
meat: carne
apple: manzana
pear: pera
banana: platano, banana
orange: naranja
lemon: limon
lime: lima
grapes: uva
strawberries: fresa
strawberry: fresa
peach: melocoton
pineapple: pina
watermelon: sandia
melon: melon
kiwi: kiwi
avocado: aguacate
tomato: tomate
tomatoes: tomate
potato: patata
potatoes: patata
onion: cebolla
garlic: ajo
carrot: zanahoria
lettuce: lechuga
cucumber: pepino
courgette: calabacin
zucchini: calabacin
aubergine: berenjena
eggplant: berenjena
spinach: espinaca
broccoli: brocoli
cauliflower: coliflor
mushrooms: champinon, seta
corn: maiz
fruit: fruta
vegetables: verdura, hortaliza
salad: ensalada
nuts: frutos, nuez
almonds: almendra
walnuts: nuez
peanuts: cacahuete
peanut butter: cacahuete
olives: aceituna
crisps: fritas, snack
chips: fritas
biscuits: galleta
cookies: galleta
cereal: cereal
oats: avena
chocolate: chocolate
cocoa: cacao
jam: mermelada
honey: miel
ice cream: helado
cake: tarta, bizcocho
pizza: pizza
frozen: congelado, congelada
tinned: conserva
canned: conserva
sauce: salsa
ketchup: ketchup
mayonnaise: mayonesa
mustard: mostaza
soup: sopa, caldo
stock: caldo
spices: especia
water: agua
sparkling: gas
still: mineral
juice: zumo
coffee: cafe
tea: te, infusion
beer: cerveza
wine: vino
red wine: tinto
white wine: blanco
soda: refresco
soft drink: refresco
cola: cola
baby food: infantil, potito
detergent: detergente
fabric softener: suavizante
bleach: lejia
dishwasher: lavavajillas
washing up liquid: lavavajillas
toilet paper: higienico
kitchen roll: cocina
napkins: servilleta
bin bags: basura
sponge: estropajo, esponja
cleaner: limpiador, limpia
light: ligero, light
organic: ecologico, bio
"""


@lru_cache
def table() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in _TABLE.strip().splitlines():
        en, _, es = line.partition(":")
        out[normalize(en)] = [normalize(x) for x in es.split(",") if normalize(x)]
    return out


@lru_cache
def _vocabulary() -> dict[str, list[str]]:
    """Single words the search knows, for typo correction: English words -> their translations,
    Spanish words -> themselves."""
    vocab: dict[str, list[str]] = {}
    for en, es in table().items():
        for word in es:
            vocab.setdefault(word, [word])
        if " " not in en:
            vocab[en] = es
    return vocab


# Short words are left alone: at 4 letters a typo is too close to other real words ("eche" ~ "leche").
_FUZZY_MIN_LEN, _FUZZY_CUTOFF = 5, 0.84


def _corrected(word: str) -> list[str]:
    """Translations of the closest known word, for a word the table doesn't know ("brest" -> pechuga)."""
    vocab = _vocabulary()
    if len(word) < _FUZZY_MIN_LEN or word.isdigit() or word in vocab:
        return []
    close = get_close_matches(word, vocab.keys(), n=1, cutoff=_FUZZY_CUTOFF)
    return vocab[close[0]] if close else []


def _singular(word: str) -> str | None:
    for suffix, repl in (("ies", "y"), ("oes", "o"), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + repl
    return None


Group = list[str]  # ordered alternatives, most likely meaning first


def query_groups(query: str, max_words: int = 6) -> list[Group]:
    """Split a search into groups of alternatives: 'olive oil' -> [[olive, oliva, aceituna], [oil, aceite]]."""
    words = [w for w in normalize(query).split() if len(w) > 1 or w.isdigit()]
    tbl = table()
    groups: list[Group] = []
    i = 0
    while i < len(words) and len(groups) < max_words:
        # Longest known phrase first ("semi skimmed", "olive oil" is word-by-word).
        for n in (3, 2, 1):
            phrase = " ".join(words[i : i + n])
            if n > 1 and len(words[i : i + n]) < n:
                continue
            key = phrase if phrase in tbl else (_singular(phrase) if n == 1 else None)
            if key and key in tbl:
                groups.append(list(dict.fromkeys([phrase, *tbl[key]])))
                i += n
                break
        else:
            # Unknown word: keep it as typed, plus the translations of a close known word, so a
            # typo still finds something instead of emptying the whole search.
            groups.append(list(dict.fromkeys([words[i], *_corrected(words[i])])))
            i += 1
    return groups


def _pattern(alt: str) -> str:
    # Match at the start of a word ("leche" -> "leches"), but short words only whole
    # ("te" must not match "tomate" or "ternera", "sal" not "salsa").
    return f"% {alt} %" if len(alt) <= 3 else f"% {alt}%"


def sql_filter(column):  # type: ignore[no-untyped-def]
    """SQL conditions for a normalised text column: every group matches, any alternative per group."""
    from sqlalchemy import and_, literal, or_

    padded = literal(" ") + column + literal(" ")

    def build(groups: list[Group]):  # type: ignore[no-untyped-def]
        return and_(*[or_(*[padded.like(_pattern(a)) for a in g]) for g in groups])

    return build


def text_matches(text: str, groups: list[Group]) -> bool:
    """Python twin of sql_filter, for small in-memory lists."""
    padded = f" {normalize(text)} "

    def hit(alt: str) -> bool:
        return f" {alt} " in padded if len(alt) <= 3 else f" {alt}" in padded

    return all(any(hit(a) for a in g) for g in groups)


# ---------------------------------------------------------------- ranking

# Spanish product names lead with what the product *is* ("Pimienta negra molida",
# "Salsa pimienta verde"), so a match on the first word counts most.
POSITION_WEIGHT = (2.0, 1.1)
EXACT, PLURAL, PREFIX = 1.0, 0.9, 0.6
BRAND_WEIGHT = 0.5


def _word_match(alt: str, word: str) -> float:
    if word == alt:
        return EXACT
    if word in (alt + "s", alt + "es"):
        return PLURAL
    if len(alt) > 3 and word.startswith(alt):
        return PREFIX
    return 0.0


def relevance(name: str, brand: str | None, groups: list[Group]) -> float:
    """How well a product matches a search; higher is better. 0 = no match.

    Per query group, the best alternative counts: translation rank (primary meaning first)
    x match quality (exact > plural > prefix) x position in the name (first word counts most).
    Then a bonus for how much of the name the query covers, and a small penalty per extra word,
    so "Pimienta negra" beats "Salsa pimienta verde" and "Salmón ahumado a la pimienta".
    """
    words = normalize(name).split()
    brand_words = set(normalize(brand).split()) if brand else set()
    if not words or not groups:
        return 0.0
    total, matched_positions = 0.0, set()
    for group in groups:
        best, best_pos = 0.0, None
        for rank, alt in enumerate(group):
            alt_words = alt.split()
            rank_weight = 1.0 if rank <= 1 else max(0.5, 1.0 - 0.15 * (rank - 1))  # typed word, primary translation
            if len(alt_words) > 1:  # typed phrase ("semi skimmed"): contiguous match
                n = len(alt_words)
                for i in range(len(words) - n + 1):
                    if words[i : i + n] == alt_words:
                        score = rank_weight * EXACT * (POSITION_WEIGHT[i] if i < len(POSITION_WEIGHT) else 1.0)
                        if score > best:
                            best, best_pos = score, i
                continue
            for i, word in enumerate(words):
                quality = _word_match(alt, word)
                if quality:
                    score = rank_weight * quality * (POSITION_WEIGHT[i] if i < len(POSITION_WEIGHT) else 1.0)
                    if score > best:
                        best, best_pos = score, i
            if best == 0.0 and any(_word_match(alt, b) for b in brand_words):
                best = rank_weight * BRAND_WEIGHT
        if best == 0.0:
            return 0.0
        total += best
        if best_pos is not None:
            matched_positions.add(best_pos)
    coverage = len(matched_positions) / len(words)
    return round(total + 0.5 * coverage - 0.02 * len(words), 4)
