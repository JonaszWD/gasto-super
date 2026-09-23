// Shared UI helpers: toasts, bottom sheets, confirm/prompt sheets, error messages.
import { ApiError } from "./api.js";
import { escapeHtml } from "./format.js";
import { t } from "./i18n.js";

export const h = escapeHtml;

export function errorMessage(err) {
  if (err instanceof ApiError) {
    const key = `error.${err.code}`;
    const msg = t(key);
    return msg === key ? t("error.generic") : msg;
  }
  return err?.message || t("error.generic");
}

let toastTimer;
export function toast(message, kind = "ok") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.dataset.kind = kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2600);
}

const $sheet = document.getElementById("sheet");
const $backdrop = document.getElementById("sheet-backdrop");
let sheetOnClose = null;

/** Hooks set by app.js (camera stop/resume around sheets). */
export const sheetHooks = { beforeOpen: () => {}, afterClose: () => {} };

export function sheetOpen() {
  return !$sheet.hidden;
}

export function openSheet(html, { onClose } = {}) {
  sheetHooks.beforeOpen();
  $sheet.innerHTML = `<div class="sheet-grip" aria-hidden="true"></div>${html}`;
  $sheet.hidden = false;
  $backdrop.hidden = false;
  sheetOnClose = onClose ?? null;
  requestAnimationFrame(() => $sheet.classList.add("open"));
  return $sheet;
}

export function closeSheet({ silent = false } = {}) {
  if ($sheet.hidden) return;
  $sheet.classList.remove("open");
  $sheet.hidden = true;
  $backdrop.hidden = true;
  $sheet.innerHTML = "";
  const cb = sheetOnClose;
  sheetOnClose = null;
  if (silent) return; // caller re-renders (and restarts the camera) itself
  if (cb) cb();
  sheetHooks.afterClose();
}

$backdrop.addEventListener("click", () => closeSheet());

export function confirmSheet(message, confirmLabel = t("common.confirm"), danger = false) {
  return new Promise((resolve) => {
    const sheet = openSheet(
      `<p class="sheet-message">${h(message)}</p>
       <div class="actions">
         <button class="btn" data-act="no">${h(t("common.cancel"))}</button>
         <button class="btn ${danger ? "danger" : "primary"}" data-act="yes">${h(confirmLabel)}</button>
       </div>`,
      { onClose: () => resolve(false) },
    );
    sheet.querySelector('[data-act="no"]').onclick = () => closeSheet();
    sheet.querySelector('[data-act="yes"]').onclick = () => {
      closeSheet({ silent: true });
      resolve(true);
    };
  });
}

export function promptSheet({ title, label, value = "", placeholder = "", inputmode = "text", pattern, submit }) {
  const sheet = openSheet(
    `<form class="stack" novalidate>
       <h2>${h(title)}</h2>
       <label class="field"><span>${h(label)}</span>
         <input name="value" value="${h(value)}" placeholder="${h(placeholder)}" inputmode="${inputmode}"
                ${pattern ? `pattern="${h(pattern)}"` : ""} autocomplete="off" autocorrect="off" enterkeyhint="done" required>
       </label>
       <p class="error" hidden></p>
       <div class="actions">
         <button type="button" class="btn" data-act="cancel">${h(t("common.cancel"))}</button>
         <button type="submit" class="btn primary">${h(t("common.confirm"))}</button>
       </div>
     </form>`,
  );
  const form = sheet.querySelector("form");
  const input = form.elements.value;
  sheet.querySelector('[data-act="cancel"]').onclick = () => closeSheet();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const v = input.value.trim();
    if (!v) return;
    try {
      await submit(v);
    } catch (err) {
      const p = form.querySelector(".error");
      p.textContent = errorMessage(err);
      p.hidden = false;
    }
  };
  input.focus();
}
