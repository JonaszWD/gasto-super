// Price comparison screens: search, product detail (history + manual matching), shopping list.
import { api } from "./api.js";
import { stepChart } from "./charts.js";
import { formatDate, formatMoney, formatShortDate } from "./format.js";
import { t } from "./i18n.js";
import { Scanner, scanFeedback } from "./scanner.js";
import { closeSheet, errorMessage, h, openSheet, toast } from "./ui.js";

// Colour follows the chain, never its rank (validated categorical slots, see app.css).
const CHAIN_SLOT = { mercadona: 1, carrefour: 2, dia: 3, lidl: 4, alcampo: 5 };
const cstate = { query: "", results: null, postalCode: "" };

function unitPrice(row) {
  if (row.unit_price_cents == null || !row.unit) return "";
  return t(`compare.per_unit.${row.unit}`, { price: formatMoney(row.unit_price_cents) });
}

function matchBadge(row) {
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
          ? `<strong>${h(unitPrice(row))}</strong><span class="muted small">${formatMoney(row.price_cents)}</span>`
          : `<strong>${formatMoney(row.price_cents)}</strong>`}
    </div>
    ${meta.length ? `<div class="prow-meta muted small">${meta.join(" · ")}</div>` : ""}
    ${actions ? rowActions(row) : ""}
  </li>`;
}

function rowActions(row) {
  const btns = [];
  if (row.can_refresh) btns.push(`<button class="chip" data-refresh="${row.listing_id}">↻ ${h(t("compare.refresh"))}</button>`);
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
  const sub = [p.brand, p.ean].filter(Boolean).join(" · ");
  const inner = `${img}<div class="product-title"><h2>${h(p.name)}</h2><p class="muted small">${h(sub)}</p></div>`;
  return link ? `<a class="product-head" href="#/compare/product/${p.id}">${inner}</a>` : `<div class="product-head">${inner}</div>`;
}

function cardHtml(card) {
  return `<article class="card pcard">
    ${productHead(card)}
    <ul class="prows">${card.prices.map((r) => priceRow(r)).join("")}${paidRows(card)}</ul>
    <button class="btn block" data-add="${card.product.id}">+ ${h(t("compare.add_to_list"))}</button>
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

function segmented(active) {
  return `<div class="segmented two" role="tablist">
    <a role="tab" href="#/compare" aria-selected="${active === "search"}">${h(t("compare.tab_search"))}</a>
    <a role="tab" href="#/compare/list" aria-selected="${active === "list"}">${h(t("compare.tab_list"))}</a>
  </div>`;
}

// ---------------------------------------------------------------- search

export async function renderCompare(view) {
  if (!cstate.postalCode) cstate.postalCode = (await api.settings()).postal_code;
  view.innerHTML = `
    <header class="screen-head"><h1>${h(t("compare.title"))}</h1></header>
    ${segmented("search")}
    <form class="searchbar" role="search">
      <input type="search" name="q" value="${h(cstate.query)}" placeholder="${h(t("compare.search_placeholder"))}"
             autocomplete="off" autocorrect="off" enterkeyhint="search" aria-label="${h(t("compare.search"))}">
      <button type="button" class="btn icon" data-act="scan" aria-label="${h(t("compare.scan"))}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V4h3M21 7V4h-3M3 17v3h3M21 17v3h-3M7 8v8M10 8v8M13 8v8M17 8v8"/></svg>
      </button>
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
      const body = await api.compareSearch(q);
      cstate.results = body.results;
      results.innerHTML = body.results.length
        ? body.results.map(cardHtml).join("")
        : `<p class="muted empty">${h(t("compare.no_results"))}</p>`;
      bindAdd(results);
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
  if (cstate.query && cstate.results) {
    results.innerHTML = cstate.results.length ? cstate.results.map(cardHtml).join("") : `<p class="muted empty">${h(t("compare.no_results"))}</p>`;
    bindAdd(results);
  } else {
    run(cstate.query);
  }
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
    <header class="screen-head"><a class="back" href="#/compare">‹ ${h(t("common.back"))}</a></header>
    <article class="card pcard">
      ${productHead(detail, false)}
      <ul class="prows">${detail.prices.map((r) => priceRow(r, { actions: true })).join("")}${paidRows(detail)}</ul>
      <button class="btn block" data-add="${detail.product.id}">+ ${h(t("compare.add_to_list"))}</button>
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
    ${segmented("list")}
    <div id="list-body"><p class="muted pad">${h(t("common.loading"))}</p></div>`;
  drawList(view.querySelector("#list-body"), await api.shoppingList());
}

