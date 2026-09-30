import { api } from "./api.js";
import { barChart, lineChart, rankedBars } from "./charts.js";
import {
  centsToInput, formatDate, formatDateTime, formatMoney, formatMonthLong, formatMonthShort,
  formatPlainDate, formatQuantity, formatShortDate, parseDecimal, parsePriceToCents,
} from "./format.js";
import { alternativeHtml, renderCompare, renderList, renderProduct, renderType } from "./compare.js";
import { applyI18n, getLocale, LOCALES, setLocale, t, tCategory } from "./i18n.js";
import { Scanner, scanFeedback } from "./scanner.js";
import { closeSheet, confirmSheet, errorMessage, h, icon, openSheet, promptSheet, sheetHooks, sheetOpen, toast } from "./ui.js";

const state = {
  stores: [],
  categories: [],
  trip: null, // current open trip (TripDetail) or null
  statsRange: "month",
  scanner: null,
};

const $view = document.getElementById("view");

// ---------- helpers ----------

const axisMoney = (cents) => `${Math.round(cents / 100)} €`;
const itemsLabel = (n) => (n === 1 ? t("trip.items_one") : t("trip.items", { n }));

function categoryOptions(selected) {
  const opts = [`<option value="">${h(t("product.no_category"))}</option>`];
  const cats = [...state.categories];
  if (selected && !cats.includes(selected)) cats.push(selected);
  // Values stay the stored (Spanish) label; only the visible text is translated.
  for (const c of cats) opts.push(`<option value="${h(c)}" ${c === selected ? "selected" : ""}>${h(tCategory(c))}</option>`);
  return opts.join("");
}

sheetHooks.beforeOpen = () => state.scanner?.stop();
sheetHooks.afterClose = () => resumeScanner();

// ---------- scan screen ----------

function resumeScanner() {
  if (currentRoute().name === "scan" && state.trip && !sheetOpen()) startScanner();
}

async function startScanner() {
  const video = document.getElementById("video");
  if (!video) return;
  const overlay = document.getElementById("camera-start");
  if (!state.scanner || state.scanner.video !== video) {
    state.scanner?.stop();
    state.scanner = new Scanner(video, onCode);
  }
  try {
    await state.scanner.start();
    overlay.hidden = true;
  } catch (err) {
    overlay.hidden = false;
    overlay.querySelector(".camera-msg").textContent = err.message;
  }
}

async function onCode(code) {
  scanFeedback();
  await showProductSheet(code);
}

function renderStorePicker() {
  $view.innerHTML = `
    <header class="screen-head">
      <h1>${h(t("trip.pick_store"))}</h1>
      <p class="muted">${h(t("trip.pick_store_hint"))}</p>
    </header>
    <div class="store-grid">
      ${state.stores.map((s) => `<button class="store-btn" data-store="${s.id}">${h(s.name)}</button>`).join("")}
      <button class="store-btn add" data-act="add-store">${icon.plus}${h(t("trip.add_store"))}</button>
    </div>`;
  $view.querySelectorAll("[data-store]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        state.trip = await api.startTrip(Number(btn.dataset.store));
        renderScan();
      } catch (err) {
        toast(errorMessage(err), "error");
        if (err.code === "trip_already_open") await loadTrip().then(renderScan);
      }
    };
  });
  $view.querySelector('[data-act="add-store"]').onclick = () => addStoreSheet(renderStorePicker);
}

function addStoreSheet(after) {
  promptSheet({
    title: t("trip.add_store"),
    label: t("trip.new_store_name"),
    submit: async (name) => {
      const store = await api.addStore(name);
      state.stores.push(store);
      closeSheet({ silent: true });
      after();
    },
  });
}

function purchaseRow(p) {
  const qty = p.quantity !== 1 ? `${formatQuantity(p.quantity)} × ${formatMoney(p.unit_price_cents)}` : "";
  return `<li><button class="item" data-purchase="${p.id}">
      <span class="item-main"><span class="item-name">${h(p.name)}</span>
        <span class="item-sub">${h(qty || tCategory(p.category) || "")}</span></span>
      <span class="item-price">${formatMoney(p.total_cents)}</span>
    </button></li>`;
}

