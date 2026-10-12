import {
  loadResearchMetadata,
  loadResearchHistory,
  sampleTrace,
  stackTraces,
} from "./research_history.js";
import { popoutButton } from "./chart_chrome.js";
import { outageBounds, outagePoints } from "./outage_history.js";

const COLORS = [
  "#2962FF",
  "#00897B",
  "#E53935",
  "#FB8C00",
  "#8E24AA",
  "#8191A8",
];
const AS_COLORS = {
  NR: COLORS[0],
  RD: COLORS[1],
  RU: COLORS[3],
  SR: COLORS[4],
  RMU: COLORS[2],
  RMD: COLORS[5],
};
let plotly;
function loadPlotly() {
  if (window.Plotly) return Promise.resolve(window.Plotly);
  if (!plotly)
    plotly = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/vendor/plotly-2.35.2.min.js";
      script.onload = () =>
        window.Plotly
          ? resolve(window.Plotly)
          : reject(new Error("ERR_DEPENDENCY_LOAD_FAILED: Plotly missing"));
      script.onerror = () => {
        plotly = null;
        reject(new Error("ERR_DEPENDENCY_LOAD_FAILED: Plotly missing"));
      };
      document.head.append(script);
    });
  return plotly;
}
function el(tag, cls, text) {
  const node = document.createElement(tag);
  node.className = cls || "";
  if (text) node.textContent = text;
  return node;
}
export function traceColor(dimensions, feed) {
  if (feed === "outages") return dimensions.ti_direction === "I" ? COLORS[0] : COLORS[1];
  if (feed === "ancillary") return AS_COLORS[dimensions.anc_type] || COLORS[5];
  if (feed === "transmission")
    return dimensions.measure === "USEAGE_MW"
      ? COLORS[0]
      : dimensions.measure === "RATING_ATC"
        ? COLORS[1]
        : COLORS[5];
  return dimensions.time_of_use === "ON"
    ? COLORS[0]
    : dimensions.xml_data_item === "LT_OFF_PRC"
      ? COLORS[4]
      : COLORS[1];
}
function utcEpoch(value) {
  const text = String(value);
  return (
    Date.parse(
      /(?:Z|[+-]\d{2}:?\d{2})$/.test(text)
        ? text
        : text.replace(" ", "T") + "Z",
    ) / 1000
  );
}
function traceLabel(trace, feed) {
  const d = trace.dimensions;
  if (feed === "outages") return `CURTAILED OTC · ${d.ti_direction === "I" ? "IMPORT" : d.ti_direction === "E" ? "EXPORT" : d.ti_direction} · ${d.record_status}`;
  if (feed === "ancillary") return d.anc_type;
  if (feed === "transmission")
    return [d.measure, d.market_run_id, d.ti_direction, d.ti_constraint_id]
      .filter(Boolean)
      .join(" · ");
  return [d.market_term, d.time_of_use, d.market_name, d.xml_data_item]
    .filter(Boolean)
    .join(" · ");
}

