// Price comparison screens: search, product types, product detail (history + manual matching), shopping list.
import { api } from "./api.js";
import { stepChart } from "./charts.js";
import { formatDate, formatMoney, formatQuantity, formatShortDate } from "./format.js";
import { getLocale, t } from "./i18n.js";
import { Scanner, scanFeedback } from "./scanner.js";
import { closeSheet, errorMessage, h, icon, openSheet, toast } from "./ui.js";

// Colour follows the chain, never its rank (validated categorical slots, see app.css).
const CHAIN_SLOT = { mercadona: 1, carrefour: 2, dia: 3, lidl: 4, alcampo: 5, consum: 6 };
const cstate = { query: "", results: null, types: [], postalCode: "" };

function unitPrice(row) {
  if (row.unit_price_cents == null || !row.unit) return "";
  return t(`compare.per_unit.${row.unit}`, { price: formatMoney(row.unit_price_cents) });
}

// Pack size ("400 g", "1,5 L", "6 ud"): names are shown without it, see app/services/names.py.
function sizeLabel(value, unit) {
  if (!value || !unit) return "";
  if (unit === "unit") return t("compare.size.unit", { n: formatQuantity(value) });
  if (value < 1) return `${formatQuantity(value * 1000)} ${unit === "kg" ? "g" : "ml"}`;
  return `${formatQuantity(value)} ${unit === "kg" ? "kg" : "L"}`;
}

function matchBadge(row) {
  if (row.match === "exact") return ""; // the default; only deviations get a badge
  const cls = { exact: "ok", similar: "warn", confirmed: "warn", none: "muted" }[row.match];
  const label = row.match === "none" ? t("compare.not_found") : t(`compare.${row.match}`);
  return `<span class="tag ${cls}">${h(label)}</span>`;
}

function priceRow(row, { actions = false } = {}) {
  const priced = row.price_cents != null;
  const meta = [];
  if (priced) {
    meta.push(row.stale
      ? `<span class="tag stale">${h(t("compare.stale"))}</span> ${h(formatDate(row.last_seen_at))}`
      : h(t("compare.updated", { date: formatDate(row.last_seen_at) })));
  }
  if (priced && !row.location_specific) meta.push(h(t("compare.not_location")));
  if (row.match !== "exact" && row.listing_name) meta.push(h(row.listing_name));
  return `<li class="prow ${row.cheapest ? "cheapest" : ""} ${priced ? "" : "absent"}">
    <div class="prow-main">
      <span class="chain"><i class="key s${CHAIN_SLOT[row.chain_id] ?? 0}"></i>${h(row.chain_name)}</span>
      ${matchBadge(row)}
      ${row.cheapest ? `<span class="tag best">${h(t("compare.cheapest"))}</span>` : ""}
    </div>
    <div class="prow-price">
      ${!priced ? `<span class="muted">${h(row.match === "none" ? "—" : t("compare.no_price"))}</span>`
        : unitPrice(row)
          // Unit price (€/kg, €/L, €/ud) is what compares across pack sizes, so it leads.
          ? `<strong>${h(unitPrice(row))}</strong><span class="muted small">${[formatMoney(row.price_cents), h(sizeLabel(row.quantity_value, row.unit))].filter(Boolean).join(" · ")}</span>`
          : `<strong>${formatMoney(row.price_cents)}</strong>`}
    </div>
    ${meta.length ? `<div class="prow-meta muted small">${meta.join(" · ")}</div>` : ""}
    ${actions ? rowActions(row) : ""}
  </li>`;
}

function rowActions(row) {
  const btns = [];
  if (row.can_refresh) btns.push(`<button class="chip" data-refresh="${row.listing_id}">${icon.refresh}${h(t("compare.refresh"))}</button>`);
  if (row.match === "similar") {
    btns.push(`<button class="chip" data-match="confirm" data-listing="${row.listing_id}">${h(t("compare.match_confirm"))}</button>`);
    btns.push(`<button class="chip" data-match="reject" data-listing="${row.listing_id}">${h(t("compare.match_reject"))}</button>`);
    btns.push(`<button class="chip" data-match="relink" data-listing="${row.listing_id}">${h(t("compare.match_relink"))}</button>`);
  } else if (row.match === "confirmed") {
    btns.push(`<button class="chip" data-match="reset" data-listing="${row.listing_id}">${h(t("compare.match_reset"))}</button>`);
  }
  return btns.length ? `<div class="prow-actions">${btns.join("")}</div>` : "";
}