function renderScan() {
  if (!state.trip) {
    state.scanner?.stop();
    renderStorePicker();
    return;
  }
  const trip = state.trip;
  $view.innerHTML = `
    <section class="trip-bar">
      <div>
        <div class="trip-store">${h(trip.store_name)}</div>
        <div class="muted small trip-count">${h(itemsLabel(trip.item_count))}</div>
      </div>
      <div class="trip-total" aria-label="${h(t("trip.total"))}">${formatMoney(trip.total_cents)}</div>
    </section>
    <section class="camera">
      <video id="video" playsinline muted autoplay></video>
      <div class="camera-frame" aria-hidden="true"></div>
      <p class="camera-hint">${h(t("scan.aim"))}</p>
      <button id="camera-start" class="camera-start" hidden>
        <span class="camera-msg"></span>
        <span class="btn primary">${h(t("scan.start_camera"))}</span>
      </button>
    </section>
    <section class="recent">
      <h2 class="section-title">${h(t("trip.recent"))}</h2>
      ${trip.purchases.length
        ? `<ul class="items">${trip.purchases.map(purchaseRow).join("")}</ul>`
        : `<p class="muted empty">${h(t("trip.empty"))}</p>`}
    </section>
    <div class="bottom-actions">
      <button class="btn" data-act="manual">${h(t("scan.manual"))}</button>
      <button class="btn" data-act="close-trip">${h(t("trip.close"))}</button>
    </div>`;

  document.getElementById("camera-start").onclick = () => startScanner();
  $view.querySelector('[data-act="manual"]').onclick = manualEntrySheet;
  $view.querySelector('[data-act="close-trip"]').onclick = closeTripFlow;
  bindPurchaseRows(trip.purchases, async () => {
    await loadTrip();
    renderScan();
  });
  startScanner();
}

function manualEntrySheet() {
  promptSheet({
    title: t("scan.manual_title"),
    label: t("scan.manual_title"),
    placeholder: t("scan.manual_placeholder"),
    inputmode: "numeric",
    submit: async (code) => {
      closeSheet({ silent: true });
      await showProductSheet(code.replace(/\D/g, ""));
    },
  });
}

async function closeTripFlow() {
  const trip = state.trip;
  const ok = await confirmSheet(
    t("trip.close_confirm", { store: trip.store_name, total: formatMoney(trip.total_cents) }),
    t("trip.close"),
  );
  if (!ok) return;
  try {
    await api.closeTrip(trip.id);
    state.trip = null;
    toast(t("trip.closed"));
    renderScan();
  } catch (err) {
    toast(errorMessage(err), "error");
  }
}

