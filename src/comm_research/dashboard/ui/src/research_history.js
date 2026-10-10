import { request } from "./http.js";
import { lowerBound } from "./node_history.js";

const metadata = new Map(),
  histories = new Map();
export function clearResearchHistories() {
  metadata.clear();
  histories.clear();
}
function cached(cache, key, callback) {
  if (!cache.has(key))
    cache.set(
      key,
      callback().catch((error) => {
        cache.delete(key);
        throw error;
      }),
    );
  return cache.get(key);
}
export function loadResearchMetadata(dataset) {
  return cached(metadata, `${dataset.key}:${dataset.revision}`, () =>
    request(`/api/research/metadata?feed=${encodeURIComponent(dataset.key)}`),
  );
}
export function prepareTrace(trace) {
  if (
    !Array.isArray(trace.timestamps) ||
    !Array.isArray(trace.values) ||
    trace.values.length !== trace.timestamps.length
  )
    throw new Error("Invalid research history schema");
  const epochs = Float64Array.from(
    trace.timestamps,
    (value) => Date.parse(value) / 1000,
  );
  const values = Float64Array.from(trace.values, (value) =>
    value == null ? NaN : value,
  );
  for (let i = 0; i < epochs.length; i++)
    if (
      !Number.isFinite(epochs[i]) ||
      (i && epochs[i] <= epochs[i - 1]) ||
      (!Number.isNaN(values[i]) && !Number.isFinite(values[i]))
    )
      throw new Error("Invalid research timestamps or values");
  const levels = [Uint32Array.from({ length: epochs.length }, (_, i) => i)];
  while (levels.at(-1).length > 32) {
    const previous = levels.at(-1),
      selected = [];
    for (let start = 0; start < previous.length; start += 16) {
      const end = Math.min(start + 16, previous.length),
        points = new Set([previous[start], previous[end - 1]]);
      let low = -1,
        high = -1;
      for (let j = start; j < end; j++) {
        const i = previous[j];
        if (!Number.isFinite(values[i])) continue;
        if (low < 0 || values[i] < values[low]) low = i;
        if (high < 0 || values[i] > values[high]) high = i;
      }
      if (low >= 0) {
        points.add(low);
        points.add(high);
      }
      selected.push(...[...points].sort((a, b) => a - b));
    }
    levels.push(Uint32Array.from(selected));
  }
  return { ...trace, epochs, values, levels };
}
export function loadResearchHistory(dataset, entity) {
  return cached(histories, `${dataset.key}:${dataset.revision}:${entity}`, () =>
    request(
      `/api/research/data?${new URLSearchParams({ feed: dataset.key, entity, max_points: "0" })}`,
    ).then((payload) => {
      if (!Array.isArray(payload.series))
        throw new Error("Invalid research series payload");
      return { ...payload, series: payload.series.map(prepareTrace) };
    }),
  );
}
export function sampleTrace(trace, min, max, budget = 1500) {
  const first = lowerBound(trace.epochs, min),
    end = lowerBound(trace.epochs, max + 0.001);
  if (first === end) return [];
  for (const level of trace.levels) {
    const left = lowerBound(level, first),
      right = lowerBound(level, end);
    if (right - left <= budget - 2)
      return [
        ...new Set([first, ...level.subarray(left, right), end - 1]),
      ].sort((a, b) => a - b);
  }
  throw new Error("Research display budget is too small");
}
export function stackTraces(traces, min, max, budget = 1500) {
  if (!traces.length) return [];
  const perTrace = Math.max(36, Math.floor(budget / traces.length));
  const epochs = [
    ...new Set(
      traces.flatMap((trace) =>
        sampleTrace(trace, min, max, perTrace).map((i) => trace.epochs[i]),
      ),
    ),
  ].sort((a, b) => a - b);
  const totals = epochs.map(() => 0),
    missing = epochs.map(() => false);
  return traces.map((trace) => {
    const values = epochs.map((epoch, i) => {
      const position = lowerBound(trace.epochs, epoch);
      const value =
        trace.epochs[position] === epoch ? trace.values[position] : NaN;
      if (!Number.isFinite(value)) missing[i] = true;
      totals[i] += Number.isFinite(value) ? value : 0;
      return value;
    });
    return {
      trace,
      epochs,
      values,
      y: totals.map((value, i) => (missing[i] ? null : value)),
    };
  });
}
