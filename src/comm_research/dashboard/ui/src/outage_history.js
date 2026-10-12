// Effective outage windows retain their individual identity and published MW.
export function prepareOutageTrace(trace) {
  if (!Array.isArray(trace.events)) throw new Error("Invalid outage history");
  const events = trace.events
    .map((event) => ({
      ...event,
      startEpoch: Date.parse(event.start) / 1000,
      endEpoch: Date.parse(event.end) / 1000,
    }))
    .sort((a, b) => a.startEpoch - b.startEpoch || a.endEpoch - b.endEpoch);
  if (
    events.some(
      (event) =>
        !Number.isFinite(event.startEpoch) ||
        !Number.isFinite(event.endEpoch) ||
        event.endEpoch < event.startEpoch ||
        (event.value != null && !Number.isFinite(event.value)),
    )
  )
    throw new Error("Invalid outage interval or value");
  return {
    ...trace,
    events,
    epochs: Float64Array.from(events, (event) => event.startEpoch),
  };
}

export function outageBounds(traces, range, today = Date.now() / 1000) {
  let min = Infinity,
    max = -Infinity;
  for (const trace of traces)
    for (const event of trace.events) {
      min = Math.min(min, event.startEpoch);
      max = Math.max(max, event.endEpoch);
    }
  if (!Number.isFinite(min)) return { min, max };
  if (max === min) max = min + 3600;
  const days = { "1D": 1, "1W": 7, "1M": 30 }[range];
  if (!days) return { min, max };
  // Near live data, show upcoming windows from today. For a past/future-only
  // lake, anchor inside its effective interval extent instead of an empty date.
  if (today >= max) return { min: Math.max(min, max - days * 86400), max };
  const anchor = Math.max(min, Math.floor(today / 86400) * 86400);
  return { min: anchor, max: anchor + days * 86400 };
}

const safe = (value) =>
  String(value ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
export function outagePoints(trace, min, max, budget = 1500) {
  const events = trace.events.filter(
    (event) =>
      event.value != null &&
      event.startEpoch <= max &&
      (event.endEpoch > min ||
        (event.startEpoch === event.endEpoch && event.startEpoch >= min)),
  );
  const limit = Math.max(1, Math.floor(budget / 3));
  const selected =
    events.length <= limit
      ? events
      : Array.from(
          { length: limit },
          (_, i) =>
            events[
              limit === 1
                ? 0
                : Math.round((i * (events.length - 1)) / (limit - 1))
            ],
        );
  const x = [],
    y = [],
    customdata = [];
  for (const event of selected) {
    const detail = [
      event.start,
      event.end,
      safe(event.equipment),
      safe(event.notes),
      safe(event.updatedAt),
      safe(trace.dimensions.record_status),
    ];
    x.push(
      new Date(Math.max(min, event.startEpoch) * 1000).toISOString(),
      new Date(Math.min(max, event.endEpoch) * 1000).toISOString(),
      null,
    );
    y.push(event.value, event.value, null);
    customdata.push(detail, detail, detail);
  }
  return { x, y, customdata, count: events.length, displayed: selected.length };
}