async function showProductSheet(code) {
  openSheet(`<p class="muted center pad">${h(t("scan.looking_up"))}</p>`);
  let scan;
  try {
    scan = await api.scan(code, state.trip?.store_id);
  } catch (err) {
    closeSheet({ silent: true });
    toast(errorMessage(err), "error");
    return;
  }

  const product = scan.product;
  const weighed = scan.kind === "variable_weight";
  const variable = scan.kind !== "standard";
  const notices = [];
  if (!scan.valid_checksum) notices.push(t("scan.bad_checksum"));
  if (variable) notices.push(t("product.weighed"));
  if (scan.kind === "variable_price") notices.push(t("product.weighed_price", { price: formatMoney(scan.embedded_price_cents) }));
  if (weighed) notices.push(t("product.weighed_weight", { kg: formatQuantity(scan.weight_grams / 1000) }));
  if (!product) notices.push(t(scan.lookup_error ? "product.lookup_error" : "product.not_found"));

  const sheet = openSheet(
    `<form class="stack" novalidate>
      <div class="product-head">
        ${product?.image_url ? `<img src="${h(product.image_url)}" alt="" class="product-img" referrerpolicy="no-referrer">` : `<div class="product-img placeholder" aria-hidden="true"></div>`}
        <div class="product-title">
          ${product ? `<h2 data-name>${h(product.name)}</h2>
            <p class="muted small">${h([product.brand, tCategory(product.category)].filter(Boolean).join(" · "))}</p>
            <button type="button" class="link" data-act="rename">${h(t("product.edit_name"))}</button>`
          : `<h2 class="muted">${h(scan.barcode)}</h2>`}
        </div>
      </div>
      ${notices.map((n) => `<p class="notice">${h(n)}</p>`).join("")}
      <div class="name-fields" ${product ? "hidden" : ""}>
        <label class="field"><span>${h(t("product.name"))}</span>
          <input name="name" value="${h(product?.name ?? "")}" autocomplete="off" enterkeyhint="next" maxlength="200"></label>
        <label class="field"><span>${h(t("product.category"))}</span>
          <select name="category">${categoryOptions(product?.category)}</select></label>
      </div>
      <div class="row">
        <label class="field grow"><span>${h(t(weighed ? "purchase.price_per_kg" : "purchase.price"))}</span>
          <input name="price" inputmode="decimal" autocomplete="off" enterkeyhint="done" placeholder="0,00"
                 value="${h(centsToInput(scan.suggested_unit_price_cents))}"></label>
        ${quantityField(scan.suggested_quantity, weighed)}
      </div>
      ${scan.last_unit_price_cents != null ? `<p class="muted small">${h(t("purchase.last_price", { price: formatMoney(scan.last_unit_price_cents) }))}</p>` : ""}
      <p class="line-total"><span>${h(t("purchase.line_total"))}</span> <strong data-total></strong></p>
      <div data-alt></div>
      <p class="error" hidden></p>
      <div class="actions">
        <button type="button" class="btn" data-act="cancel">${h(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${h(t("purchase.save"))}</button>
      </div>
    </form>`,
  );

  const form = sheet.querySelector("form");
  wireQuantityAndTotal(form, weighed);
  if (scan.kind === "standard") {
    // "¿Más barato en otro sitio?": compare against the price about to be saved.
    const altBox = sheet.querySelector("[data-alt]");
    let altTimer;
    const refreshAlt = async () => {
      const html = await alternativeHtml(scan.barcode, state.trip?.store_id, parsePriceToCents(form.elements.price.value));
      if (altBox.isConnected) altBox.innerHTML = html;
    };
    form.elements.price.addEventListener("input", () => {
      clearTimeout(altTimer);
      altTimer = setTimeout(refreshAlt, 500);
    });
    refreshAlt();
  }
  sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  sheet.querySelector('[data-act="rename"]')?.addEventListener("click", () => {
    sheet.querySelector(".name-fields").hidden = false;
    form.elements.name.focus();
  });
  if (!product) form.elements.name.focus();
  else if (!form.elements.price.value) form.elements.price.focus();

  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = form.querySelector(".error");
    const showError = (msg) => {
      err.textContent = msg;
      err.hidden = false;
    };
    const priceCents = parsePriceToCents(form.elements.price.value);
    const quantity = parseDecimal(form.elements.quantity.value, 3);
    const name = form.elements.name.value.trim();
    const category = form.elements.category.value || null;
    if (priceCents == null) return showError(t("error.invalid_price"));
    if (!quantity || quantity <= 0) return showError(t("error.invalid_quantity"));
    if (!name) return showError(t("error.name_required"));
    try {
      if (product && (name !== product.name || category !== (product.category ?? null))) {
        await api.saveProduct(scan.product_key, { name, category });
      }
      const saved = await api.addPurchase(state.trip.id, {
        barcode: scan.barcode,
        name,
        category,
        unit_price: centsToInput(priceCents),
        quantity: String(quantity),
        weight_grams: scan.weight_grams,
      });
      toast(t("purchase.saved", { name: saved.name }));
      await loadTrip();
      closeSheet({ silent: true });
      renderScan();
    } catch (error) {
      showError(errorMessage(error));
    }
  };
}

function quantityField(quantity, weighed) {
  return `<div class="field qty"><span>${h(t(weighed ? "purchase.quantity_kg" : "purchase.quantity"))}</span>
    <div class="stepper">
      <button type="button" class="step" data-step="-1" aria-label="${h(t("purchase.decrease"))}" ${weighed ? "hidden" : ""}>${icon.minus}</button>
      <input name="quantity" inputmode="decimal" autocomplete="off" value="${h(formatQuantity(quantity))}">
      <button type="button" class="step" data-step="1" aria-label="${h(t("purchase.increase"))}" ${weighed ? "hidden" : ""}>${icon.plus}</button>
    </div></div>`;
}

