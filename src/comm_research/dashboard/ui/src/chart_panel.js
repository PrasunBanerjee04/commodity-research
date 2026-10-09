import { loadMetadata, loadOptions, loadSeries } from "./api.js";
import { feedCache } from "./query_cache.js";
import { emptyPaths } from "./canvas_paths.js";
import { LineRenderer } from "./line_renderer.js";

const NODE_COLORS = ["#2962FF", "#089981", "#D18B35", "#8191A8", "#F23645"];
const COMPONENT_ALIASES = { MCE: "ENERGY", MCC: "CONG", MCL: "LOSS", MGHG: "GHG" };

export function traceStyle(row) {
  const node = row.node || row.series.split(" · ")[1] || row.series;
  let hash = 0;
  for (const char of node) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0;
  const base = node.includes("NP15") ? NODE_COLORS[0] : node.includes("SP15") ? NODE_COLORS[1] : node.includes("ZP26") ? NODE_COLORS[2] : NODE_COLORS[hash % NODE_COLORS.length];
  const component = row.component || "LMP";
  const alpha = { LMP: "FF", ENERGY: "AA", CONG: "DD", LOSS: "88", GHG: "66" }[component] || "FF";
  return { stroke: `${base}${alpha}`, dash: component === "CONG" ? [6, 4] : component === "LOSS" ? [2, 3] : [], width: component === "LMP" ? 1.6 : 1.2 };
}
const DATE_RANGES = ["1D", "5D", "1M", "1Y", "ALL"];
const METRICS = [
  ["LAST", "last"],
  ["CHG", "change_24h"],
  ["MIN", "min"],
  ["MAX", "max"],
  ["MEAN", "mean"],
  ["STD", "std"],
];

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function formatNumber(value, percent = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const formatted = Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
  });
  return percent ? `${formatted}%` : formatted;
}

function utcDate(date) {
  return date.toISOString().slice(0, 10);
}

function dateBefore(date, range) {
  const next = new Date(`${date}T00:00:00Z`);
  const days = { "1D": 0, "5D": 4, "1M": 29, "1Y": 364 }[range] || 0;
  next.setUTCDate(next.getUTCDate() - days);
  return utcDate(next);
}

function titleFor(dataset) {
  const text = dataset.title || dataset.name || dataset.key;
  return text.includes(" / ") ? text.replace(" / ", ": ") : text;
}

function choiceButton(value, label, selected) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "choice-toggle"; button.value = value;
  button.setAttribute("role", "checkbox"); button.setAttribute("aria-label", label);
  button.setAttribute("aria-checked", String(selected));
  const marker = document.createElement("span"); marker.className = "choice-marker";
  marker.setAttribute("aria-hidden", "true");
  const text = document.createElement("span"); text.textContent = label;
  button.append(marker, text);
  return button;
}

function savedSettings(key) {
  try {
    const settings = JSON.parse(localStorage.getItem("commodity-panel-settings") || "{}");
    return settings[key] || {};
  } catch (error) {
    console.warn("Unable to restore saved panel controls.", error);
    return {};
  }
}

function saveSettings(key, settings) {
  try {
    const saved = JSON.parse(localStorage.getItem("commodity-panel-settings") || "{}");
    saved[key] = settings;
    localStorage.setItem("commodity-panel-settings", JSON.stringify(saved));
  } catch (error) {
    console.warn("Unable to save panel controls.", error);
  }
}

export class ChartPanel {
  constructor(dataset, container, onError, settingsKey = dataset.key) {
    this.dataset = dataset;
    this.container = container;
    this.onError = onError;
    this.settingsKey = dataset.key;
    this.metadata = null;
    this.chart = null;
    this.settings = savedSettings(this.settingsKey);
    if (!Object.keys(this.settings).length && settingsKey !== this.settingsKey) this.settings = savedSettings(settingsKey);
    this.hiddenSeries = new Set(this.settings.hiddenSeries || []);
    this.filters = {};
    this.resizeObserver = null;
    this.resizeFrame = null;
    this.disposed = false;
    this.signals = [];
    this.lastRows = [];
    this.requestSequence = 0;
    this.nodeValues = [];
    this.saveTimer = null;
    this.pageHideListener = () => saveSettings(this.settingsKey, this.settings);
    this.themeListener = () => { this.destroyChart(); if (this.lastRows.length) this.renderChart(this.lastRows); };
  }