function paidRows(card) {
  return card.paid.map((p) => `<li class="prow paid">
      <div class="prow-main"><span class="chain">${h(p.store_name)}</span><span class="tag paid">${h(t("compare.paid"))}</span></div>
      <div class="prow-price"><strong>${formatMoney(p.unit_price_cents)}</strong></div>
      <div class="prow-meta muted small">${h(formatDate(p.paid_at))}</div>
    </li>`).join("");
}

function productHead(card, link = true) {
  const p = card.product;
  const img = card.image_url ? `<img src="${h(card.image_url)}" alt="" class="product-img" loading="lazy" referrerpolicy="no-referrer">`
                             : `<div class="product-img placeholder" aria-hidden="true"></div>`;
  const sub = [sizeLabel(p.quantity_value, p.quantity_unit), p.brand, p.ean].filter(Boolean).join(" · ");
  const inner = `${img}<div class="product-title"><h2>${h(p.name)}</h2><p class="muted small">${h(sub)}</p></div>`;
  return link ? `<a class="product-head" href="#/compare/product/${p.id}">${inner}</a>` : `<div class="product-head">${inner}</div>`;
}

function cardHtml(card) {
  return `<article class="card pcard">
    ${productHead(card)}
    <ul class="prows">${card.prices.map((r) => priceRow(r)).join("")}${paidRows(card)}</ul>
    <button class="btn block" data-add="${card.product.id}">${icon.plus}${h(t("compare.add_to_list"))}</button>
  </article>`;
}

function bindAdd(root) {
  root.querySelectorAll("[data-add]").forEach((b) => {
    b.onclick = async () => {
      try {
        await api.addToList(Number(b.dataset.add));
        toast(t("compare.added_to_list"));
      } catch (err) {
        toast(errorMessage(err), "error");
      }
    };
  });
}

// ---------------------------------------------------------------- search

export async function renderCompare(view) {
  if (!cstate.postalCode) cstate.postalCode = (await api.settings()).postal_code;
  view.innerHTML = `
    <header class="screen-head"><h1>${h(t("compare.title"))}</h1></header>
    <form class="searchbar" role="search">
      ${icon.search}
      <input type="search" name="q" value="${h(cstate.query)}" placeholder="${h(t("compare.search_placeholder"))}"
             autocomplete="off" autocorrect="off" enterkeyhint="search" aria-label="${h(t("compare.search"))}">
      <button type="button" class="btn icon" data-act="scan" aria-label="${h(t("compare.scan"))}">${icon.scan}</button>
    </form>
    <div id="results"></div>`;
  const form = view.querySelector("form");
  const results = view.querySelector("#results");
  const run = async (q) => {
    cstate.query = q;
    if (!q) {
      results.innerHTML = `<p class="muted empty">${h(t("compare.hint", { pc: cstate.postalCode }))}</p>`;
      return;
    }
    results.innerHTML = `<p class="muted pad">${h(t("common.loading"))}</p>`;
    try {
      const [body, types] = await Promise.all([api.compareSearch(q), searchTypes(q)]);
      cstate.results = body.results;
      cstate.types = types;
      drawResults();
    } catch (err) {
      results.innerHTML = `<p class="error pad">${h(errorMessage(err))}</p>`;
    }
  };
  form.onsubmit = (e) => {
    e.preventDefault();
    form.elements.q.blur();
    run(form.elements.q.value.trim());
  };
  view.querySelector('[data-act="scan"]').onclick = () =>
    scanSheet((code) => {
      form.elements.q.value = code;
      run(code);
    });
  function drawResults() {
    const cards = cstate.results.map(cardHtml).join("");
    results.innerHTML = cstate.types.length
      ? `<h2 class="section-title">${h(t("type.section"))}</h2>${typeListHtml(cstate.types, "#/compare")}
         ${cards ? `<h2 class="section-title">${h(t("type.products_section"))}</h2>${cards}` : ""}`
      : cards || `<p class="muted empty">${h(t("compare.no_results"))}</p>`;
    bindAdd(results);
  }
  if (cstate.query && cstate.results) {
    drawResults();
  } else {
    run(cstate.query);
  }
}