function wireQuantityAndTotal(form, weighed) {
  const out = form.querySelector("[data-total]");
  const update = () => {
    const price = parsePriceToCents(form.elements.price.value);
    const qty = parseDecimal(form.elements.quantity.value, 3);
    out.textContent = price != null && qty ? formatMoney(Math.round(price * qty)) : "—";
  };
  form.querySelectorAll("[data-step]").forEach((b) => {
    b.onclick = () => {
      const q = parseDecimal(form.elements.quantity.value, 3) ?? 1;
      form.elements.quantity.value = formatQuantity(Math.max(1, Math.round(q) + Number(b.dataset.step)));
      update();
    };
  });
  form.elements.price.addEventListener("input", update);
  form.elements.quantity.addEventListener("input", update);
  if (weighed) form.elements.quantity.setAttribute("inputmode", "decimal");
  update();
}

// ---------- edit purchase (scan screen + trip detail) ----------

function bindPurchaseRows(purchases, onChange) {
  $view.querySelectorAll("[data-purchase]").forEach((btn) => {
    btn.onclick = () => {
      const p = purchases.find((x) => x.id === Number(btn.dataset.purchase));
      if (p) editPurchaseSheet(p, onChange);
    };
  });
}

function editPurchaseSheet(p, onChange) {
  const weighed = p.weight_grams != null;
  const sheet = openSheet(
    `<form class="stack" novalidate>
      <h2>${h(p.name)}</h2>
      <p class="muted small">${h(formatDateTime(p.created_at))} · ${h(p.barcode)}</p>
      <label class="field"><span>${h(t("product.name"))}</span>
        <input name="name" value="${h(p.name)}" autocomplete="off" maxlength="200"></label>
      <label class="field"><span>${h(t("product.category"))}</span>
        <select name="category">${categoryOptions(p.category)}</select></label>
      <div class="row">
        <label class="field grow"><span>${h(t(weighed ? "purchase.price_per_kg" : "purchase.price"))}</span>
          <input name="price" inputmode="decimal" autocomplete="off" value="${h(centsToInput(p.unit_price_cents))}"></label>
        ${quantityField(p.quantity, weighed)}
      </div>
      <p class="line-total"><span>${h(t("purchase.line_total"))}</span> <strong data-total></strong></p>
      <p class="error" hidden></p>
      <div class="actions">
        <button type="button" class="btn danger" data-act="delete">${h(t("purchase.delete"))}</button>
        <button type="submit" class="btn primary">${h(t("purchase.update"))}</button>
      </div>
    </form>`,
  );
  const form = sheet.querySelector("form");
  wireQuantityAndTotal(form, weighed);
  sheet.querySelector('[data-act="delete"]').onclick = async () => {
    if (!(await confirmSheet(t("purchase.delete_confirm"), t("purchase.delete"), true))) return;
    try {
      await api.deletePurchase(p.id);
      toast(t("purchase.deleted"));
      await onChange();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = form.querySelector(".error");
    const priceCents = parsePriceToCents(form.elements.price.value);
    const quantity = parseDecimal(form.elements.quantity.value, 3);
    const name = form.elements.name.value.trim();
    const category = form.elements.category.value || null;
    if (priceCents == null || !quantity || !name) {
      err.textContent = t(!name ? "error.name_required" : priceCents == null ? "error.invalid_price" : "error.invalid_quantity");
      err.hidden = false;
      return;
    }
    try {
      // Name/category belong to the product, so a rename applies to every purchase of it.
      if (name !== p.name || category !== (p.category ?? null)) {
        await api.saveProduct(p.product_key, { name, category });
      }
      await api.updatePurchase(p.id, { unit_price: centsToInput(priceCents), quantity: String(quantity) });
      toast(t("purchase.updated"));
      closeSheet({ silent: true });
      await onChange();
    } catch (error) {
      err.textContent = errorMessage(error);
      err.hidden = false;
    }
  };
}

// ---------- history ----------

async function renderHistory() {
  $view.innerHTML = `<header class="screen-head"><h1>${h(t("history.title"))}</h1></header><p class="muted pad">${h(t("common.loading"))}</p>`;
  const trips = await api.trips();
  $view.innerHTML = `
    <header class="screen-head"><h1>${h(t("history.title"))}</h1></header>
    <a class="btn primary block history-scan" href="#/scan">${icon.scan}${h(t(state.trip ? "history.continue_trip" : "history.new_trip"))}</a>
    ${trips.length ? `<ul class="items">${trips.map((tr) => `<li><a class="item" href="#/trip/${tr.id}">
        <span class="item-main"><span class="item-name">${h(tr.store_name)}</span>
          <span class="item-sub">${tr.closed_at ? "" : `<span class="badge">${h(t("trip.open_badge"))}</span> `}${h(formatDate(tr.started_at))} · ${h(itemsLabel(tr.item_count))}</span></span>
        <span class="item-price">${formatMoney(tr.total_cents)}</span>${icon.chevron.replace("<svg", '<svg class="chev"')}</a></li>`).join("")}</ul>`
      : `<p class="muted empty">${h(t("history.empty"))}</p>`}`;
}

async function renderTrip(id) {
  let trip;
  try {
    trip = await api.trip(id);
  } catch (err) {
    toast(errorMessage(err), "error");
    location.hash = "#/history";
    return;
  }
  $view.innerHTML = `
    <header class="screen-head">
      <a class="back" href="#/history">${icon.back}${h(t("common.back"))}</a>
      <h1>${h(trip.store_name)}</h1>
      <p class="muted">${h(formatDateTime(trip.started_at))} · ${h(itemsLabel(trip.item_count))}</p>
      <p class="hero">${formatMoney(trip.total_cents)}</p>
    </header>
    ${trip.purchases.length ? `<ul class="items">${trip.purchases.map(purchaseRow).join("")}</ul>` : `<p class="muted empty">${h(t("trip.empty"))}</p>`}
    <div class="bottom-actions">
      <button class="btn danger" data-act="delete">${h(t("trip.delete"))}</button>
      ${trip.closed_at
        ? `<button class="btn" data-act="reopen">${h(t("trip.reopen"))}</button>`
        : `<button class="btn" data-act="close">${h(t("trip.close"))}</button>`}
    </div>`;
  bindPurchaseRows(trip.purchases, async () => {
    await loadTrip();
    renderTrip(id);
  });
  $view.querySelector('[data-act="delete"]').onclick = async () => {
    if (!(await confirmSheet(t("trip.delete_confirm"), t("trip.delete"), true))) return;
    await api.deleteTrip(id);
    await loadTrip();
    toast(t("trip.deleted"));
    location.hash = "#/history";
  };
  $view.querySelector('[data-act="reopen"]')?.addEventListener("click", async () => {
    try {
      state.trip = await api.reopenTrip(id);
      location.hash = "#/scan";
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  });
  $view.querySelector('[data-act="close"]')?.addEventListener("click", async () => {
    await api.closeTrip(id);
    await loadTrip();
    toast(t("trip.closed"));
    renderTrip(id);
  });
}

// ---------- stats ----------

async function renderStats() {
  const ranges = ["month", "3months", "year", "all"];
  $view.innerHTML = `
    <header class="screen-head"><h1>${h(t("stats.title"))}</h1></header>
    <div class="segmented" role="tablist">
      ${ranges.map((r) => `<button role="tab" data-range="${r}" aria-selected="${r === state.statsRange}">${h(t(`stats.range.${r}`))}</button>`).join("")}
    </div>
    <div id="stats-body"><p class="muted pad">${h(t("common.loading"))}</p></div>`;
  $view.querySelectorAll("[data-range]").forEach((b) => {
    b.onclick = () => {
      state.statsRange = b.dataset.range;
      renderStats();
    };
  });

  const [stats, products] = await Promise.all([api.stats(state.statsRange), api.statsProducts()]);
  const body = document.getElementById("stats-body");
  body.innerHTML = `
    <section class="stat-hero">
      <p class="muted">${h(t("stats.total_range"))}</p>
      <p class="hero">${formatMoney(stats.range_total_cents)}</p>
    </section>
    <section class="card"><h2>${h(t("stats.by_category"))}</h2><div id="ch-cat"></div></section>
    <section class="card"><h2>${h(t("stats.by_store"))}</h2><div id="ch-store"></div></section>
    <section class="card"><h2>${h(t("stats.weekly"))}</h2><div id="ch-week"></div>${seriesTable(stats.weekly, (b) => t("stats.week_of", { date: formatPlainDate(b.start, true) }))}</section>
    <section class="card"><h2>${h(t("stats.monthly"))}</h2><div id="ch-month"></div>${seriesTable(stats.monthly, (b) => formatMonthLong(b.start))}</section>
    <section class="card">
      <h2>${h(t("stats.price_history"))}</h2>
      <label class="field"><span class="visually-hidden">${h(t("stats.pick_product"))}</span>
        <select id="ph-product"><option value="">${h(t("stats.pick_product"))}</option>
          ${products.map((p) => `<option value="${h(p.product_key)}">${h(p.name)} (${p.purchases})</option>`).join("")}
        </select></label>
      <div id="ch-price"></div><div id="ph-table"></div>
    </section>`;

  const ranked = (id, buckets) => {
    const node = document.getElementById(id);
    if (!buckets.length) node.innerHTML = `<p class="muted">${h(t("stats.no_data"))}</p>`;
    else rankedBars(node, buckets.map((b) => ({ label: b.label, value: b.total_cents })), formatMoney);
  };
  ranked("ch-cat", stats.by_category.map((b) => ({ ...b, label: tCategory(b.label) })));
  ranked("ch-store", stats.by_store);
  barChart(
    document.getElementById("ch-week"),
    stats.weekly.map((b) => ({
      label: formatPlainDate(b.start),
      value: b.total_cents,
      tip: `${h(t("stats.week_of", { date: formatPlainDate(b.start, true) }))}<br><strong>${formatMoney(b.total_cents)}</strong>`,
    })),
    { fmtAxis: axisMoney, labelEvery: 3 },
  );
  barChart(
    document.getElementById("ch-month"),
    stats.monthly.map((b) => ({
      label: formatMonthShort(b.start),
      value: b.total_cents,
      tip: `${h(formatMonthLong(b.start))}<br><strong>${formatMoney(b.total_cents)}</strong>`,
    })),
    { fmtAxis: axisMoney, labelEvery: 2 },
  );

  document.getElementById("ph-product").onchange = async (e) => {
    const key = e.target.value;
    const chart = document.getElementById("ch-price");
    const table = document.getElementById("ph-table");
    chart.innerHTML = table.innerHTML = "";
    if (!key) return;
    const hist = await api.priceHistory(key);
    lineChart(
      chart,
      hist.points.map((p) => ({
        value: p.unit_price_cents,
        label: formatShortDate(p.date),
        tip: `${h(formatDate(p.date))} · ${h(p.store_name)}<br><strong>${formatMoney(p.unit_price_cents)}</strong>`,
      })),
      { fmtAxis: (c) => formatMoney(c) },
    );
    table.innerHTML = `<details><summary>${h(t("stats.show_table"))}</summary><table class="table">
      <thead><tr><th>${h(t("stats.col_date"))}</th><th>${h(t("stats.col_store"))}</th><th class="num">${h(t("stats.col_price"))}</th></tr></thead>
      <tbody>${hist.points.map((p) => `<tr><td>${h(formatDate(p.date))}</td><td>${h(p.store_name)}</td><td class="num">${formatMoney(p.unit_price_cents)}</td></tr>`).join("")}</tbody>
    </table></details>`;
  };
}

function seriesTable(buckets, fmtLabel) {
  return `<details><summary>${h(t("stats.show_table"))}</summary><table class="table">
    <thead><tr><th>${h(t("stats.col_period"))}</th><th class="num">${h(t("stats.col_total"))}</th></tr></thead>
    <tbody>${[...buckets].reverse().map((b) => `<tr><td>${h(fmtLabel(b))}</td><td class="num">${formatMoney(b.total_cents)}</td></tr>`).join("")}</tbody>
  </table></details>`;
}

// ---------- more ----------

async function renderMore() {
  $view.innerHTML = `
    <header class="screen-head"><h1>${h(t("more.title"))}</h1></header>
    <section class="card more-card">
      <a class="btn primary block" href="/api/export.csv?lang=${getLocale()}" download>${h(t("more.export"))}</a>
      <p class="muted small">${h(t("more.export_hint"))}</p>
    </section>
    <section class="card more-card">
      <h2>${h(t("settings.language"))}</h2>
      <div class="segmented two" role="radiogroup" aria-label="${h(t("settings.language"))}">
        ${LOCALES.map((l) => `<button type="button" role="radio" data-lang="${l}" aria-selected="${l === getLocale()}" aria-checked="${l === getLocale()}">${l === "es" ? "Español" : "English"}</button>`).join("")}
      </div>
      <p class="muted small">${h(t("settings.language_hint"))}</p>
    </section>
    <section class="card more-card">
      <h2>${h(t("settings.postal_code"))}</h2>
      <form class="row" id="pc-form" novalidate>
        <label class="field grow"><span class="visually-hidden">${h(t("settings.postal_code"))}</span>
          <input name="pc" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" autocomplete="postal-code" value=""></label>
        <button class="btn primary" type="submit">${h(t("common.confirm"))}</button>
      </form>
      <p class="muted small">${h(t("settings.postal_code_hint"))}</p>
    </section>
    <section class="card more-card">
      <h2>${h(t("more.stores"))}</h2>
      <ul class="chips">${state.stores.map((s) => `<li>${h(s.name)}</li>`).join("")}</ul>
      <button class="btn block" data-act="add-store">${icon.plus}${h(t("trip.add_store"))}</button>
    </section>
    <section class="card more-card">
      <h2>${h(t("more.products"))}</h2>
      <p class="muted small">${h(t("more.products_hint"))}</p>
      <label class="field"><span class="visually-hidden">${h(t("product.search"))}</span>
        <input type="search" id="product-q" placeholder="${h(t("product.search"))}" autocomplete="off" enterkeyhint="search"></label>
      <ul class="items" id="product-list"></ul>
    </section>
    <button class="btn block danger" data-act="logout">${h(t("login.logout"))}</button>`;
  $view.querySelector('[data-act="add-store"]').onclick = () => addStoreSheet(renderMore);
  $view.querySelector('[data-act="logout"]').onclick = async () => {
    await api.logout().catch(() => {});
    showLogin();
  };
  $view.querySelectorAll("[data-lang]").forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.lang === getLocale()) return;
      switchLanguage(b.dataset.lang);
      toast(t("settings.language_saved"));
      api.saveSettings({ language: b.dataset.lang }).catch((err) => toast(errorMessage(err), "error"));
    };
  });
  const pcForm = document.getElementById("pc-form");
  api.settings().then((st) => (pcForm.elements.pc.value = st.postal_code)).catch(() => {});
  pcForm.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api.saveSettings({ postal_code: pcForm.elements.pc.value.trim() });
      toast(t("settings.saved"));
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const list = document.getElementById("product-list");
  const load = async (q) => {
    const products = await api.products(q);
    list.innerHTML = products.length
      ? products.map((p) => `<li><button class="item" data-key="${h(p.key)}">
          <span class="item-main"><span class="item-name">${h(p.name)}</span>
          <span class="item-sub">${h([tCategory(p.category), p.key.startsWith("vw:") ? t("product.weighed") : p.key].filter(Boolean).join(" · "))}</span></span>
        </button></li>`).join("")
      : `<li class="muted empty">${h(t("product.none"))}</li>`;
    list.querySelectorAll("[data-key]").forEach((b) => {
      const p = products.find((x) => x.key === b.dataset.key);
      b.onclick = () => renameProductSheet(p, () => load(document.getElementById("product-q").value));
    });
  };
  let debounce;
  document.getElementById("product-q").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => load(e.target.value), 250);
  });
  load("");
}

