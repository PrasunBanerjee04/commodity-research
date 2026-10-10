export async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok)
      throw new Error(
        `HTTP ${response.status}: ${response.statusText || "Request failed"}`,
      );
    throw new Error("Invalid JSON response from the data API.");
  }
  if (!response.ok) {
    throw new Error(
      `HTTP ${response.status}: ${payload?.error || response.statusText || "Request failed"}`,
    );
  }
  return payload;
}