function drawList(root, data) {
  if (!data.items.length) {
    root.innerHTML = `<p class="muted empty">${h(t("list.empty"))}</p>`;
    return;
  }
  const split = data.split;
  root.innerHTML = `
    <ul class="items">${data.items.map((it) => `<li class="item list-item">
        <a class="item-main" href="#/compare/product/${it.product_id}"><span class="item-name">${h(it.name)}</span></a>
        <div class="stepper small">
          <button class="step" data-qty="${it.product_id}" data-d="-1" aria-label="${h(t("purchase.decrease"))}">−</button>
          <span class="qty-val">${it.quantity}</span>
          <button class="step" data-qty="${it.product_id}" data-d="1" aria-label="${h(t("purchase.increase"))}">+</button>
        </div>
      </li>`).join("")}</ul>
    <section class="card">
      <h2>${h(t("list.by_chain"))}</h2>
      <ul class="prows">${data.chains.map((c, i) => `<li class="prow ${i === 0 && c.missing === 0 ? "cheapest" : ""}">
          <div class="prow-main"><span class="chain"><i class="key s${CHAIN_SLOT[c.chain_id] ?? 0}"></i>${h(c.chain_name)}</span>
            ${c.missing ? `<span class="tag warn">${h(c.missing === 1 ? t("list.missing_one") : t("list.missing", { n: c.missing }))}</span>`
                        : `<span class="tag ok">${h(t("list.complete"))}</span>`}
            ${c.similar ? `<span class="tag muted">${h(t("list.similar_count", { n: c.similar }))}</span>` : ""}
            ${c.stale ? `<span class="tag stale">${h(t("compare.stale"))}</span>` : ""}
          </div>
          <div class="prow-price">${c.missing === c.lines.length ? `<span class="muted">—</span>` : `<strong>${formatMoney(c.total_cents)}</strong>`}</div>
        </li>`).join("")}</ul>
      <p class="muted small">${h(t("list.caveat"))}</p>
    </section>
    ${split ? `<section class="card">
      <h2>${h(t("list.split"))}</h2>
      <p class="hero">${formatMoney(split.total_cents)}</p>
      <p>${h(t("list.split_detail", { a: split.chain_names[0], b: split.chain_names[1], total: formatMoney(split.total_cents) }))}</p>
      ${split.missing ? `<p class="notice">${h(t("list.split_missing", { n: split.missing }))}</p>` : ""}
      <ul class="plain small">${data.items.map((it) => {
        const chain = split.assignment[it.product_id];
        const name = chain ? split.chain_names[split.chain_ids.indexOf(chain)] : "—";
        return `<li><i class="key s${CHAIN_SLOT[chain] ?? 0}"></i>${h(it.name)} → <strong>${h(name)}</strong></li>`;
      }).join("")}</ul>
    </section>` : ""}`;

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
      <strong>${h(t("compare.cheaper_elsewhere"))}</strong>
      <span>${h(t("compare.cheaper_detail", { chain: alt.chain_name, price: formatMoney(alt.price_cents), savings: formatMoney(alt.savings_cents) }))}${
        alt.match !== "exact" ? ` · ${h(t("compare.cheaper_similar"))}` : ""}</span>
    </a>`;
  } catch {
    return ""; // never block the scan flow
  }
}
