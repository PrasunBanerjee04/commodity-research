// Session-only display snapshots. Each trace has <=1,500 server-sampled points.
const feeds = new Map();
const MAX_WINDOWS = 8;
const MAX_POINTS = 250_000;

function lowerBound(rows, time) {
  let low = 0, high = rows.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (rows[middle]._epoch < time) low = middle + 1; else high = middle;
  }
  return low;
}

export function viewStatistics(rows) {
  if (!rows.length) return [];
  let mean = 0, squares = 0, min = Infinity, max = -Infinity;
  rows.forEach((row, index) => {
    const delta = row.value - mean;
    mean += delta / (index + 1);
    squares += delta * (row.value - mean);
    min = Math.min(min, row.value); max = Math.max(max, row.value);
  });
  const last = rows.at(-1);
  const target = last._epoch - 86400;
  const reference = rows[lowerBound(rows, target)];
  const change = reference?._epoch === target && reference.value !== 0
    ? (last.value - reference.value) / Math.abs(reference.value) * 100 : null;
  return [{ series: last.series, last: last.value, change_24h: change,
    min, max, mean, std: rows.length > 1 ? Math.sqrt(squares / (rows.length - 1)) : null,
    count: rows.length, unit: last.unit }];
}

export class QueryCache {
  constructor() { this.windows = []; this.pending = new Map(); }

  add(query, result) {
    if (!result || !Array.isArray(result.plot)) throw new Error("Invalid series response from the data API.");
    const grouped = new Map();
    for (const row of result.plot) {
      const epoch = Date.parse(row.timestamp) / 1000;
      if (!Number.isFinite(epoch) || !Number.isFinite(row.value) || !row.signal || !row.series) {
        throw new Error("Invalid timestamp, signal or value in cached series.");
      }
      if (!grouped.has(row.series)) grouped.set(row.series, []);
      grouped.get(row.series).push({ ...row, _epoch: epoch });
    }
    if ([...grouped.values()].some(rows => rows.length > 1500)) throw new Error("Data API exceeded 1,500 points per series.");
    let entry = this.windows.find(item => item.start === query.start && item.end === query.end);
    if (!entry) {
      entry = { start: query.start, end: query.end, traces: new Map(), scope: new Map(),
        signals: new Set(), resolutions: new Set(), statistics: new Map(), downsampled: false };
      this.windows.push(entry);
    }
    for (const [label, rows] of grouped) {
      rows.sort((a, b) => a._epoch - b._epoch);
      entry.traces.set(label, rows);
    }
    for (const node of query.filters.node || ["*"]) {
      if (!entry.scope.has(node)) entry.scope.set(node, new Set());
      for (const signal of query.signals) entry.scope.get(node).add(signal);
    }
    for (const signal of query.signals) entry.signals.add(signal);
    for (const statistic of result.statistics || []) entry.statistics.set(statistic.series, statistic);
    entry.resolutions.add(result.resolution || "native");
    entry.downsampled ||= result.downsampled;
    const size = () => this.windows.reduce((total, window) => total + [...window.traces.values()].reduce((n, rows) => n + rows.length, 0), 0);
    while (this.windows.length > MAX_WINDOWS || size() > MAX_POINTS) {
      if (this.windows.length === 1) {
        this.windows = [];
        throw new Error("Feed exceeds 250,000 browser cache points; split it into narrower datasets.");
      }
      const oldest = this.windows.findIndex(item => item !== entry);
      this.windows.splice(oldest, 1);
    }
  }

  find(start, end, signals, nodes) {
    return this.windows.filter(entry => entry.start <= start && entry.end >= end
      && signals.every(signal => entry.signals.has(signal))
      && (nodes || ["*"]).every(node => signals.every(signal => entry.scope.get(node)?.has(signal))))
      .sort((a, b) => (Date.parse(a.end) - Date.parse(a.start)) - (Date.parse(b.end) - Date.parse(b.start)))[0];
  }

  select(start, end, signals, filters) {
    if (!signals.length || Object.values(filters).some(values => !values.length)) {
      return { plot: [], statistics: [], resolution: "native", statisticsMode: "display" };
    }
    const entry = this.find(start, end, signals, filters.node);
    if (!entry) return null;
    const lower = Date.parse(`${start}T00:00:00Z`) / 1000;
    const upper = Date.parse(`${end}T00:00:00Z`) / 1000 + 86400;
    const chosen = new Set(signals);
    const plot = [];
    let first = [];
    for (const rows of entry.traces.values()) {
      const row = rows[0];
      if (!chosen.has(row.signal) || Object.entries(filters).some(([column, values]) =>
        !values.includes(row.dimensions?.[column] ?? row[column]))) continue;
      const slice = rows.slice(lowerBound(rows, lower), lowerBound(rows, upper));
      if (!first.length) first = slice;
      plot.push(...slice);
    }
    const exact = entry.start === start && entry.end === end && first.length
      ? entry.statistics.get(first[0].series) : null;
    return { plot, statistics: exact ? [exact] : viewStatistics(first),
      statisticsMode: exact ? "source" : "display", resolution: [...entry.resolutions].join("/"), downsampled: entry.downsampled };
  }
}

export function feedCache(key, revision = "") {
  const identity = `${key}:${revision}`;
  if (!feeds.has(identity)) {
    while (feeds.size >= 4) feeds.delete(feeds.keys().next().value);
    feeds.set(identity, new QueryCache());
  }
  const cache = feeds.get(identity);
  feeds.delete(identity); feeds.set(identity, cache);
  return cache;
}

export function clearQueryCaches() {
  for (const cache of feeds.values()) cache.windows = [];
  feeds.clear();
}
