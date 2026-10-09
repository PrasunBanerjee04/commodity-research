export async function request(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

export function loadCatalog() {
  return request("/api/datasets");
}

export function loadMetadata(key) {
  return request(`/api/metadata?key=${encodeURIComponent(key)}`);
}

export function loadOptions(key, dimension) {
  return request(`/api/options?key=${encodeURIComponent(key)}&dimension=${encodeURIComponent(dimension)}`);
}

export function loadSeries(query) {
  return request("/api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  });
}

export function rescanCatalog() {
  return request("/api/rescan", { method: "POST" });
}