  async init() {
    this.container.classList.add("market-panel");
    this.container.innerHTML = `
      <div class="panel-toolbar">
        <strong class="panel-title">${escapeHtml(titleFor(this.dataset))}</strong>
        <div class="range-control" role="group" aria-label="Date range">
          ${DATE_RANGES.map((range) => `<button type="button" data-range="${range}">${range}</button>`).join("")}
        </div>
        <label><span class="sr-only">Start date</span><input class="panel-date" data-date="start" type="date"></label>
        <label><span class="sr-only">End date</span><input class="panel-date" data-date="end" type="date"></label>
        <div class="component-ribbon" role="group" aria-label="LMP Components"></div>
        <details class="signal-picker">
          <summary data-signal-label>SIGNALS</summary>
          <div class="signal-options" data-signal-options></div>
        </details>
        <div class="dimension-controls"></div>
        <button class="panel-update" type="button">UPDATE</button>
      </div>
      <div class="panel-metrics" aria-label="Market metrics"></div>
      <section class="panel-chart chart-viewport" aria-label="Chart viewport">
        <div class="chart-mount"></div>
        <div class="chart-legend" role="group" aria-label="Chart legend"></div>
        <div class="chart-resolution"></div>
        <div class="chart-empty" role="status">LOADING_DATA…</div>
      </section>
      <div class="panel-error" role="status"></div>
    `;
    this.mount = this.container.querySelector(".chart-mount");
    this.empty = this.container.querySelector(".chart-empty");
    this.error = this.container.querySelector(".panel-error");
    this.metrics = this.container.querySelector(".panel-metrics");
    this.startInput = this.container.querySelector('[data-date="start"]');
    this.endInput = this.container.querySelector('[data-date="end"]');
    this.signalLabel = this.container.querySelector("[data-signal-label]");
    this.signalOptions = this.container.querySelector("[data-signal-options]");
    this.componentRibbon = this.container.querySelector(".component-ribbon");
    this.legend = this.container.querySelector(".chart-legend");
    this.resolutionLabel = this.container.querySelector(".chart-resolution");
    this.dimensionControls = this.container.querySelector(".dimension-controls");
    this.container.addEventListener("toggle", () => this.positionPickers(), true);
    this.container.querySelector(".panel-toolbar").addEventListener("scroll", () => this.positionPickers());
    // Observe before awaiting metadata: tabs may mount while hidden or before layout.
    this.resizeObserver = new ResizeObserver(() => this.resizeChart());
    this.resizeObserver.observe(this.mount);
    document.addEventListener("commodities-theme-change", this.themeListener);
    window.addEventListener("pagehide", this.pageHideListener);

    this.container.querySelector(".panel-update").addEventListener("click", () => this.refresh());
    this.container.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => this.applyRange(button.dataset.range));
    });
    const dateChange = () => {
      this.setRangeActive(null);
      if (this.startInput.value && this.endInput.value && this.startInput.value <= this.endInput.value) this.persistAndRefresh();
    };
    this.startInput.addEventListener("change", dateChange);
    this.endInput.addEventListener("change", dateChange);
    const signalChange = (event) => {
      const button = event.target.closest("[data-signal-key]");
      if (!button) return;
      const selected = button.getAttribute("aria-checked") !== "true";
      button.setAttribute("aria-checked", String(selected));
      const checked = this.container.querySelectorAll('[data-signal-key][aria-checked="true"]');
      if (checked.length > 8) {
        button.setAttribute("aria-checked", "false");
        this.showError(new Error("Select no more than eight signals."));
        return;
      }
      this.persistAndRefresh();
    };
    this.signalOptions.addEventListener("click", signalChange);
    this.componentRibbon.addEventListener("click", signalChange);
    this.dimensionControls.addEventListener("click", (event) => {
      const node = event.target.closest("[data-node]");
      if (node) {
        const chosen = new Set(this.filters.node || []);
        const selectedNode = node.getAttribute("aria-checked") !== "true";
        if (selectedNode) chosen.add(node.value); else chosen.delete(node.value);
        const selected = [...chosen];
        if (selected.length > 8) { this.showError(new Error("Select up to eight nodes.")); return; }
        node.setAttribute("aria-checked", String(selectedNode));
        this.filters.node = selected;
        this.updateNodeLabel();
        this.persistAndRefresh();
        return;
      }
    });
    this.dimensionControls.addEventListener("change", (event) => {
      const select = event.target.closest("select[data-dimension]");
      if (!select) return;
      const { dimension } = select.dataset;
      if (select.value) this.filters[dimension] = [select.value];
      else delete this.filters[dimension];
      this.persistAndRefresh();
    });

    try {
      if (typeof window.uPlot !== "function") {
        this.showError(new Error("ERR_DEPENDENCY_LOAD_FAILED: uPlot missing"));
        return;
      }
      this.metadata = await loadMetadata(this.dataset.key, this.dataset.revision);
      if (this.disposed) return;
      this.dataset.revision = this.metadata.dataset.revision;
      this.cache = feedCache(this.dataset.key, this.dataset.revision);
      this.configureDates();
      this.configureSignals();
      await this.configureDimensions();
      if (this.disposed) return;
      if (this.startInput.value && this.endInput.value) await this.ensureWindow(this.startInput.value, this.endInput.value);
      if (this.disposed) return;
      await this.refresh();
      this.prefetchHistory();
    } catch (error) {
      if (!this.disposed) this.showError(error);
    }
  }

  configureDates() {
    const { earliest, latest } = this.metadata;
    if (!earliest || !latest) {
      this.startInput.disabled = true;
      this.endInput.disabled = true;
      this.showError(new Error("This feed has no recognized time field."));
      return;
    }
    this.startInput.min = earliest;
    this.startInput.max = latest;
    this.endInput.min = earliest;
    this.endInput.max = latest;
    const validSavedRange = this.settings.start >= earliest && this.settings.end <= latest
      && this.settings.start <= this.settings.end;
    this.endInput.value = validSavedRange ? this.settings.end : latest;
    this.startInput.value = validSavedRange ? this.settings.start : earliest;
    const initialRange = this.settings.range === undefined || !validSavedRange ? "1M" : this.settings.range;
    this.setRangeActive(initialRange);
    if (initialRange) this.applyRange(initialRange, false);
  }

  configureSignals() {
    this.signals = this.metadata.signals || [];
    const requested = (this.settings.signals || []).map(key => {
      const [column, component] = key.split(":");
      return component ? `${column}:${COMPONENT_ALIASES[component] || component}` : key;
    });
    const selected = requested.filter((key) => this.signals.some((signal) => signal.key === key));
    if (selected.length || (Array.isArray(this.settings.signals) && !this.settings.signals.length)) {
      this.settings.signals = selected;
    } else {
      this.settings.signals = this.signals.slice(0, 1).map((signal) => signal.key);
    }
    this.signalOptions.replaceChildren();
    this.componentRibbon.replaceChildren();
    for (const signal of this.signals) {
      const checkbox = choiceButton(signal.key, signal.label, this.settings.signals.includes(signal.key));
      checkbox.dataset.signalKey = signal.key;
      if (["LMP", "ENERGY", "CONG", "LOSS"].includes(signal.component)) this.componentRibbon.append(checkbox);
      else this.signalOptions.append(checkbox);
    }
    this.updateSignalLabel();
  }

  async configureDimensions() {
    const dimensions = this.metadata.dimensions || [];
    const preferred = dimensions.filter((dimension) => /node|pnode/i.test(dimension));
    const ordered = [...preferred, ...dimensions.filter((dimension) => !preferred.includes(dimension))];
    const fragments = [];
    const results = await Promise.all(ordered.map(dimension => loadOptions(this.dataset.key, dimension, this.dataset.revision)));
    for (let index = 0; index < ordered.length; index++) {
      const dimension = ordered[index];
      const result = results[index];
      if (this.disposed) return;
      if (dimension === "node") {
        const picker = document.createElement("details");
        picker.className = "signal-picker node-picker";
        const summary = document.createElement("summary");
        summary.dataset.nodeLabel = "";
        summary.textContent = "NODES";
        const options = document.createElement("div");
        options.className = "signal-options";
        const values = result.values || [];
        this.nodeValues = values;
        const requested = this.settings.filters?.node;
        // A bounded initial selection keeps an all-node feed below the trace limit.
        const selected = requested === undefined ? values.slice(0, 1) : requested;
        this.filters.node = values.filter(value => selected.includes(value));
        const search = document.createElement("input");
        search.type = "search"; search.placeholder = "Find PNode…"; search.setAttribute("aria-label", "Find PNode");
        const list = document.createElement("div");
        const render = () => {
          list.replaceChildren();
          const matches = values.filter(value => value.toLowerCase().includes(search.value.toLowerCase()));
          for (const value of matches.slice(0, 200)) {
            const input = choiceButton(value, value, this.filters.node.includes(value));
            input.dataset.node = "";
            list.append(input);
          }
          if (matches.length > 200) {
            const note = document.createElement("small"); note.textContent = `${matches.length} matches; refine search`; list.append(note);
          }
        };
        search.addEventListener("input", render); render(); options.append(search, list);
        picker.append(summary, options); fragments.push(picker); continue;
      }
      const select = document.createElement("select");
      select.className = "panel-control";
      select.dataset.dimension = dimension;
      select.title = dimension;
      const all = document.createElement("option");
      all.value = "";
      all.textContent = `${dimension.toUpperCase()}: ALL`;
      const savedValues = this.settings.filters?.[dimension] || [];
      all.selected = !savedValues.length;
      select.append(all);
      const values = result.values || [];
      for (const value of values) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = savedValues.includes(value);
        select.append(option);
      }
      if (select.value) this.filters[dimension] = [select.value];
      fragments.push(select);
    }
    this.dimensionControls.replaceChildren(...fragments);
    this.updateNodeLabel();
  }

  applyRange(range, refresh = true) {
    const { earliest, latest } = this.metadata || {};
    if (!earliest || !latest) return;
    this.endInput.value = latest;
    this.startInput.value = range === "ALL" ? earliest : dateBefore(latest, range);
    if (this.startInput.value < earliest) this.startInput.value = earliest;
    this.setRangeActive(range);
    if (refresh) this.persistAndRefresh();
  }

  setRangeActive(range) {
    this.container.querySelectorAll("[data-range]").forEach((button) => {
      button.classList.toggle("active", button.dataset.range === range);
    });
    this.settings.range = range;
  }

  selectedSignals() {
    return [...this.container.querySelectorAll('[data-signal-key][aria-checked="true"]')]
      .slice(0, 8)
      .map((checkbox) => checkbox.value);
  }

  updateNodeLabel() {
    const summary = this.dimensionControls.querySelector("[data-node-label]");
    if (summary) summary.textContent = `NODES ${this.filters.node?.length || 0}`;
  }

  updateSignalLabel() {
    const count = this.selectedSignals().length;
    this.signalLabel.textContent = count ? `SIGNALS ${count}` : "SIGNALS 0";
  }

  positionPickers() {
    for (const picker of this.container.querySelectorAll(".signal-picker[open]")) {
      const options = picker.querySelector(".signal-options");
      const anchor = picker.querySelector("summary").getBoundingClientRect();
      options.style.maxHeight = `${Math.min(240, Math.max(60, window.innerHeight - 16))}px`;
      options.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - options.offsetWidth - 8))}px`;
      options.style.top = `${Math.max(8, Math.min(anchor.bottom + 2, window.innerHeight - options.offsetHeight - 8))}px`;
    }
  }

  async persistAndRefresh() {
    this.settings = {
      ...this.settings,
      end: this.endInput.value,
      filters: this.filters,
      range: this.settings.range,
      signals: this.selectedSignals(),
      start: this.startInput.value,
    };
    for (const select of this.dimensionControls.querySelectorAll("[data-dimension]")) {
      const { dimension } = select.dataset;
      if (select.value) this.filters[dimension] = [select.value];
      else delete this.filters[dimension];
    }
    this.settings.filters = this.filters;
    this.scheduleSave();
    this.updateSignalLabel();
    await this.refresh();
  }

  async refresh() {
    if (!this.metadata || this.disposed) return;
    const requestSequence = ++this.requestSequence;
    this.clearError();
    const signals = this.selectedSignals();
    this.settings.signals = signals;
    this.updateSignalLabel();
    if (!this.startInput.value || !this.endInput.value || this.startInput.value > this.endInput.value) {
      this.showError(new Error("Select a valid date range."));
      return;
    }
    try {
      let result = this.cache.select(this.startInput.value, this.endInput.value, signals, this.filters);
      if (!result) {
        this.showState("LOADING_DATA…");
        await this.ensureWindow(this.startInput.value, this.endInput.value);
        if (this.disposed || requestSequence !== this.requestSequence) return;
        result = this.cache.select(this.startInput.value, this.endInput.value, signals, this.filters);
      }
      if (this.disposed || requestSequence !== this.requestSequence) return;
      if (!result || !Array.isArray(result.plot)) throw new Error("Invalid series response from the data API.");
      if (!result.plot.length && this.settings.range === "1M" && signals.length && Object.values(this.filters).every(values => values.length)) {
        await this.ensureWindow(this.metadata.earliest, this.metadata.latest);
        if (this.disposed || requestSequence !== this.requestSequence) return;
        const history = this.cache.select(this.metadata.earliest, this.metadata.latest, signals, this.filters);
        if (history?.plot.length) {
          const latest = new Date(Math.max(...history.plot.map(row => row._epoch)) * 1000).toISOString().slice(0, 10);
          this.endInput.value = latest;
          this.startInput.value = dateBefore(latest, "1M") < this.metadata.earliest ? this.metadata.earliest : dateBefore(latest, "1M");
          result = this.cache.select(this.startInput.value, this.endInput.value, signals, this.filters);
          this.error.textContent = "Default horizon was empty; showing the latest available selected series.";
        }
      }
      this.renderMetrics(result.statistics || []);
      this.metrics.title = result.statisticsMode === "source" ? "Statistics from all source intervals in this window"
        : "Statistics from displayed cached samples; use a narrower uncached window for native resolution";
      this.mount.dataset.resolution = result.resolution;
      this.resolutionLabel.textContent = `${result.resolution === "native" ? (result.downsampled ? "Native sample" : "Native") : result.resolution + " mean"} · ${result.statisticsMode === "source" ? "Source stats" : "Display stats"}`;
      this.pendingView = { points: result.plot.length, resolution: result.resolution };
      this.renderChart(result.plot || []);
      this.settings = {
        ...this.settings,
        end: this.endInput.value,
        filters: this.filters,
        signals,
        start: this.startInput.value,
      };
      this.scheduleSave();
    } catch (error) {
      if (!this.disposed && requestSequence === this.requestSequence) this.showError(error);
    }
  }

  scheduleSave() {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => saveSettings(this.settingsKey, this.settings), 150);
  }

  viewRendered() {
    if (!this.pendingView) return;
    const detail = this.pendingView;
    this.pendingView = null;
    this.container.dispatchEvent(new CustomEvent("chart-view-updated", { detail }));
  }

  async ensureWindow(start, end) {
    const cache = this.cache;
    const signals = this.signals.map(signal => signal.key);
    if (cache.find(start, end, signals, this.nodeValues.length ? this.nodeValues : undefined)) return;
    for (let signalIndex = 0; signalIndex < signals.length; signalIndex += 8) {
      const batch = signals.slice(signalIndex, signalIndex + 8);
      const days = (Date.parse(end) - Date.parse(start)) / 86400000 + 1;
      const nodeBatch = Math.max(1, Math.min(8, Math.floor(1500000 / (days * 288 * batch.length))));
      const nodes = this.nodeValues.length ? this.nodeValues : [null];
      for (let index = 0; index < nodes.length; index += nodeBatch) {
        const subset = nodes.slice(index, index + nodeBatch);
        if (cache.find(start, end, batch, subset[0] === null ? undefined : subset)) continue;
        const query = { key: this.dataset.key, start, end, zone: "UTC", signals: batch,
          filters: subset[0] === null ? {} : { node: subset }, frequency: "native", aggregation: "Mean" };
        const identity = JSON.stringify(query);
        if (!cache.pending.has(identity)) {
          const pending = loadSeries(query).then(result => cache.add(query, result)).finally(() => cache.pending.delete(identity));
          cache.pending.set(identity, pending);
        }
        await cache.pending.get(identity);
        if (this.disposed) return;
      }
    }
  }

  prefetchHistory() {
    if (this.disposed || !this.cache?.windows.length) return;
    void this.ensureWindow(this.metadata.earliest, this.metadata.latest).catch(error => {
      console.warn("Historical cache warm-up failed; ALL can retry.", error);
    });
  }

  async reload(dataset) {
    ++this.requestSequence;
    this.dataset = dataset;
    try {
      this.metadata = await loadMetadata(dataset.key, dataset.revision);
      if (this.disposed) return;
      this.cache = feedCache(dataset.key, dataset.revision);
      this.configureDates(); this.configureSignals();
      await this.configureDimensions();
      await this.refresh(); this.prefetchHistory();
    } catch (error) { if (!this.disposed) this.showError(error); }
  }

  renderMetrics(statistics) {
    const summary = statistics[0];
    if (!summary) {
      this.metrics.replaceChildren();
      return;
    }
    this.metrics.replaceChildren(...METRICS.map(([label, key]) => {
      const cell = document.createElement("div");
      cell.className = "metric-cell";
      const name = document.createElement("span");
      name.textContent = `${label}:`;
      const value = document.createElement("strong");
      value.textContent = formatNumber(summary[key], key === "change_24h");
      if (key === "change_24h" && Number(summary[key]) > 0) value.classList.add("positive");
      if (key === "change_24h" && Number(summary[key]) < 0) value.classList.add("negative");
      cell.append(name, value);
      return cell;
    }));
  }

  renderChart(rows) {
    try {
      this.drawChart(rows);
    } catch (error) {
      this.showError(error, "CHART_RENDER_ERROR");
    }
  }

  drawChart(rows) {
    if (this.disposed) return;
    this.lastRows = rows;
    if (!rows.length) {
      const cleared = !this.selectedSignals().length || Object.values(this.filters).some(values => !values.length);
      if (cleared && this.chart) {
        // Clearing a selection should not discard the warm canvas/GPU context.
        this.mount.style.visibility = "hidden";
        this.chartLabels = [];
        this.chart.setData([[], ...this.chart.series.slice(1).map(() => [])]);
      } else this.destroyChart();
      this.legend.replaceChildren();
      this.showState("NO_RECORDS_FOUND_FOR_DATE_RANGE");
      this.viewRendered();
      return;
    }
    this.mount.style.visibility = "";
    if (typeof window.uPlot !== "function") {
      this.showError(new Error("ERR_DEPENDENCY_LOAD_FAILED: uPlot missing"));
      return;
    }
    // Never initialize the chart at 1x1 while Dockview is measuring/activating a tab.
    const width = this.mount.clientWidth;
    const height = this.mount.clientHeight;
    if (width <= 0 || height <= 0) {
      this.showState("WAITING_FOR_VIEWPORT…");
      return;
    }
    this.empty.hidden = true;
    const firstRows = new Map();
    for (const row of rows) if (!firstRows.has(row.series)) firstRows.set(row.series, row);
    const labels = [...firstRows.keys()];
    const timestamps = [...new Set(rows.map(row => row._epoch ?? Date.parse(row.timestamp) / 1000))].sort((a, b) => a - b);
    const valuesBySeries = new Map(labels.map((label) => [label, new Map()]));
    for (const row of rows) {
      const timestamp = row._epoch ?? Date.parse(row.timestamp) / 1000;
      valuesBySeries.get(row.series).set(timestamp, row.value);
    }
    const data = [
      timestamps,
      ...labels.map((label) => timestamps.map((timestamp) => valuesBySeries.get(label).get(timestamp) ?? null)),
    ];
    const definitions = labels.map(label => ({ label, ...traceStyle(firstRows.get(label)),
      show: !this.hiddenSeries.has(label), points: { show: false }, paths: emptyPaths, spanGaps: true }));
    const changed = labels.join("\n") !== this.chartLabels?.join("\n");
    if (this.chart) {
      this.chart.batch(() => {
        if (changed) {
          for (let index = this.chart.series.length - 1; index > 0; index--) this.chart.delSeries(index);
          for (const definition of definitions) this.chart.addSeries(definition);
        }
        this.chart.setData(data);
      });
      this.chartLabels = labels;
      if (!changed) return;
    } else {
      const rootStyles = getComputedStyle(document.body);
      const text = rootStyles.getPropertyValue("--text-muted").trim();
      const grid = rootStyles.getPropertyValue("--grid-line").trim();
      const renderer = new LineRenderer();
      this.chart = new window.uPlot({
        width,
        height,
        padding: [0, 40, 18, 0],
        scales: { x: { time: true } },
        hooks: { draw: [plot => renderer.draw(plot), () => this.viewRendered()], destroy: [() => renderer.dispose()] },
        series: [
          { value: (_plot, timestamp) => timestamp == null ? "" : new Date(timestamp * 1000).toISOString() },
          ...definitions,
        ],
        axes: [
          {
            stroke: text,
            grid: { stroke: grid, width: 1 },
            ticks: { stroke: grid, width: 1 },
            font: "10px Consolas, monospace",
          },
          {
            side: 1,
            stroke: text,
            grid: { stroke: grid, width: 1 },
            ticks: { stroke: grid, width: 1 },
            font: "10px Consolas, monospace",
            values: (_plot, values) => values.map((value) => Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })),
          },
        ],
        cursor: {
          show: true,
          x: true,
          y: true,
        // Default marker factory creates elements required by dynamic delSeries.
        // CSS hides the markers while retaining the crosshair.
          drag: { setScale: false, x: false, y: false },
        },
        legend: { show: false },
      }, data, this.mount);
      this.chartLabels = labels;
    }
    this.legend.replaceChildren();
    labels.forEach((label, index) => {
      const button = document.createElement("button");
      button.type = "button"; button.textContent = label; button.title = label;
      button.style.setProperty("--trace-color", traceStyle(firstRows.get(label)).stroke);
      button.setAttribute("aria-pressed", String(!this.hiddenSeries.has(label)));
      button.addEventListener("click", () => {
        const show = this.hiddenSeries.has(label);
        if (show) this.hiddenSeries.delete(label); else this.hiddenSeries.add(label);
        this.chart.setSeries(index + 1, { show });
        button.setAttribute("aria-pressed", String(show));
        this.settings.hiddenSeries = [...this.hiddenSeries];
        this.scheduleSave();
      });
      this.legend.append(button);
    });
  }

  resizeChart() {
    if (this.disposed || this.resizeFrame !== null) return;
    this.resizeFrame = requestAnimationFrame(() => {
      this.resizeFrame = null;
      if (this.disposed || !this.mount) return;
      this.positionPickers();
      const width = this.mount.clientWidth;
      const height = this.mount.clientHeight;
      if (width <= 0 || height <= 0) return;
      try {
        if (this.chart) {
          if (this.chart.width !== width || this.chart.height !== height) this.chart.setSize({ width, height });
        } else if (this.lastRows.length) this.renderChart(this.lastRows);
      } catch (error) {
        this.showError(error, "CHART_RENDER_ERROR");
      }
    });
  }

  destroyChart() {
    if (this.chart) {
      this.chart.destroy();
      this.chart = null;
    }
  }

  showState(message, error = false) {
    this.empty.textContent = message;
    this.empty.classList.toggle("is-error", error);
    this.empty.hidden = false;
  }

  showError(error, code = "DATA_FETCH_ERROR") {
    console.error(code, error);
    this.lastRows = [];
    this.pendingView = null;
    this.destroyChart();
    this.legend.replaceChildren();
    this.metrics.replaceChildren();
    this.showState(error.message.startsWith("ERR_DEPENDENCY_LOAD_FAILED") ? error.message : `${code}: ${error.message}`, true);
    this.error.textContent = error.message;
    this.onError?.(error);
  }

  clearError() {
    this.error.textContent = "";
  }

  dispose() {
    this.disposed = true;
    document.removeEventListener("commodities-theme-change", this.themeListener);
    window.removeEventListener("pagehide", this.pageHideListener);
    this.resizeObserver?.disconnect();
    clearTimeout(this.saveTimer);
    saveSettings(this.settingsKey, this.settings);
    if (this.resizeFrame !== null) cancelAnimationFrame(this.resizeFrame);
    this.destroyChart();
  }
}