// ---------------------------------------------------------------- product types

function typeName(pt) {
  return getLocale() === "en" ? pt.name_en : pt.name_es;
}

/** Types never block a search: without them the product results still show. */
async function searchTypes(q) {
  try {
    return (await api.productTypes(q)).types;
  } catch {
    return [];
  }
}

function perUnit(cents, unit) {
  return cents == null || !unit ? "" : t(`compare.per_unit.${unit}`, { price: formatMoney(cents) });
}

// Amount of a type on the list: "250 g", "1,5 kg", "1 L", "12 ud".
function amountLabel(amount, unit) {
  return sizeLabel(amount, unit);
}

// Stepper increments per unit: meat and veg by 250 g, drinks by half litres, units one by one.
const AMOUNT_STEP = { kg: 0.25, l: 0.5, unit: 1 };

function parseAmount(text) {
  const n = Number(String(text).trim().replace(",", "."));
  return Number.isFinite(n) && n > 0 && n <= 100 ? Math.round(n * 1000) / 1000 : null;
}

/** Ask how much of a type to add ("1 kg" by default), then add it. */
function addTypeSheet(pt) {
  const unitName = t(`type.unit.${pt.unit}`);
  const sheet = openSheet(`
    <form class="stack" novalidate>
      <h2>${h(t("type.amount_title", { name: typeName(pt) }))}</h2>
      <div class="field qty"><span>${h(t("type.amount", { unit: unitName }))}</span>
        <div class="stepper">
          <button type="button" class="step" data-step="-1" aria-label="${h(t("purchase.decrease"))}">${icon.minus}</button>
          <input name="amount" inputmode="decimal" autocomplete="off" value="${h(formatQuantity(pt.default_amount))}">
          <button type="button" class="step" data-step="1" aria-label="${h(t("purchase.increase"))}">${icon.plus}</button>
        </div></div>
      <p class="muted small">${h(t("type.amount_hint"))}</p>
      <p class="error" hidden></p>
      <div class="actions">
        <button type="button" class="btn" data-act="cancel">${h(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${h(t("compare.add_to_list"))}</button>
      </div>
    </form>`);
  const form = sheet.querySelector("form");
  const input = form.elements.amount;
  const step = AMOUNT_STEP[pt.unit] ?? 1;
  sheet.querySelectorAll("[data-step]").forEach((b) => {
    b.onclick = () => {
      const next = (parseAmount(input.value) ?? 0) + step * Number(b.dataset.step);
      input.value = formatQuantity(Math.max(step, Math.round(next * 1000) / 1000));
    };
  });
  sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const amount = parseAmount(input.value);
    const err = form.querySelector(".error");
    if (amount == null) {
      err.textContent = t("type.amount_invalid");
      err.hidden = false;
      return;
    }
    try {
      await api.addTypeToList(pt.slug, amount, pt.unit);
      closeSheet({ silent: true });
      toast(t("compare.added_to_list"));
    } catch (error) {
      err.textContent = errorMessage(error);
      err.hidden = false;
    }
  };
}

function typeListHtml(types, base) {
  return `<ul class="items">${types.map((pt) => `<li><a class="item" href="${base}/type/${h(pt.slug)}" data-type="${h(pt.slug)}">
      <span class="item-main"><span class="item-name">${h(typeName(pt))}</span>
        <span class="item-sub">${h(t("type.summary", { n: pt.products, c: pt.chains }))}</span></span>
      ${pt.min_unit_price_cents != null ? `<span class="item-price">${h(t("type.from", { price: perUnit(pt.min_unit_price_cents, pt.unit) }))}
        <span class="muted small pick-chain">${h(t("list.from_chain", { chain: pt.min_chain_name }))}</span></span>` : ""}
      ${icon.chevron.replace("<svg", '<svg class="chev"')}</a></li>`).join("")}</ul>`;
}