function renameProductSheet(product, after) {
  const sheet = openSheet(
    `<form class="stack" novalidate>
      <h2>${h(t("product.edit_name"))}</h2>
      <label class="field"><span>${h(t("product.name"))}</span>
        <input name="name" value="${h(product.name)}" autocomplete="off" maxlength="200"></label>
      <label class="field"><span>${h(t("product.category"))}</span>
        <select name="category">${categoryOptions(product.category)}</select></label>
      <p class="error" hidden></p>
      <div class="actions">
        <button type="button" class="btn" data-act="cancel">${h(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${h(t("purchase.update"))}</button>
      </div>
    </form>`,
  );
  const form = sheet.querySelector("form");
  sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const name = form.elements.name.value.trim();
    if (!name) return;
    try {
      await api.saveProduct(product.key, { name, category: form.elements.category.value || null });
      toast(t("product.renamed"));
      closeSheet({ silent: true });
      after();
    } catch (err) {
      const p = form.querySelector(".error");
      p.textContent = errorMessage(err);
      p.hidden = false;
    }
  };
}

// ---------- routing ----------

function currentRoute() {
  const [, name = "compare", arg, arg2] = location.hash.replace(/^#/, "").split("/");
  return { name: name || "compare", arg, arg2 };
}

async function route() {
  closeSheet({ silent: true });
  const { name, arg, arg2 } = currentRoute();
  if (name !== "scan") state.scanner?.stop();
  // Scanning a trip starts from History, so it keeps that tab lit.
  const tab = name === "trip" || name === "scan" ? "history" : name;
  $view.dataset.screen = name;
  document.querySelectorAll(".tabbar a").forEach((a) => a.setAttribute("aria-current", a.dataset.tab === tab ? "page" : "false"));
  window.scrollTo(0, 0);
  try {
    switch (name) {
      case "history": return await renderHistory();
      case "trip": return await renderTrip(Number(arg));
      case "stats": return await renderStats();
      case "compare":
        if (arg === "product") return await renderProduct($view, Number(arg2));
        if (arg === "type") return await renderType($view, arg2, "#/compare");
        if (arg === "list") return location.replace("#/list"); // old link, before the list had its own tab
        return await renderCompare($view);
      case "list":
        if (arg === "type") return await renderType($view, arg2, "#/list");
        return await renderList($view);
      case "more": return await renderMore();
      case "scan": return renderScan();
      default: return await renderCompare($view);
    }
  } catch (err) {
    toast(errorMessage(err), "error");
  }
}

async function loadTrip() {
  state.trip = await api.currentTrip();
}

document.addEventListener("visibilitychange", () => {
  // Release the camera when the app goes to the background; resume on return.
  if (document.hidden) state.scanner?.stop();
  else resumeScanner();
});

// ---------- language ----------

/** Apply a language to the whole app: static labels, then re-render the current screen. */
function switchLanguage(lang) {
  setLocale(lang);
  applyI18n();
  if (!document.body.classList.contains("logged-out")) route();
}

// ---------- login ----------

const $login = document.getElementById("login");

function showLogin() {
  state.scanner?.stop();
  closeSheet({ silent: true });
  document.body.classList.add("logged-out");
  $login.hidden = false;
  const form = $login.querySelector("form");
  form.elements.password.value = "";
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = form.querySelector(".error");
    err.hidden = true;
    try {
      await api.login(form.elements.password.value);
      form.elements.password.blur();
      await start();
    } catch (error) {
      err.textContent = errorMessage(error);
      err.hidden = false;
    }
  };
}

window.addEventListener("unauthorized", showLogin);

let routerBound = false;

async function start() {
  $login.hidden = true;
  document.body.classList.remove("logged-out");
  try {
    let settings;
    [state.stores, state.categories, , settings] = await Promise.all([
      api.stores(), api.categories(), loadTrip(), api.settings(),
    ]);
    // The server keeps the language, so every device follows the same choice.
    if (settings?.language && settings.language !== getLocale()) {
      setLocale(settings.language);
      applyI18n();
    }
  } catch (err) {
    toast(errorMessage(err), "error");
  }
  if (!routerBound) {
    window.addEventListener("hashchange", route);
    routerBound = true;
  }
  route();
}

async function init() {
  applyI18n();
  try {
    const { authenticated } = await api.session();
    if (!authenticated) return showLogin();
  } catch (err) {
    toast(errorMessage(err), "error");
  }
  start();
}

init();
