import { loadMetadata, loadOptions, loadSeries } from "./api.js";

const TRACE_COLORS = ["#2962FF", "#089981", "#F23645", "#8B5CF6", "#FF9800", "#00BCD4"];
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
    this.settingsKey = settingsKey;
    this.metadata = null;
    this.chart = null;
    this.settings = savedSettings(this.settingsKey);
    this.filters = {};
    this.resizeObserver = null;
    this.disposed = false;
    this.signals = [];
    this.lastRows = [];
    this.requestSequence = 0;
    this.themeListener = () => this.renderChart(this.lastRows);
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
        <details class="signal-picker">
          <summary data-signal-label>SIGNALS</summary>
          <div class="signal-options" data-signal-options></div>
        </details>
        <div class="dimension-controls"></div>
        <button class="panel-update" type="button">UPDATE</button>
      </div>
      <div class="panel-metrics" aria-label="Market metrics"></div>
      <section class="panel-chart">
        <div class="chart-mount"></div>
        <div class="chart-empty" hidden>NO_DATA_AVAILABLE_FOR_FILTER_RANGE</div>
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
    this.dimensionControls = this.container.querySelector(".dimension-controls");

    this.container.querySelector(".panel-update").addEventListener("click", () => this.refresh());
    this.container.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => this.applyRange(button.dataset.range));
    });
    this.startInput.addEventListener("change", () => this.setRangeActive(null));
    this.endInput.addEventListener("change", () => this.setRangeActive(null));
    this.signalOptions.addEventListener("change", (event) => {
      const checked = this.signalOptions.querySelectorAll('input[type="checkbox"]:checked');
      if (checked.length > 8) {
        event.target.checked = false;
        this.showError(new Error("Select no more than eight signals."));
        return;
      }
      this.persistAndRefresh();
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
      this.metadata = await loadMetadata(this.dataset.key);
      if (this.disposed) return;
      this.configureDates();
      this.configureSignals();
      await this.configureDimensions();
      if (this.disposed) return;
      this.resizeObserver = new ResizeObserver(() => this.resizeChart());
      this.resizeObserver.observe(this.container.querySelector(".panel-chart"));
      document.addEventListener("commodities-theme-change", this.themeListener);
      await this.refresh();
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
    this.endInput.value = this.settings.end && this.settings.end <= latest
      ? this.settings.end
      : latest;
    this.startInput.value = this.settings.start && this.settings.start >= earliest
      ? this.settings.start
      : earliest;
    const initialRange = this.settings.range || "1M";
    this.setRangeActive(initialRange);
    if (initialRange !== "ALL") this.applyRange(initialRange, false);
  }

  configureSignals() {
    this.signals = this.metadata.signals || [];
    const requested = this.settings.signals || [];
    const selected = requested.filter((key) => this.signals.some((signal) => signal.key === key));
    if (selected.length) {
      this.settings.signals = selected;
    } else {
      this.settings.signals = this.signals.slice(0, 1).map((signal) => signal.key);
    }
    this.signalOptions.replaceChildren();
    for (const signal of this.signals) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = signal.key;
      checkbox.checked = this.settings.signals.includes(signal.key);
      checkbox.setAttribute("aria-label", signal.label);
      const text = document.createElement("span");
      text.textContent = signal.label;
      label.append(checkbox, text);
      this.signalOptions.append(label);
    }
    this.updateSignalLabel();
  }

  async configureDimensions() {
    const dimensions = this.metadata.dimensions || [];
    const preferred = dimensions.filter((dimension) => /node|pnode/i.test(dimension));
    const ordered = [...preferred, ...dimensions.filter((dimension) => !preferred.includes(dimension))];
    const fragments = [];
    for (const dimension of ordered) {
      const result = await loadOptions(this.dataset.key, dimension);
      if (this.disposed) return;
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
    return [...this.signalOptions.querySelectorAll('input[type="checkbox"]:checked')]
      .slice(0, 8)
      .map((checkbox) => checkbox.value);
  }

  updateSignalLabel() {
    const count = this.selectedSignals().length;
    this.signalLabel.textContent = count ? `SIGNALS ${count}` : "SIGNALS 0";
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
    saveSettings(this.settingsKey, this.settings);
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
    if (!this.startInput.value || !this.endInput.value) {
      this.showError(new Error("Select a valid date range."));
      return;
    }
    try {
      const result = await loadSeries({
        key: this.dataset.key,
        start: this.startInput.value,
        end: this.endInput.value,
        zone: "UTC",
        signals,
        filters: this.filters,
        frequency: "native",
        aggregation: "Mean",
      });
      if (this.disposed || requestSequence !== this.requestSequence) return;
      this.renderMetrics(result.statistics || []);
      this.renderChart(result.plot || []);
      this.settings = {
        ...this.settings,
        end: this.endInput.value,
        filters: this.filters,
        signals,
        start: this.startInput.value,
      };
      saveSettings(this.settingsKey, this.settings);
    } catch (error) {
      if (!this.disposed && requestSequence === this.requestSequence) this.showError(error);
    }
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
    this.lastRows = rows;
    this.destroyChart();
    if (!rows.length) {
      this.empty.hidden = false;
      return;
    }
    this.empty.hidden = true;
    const labels = [...new Set(rows.map((row) => row.series))];
    const timestamps = [...new Set(rows.map((row) => Math.floor(new Date(row.timestamp).getTime() / 1000)))].sort((a, b) => a - b);
    const valuesBySeries = new Map(labels.map((label) => [label, new Map()]));
    for (const row of rows) {
      const timestamp = Math.floor(new Date(row.timestamp).getTime() / 1000);
      valuesBySeries.get(row.series).set(timestamp, row.value);
    }
    const data = [
      timestamps,
      ...labels.map((label) => timestamps.map((timestamp) => valuesBySeries.get(label).get(timestamp) ?? null)),
    ];
    const width = Math.max(1, this.mount.clientWidth);
    const height = Math.max(1, this.mount.clientHeight);
    const rootStyles = getComputedStyle(document.body);
    const text = rootStyles.getPropertyValue("--text-muted").trim();
    const grid = rootStyles.getPropertyValue("--grid-line").trim();
    this.chart = new window.uPlot({
      width,
      height,
      padding: [0, 40, 18, 0],
      scales: { x: { time: true } },
      series: [
        { value: (_plot, timestamp) => timestamp == null ? "" : new Date(timestamp * 1000).toISOString() },
        ...labels.map((label, index) => ({
          label,
          stroke: TRACE_COLORS[index % TRACE_COLORS.length],
          width: 1.5,
          points: { show: false },
        })),
      ],
      axes: [
        {
          stroke: text,
          grid: { stroke: grid, width: 1, dash: [2, 3] },
          ticks: { stroke: grid, width: 1 },
          font: "10px Consolas, monospace",
        },
        {
          side: 1,
          stroke: text,
          grid: { stroke: grid, width: 1, dash: [2, 3] },
          ticks: { stroke: grid, width: 1 },
          font: "10px Consolas, monospace",
          values: (_plot, values) => values.map((value) => Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })),
        },
      ],
      cursor: {
        show: true,
        x: true,
        y: true,
        points: { show: false },
        drag: { setScale: false, x: false, y: false },
      },
      legend: { show: false },
    }, data, this.mount);
  }

  resizeChart() {
    if (!this.chart || this.disposed) return;
    const width = this.mount.clientWidth;
    const height = this.mount.clientHeight;
    if (width > 0 && height > 0) this.chart.setSize({ width, height });
  }

  destroyChart() {
    if (this.chart) {
      this.chart.destroy();
      this.chart = null;
    }
  }

  showError(error) {
    this.error.textContent = error.message;
    this.onError?.(error);
  }

  clearError() {
    this.error.textContent = "";
  }

  dispose() {
    this.disposed = true;
    document.removeEventListener("commodities-theme-change", this.themeListener);
    this.resizeObserver?.disconnect();
    this.destroyChart();
  }
}