function offerPrice(o) {
  const size = sizeLabel(o.quantity_value, o.unit);
  const up = perUnit(o.unit_price_cents, o.unit);
  return up
    ? `<strong>${h(up)}</strong><span class="muted small">${[formatMoney(o.price_cents), h(size)].filter(Boolean).join(" · ")}</span>`
    : `<strong>${formatMoney(o.price_cents)}</strong>`;
}

function offerMeta(o) {
  const seen = o.stale
    ? `<span class="tag stale">${h(t("compare.stale"))}</span> ${h(formatDate(o.last_seen_at))}`
    : h(t("compare.updated", { date: formatDate(o.last_seen_at) }));
  return [seen, o.location_specific ? "" : h(t("compare.not_location"))].filter(Boolean).join(" · ");
}

/** One chain: its cheapest product of the type, the others folded away. */
function typeChainRow(c) {
  if (!c.offers.length) {
    return `<li class="prow absent">
      <div class="prow-main"><span class="chain"><i class="key s${CHAIN_SLOT[c.chain_id] ?? 0}"></i>${h(c.chain_name)}</span></div>
      <div class="prow-price"><span class="muted">—</span></div>
      <div class="prow-meta muted small">${h(t("type.none"))}</div>
    </li>`;
  }
  const [best, ...rest] = c.offers;
  return `<li class="prow ${c.cheapest ? "cheapest" : ""}">
    <div class="prow-main">
      <span class="chain"><i class="key s${CHAIN_SLOT[c.chain_id] ?? 0}"></i>${h(c.chain_name)}</span>
      ${c.cheapest ? `<span class="tag best">${h(t("compare.cheapest"))}</span>` : ""}
    </div>
    <div class="prow-price">${offerPrice(best)}</div>
    <div class="prow-meta small"><a href="#/compare/product/${best.product_id}">${h(best.name)}</a>
      <span class="muted"> · ${offerMeta(best)}</span></div>
    <div class="prow-actions"><button class="chip" data-add="${best.product_id}">${icon.plus}${h(t("compare.add_to_list"))}</button></div>
    ${rest.length ? `<details class="prow-meta type-more"><summary>${h(rest.length === 1 ? t("type.more_one") : t("type.more", { n: rest.length }))}</summary>
      <ul class="type-offers">${rest.map((o) => `<li>
          <a href="#/compare/product/${o.product_id}">${h(o.name)}</a>
          <span class="prow-price">${offerPrice(o)}</span>
          <button class="btn icon" data-add="${o.product_id}" aria-label="${h(t("compare.add_to_list"))}">${icon.plus}</button>
        </li>`).join("")}</ul>
    </details>` : ""}
  </li>`;
}

export async function renderType(view, slug, back) {
  view.innerHTML = `<p class="muted pad">${h(t("common.loading"))}</p>`;
  const pt = await api.productType(slug);
  const stocked = pt.chains.filter((c) => c.offers.length);
  const products = stocked.reduce((n, c) => n + c.offers.length, 0);
  const best = pt.chains.find((c) => c.cheapest);
  view.innerHTML = `
    <header class="screen-head">
      <a class="back" href="${back}">${icon.back}${h(t("common.back"))}</a>
      <h1>${h(typeName(pt))}</h1>
      <p class="muted">${h(t("type.count", { n: products, c: stocked.length }))}</p>
    </header>
    ${best ? `<section class="list-answer">
      <p>${h(t("type.cheapest_at", { chain: best.chain_name }))}</p>
      <p class="hero">${h(perUnit(best.offers[0].unit_price_cents, best.offers[0].unit))}</p>
    </section>` : ""}
    ${pt.unit && stocked.length ? `<button class="btn primary block type-add" data-act="add-type">${icon.plus}${h(t("type.add_to_list", { amount: amountLabel(pt.default_amount, pt.unit) }))}</button>
      <p class="muted small type-add-hint">${h(t("type.add_hint"))}</p>` : ""}
    <article class="card pcard"><ul class="prows">${pt.chains.map(typeChainRow).join("")}</ul>
      <p class="muted small caveat">${h(t("type.caveat"))}</p>
    </article>`;
  bindAdd(view);
  view.querySelector('[data-act="add-type"]')?.addEventListener("click", () => addTypeSheet(pt));
}

