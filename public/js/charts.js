// Minimal responsive SVG charts. Single-series only: one accent hue, recessive grid,
// thin rounded bars with 2px gaps, tap/hover tooltips, and a table alternative.
import { escapeHtml } from "./format.js";

const NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}

/** Bar path with 4px rounded top corners anchored to the baseline. */
function barPath(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function attachTooltip(container) {
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  container.appendChild(tip);
  return {
    show(html, x, y) {
      tip.innerHTML = html;
      tip.hidden = false;
      const cw = container.clientWidth;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(cw - tw, x - tw / 2))}px`;
      tip.style.top = `${Math.max(0, y - tip.offsetHeight - 8)}px`;
    },
    hide() {
      tip.hidden = true;
    },
  };
}

function gridAndAxis(svg, { left, top, plotW, plotH, max, min = 0, fmtAxis }) {
  for (const f of [0, 0.5, 1]) {
    const y = top + plotH - f * plotH;
    svg.appendChild(el("line", { x1: left, x2: left + plotW, y1: y, y2: y, class: f === 0 ? "axis" : "grid" }));
    if (f > 0 || min > 0) {
      const label = el("text", { x: left, y: y - 4, class: "tick" });
      label.textContent = fmtAxis(min + (max - min) * f);
      svg.appendChild(label);
    }
  }
}

/**
 * Vertical bars. items: [{label, value, tip}] — label drawn under selected bars only.
 */
export function barChart(container, items, { fmtAxis, labelEvery = 1 } = {}) {
  container.innerHTML = "";
  container.classList.add("chart");
  const width = Math.max(container.clientWidth, 280);
  const height = 180;
  const m = { left: 4, right: 4, top: 18, bottom: 22 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const max = niceMax(Math.max(...items.map((i) => i.value), 0));
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });
  gridAndAxis(svg, { left: m.left, top: m.top, plotW, plotH, max, fmtAxis });

  const slot = plotW / items.length;
  const barW = Math.max(4, Math.min(28, slot - 2)); // ≥2px surface gap between bars
  const tip = attachTooltip(container);
  items.forEach((item, i) => {
    const h = (item.value / max) * plotH;
    const x = m.left + i * slot + (slot - barW) / 2;
    const y = m.top + plotH - h;
    if (h > 0) svg.appendChild(el("path", { d: barPath(x, y, barW, h), class: "bar" }));
    // Count from the newest bar so the most recent period is always labeled.
    if ((items.length - 1 - i) % labelEvery === 0) {
      const lx = el("text", { x: x + barW / 2, y: height - 6, class: "tick", "text-anchor": "middle" });
      lx.textContent = item.label;
      svg.appendChild(lx);
    }
    // Hit target: full slot height, wider than the mark.
    const hit = el("rect", { x: m.left + i * slot, y: m.top, width: slot, height: plotH, class: "hit" });
    const showTip = () => tip.show(item.tip, x + barW / 2, Math.min(y, m.top + plotH - 4));
    hit.addEventListener("pointerenter", showTip);
    hit.addEventListener("click", showTip);
    hit.addEventListener("pointerleave", () => tip.hide());
    svg.appendChild(hit);
  });
  container.appendChild(svg);
}

/** Ranked horizontal bars, direct-labeled (label + value), so no legend or tooltip needed. */
export function rankedBars(container, items, fmtValue) {
  const max = Math.max(...items.map((i) => i.value), 1);
  container.innerHTML = `<ul class="ranked">${items
    .map(
      (i) => `<li>
        <div class="ranked-row"><span>${escapeHtml(i.label)}</span><strong>${escapeHtml(fmtValue(i.value))}</strong></div>
        <div class="ranked-track"><div class="ranked-bar" style="width:${Math.max(1, (i.value / max) * 100)}%"></div></div>
      </li>`,
    )
    .join("")}</ul>`;
}

/**
 * Line with markers for a price history. points: [{value, label, tip}] in time order.
 */
export function lineChart(container, points, { fmtAxis }) {
  container.innerHTML = "";
  container.classList.add("chart");
  const width = Math.max(container.clientWidth, 280);
  const height = 180;
  const m = { left: 8, right: 8, top: 18, bottom: 22 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  // Prices: zoom to the observed range (a line need not start at zero) so changes are visible.
  const values = points.map((p) => p.value);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = Math.max(10, (hi - lo) * 0.25);
  const min = Math.max(0, Math.floor((lo - pad) / 10) * 10);
  const max = Math.ceil((hi + pad) / 10) * 10;
  const yOf = (v) => m.top + plotH - ((v - min) / (max - min)) * plotH;
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });
  gridAndAxis(svg, { left: m.left, top: m.top, plotW, plotH, max, min, fmtAxis });

  const step = points.length > 1 ? plotW / (points.length - 1) : 0;
  const xy = points.map((p, i) => [m.left + (points.length > 1 ? i * step : plotW / 2), yOf(p.value)]);
  if (xy.length > 1) {
    svg.appendChild(el("path", { d: xy.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(""), class: "line" }));
  }
  const tip = attachTooltip(container);
  xy.forEach(([x, y], i) => {
    svg.appendChild(el("circle", { cx: x, cy: y, r: 4, class: "dot" }));
    const hit = el("rect", {
      x: x - Math.max(step, 24) / 2, y: m.top, width: Math.max(step, 24), height: plotH, class: "hit",
    });
    const showTip = () => tip.show(points[i].tip, x, y);
    hit.addEventListener("pointerenter", showTip);
    hit.addEventListener("click", showTip);
    hit.addEventListener("pointerleave", () => tip.hide());
    svg.appendChild(hit);
  });
  const first = el("text", { x: m.left, y: height - 6, class: "tick" });
  first.textContent = points[0].label;
  svg.appendChild(first);
  if (points.length > 1) {
    const last = el("text", { x: width - m.right, y: height - 6, class: "tick", "text-anchor": "end" });
    last.textContent = points.at(-1).label;
    svg.appendChild(last);
  }
  container.appendChild(svg);
}

/**
 * Price history per chain as step lines (a price holds until it changes).
 * series: [{label, slot (1-5, fixed per chain), periods: [{start, end, value}] (ms timestamps)}]
 * Always has a legend (identity is never colour-only) and a crosshair tooltip listing every chain.
 */
export function stepChart(container, series, { fmtValue, fmtDate }) {
  container.innerHTML = "";
  container.classList.add("chart");
  const all = series.flatMap((s) => s.periods);
  if (!all.length) return;
  const width = Math.max(container.clientWidth, 280);
  const height = 200;
  const m = { left: 8, right: 8, top: 18, bottom: 22 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const t0 = Math.min(...all.map((p) => p.start));
  const t1 = Math.max(...all.map((p) => p.end), t0 + 86400000);
  const lo = Math.min(...all.map((p) => p.value));
  const hi = Math.max(...all.map((p) => p.value));
  const pad = Math.max(10, (hi - lo) * 0.2, hi * 0.05);
  const min = Math.max(0, Math.floor((lo - pad) / 10) * 10);
  const max = Math.ceil((hi + pad) / 10) * 10;
  const x = (t) => m.left + ((t - t0) / (t1 - t0)) * plotW;
  const y = (v) => m.top + plotH - ((v - min) / (max - min)) * plotH;

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });
  gridAndAxis(svg, { left: m.left, top: m.top, plotW, plotH, max, min, fmtAxis: fmtValue });
  for (const s of series) {
    if (!s.periods.length) continue;
    let d = "";
    s.periods.forEach((p, i) => {
      d += `${i ? "L" : "M"}${x(p.start)},${y(p.value)}H${x(p.end)}`;
    });
    svg.appendChild(el("path", { d, class: `line s${s.slot}` }));
    const last = s.periods.at(-1);
    svg.appendChild(el("circle", { cx: x(last.end), cy: y(last.value), r: 4, class: `dot s${s.slot}` }));
  }
  const first = el("text", { x: m.left, y: height - 6, class: "tick" });
  first.textContent = fmtDate(t0);
  svg.appendChild(first);
  const lastLbl = el("text", { x: width - m.right, y: height - 6, class: "tick", "text-anchor": "end" });
  lastLbl.textContent = fmtDate(t1);
  svg.appendChild(lastLbl);

  const cross = el("line", { x1: 0, x2: 0, y1: m.top, y2: m.top + plotH, class: "crosshair", visibility: "hidden" });
  svg.appendChild(cross);
  const hit = el("rect", { x: m.left, y: m.top, width: plotW, height: plotH, class: "hit" });
  svg.appendChild(hit);
  container.appendChild(svg);
  const tip = attachTooltip(container);

  const valueAt = (s, t) => {
    let v = null;
    for (const p of s.periods) if (p.start <= t) v = p.value;
    return v;
  };
  const show = (evt) => {
    const rect = svg.getBoundingClientRect();
    const px = ((evt.clientX - rect.left) / rect.width) * width;
    const t = t0 + ((Math.min(Math.max(px, m.left), m.left + plotW) - m.left) / plotW) * (t1 - t0);
    cross.setAttribute("x1", x(t));
    cross.setAttribute("x2", x(t));
    cross.setAttribute("visibility", "visible");
    const rows = series
      .map((s) => ({ s, v: valueAt(s, t) }))
      .filter((r) => r.v != null)
      .sort((a, b) => a.v - b.v)
      .map((r) => `<span class="tip-row"><i class="key s${r.s.slot}"></i>${escapeHtml(r.s.label)} <strong>${escapeHtml(fmtValue(r.v))}</strong></span>`)
      .join("");
    tip.show(`${escapeHtml(fmtDate(t))}${rows}`, x(t), m.top + 20);
  };
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    tip.hide();
  });

  const legend = document.createElement("ul");
  legend.className = "legend";
  legend.innerHTML = series
    .filter((s) => s.periods.length)
    .map((s) => `<li><i class="key s${s.slot}"></i>${escapeHtml(s.label)}</li>`)
    .join("");
  container.appendChild(legend);
}
