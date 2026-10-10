import { request } from "./http.js";

export const COMPONENTS = [
  { key: "lmp", label: "LMP", color: "#2962FF", width: 2 },
  { key: "energy", label: "Energy", color: "#089981", width: 1.5 },
  {
    key: "congestion",
    label: "Congestion",
    color: "#F23645",
    width: 1.5,
    dash: [6, 4],
  },
  { key: "loss", label: "Loss", color: "#FF9800", width: 1.5 },
];
const histories = new Map();
const nodeLists = new Map();

export function clearNodeHistories() {
  histories.clear();
  nodeLists.clear();
}
export function loadNodes(dataset) {
  const key = `${dataset.key}:${dataset.revision}`;
  if (!nodeLists.has(key))
    nodeLists.set(
      key,
      request(`/api/nodes?feed=${encodeURIComponent(dataset.key)}`).catch(
        (error) => {
          nodeLists.delete(key);
          throw error;
        },
      ),
    );
  return nodeLists.get(key);
}

export function lowerBound(values, target, high = values.length) {
  let low = 0;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (values[mid] < target) low = mid + 1;
    else high = mid;
  }
  return low;
}

export function prepareHistory(payload) {
  const times = payload.timestamps;
  if (
    !Array.isArray(times) ||
    !COMPONENTS.every(
      ({ key }) =>
        Array.isArray(payload[key]) && payload[key].length === times.length,
    )
  )
    throw new Error("Invalid CAISO history schema.");
  const epochs = Float64Array.from(times, (value) => Date.parse(value) / 1000);
  for (let i = 0; i < epochs.length; i++)
    if (!Number.isFinite(epochs[i]) || (i && epochs[i] <= epochs[i - 1]))
      throw new Error(
        "CAISO timestamps must be finite, unique and increasing.",
      );
  const values = COMPONENTS.map(({ key }) =>
    Float64Array.from(payload[key], (value) => (value == null ? NaN : value)),
  );
  if (
    values.some((column) =>
      column.some((value) => !Number.isNaN(value) && !Number.isFinite(value)),
    )
  )
    throw new Error("Invalid CAISO price.");
  const levels = [Uint32Array.from({ length: epochs.length }, (_, i) => i)];
  // A multiresolution min/max index built once per node. Retain exact source
  // values for hover; range changes visit only the chosen small display level.
  while (levels.at(-1).length > 256) {
    const previous = levels.at(-1),
      selected = [];
    for (let start = 0; start < previous.length; start += 32) {
      const end = Math.min(start + 32, previous.length);
      const indices = new Set([previous[start], previous[end - 1]]);
      for (const column of values) {
        let min = -1,
          max = -1;
        for (let j = start; j < end; j++) {
          const i = previous[j],
            value = column[i];
          if (!Number.isFinite(value)) continue;
          if (min < 0 || value < column[min]) min = i;
          if (max < 0 || value > column[max]) max = i;
        }
        if (min >= 0) {
          indices.add(min);
          indices.add(max);
        }
      }
      selected.push(...[...indices].sort((a, b) => a - b));
    }
    levels.push(Uint32Array.from(selected));
  }
  return {
    node: payload.node,
    epochs,
    values,
    levels,
    revision: payload.revision,
  };
}

export function loadNodeHistory(dataset, node) {
  const key = `${dataset.key}:${dataset.revision}:${node}`;
  if (!histories.has(key))
    histories.set(
      key,
      request(
        `/api/data?feed=${encodeURIComponent(dataset.key)}&node=${encodeURIComponent(node)}`,
      )
        .then(prepareHistory)
        .catch((error) => {
          histories.delete(key);
          throw error;
        }),
    );
  return histories.get(key);
}

function levelBound(history, level, epoch) {
  let low = 0,
    high = level.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (history.epochs[level[mid]] < epoch) low = mid + 1;
    else high = mid;
  }
  return low;
}

export function displayData(history, min, max, budget = 1500) {
  const first = lowerBound(history.epochs, min),
    end = lowerBound(history.epochs, max + 0.001);
  if (first === end) return [[], ...COMPONENTS.map(() => [])];
  let indices;
  for (const level of history.levels) {
    const left = levelBound(history, level, min),
      right = levelBound(history, level, max + 0.001);
    if (right - left <= budget - 2) {
      indices = [first, ...level.subarray(left, right), end - 1];
      indices = [...new Set(indices)].sort((a, b) => a - b);
      break;
    }
  }
  // The final level has <=256 vertices; pane budgets are always >=320.
  if (!indices) throw new Error("Display point budget is too small.");
  return [
    indices.map((i) => history.epochs[i]),
    ...history.values.map((column) =>
      indices.map((i) => (Number.isFinite(column[i]) ? column[i] : null)),
    ),
  ];
}