function scanSheet(onCode) {
  const sheet = openSheet(`
    <h2>${h(t("compare.scan"))}</h2>
    <section class="camera"><video playsinline muted autoplay></video><div class="camera-frame" aria-hidden="true"></div>
      <p class="camera-hint">${h(t("scan.aim"))}</p></section>
    <p class="error" hidden></p>
    <div class="actions"><button class="btn" data-act="cancel">${h(t("common.cancel"))}</button></div>`,
  { onClose: () => scanner.stop() });
  const scanner = new Scanner(sheet.querySelector("video"), (code) => {
    scanFeedback();
    closeSheet({ silent: true });
    onCode(code);
  });
  sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  scanner.start().catch((err) => {
    const p = sheet.querySelector(".error");
    p.textContent = err.message;
    p.hidden = false;
  });
}

// ---------------------------------------------------------------- product detail

export async function renderProduct(view, id) {
  view.innerHTML = `<p class="muted pad">${h(t("common.loading"))}</p>`;
  const detail = await api.compareProduct(id);
  draw(view, detail);
}

function draw(view, detail) {
  view.innerHTML = `
    <header class="screen-head">
      <a class="back" href="#/compare">${icon.back}${h(t("common.back"))}</a>
      <div class="product-detail">${productHead(detail, false)}</div>
    </header>
    <article class="card pcard">
      <ul class="prows">${detail.prices.map((r) => priceRow(r, { actions: true })).join("")}${paidRows(detail)}</ul>
      <button class="btn primary block" data-add="${detail.product.id}">${icon.plus}${h(t("compare.add_to_list"))}</button>
    </article>
    <section class="card">
      <h2>${h(t("compare.history"))}</h2>
      <div id="history-chart"></div>
      <div id="history-table"></div>
    </section>`;
  bindAdd(view);

  view.querySelectorAll("[data-match]").forEach((b) => {
    b.onclick = async () => {
      try {
        const updated = await api.setMatch(detail.product.id, Number(b.dataset.listing), b.dataset.match);
        toast(t("compare.match_saved"));
        draw(view, updated);
      } catch (err) {
        toast(errorMessage(err), "error");
      }
    };
  });
  view.querySelectorAll("[data-refresh]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api.refreshListing(Number(b.dataset.refresh));
        toast(t("compare.refreshed"));
        draw(view, await api.compareProduct(detail.product.id));
      } catch (err) {
        toast(errorMessage(err), "error");
        b.disabled = false;
      }
    };
  });

  const series = detail.history.map((hst) => ({
    label: hst.match === "exact" ? hst.chain_name : `${hst.chain_name} (${t(`compare.${hst.match}`).toLowerCase()})`,
    slot: CHAIN_SLOT[hst.chain_id] ?? 0,
    periods: hst.points.map((p) => ({ start: Date.parse(p.first_seen_at), end: Date.parse(p.last_seen_at), value: p.price_cents })),
  }));
  const chart = document.getElementById("history-chart");
  if (!series.some((s) => s.periods.length)) {
    chart.innerHTML = `<p class="muted">${h(t("compare.no_history"))}</p>`;
    return;
  }
  stepChart(chart, series, { fmtValue: formatMoney, fmtDate: (ms) => formatShortDate(new Date(ms).toISOString()) });
  const rows = detail.history.flatMap((hst) => hst.points.map((p) => ({ chain: hst.chain_name, ...p })))
    .sort((a, b) => Date.parse(b.first_seen_at) - Date.parse(a.first_seen_at));
  document.getElementById("history-table").innerHTML = `<details><summary>${h(t("stats.show_table"))}</summary>
    <table class="table"><thead><tr><th>${h(t("stats.col_store"))}</th><th>${h(t("stats.col_date"))}</th><th class="num">${h(t("stats.col_price"))}</th></tr></thead>
    <tbody>${rows.map((r) => `<tr><td>${h(r.chain)}</td><td>${h(formatDate(r.first_seen_at))} – ${h(formatDate(r.last_seen_at))}</td><td class="num">${formatMoney(r.price_cents)}</td></tr>`).join("")}</tbody></table></details>`;
}

