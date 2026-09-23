// Thin wrapper around the backend JSON API. Errors carry the backend's `detail` code.
export class ApiError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network");
  }
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data?.detail;
    const code = typeof detail === "string" ? detail : "generic";
    if (resp.status === 401 && code === "unauthorized") window.dispatchEvent(new Event("unauthorized"));
    throw new ApiError(resp.status, code);
  }
  return data;
}

const enc = encodeURIComponent;

export const api = {
  stores: () => request("GET", "/api/stores"),
  addStore: (name) => request("POST", "/api/stores", { name }),
  categories: () => request("GET", "/api/categories"),

  scan: (code, storeId) => request("GET", `/api/scan/${enc(code)}${storeId ? `?store_id=${storeId}` : ""}`),
  products: (q = "") => request("GET", `/api/products?q=${enc(q)}`),
  saveProduct: (key, data) => request("PUT", `/api/products/${enc(key)}`, data),

  currentTrip: () => request("GET", "/api/trips/current"),
  trips: () => request("GET", "/api/trips"),
  trip: (id) => request("GET", `/api/trips/${id}`),
  startTrip: (storeId) => request("POST", "/api/trips", { store_id: storeId }),
  closeTrip: (id) => request("POST", `/api/trips/${id}/close`),
  reopenTrip: (id) => request("POST", `/api/trips/${id}/reopen`),
  deleteTrip: (id) => request("DELETE", `/api/trips/${id}`),

  addPurchase: (tripId, data) => request("POST", `/api/trips/${tripId}/purchases`, data),
  updatePurchase: (id, data) => request("PATCH", `/api/purchases/${id}`, data),
  deletePurchase: (id) => request("DELETE", `/api/purchases/${id}`),

  session: () => request("GET", "/api/auth/session"),
  login: (password) => request("POST", "/api/auth/login", { password }),
  logout: () => request("POST", "/api/auth/logout"),

  settings: () => request("GET", "/api/settings"),
  saveSettings: (data) => request("PUT", "/api/settings", data),

  compareSearch: (q) => request("GET", `/api/compare/search?q=${enc(q)}`),
  compareProduct: (id) => request("GET", `/api/compare/products/${id}`),
  setMatch: (id, listingId, action) => request("POST", `/api/compare/products/${id}/matches`, { listing_id: listingId, action }),
  refreshListing: (listingId) => request("POST", `/api/compare/listings/${listingId}/refresh`),
  alternative: (barcode, storeId, priceCents) => {
    const qs = new URLSearchParams({ barcode });
    if (storeId) qs.set("store_id", storeId);
    if (priceCents != null) qs.set("price_cents", priceCents);
    return request("GET", `/api/compare/alternative?${qs}`);
  },

  shoppingList: () => request("GET", "/api/shopping-list"),
  addToList: (productId, quantity = 1) => request("POST", "/api/shopping-list/items", { product_id: productId, quantity }),
  setListQuantity: (productId, quantity) =>
    request("PUT", `/api/shopping-list/items/${productId}`, { product_id: productId, quantity }),
  removeFromList: (productId) => request("DELETE", `/api/shopping-list/items/${productId}`),

  stats: (range) => request("GET", `/api/stats?range=${enc(range)}`),
  statsProducts: () => request("GET", "/api/stats/products"),
  priceHistory: (key) => request("GET", `/api/stats/price-history/${enc(key)}`),
};
