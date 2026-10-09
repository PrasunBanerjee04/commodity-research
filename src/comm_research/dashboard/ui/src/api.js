import { clearQueryCaches } from "./query_cache.js";

const lookups = new Map();

function lookup(identity, path) {
  if (!lookups.has(identity)) {
    const pending = request(path).catch(error => { lookups.delete(identity); throw error; });
    lookups.set(identity, pending);
  }
  return lookups.get(identity);
}

export async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText || "Request failed"}`);
    throw new Error("Invalid JSON response from the data API.");
  }
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${payload?.error || response.statusText || "Request failed"}`);
  }
  return payload;
}

export function loadCatalog() {
  return request("/api/datasets");
}

export function loadMetadata(key, revision = "") {
  return lookup(`metadata:${key}:${revision}`, `/api/metadata?key=${encodeURIComponent(key)}`);
}

export function loadOptions(key, dimension, revision = "") {
  return lookup(`options:${key}:${revision}:${dimension}`, `/api/options?key=${encodeURIComponent(key)}&dimension=${encodeURIComponent(dimension)}`);
}

export function loadSeries(query) {
  return request("/api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  });
}

export async function rescanCatalog() {
  const result = await request("/api/rescan", { method: "POST" });
  lookups.clear(); clearQueryCaches();
  return result;
}