// ---------------------------------------------------------------- shopping list

export async function renderList(view) {
  view.innerHTML = `
    <header class="screen-head"><h1>${h(t("list.title"))}</h1></header>
    <form class="searchbar" novalidate>
      ${icon.plus}
      <input type="text" name="q" placeholder="${h(t("list.add_placeholder"))}" aria-label="${h(t("list.add_label"))}"
             autocomplete="off" autocorrect="off" enterkeyhint="done">
      <button type="button" class="btn icon" data-act="scan" aria-label="${h(t("compare.scan"))}">${icon.scan}</button>
    </form>
    <div id="list-body"><p class="muted pad">${h(t("common.loading"))}</p></div>`;
  const form = view.querySelector("form");
  const root = view.querySelector("#list-body");
  // "leche, pan; huevos" adds three products, each picked from the catalogue in turn.
  form.onsubmit = (e) => {
    e.preventDefault();
    const terms = form.elements.q.value.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
    if (!terms.length) return;
    form.elements.q.value = "";
    form.elements.q.blur();
    addTerms(root, terms);
  };
  view.querySelector('[data-act="scan"]').onclick = () => scanSheet((code) => addTerms(root, [code], { exactAdd: true }));
  drawList(root, await api.shoppingList());
}

/** Search each term and let the user pick the product; the list redraws after every add. */
async function addTerms(root, terms, { exactAdd = false } = {}) {
  for (const [i, term] of terms.entries()) {
    let results;
    let types = [];
    try {
      [results, types] = await Promise.all([
        api.compareSearch(term).then((b) => b.results),
        exactAdd ? [] : searchTypes(term),
      ]);
    } catch (err) {
      toast(errorMessage(err), "error");
      return;
    }
    // A scanned barcode that hits exactly one product needs no choice.
    const choice = exactAdd && results.length === 1
      ? results[0].product.id
      : await pickSheet(term, results, types, i + 1, terms.length);
    if (choice === "cancel") return;
    if (choice == null) continue;
    try {
      drawList(root, await api.addToList(choice));
      toast(t("compare.added_to_list"));
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }
}

function cheapestPrice(card) {
  const priced = card.prices.filter((r) => r.price_cents != null);
  if (!priced.length) return null;
  return priced.find((r) => r.cheapest) ?? priced.reduce((a, b) => (b.price_cents < a.price_cents ? b : a));
}

/** Resolves to a product id, null (skip) or "cancel" (sheet dismissed, or a type opened). */
function pickSheet(term, results, types, index, total) {
  return new Promise((resolve) => {
    const options = results.slice(0, 6).map((card) => {
      const p = card.product;
      const best = cheapestPrice(card);
      const sub = [sizeLabel(p.quantity_value, p.quantity_unit), p.brand].filter(Boolean).join(" · ");
      return `<li><button class="item" data-pick="${p.id}">
          <span class="item-main"><span class="item-name">${h(p.name)}</span>
            <span class="item-sub">${h(sub)}</span></span>
          <span class="item-price">${best
            ? `${formatMoney(best.price_cents)}<span class="muted small pick-chain">${h(t("list.from_chain", { chain: best.chain_name }))}</span>`
            : `<span class="muted small">${h(t("compare.no_price"))}</span>`}</span>
        </button></li>`;
    }).join("");
    const sheet = openSheet(`
      ${total > 1 ? `<p class="muted small">${h(t("list.pick_progress", { i: index, n: total }))}</p>` : ""}
      <h2>${h(t("list.pick_title", { term }))}</h2>
      ${types.length ? `<h3 class="section-title">${h(t("type.section"))}</h3>${typeListHtml(types, "#/list")}
        ${options ? `<h3 class="section-title">${h(t("type.products_section"))}</h3>` : ""}` : ""}
      ${options ? `<ul class="items">${options}</ul>` : types.length ? "" : `<p class="muted">${h(t("list.pick_none", { term }))}</p>`}
      <div class="actions">
        <button class="btn" data-act="cancel">${h(t("common.cancel"))}</button>
        ${total > 1 && index < total ? `<button class="btn" data-act="skip">${h(t("list.skip"))}</button>` : ""}
      </div>`, { onClose: () => resolve("cancel") });
    const done = (value) => {
      closeSheet({ silent: true });
      resolve(value);
    };
    sheet.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => done(Number(b.dataset.pick))));
    // Opening a type leaves the list screen, so the remaining terms are dropped.
    sheet.querySelectorAll("[data-type]").forEach((a) => (a.onclick = () => done("cancel")));
    sheet.querySelector('[data-act="skip"]')?.addEventListener("click", () => done(null));
    sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  });
}