export class ResearchPanel {
  constructor(dataset, element, onError) {
    this.dataset = dataset;
    this.element = element;
    this.onError = onError;
    this.filters = {};
    this.hidden = new Set();
    this.range = "1M";
    this.generation = 0;
    this.disposed = false;
    this.settingsKey = `research-panel:${dataset.key}`;
    try {
      this.saved = JSON.parse(localStorage.getItem(this.settingsKey) || "{}");
    } catch {
      this.saved = {};
    }
  }
  async init() {
    this.element.className = "research-panel caiso-panel pane-container";
    this.element.dataset.feed = this.dataset.key;
    this.ribbon = el("div", "caiso-ribbon research-ribbon");
    this.search = el("input", "research-search");
    this.search.type = "search";
    this.search.placeholder = "Find node / path / region";
    this.search.setAttribute("aria-label", "Search node, path, or region");
    this.entitySelect = el("select", "caiso-node");
    this.entitySelect.setAttribute("aria-label", "Research entity");
    this.ribbon.append(
      this.search,
      this.entitySelect,
      popoutButton(this.element),
    );
    this.controls = el("div", "research-controls");
    this.subheader = el("div", "panel-subheader");
    this.ranges = el("div", "range-control");
    this.ranges.setAttribute("role", "group");
    this.ranges.setAttribute("aria-label", "Date range");
    for (const range of ["1D", "1W", "1M", "ALL"]) {
      const button = el("button", "", range);
      button.type = "button";
      button.dataset.range = range;
      button.onclick = () => {
        this.range = range;
        this.customBounds = null;
        this.customY = null;
        this.draw();
      };
      this.ranges.append(button);
    }
    this.reset = el("button", "panel-reset", "RESET");
    this.reset.type = "button";
    this.reset.onclick = () => {
      this.range = "1M";
      this.customBounds = null;
      this.customY = null;
      this.hidden.clear();
      this.filters = {};
      void this.selectEntity(this.defaultEntity);
    };
    this.legend = el("div", "chart-legend research-legend");
    this.eventNotice = el("span", "research-filter-label outage-notice");
    this.eventNotice.hidden = true;
    this.legend.setAttribute("role", "group");
    this.legend.setAttribute("aria-label", "Chart legend");
    this.subheader.append(this.ranges, this.reset, this.eventNotice, this.legend);
    this.viewport = el("div", "chart-viewport caiso-viewport pane-body");
    this.mount = el("div", "research-chart");
    this.mount.setAttribute("aria-label", `${this.dataset.title} plot`);
    this.mount.title = "Drag to zoom · Wheel to zoom · Double-click or RESET to restore";
    this.state = el("div", "caiso-empty", "LOADING_RESEARCH_HISTORY");
    this.state.setAttribute("role", "status");
    this.viewport.append(this.mount, this.state);
    this.element.replaceChildren(
      this.ribbon,
      this.controls,
      this.subheader,
      this.viewport,
    );
    this.search.oninput = () => this.populateEntities();
    this.entitySelect.onchange = () => {
      this.filters = {};
      this.hidden.clear();
      this.customBounds = null;
      this.customY = null;
      void this.selectEntity(this.entitySelect.value);
    };
    this.resizeObserver = new ResizeObserver(() => this.resizeChart());
    this.resizeObserver.observe(this.mount);
    try {
      [this.engine, this.meta] = await Promise.all([
        loadPlotly(),
        loadResearchMetadata(this.dataset),
      ]);
      if (this.disposed) return;
      this.defaultEntity = this.meta.entities.includes("AS_CAISO")
        ? "AS_CAISO"
        : this.meta.entities.includes("TH_SP15_GEN-APND")
          ? "TH_SP15_GEN-APND"
          : this.meta.entities[0];
      this.range = ["1D", "1W", "1M", "ALL"].includes(this.saved.range)
        ? this.saved.range
        : "1M";
      this.filters = this.saved.filters || {};
      this.hidden = new Set(this.saved.hidden || []);
      this.populateEntities();
      await this.selectEntity(
        this.meta.entities.includes(this.saved.entity)
          ? this.saved.entity
          : this.defaultEntity,
      );
    } catch (error) {
      this.showError(error);
    }
  }
  populateEntities() {
    const selected = this.entity || this.entitySelect.value;
    const values =
      this.meta?.entities.filter((value) =>
        value.toLowerCase().includes(this.search.value.toLowerCase()),
      ) || [];
    const displayed = values.slice(0, 200);
    if (selected && !displayed.includes(selected)) displayed.unshift(selected);
    this.entitySelect.replaceChildren(
      ...displayed.map((value) => {
        const option = el("option", "", value);
        option.value = value;
        return option;
      }),
    );
    if (displayed.includes(selected)) this.entitySelect.value = selected;
  }
  async selectEntity(entity) {
    const generation = ++this.generation;
    if (!entity) {
      this.showState("NO_RECORDS_FOUND_IN_DATA_LAKE");
      return;
    }
    this.entitySelect.disabled = true;
    try {
      const history = await loadResearchHistory(this.dataset, entity);
      if (this.disposed || generation !== this.generation) return;
      this.history = history;
      this.entity = entity;
      this.entitySelect.value = entity;
      this.configureControls();
      await this.draw();
      this.element.dataset.activeEntity = entity;
    } catch (error) {
      if (generation === this.generation) this.showError(error);
    } finally {
      if (generation === this.generation) this.entitySelect.disabled = false;
    }
  }
  configureControls() {
    this.controls.replaceChildren();
    const feed = this.meta.feed;
    const definitions =
      feed === "crr"
        ? [
            ["market_term", "toggle"],
            ["time_of_use", "toggle"],
          ]
        : feed === "transmission"
          ? [
              ["ti_direction", "select"],
              ["market_run_id", "toggle"],
              ["measure", "toggle"],
            ]
          : feed === "outages" ? [["ti_direction", "toggle"], ["record_status", "toggle"]] : [
              ["measure", "select"],
              ["anc_type", "toggle"],
            ];
    for (const [dimension, kind] of definitions) {
      const values = [
        ...new Set(
          this.history.series
            .map((trace) => trace.dimensions[dimension])
            .filter((value) => value != null),
        ),
      ].sort();
      let selected = this.filters[dimension]?.filter((value) =>
        values.includes(value),
      );
      if (!selected || (this.filters[dimension]?.length && !selected.length)) {
        if (kind === "select")
          selected = [
            values.includes("MINIMUM") ? "MINIMUM" : values[0],
          ].filter(Boolean);
        else if (dimension === "measure")
          selected = values.filter((value) =>
            ["USEAGE_MW", "RATING_ATC"].includes(value),
          );
        else if (dimension === "record_status") selected = values.includes("V") ? ["V"] : [...values];
        else if (dimension === "anc_type")
          selected = values.filter((value) =>
            ["NR", "RD", "RU", "SR"].includes(value),
          );
        else selected = [...values];
        if (!selected.length && values.length) selected = [values[0]];
      }
      this.filters[dimension] = selected;
      const group = el("div", "research-filter");
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", dimension);
      group.append(
        el("span", "research-filter-label", dimension.replaceAll("_", " ")),
      );
      if (kind === "select") {
        const select = el("select", "panel-control");
        select.setAttribute("aria-label", dimension);
        select.replaceChildren(
          ...values.map((value) => {
            const option = el("option", "", value);
            option.value = value;
            return option;
          }),
        );
        select.value = selected[0] || "";
        select.onchange = () => {
          this.filters[dimension] = [select.value];
          this.draw();
        };
        group.append(select);
      } else
        for (const value of values) {
          const button = el("button", "choice-toggle research-toggle");
          button.type = "button";
          const marker = el("span", "choice-marker");
          marker.setAttribute("aria-hidden", "true");
          button.append(marker, el("span", "", value));
          button.setAttribute("aria-label", `${dimension}: ${value}`);
          button.style.setProperty(
            "--trace-color",
            traceColor({ [dimension]: value }, feed),
          );
          button.dataset.dimension = dimension;
          button.dataset.value = value;
          button.onclick = () => {
            const chosen = new Set(this.filters[dimension]);
            chosen.has(value) ? chosen.delete(value) : chosen.add(value);
            this.filters[dimension] = [...chosen];
            this.draw();
          };
          group.append(button);
        }
      this.controls.append(group);
    }
  }
  bounds() {
    if (this.customBounds) return this.customBounds;
    if (this.meta.feed === "outages") return outageBounds(this.history.series, this.range);
    const traces = this.history.series.filter((trace) => trace.epochs.length);
    const min = Math.min(...traces.map((trace) => trace.epochs[0])),
      max = Math.max(...traces.map((trace) => trace.epochs.at(-1)));
    const days = { "1D": 1, "1W": 7, "1M": 30 }[this.range];
    return { min: days ? Math.max(min, max - days * 86400) : min, max };
  }
  async draw() {
    this.drawQueued = true;
    if (this.drawing || !this.history || !this.engine || this.disposed) return;
    this.drawing = true;
    try {
      while (this.drawQueued && !this.disposed) {
        this.drawQueued = false;
        await this.drawCurrent();
      }
    } catch (error) {
      this.showError(error);
    } finally {
      this.drawing = false;
    }
  }
  async drawCurrent() {
    for (const button of this.ranges.children)
      button.classList.toggle(
        "active",
        button.dataset.range === this.range && !this.customBounds,
      );
    for (const button of this.controls.querySelectorAll("[data-dimension]"))
      button.setAttribute(
        "aria-pressed",
        String(
          this.filters[button.dataset.dimension].includes(button.dataset.value),
        ),
      );
    const eligible = this.history.series.filter((trace) =>
      Object.entries(this.filters).every(([key, values]) =>
        values.includes(trace.dimensions[key]),
      ),
    );
    this.legend.replaceChildren(
      ...eligible.map((trace) => {
        const button = el("button", "", traceLabel(trace, this.meta.feed));
        button.type = "button";
        button.title = button.textContent;
        button.style.setProperty(
          "--trace-color",
          traceColor(trace.dimensions, this.meta.feed),
        );
        button.setAttribute(
          "aria-pressed",
          String(!this.hidden.has(trace.key)),
        );
        button.onclick = () => {
          this.hidden.has(trace.key)
            ? this.hidden.delete(trace.key)
            : this.hidden.add(trace.key);
          this.draw();
        };
        return button;
      }),
    );
    const visible = eligible.filter((trace) => !this.hidden.has(trace.key)),
      bounds = this.bounds();
    if (
      !this.history.series.length ||
      !visible.length ||
      !Number.isFinite(bounds.min)
    ) {
      this.showState("NO_RECORDS_FOUND_FOR_SELECTION");
      this.save();
      this.rendered();
      return;
    }
    if (!this.mount.clientWidth || !this.mount.clientHeight) {
      this.showState("WAITING_FOR_VIEWPORT");
      return;
    }
    const budget = Math.max(320, Math.min(1500, this.mount.clientWidth));
    const stacked =
      this.meta.feed === "ancillary"
        ? stackTraces(visible, bounds.min, bounds.max, budget)
        : null;
    const traces = visible.map((trace, index) => {
      if (this.meta.feed === "outages") {
        const points = outagePoints(trace, bounds.min, bounds.max, budget);
        const color = traceColor(trace.dimensions, this.meta.feed);
        return { type: "scattergl", name: traceLabel(trace, this.meta.feed),
          x: points.x, y: points.y, customdata: points.customdata,
          mode: "lines+markers", connectgaps: false,
          line: { color, width: 1.75 }, marker: { color, size: 4, symbol: "square" },
          hovertemplate: `${traceLabel(trace, this.meta.feed)}<br>Curtailed line rating: %{y:.2f} MW<br>START %{customdata[0]}<br>END %{customdata[1]}<br>EQUIPMENT %{customdata[2]}<br>%{customdata[3]}<br>UPDATED %{customdata[4]} UTC<extra></extra>`,
          meta: { events: points.count, displayed: points.displayed },
        };
      }
      const points = stacked?.[index],
        indices = points
          ? null
          : sampleTrace(trace, bounds.min, bounds.max, budget);
      const x = points
        ? points.epochs.map((epoch) => new Date(epoch * 1000).toISOString())
        : indices.map((i) => trace.timestamps[i]);
      const y = points
        ? points.y
        : indices.map((i) =>
            Number.isFinite(trace.values[i]) ? trace.values[i] : null,
          );
      const customdata = points
        ? points.values.map((value) => (Number.isFinite(value) ? value : null))
        : y;
      const color = traceColor(trace.dimensions, this.meta.feed);
      return {
        type: "scattergl",
        name: traceLabel(trace, this.meta.feed),
        x,
        y,
        customdata,
        mode: x.length === 1 ? "lines+markers" : "lines",
        marker: { color, size: 5, symbol: "square" },
        connectgaps: false,
        line: {
          color,
          width: 1.25,
          dash: trace.dimensions.measure === "RATING_ATC" ? "dash" : "solid",
        },
        ...(points
          ? {
              fill: index === 0 ? "tozeroy" : "tonexty",
              fillcolor: color + "66",
            }
          : {}),
        hovertemplate: `%{x|%Y-%m-%d %H:%M} UTC<br>${traceLabel(trace, this.meta.feed).replaceAll("<", "&lt;")}<br>%{customdata:.2f} ${this.meta.unit}<extra></extra>`,
      };
    });
    if (!traces.some((trace) => trace.x.length && trace.y.some(Number.isFinite))) {
      this.showState("NO_RECORDS_FOUND_FOR_DATE_RANGE");
      this.rendered();
      return;
    }
    this.state.hidden = true;
    this.mount.style.visibility = "";
    const span = bounds.max === bounds.min ? 3600 : 0;
    const axis = {
      gridcolor: "#242733",
      griddash: "dash",
      gridwidth: 1,
      color: "#787B86",
      tickfont: { family: "Consolas, monospace", size: 10 },
      showspikes: true,
      spikethickness: 1,
      spikedash: "dash",
      spikecolor: "#434651",
      spikemode: "across",
      spikesnap: "cursor",
      zeroline: false,
    };
    const layout = {
      paper_bgcolor: "#131722",
      plot_bgcolor: "#131722",
      font: { family: "Consolas, monospace", size: 10, color: "#D1D4DC" },
      width: this.mount.clientWidth,
      height: this.mount.clientHeight,
      autosize: false,
      margin: { l: 10, r: 65, t: 8, b: 32 },
      showlegend: false,
      hovermode: this.meta.feed === "outages" ? "closest" : "x unified",
      dragmode: "zoom",
      hoverlabel: {
        bgcolor: "#1E222D",
        bordercolor: "#434651",
        font: { family: "Consolas, monospace", size: 10 },
      },
      xaxis: {
        ...axis,
        type: "date",
        tickformat: "%m-%d %H:%M",
        range: [
          new Date((bounds.min - span) * 1000).toISOString(),
          new Date((bounds.max + span) * 1000).toISOString(),
        ],
      },
      yaxis: {
        ...axis,
        side: "right",
        showline: true,
        linecolor: "#2A2E39",
        linewidth: 1,
        tickformat: ".2f",
        rangemode: stacked ? "tozero" : "normal",
        ...(this.customY ? { range: this.customY, autorange: false } : {}),
      },
      shapes: [
        {
          type: "line",
          xref: "paper",
          x0: 0,
          x1: 1,
          yref: "y",
          y0: 0,
          y1: 0,
          line: { color: "#363A45", width: 1, dash: "dot" },
          layer: "below",
        },
      ],
    };
    // Plotly's GL fills use precomputed cumulative boundaries; tooltip values
    // remain each service's original MW, and missing requirements remain gaps.
    await this.engine.react(this.mount, traces, layout, {
      displayModeBar: false,
      scrollZoom: true,
      responsive: false,
      doubleClick: "reset",
    });
    if (this.disposed) {
      this.engine.purge(this.mount);
      return;
    }
    if (!this.zoomListener) {
      this.zoomListener = (event) => {
        if (this.drawing) return;
        const left = event["xaxis.range[0]"],
          right = event["xaxis.range[1]"];
        const bottom = event["yaxis.range[0]"], top = event["yaxis.range[1]"];
        if (bottom !== undefined && top !== undefined) this.customY = [bottom, top];
        if (event["yaxis.autorange"]) this.customY = null;
        if (left && right) {
          this.customBounds = { min: utcEpoch(left), max: utcEpoch(right) };
          this.draw();
        } else if (event["xaxis.autorange"]) {
          this.customBounds = null;
          this.range = "ALL";
          this.customY = null;
          this.draw();
        } else if (bottom !== undefined || event["yaxis.autorange"]) {
          this.draw();
        }
      };
      this.mount.on("plotly_relayout", this.zoomListener);
    }
    this.element.dataset.traceCount = String(traces.length);
    this.element.dataset.displayedEvents = String(traces.reduce((sum, trace) => sum + (trace.meta?.displayed || 0), 0));
    this.element.dataset.matchingEvents = String(traces.reduce((sum, trace) => sum + (trace.meta?.events || 0), 0));
    this.eventNotice.hidden = this.meta.feed !== "outages";
    this.eventNotice.textContent = traces.some(trace => trace.meta?.events > trace.meta?.displayed)
      ? "EVENT DISPLAY LIMIT · NARROW RANGE" : "CURTAILED LINE RATING · MW";
    this.save();
    this.rendered();
    this.resizeChart();
  }
  rendered() {
    this.element.dispatchEvent(
      new CustomEvent("chart-view-updated", {
        bubbles: true,
        detail: { feed: this.dataset.key },
      }),
    );
  }
  save() {
    try {
      localStorage.setItem(
        this.settingsKey,
        JSON.stringify({
          entity: this.entity,
          range: this.range,
          filters: this.filters,
          hidden: [...this.hidden],
        }),
      );
    } catch {}
  }
  showState(message) {
    this.state.textContent = message;
    this.state.hidden = false;
    this.mount.style.visibility = "hidden";
  }
  showError(error) {
    if (this.disposed) return;
    console.error("RESEARCH_DATA_ERROR", error);
    this.showState(`DATA_FETCH_ERROR: ${error.message}`);
    this.onError?.(error);
  }
  resizeChart() {
    if (this.disposed || this.resizeFrame) return;
    this.resizeFrame = requestAnimationFrame(() => {
      this.resizeFrame = null;
      if (
        this.disposed ||
        !this.mount.isConnected ||
        !this.mount.clientWidth ||
        !this.mount.clientHeight ||
        this.mount.style.visibility === "hidden"
      )
        return;
      if (this.drawing) {
        this.resizeChart();
        return;
      }
      if (this.mount._fullLayout) {
        const width = this.mount.clientWidth,
          height = this.mount.clientHeight;
        if (
          width !== this.mount._fullLayout.width ||
          height !== this.mount._fullLayout.height
        )
          this.engine.relayout(this.mount, { width, height }).catch((error) => {
            if (
              !this.disposed &&
              this.mount.isConnected &&
              this.mount.clientWidth
            )
              this.showError(error);
          });
      } else if (this.history) this.draw();
    });
  }
  async reload(dataset) {
    this.dataset = dataset;
    try {
      this.meta = await loadResearchMetadata(dataset);
      if (!this.disposed) {
        this.populateEntities();
        await this.selectEntity(
          this.meta.entities.includes(this.entity)
            ? this.entity
            : this.meta.entities[0],
        );
      }
    } catch (error) {
      this.showError(error);
    }
  }
  dispose() {
    this.disposed = true;
    ++this.generation;
    this.resizeObserver?.disconnect();
    cancelAnimationFrame(this.resizeFrame);
    if (this.mount?._fullLayout) this.engine?.purge(this.mount);
  }
}
