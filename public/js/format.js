// Formatting helpers. Money and dates keep Spanish conventions (1.234,56 €, DD/MM/YYYY) in every
// language; only month names follow the UI language. Money is always handled as integer cents.
import { getLocale } from "./i18n.js";
export const TIMEZONE = "Europe/Madrid";

const NBSP = " ";

/** 123456 -> "1.234,56 €" (always groups thousands, unlike Intl's es-ES default). */
export function formatMoney(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.round(cents));
  const euros = Math.floor(abs / 100).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const rest = String(abs % 100).padStart(2, "0");
  return `${sign}${euros},${rest}${NBSP}€`;
}

/** 125 -> "1,25" (for pre-filling inputs). */
export function centsToInput(cents) {
  if (cents == null) return "";
  return (cents / 100).toFixed(2).replace(".", ",");
}

/** 0.755 -> "0,755", 2 -> "2". */
export function formatQuantity(q) {
  return String(Math.round(q * 1000) / 1000).replace(".", ",");
}

/**
 * Parse a user-typed number with comma or dot decimals ("1,25", "1.25", "1.234,56").
 * Mirrors app/services/money.py. Returns null when invalid.
 */
export function parseDecimal(text, maxDecimals = 2) {
  let s = String(text ?? "").trim().replace(/[€\s ]/g, "");
  if (!s || !/^[0-9.,]+$/.test(s)) return null;
  const last = Math.max(s.lastIndexOf(","), s.lastIndexOf("."));
  let intPart = s;
  let frac = "";
  if (last !== -1) {
    const candidate = s.slice(last + 1);
    const sep = s[last];
    if (candidate.length > 0 && candidate.length <= maxDecimals && s.split(sep).length === 2) {
      intPart = s.slice(0, last);
      frac = candidate;
    }
  }
  intPart = intPart.replace(/[.,]/g, "");
  const value = Number(`${intPart || "0"}.${frac || "0"}`);
  return Number.isFinite(value) ? value : null;
}

export function parsePriceToCents(text) {
  const v = parseDecimal(text, 2);
  return v == null ? null : Math.round(v * 100);
}

const dateFmt = new Intl.DateTimeFormat("es-ES", {
  timeZone: TIMEZONE, day: "2-digit", month: "2-digit", year: "numeric",
});
const timeFmt = new Intl.DateTimeFormat("es-ES", { timeZone: TIMEZONE, hour: "2-digit", minute: "2-digit" });
const INTL_LOCALE = { es: "es-ES", en: "en-GB" };
const monthFmt = () => new Intl.DateTimeFormat(INTL_LOCALE[getLocale()], { timeZone: "UTC", month: "short" });
const monthYearFmt = () => new Intl.DateTimeFormat(INTL_LOCALE[getLocale()], { timeZone: "UTC", month: "long", year: "numeric" });

/** ISO timestamp -> "23/09/2026". */
export const formatDate = (iso) => dateFmt.format(new Date(iso));
export const formatTime = (iso) => timeFmt.format(new Date(iso));
export const formatDateTime = (iso) => `${formatDate(iso)} ${formatTime(iso)}`;

/** "2026-09-21" (a calendar date, no time) -> "21/09" / "21/09/2026". */
export function formatPlainDate(isoDate, withYear = false) {
  const [y, m, d] = isoDate.split("-");
  return withYear ? `${d}/${m}/${y}` : `${d}/${m}`;
}
/** ISO timestamp -> "23/09". */
export const formatShortDate = (iso) => formatDate(iso).slice(0, 5);

/** "2026-09-01" -> "sept" / "septiembre de 2026" (or "Sep" / "September 2026"). */
export const formatMonthShort = (isoDate) => monthFmt().format(new Date(`${isoDate}T00:00:00Z`)).replace(".", "");
export const formatMonthLong = (isoDate) => monthYearFmt().format(new Date(`${isoDate}T00:00:00Z`));

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