function lineName(ln) {
  return ln.product_type && getLocale() === "en" && ln.name_en ? ln.name_en : ln.name;
}

/** What a chain's total is made of; type lines say which product was picked and how many. */
function chainBreakdown(c) {
  return `<details class="prow-meta type-more"><summary>${h(t("list.detail"))}</summary>
    <ul class="type-offers">${c.lines.map((ln) => {
      const what = ln.product_type
        ? (ln.chosen_name ? `${h(ln.chosen_name)} · ${h(ln.packs == null ? t("list.loose", { amount: amountLabel(ln.amount, ln.unit) }) : t("list.packs", { n: ln.packs }))}` : "")
        : (ln.quantity > 1 ? `${ln.quantity} ×` : "");
      return `<li><span><span>${h(lineName(ln))}</span>${what ? `<span class="muted small block-line">${what}</span>` : ""}</span>
        <span class="prow-price">${ln.price_cents == null ? `<span class="muted">—</span>` : `<strong>${formatMoney(ln.price_cents)}</strong>`}</span></li>`;
    }).join("")}</ul>
  </details>`;
}

function drawList(root, data) {
  if (!data.items.length) {
    root.innerHTML = `<p class="muted empty">${h(t("list.empty"))}</p>`;
    return;
  }
  const split = data.split;
  const hasTypes = data.items.some((it) => it.product_type);
  // The answer leads: the cheapest chain (chains come sorted by fewest missing, then total), then the split, then the items.
  const top = data.chains[0];
  const best = top && top.missing < top.lines.length ? top : null;
  root.innerHTML = `
    ${best ? `<section class="list-answer">
      <p>${h(best.missing ? t("list.best_partial", { chain: best.chain_name, n: best.missing }) : t("list.best_at", { chain: best.chain_name }))}</p>
      <p class="hero">${hasTypes ? "≈ " : ""}${formatMoney(best.total_cents)}</p>
    </section>` : ""}
    <section class="card">
      <h2>${h(t("list.by_chain"))}</h2>
      <ul class="prows">${data.chains.map((c) => `<li class="prow ${c === best ? "cheapest" : ""}">
          <div class="prow-main"><span class="chain"><i class="key s${CHAIN_SLOT[c.chain_id] ?? 0}"></i>${h(c.chain_name)}</span>
            ${c.missing ? `<span class="tag warn">${h(c.missing === 1 ? t("list.missing_one") : t("list.missing", { n: c.missing }))}</span>`
                        : `<span class="tag ok">${h(t("list.complete"))}</span>`}
            ${c.similar ? `<span class="tag muted">${h(t("list.similar_count", { n: c.similar }))}</span>` : ""}
            ${c.stale ? `<span class="tag stale">${h(t("compare.stale"))}</span>` : ""}
          </div>
          <div class="prow-price">${c.missing === c.lines.length ? `<span class="muted">—</span>` : `<strong>${hasTypes ? "≈ " : ""}${formatMoney(c.total_cents)}</strong>`}</div>
          ${c.missing === c.lines.length ? "" : chainBreakdown(c)}
        </li>`).join("")}</ul>
      <p class="muted small caveat">${h(t("list.caveat"))}${hasTypes ? ` ${h(t("list.caveat_types"))}` : ""}</p>
    </section>
    ${split ? `<section class="card">
      <h2>${h(t("list.split"))}</h2>
      <p class="hero">${hasTypes ? "≈ " : ""}${formatMoney(split.total_cents)}</p>
      <p class="muted">${h(t("list.split_detail", { a: split.chain_names[0], b: split.chain_names[1], total: formatMoney(split.total_cents) }))}</p>
      ${split.missing ? `<p class="notice">${h(t("list.split_missing", { n: split.missing }))}</p>` : ""}
      <ul class="plain small">${data.items.map((it) => {
        const chain = split.assignment[it.key];
        const name = chain ? split.chain_names[split.chain_ids.indexOf(chain)] : "—";
        return `<li><i class="key s${CHAIN_SLOT[chain] ?? 0}"></i><span>${h(lineName(it))}</span><strong>${h(name)}</strong></li>`;
      }).join("")}</ul>
    </section>` : ""}
    <h2 class="section-title">${h(t("list.items", { n: data.items.length }))}</h2>
    <ul class="items">${data.items.map((it) => it.product_type ? `<li class="item list-item">
        <a class="item-main" href="#/list/type/${h(it.product_type)}"><span class="item-name">${h(lineName(it))}</span>
          <span class="item-sub"><span class="tag muted">${h(t("list.type_tag"))}</span></span></a>
        <div class="stepper small">
          <button class="step" data-type-amount="${h(it.product_type)}" data-d="-1" aria-label="${h(t("purchase.decrease"))}">${icon.minus}</button>
          <span class="qty-val">${h(amountLabel(it.amount, it.unit))}</span>
          <button class="step" data-type-amount="${h(it.product_type)}" data-d="1" aria-label="${h(t("purchase.increase"))}">${icon.plus}</button>
        </div>
      </li>` : `<li class="item list-item">
        <a class="item-main" href="#/compare/product/${it.product_id}"><span class="item-name">${h(it.name)}</span></a>
        <div class="stepper small">
          <button class="step" data-qty="${it.product_id}" data-d="-1" aria-label="${h(t("purchase.decrease"))}">${icon.minus}</button>
          <span class="qty-val">${it.quantity}</span>
          <button class="step" data-qty="${it.product_id}" data-d="1" aria-label="${h(t("purchase.increase"))}">${icon.plus}</button>
        </div>
      </li>`).join("")}</ul>`;

  root.querySelectorAll("[data-type-amount]").forEach((b) => {
    b.onclick = async () => {
      const slug = b.dataset.typeAmount;
      const item = data.items.find((x) => x.product_type === slug);
      const step = AMOUNT_STEP[item.unit] ?? 1;
      const next = Math.round((item.amount + step * Number(b.dataset.d)) * 1000) / 1000;
      try {
        drawList(root, next <= 0 ? await api.removeTypeFromList(slug) : await api.setListTypeAmount(slug, next));
      } catch (err) {
        toast(errorMessage(err), "error");
      }
    };
  });

  root.querySelectorAll("[data-qty]").forEach((b) => {
    b.onclick = async () => {
      const id = Number(b.dataset.qty);
      const item = data.items.find((x) => x.product_id === id);
      const next = item.quantity + Number(b.dataset.d);
      try {
        drawList(root, next < 1 ? await api.removeFromList(id) : await api.setListQuantity(id, next));
      } catch (err) {
        toast(errorMessage(err), "error");
      }
    };
  });
}

/** "¿Más barato en otro sitio?" block for the scan sheet. Returns HTML or "". */
export async function alternativeHtml(barcode, storeId, priceCents) {
  try {
    const { alternative: alt } = await api.alternative(barcode, storeId, priceCents);
    if (!alt) return "";
    return `<a class="alt" href="#/compare/product/${alt.product_id}">
      <strong>${h(t("compare.cheaper_elsewhere"))}</strong>${icon.chevron}
      <span>${h(t("compare.cheaper_detail", { chain: alt.chain_name, price: formatMoney(alt.price_cents), savings: formatMoney(alt.savings_cents) }))}${
        alt.match !== "exact" ? ` · ${h(t("compare.cheaper_similar"))}` : ""}</span>
    </a>`;
  } catch {
    return ""; // never block the scan flow
  }
}
