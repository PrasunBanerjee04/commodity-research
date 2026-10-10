import {
  COMPONENTS,
  loadNodes,
  loadNodeHistory,
  lowerBound,
  displayData,
  prepareHistory,
} from "./node_history.js";
import { emptyPaths, drawBoundedSeries } from "./canvas_paths.js";
import { LineRenderer } from "./line_renderer.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className || "";
  if (text) node.textContent = text;
  return node;
}
const utcLabel = (epoch) =>
  new Date(epoch * 1000).toISOString().slice(0, 16).replace("T", " ");

export class CaisoPanel {
  constructor(dataset, element, onError, settingsKey) {
    this.dataset = dataset;
    this.element = element;
    this.onError = onError;
    this.settingsKey = `caiso-history:${settingsKey}`;
    this.visible = COMPONENTS.map(() => true);
    this.generation = 0;
    this.metadata = {};
    try {
      this.settings = JSON.parse(
        localStorage.getItem(this.settingsKey) || "{}",
      );
    } catch {
      this.settings = {};
    }
    if (
      Array.isArray(this.settings.visible) &&
      this.settings.visible.length === 4
    )
      this.visible = this.settings.visible.map(Boolean);
    this.range = this.settings.range || "ALL";
    this.disposed = false;
  }

  async init() {
    this.element.className = "caiso-panel pane-container";
    this.element.dataset.feed = this.dataset.key;
    this.ribbon = el("div", "caiso-ribbon");
    this.nodeSelect = el("select", "caiso-node");
    this.nodeSelect.setAttribute("aria-label", "PNode");
    this.nodeSelect.addEventListener("change", () => {
      void this.selectNode(this.nodeSelect.value);
    });
    this.ribbon.append(this.nodeSelect);
    this.componentButtons = COMPONENTS.map((component, index) => {
      const button = el("button", "caiso-component", component.label);
      button.type = "button";
      button.setAttribute("role", "checkbox");
      button.style.setProperty("--trace-color", component.color);
      button.addEventListener("click", () => this.toggle(index));
      this.ribbon.append(button);
      return button;
    });
    this.ranges = el("div", "caiso-ranges");
    for (const range of ["1D", "5D", "1M", "YTD", "ALL"]) {
      const button = el("button", "", range);
      button.type = "button";
      button.dataset.range = range;
      button.addEventListener("click", () => this.selectRange(range));
      this.ranges.append(button);
    }
    this.viewport = el("div", "chart-viewport caiso-viewport pane-body");
    this.mount = el("div", "caiso-chart");
    this.empty = el("div", "caiso-empty", "LOADING_NODE_HISTORY");
    this.legend = el("div", "caiso-legend");
    this.tooltip = el("div", "caiso-tooltip");
    this.tooltip.hidden = true;
    this.resolution = el("div", "caiso-resolution");
    this.viewport.append(
      this.mount,
      this.empty,
      this.legend,
      this.resolution,
      this.tooltip,
    );
    this.element.replaceChildren(this.ribbon, this.ranges, this.viewport);
    this.updateButtons();
    this.resizeObserver = new ResizeObserver(() => this.resizeChart());
    this.resizeObserver.observe(this.mount);
    this.themeListener = () => this.chart?.redraw();
    document.addEventListener("commodities-theme-change", this.themeListener);
    try {
      if (typeof window.uPlot !== "function")
        throw new Error("ERR_DEPENDENCY_LOAD_FAILED: uPlot missing");
      const preview = this.dataset.preview;
      if (
        preview &&
        (!this.settings.node || this.settings.node === preview.node)
      ) {
        this.history = {
          ...prepareHistory(preview),
          preview: true,
          sourceCount: preview.sourceCount,
        };
        const option = el("option", "", preview.node);
        option.value = preview.node;
        this.nodeSelect.append(option);
        this.empty.hidden = true;
        this.createChart();
        this.selectRange(this.range);
      }
      const { nodes } = await loadNodes(this.dataset);
      if (this.disposed) return;
      this.nodeSelect.replaceChildren(
        ...nodes.map((node) => {
          const option = el("option", "", node);
          option.value = node;
          return option;
        }),
      );
      if (!nodes.length) {
        this.showState("NO_RECORDS_FOUND_IN_DATA_LAKE");
        return;
      }
      const initial = nodes.includes(this.settings.node)
        ? this.settings.node
        : nodes.includes("TH_NP15_GEN-APND")
          ? "TH_NP15_GEN-APND"
          : nodes[0];
      await this.selectNode(initial);
    } catch (error) {
      this.showError(error);
    }
  }

  async selectNode(node) {
    const generation = ++this.generation;
    this.nodeSelect.disabled = true;
    try {
      // A resolved history promise switches synchronously after one microtask.
      const history = await loadNodeHistory(this.dataset, node);
      if (this.disposed || generation !== this.generation) return;
      this.failed = false;
      const previous = this.history,
        zoom = this.chart?.scales.x;
      const keepZoom =
        this.range === "CUSTOM" && previous?.node === history.node;
      this.history = history;
      this.nodeSelect.value = history.node;
      if (!history.epochs.length) {
        this.showState("NO_RECORDS_FOUND_IN_DATA_LAKE");
        return;
      }
      this.empty.hidden = true;
      this.mount.style.visibility = "";
      this.tooltip.hidden = true;
      if (!this.chart) this.createChart();
      if (!this.renderer) this.renderer = new LineRenderer({ thick: true });
      if (keepZoom) this.renderRange(zoom.min, zoom.max);
      else this.selectRange(this.range);
      this.element.dataset.activeNode = history.node;
      this.save();
    } catch (error) {
      if (generation === this.generation && !this.disposed)
        this.showError(error);
    } finally {
      if (generation === this.generation && !this.disposed)
        this.nodeSelect.disabled = false;
    }
  }

  createChart() {
    const bounds = this.bounds("ALL");
    if (!this.history.preview)
      this.renderer = new LineRenderer({ thick: true });
    const series = COMPONENTS.map((component, index) => ({
      label: component.label,
      stroke: component.color,
      width: component.width,
      dash: component.dash || [],
      show: this.visible[index],
      spanGaps: false,
      points: { show: false },
      paths: emptyPaths,
    }));
    this.chart = new window.uPlot(
      {
        width: Math.max(1, this.mount.clientWidth),
        height: Math.max(250, this.mount.clientHeight),
        padding: [26, 8, 0, 8],
        legend: { show: false },
        tzDate: (timestamp) =>
          window.uPlot.tzDate(new Date(timestamp * 1000), "Etc/UTC"),
        scales: {
          x: { time: true, auto: false, min: bounds.min, max: bounds.max },
        },
        series: [{}, ...series],
        axes: [
          {
            stroke: "#787b86",
            font: "10px Consolas, monospace",
            space: 90,
            grid: { stroke: "#242733", width: 1, dash: [3, 3] },
            ticks: { stroke: "#242733" },
            values: (_plot, ticks) =>
              ticks.map((epoch) => utcLabel(epoch).slice(5)),
          },
          {
            side: 1,
            size: 60,
            stroke: "#787b86",
            font: "10px Consolas, monospace",
            grid: { stroke: "#242733", width: 1, dash: [3, 3] },
            ticks: { stroke: "#242733" },
            values: (_plot, ticks) => ticks.map((value) => value.toFixed(2)),
          },
        ],
        cursor: {
          x: true,
          y: true,
          drag: { x: true, y: false, setScale: true },
        },
        hooks: {
          draw: [
            (plot) => {
              if (this.renderer) this.renderer.draw(plot);
              else
                for (let index = 1; index < plot.series.length; index++)
                  if (plot.series[index].show) drawBoundedSeries(plot, index);
              this.element.dispatchEvent(
                new CustomEvent("chart-view-updated", {
                  bubbles: true,
                  detail: {
                    feed: this.dataset.key,
                    min: plot.scales.x.min,
                    max: plot.scales.x.max,
                    points: plot.data[0].length,
                  },
                }),
              );
            },
          ],
          setScale: [
            (plot, key) => {
              if (
                key !== "x" ||
                this.updating ||
                this.failed ||
                !this.history ||
                !this.chart
              )
                return;
              this.range = "CUSTOM";
              this.updateButtons();
              this.renderRange(plot.scales.x.min, plot.scales.x.max);
              this.save();
            },
          ],
          setCursor: [(plot) => this.showTooltip(plot)],
          destroy: [
            () => {
              this.renderer?.dispose();
              this.renderer = null;
            },
          ],
        },
      },
      displayData(this.history, bounds.min, bounds.max),
      this.mount,
    );
    this.chart.over.addEventListener("mouseleave", () => {
      this.tooltip.hidden = true;
    });
    // Wheel zoom and Shift+drag pan are local; ordinary drag remains zoom-box.
    this.wheelListener = (event) => {
      event.preventDefault();
      const scale = this.chart.scales.x,
        center = this.chart.posToVal(event.offsetX, "x");
      const factor = Math.exp(
        Math.max(-100, Math.min(100, event.deltaY)) * 0.004,
      );
      this.setLocalRange(
        center + (scale.min - center) * factor,
        center + (scale.max - center) * factor,
      );
    };
    this.chart.over.addEventListener("wheel", this.wheelListener, {
      passive: false,
    });
    this.panStart = (event) => {
      if (!event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const scale = this.chart.scales.x;
      this.pan = { x: event.clientX, min: scale.min, max: scale.max };
    };
    this.panMove = (event) => {
      if (!this.pan || this.disposed) return;
      const shift =
        ((event.clientX - this.pan.x) / this.chart.bbox.width) *
        (this.pan.max - this.pan.min) *
        devicePixelRatio;
      this.setLocalRange(this.pan.min - shift, this.pan.max - shift);
    };
    this.panEnd = () => {
      this.pan = null;
    };
    this.chart.over.addEventListener("mousedown", this.panStart, true);
    window.addEventListener("mousemove", this.panMove);
    window.addEventListener("mouseup", this.panEnd);
    this.legend.replaceChildren(
      ...COMPONENTS.map((component, index) => {
        const button = el("button", "", component.label);
        button.type = "button";
        button.style.setProperty("--trace-color", component.color);
        button.addEventListener("click", () => this.toggle(index));
        return button;
      }),
    );
    this.updateButtons();
  }

  bounds(range) {
    const epochs = this.history.epochs,
      first = epochs[0],
      last = epochs.at(-1);
    let min = first;
    if (range === "1D") min = last - 86400;
    if (range === "5D") min = last - 5 * 86400;
    if (range === "1M") min = last - 30 * 86400;
    if (range === "YTD")
      min = Date.UTC(new Date(last * 1000).getUTCFullYear(), 0, 1) / 1000;
    return { min: Math.max(first, min), max: last === first ? last + 1 : last };
  }

  selectRange(range) {
    if (!this.history?.epochs.length) return;
    this.range = range === "CUSTOM" ? "ALL" : range;
    const bounds = this.bounds(this.range);
    this.renderRange(bounds.min, bounds.max);
    this.updateButtons();
    this.save();
  }

  setLocalRange(min, max) {
    const history = this.history,
      first = history.epochs[0],
      last = history.epochs.at(-1);
    if (max - min >= last - first) {
      min = first;
      max = Math.max(last, first + 1);
    } else {
      const span = max - min;
      if (min < first) {
        min = first;
        max = first + span;
      }
      if (max > last) {
        max = last;
        min = last - span;
      }
    }
    if (max <= min || max - min < 1) return;
    this.range = "CUSTOM";
    this.renderRange(min, max);
    this.updateButtons();
  }

  renderRange(min, max) {
    if (!this.chart || this.updating || this.failed) return;
    // Match display density to the actual pane width. Exact tick prices remain
    // in the node cache; drawing multiple vertices per pixel wastes frames.
    const budget = Math.min(
      1500,
      Math.max(320, Math.floor(this.chart.bbox.width / devicePixelRatio)),
    );
    const data = displayData(this.history, min, max, budget);
    this.updating = true;
    try {
      this.empty.hidden = data[0].length > 0;
      this.empty.textContent = "NO_RECORDS_FOUND_FOR_DATE_RANGE";
      this.resolution.textContent = this.history.preview
        ? `${this.history.sourceCount.toLocaleString()} TIMESTAMPS · CACHING EXACT HISTORY · UTC`
        : `${this.history.epochs.length.toLocaleString()} EXACT TIMESTAMPS · ${data[0].length.toLocaleString()} DISPLAY POINTS · UTC`;
      this.chart.batch(() => {
        this.chart.setData(data, false);
        this.chart.setScale("x", { min, max });
      });
    } finally {
      this.updating = false;
    }
  }

  toggle(index) {
    this.visible[index] = !this.visible[index];
    this.chart?.setSeries(index + 1, { show: this.visible[index] });
    this.updateButtons();
    this.tooltip.hidden = true;
    this.save();
  }

  updateButtons() {
    this.componentButtons?.forEach((button, index) =>
      button.setAttribute("aria-checked", String(this.visible[index])),
    );
    [...(this.legend?.children || [])].forEach((button, index) =>
      button.setAttribute("aria-pressed", String(this.visible[index])),
    );
    for (const button of this.ranges?.children || [])
      button.classList.toggle("active", button.dataset.range === this.range);
  }

  showTooltip(plot) {
    if (
      !this.history ||
      this.failed ||
      plot.cursor.left < 0 ||
      plot.cursor.top < 0
    ) {
      this.tooltip.hidden = true;
      return;
    }
    const target = plot.posToVal(plot.cursor.left, "x");
    let index = lowerBound(this.history.epochs, target);
    if (index >= this.history.epochs.length)
      index = this.history.epochs.length - 1;
    if (
      index > 0 &&
      target - this.history.epochs[index - 1] <
        this.history.epochs[index] - target
    )
      index--;
    const title = el(
      "div",
      "caiso-tooltip-time",
      `${utcLabel(this.history.epochs[index])} UTC${this.history.preview ? " · OVERVIEW" : ""}`,
    );
    const rows = COMPONENTS.flatMap((component, column) => {
      if (!this.visible[column]) return [];
      const row = el("div", "caiso-tooltip-row");
      const label = el("span", "", component.label);
      label.style.setProperty("--trace-color", component.color);
      const value = this.history.values[column][index];
      row.append(
        label,
        el(
          "strong",
          "",
          Number.isFinite(value)
            ? `$${value.toFixed(2)}${column === 0 ? "/MWh" : ""}`
            : "—",
        ),
      );
      return [row];
    });
    this.tooltip.replaceChildren(title, ...rows);
    this.tooltip.hidden = false;
  }

  resizeChart() {
    if (this.disposed || !this.chart || this.resizeFrame) return;
    this.resizeFrame = requestAnimationFrame(() => {
      this.resizeFrame = null;
      if (!this.chart || this.disposed) return;
      const width = this.mount.clientWidth,
        height = this.mount.clientHeight;
      if (
        width > 0 &&
        height > 0 &&
        (width !== this.chart.width || height !== this.chart.height)
      )
        this.chart.setSize({ width, height: Math.max(250, height) });
    });
  }

  showState(message) {
    this.failed = true;
    this.empty.textContent = message;
    this.empty.hidden = false;
    this.mount.style.visibility = "hidden";
    this.tooltip.hidden = true;
  }
  showError(error) {
    if (this.disposed) return;
    console.error("DATA_FETCH_ERROR", error);
    this.showState(
      error.message.startsWith("ERR_DEPENDENCY")
        ? error.message
        : `DATA_FETCH_ERROR: ${error.message}`,
    );
    this.onError?.(error);
  }
  save() {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      try {
        localStorage.setItem(
          this.settingsKey,
          JSON.stringify({
            node: this.history?.node,
            range: this.range,
            visible: this.visible,
          }),
        );
      } catch (error) {
        console.warn("Settings save failed", error);
      }
    }, 150);
  }
  async reload(dataset) {
    this.dataset = dataset;
    const previous = this.history?.node;
    const { nodes } = await loadNodes(dataset);
    if (this.disposed) return;
    this.nodeSelect.replaceChildren(
      ...nodes.map((node) => {
        const option = el("option", "", node);
        option.value = node;
        return option;
      }),
    );
    if (!nodes.length) {
      this.showState("NO_RECORDS_FOUND_IN_DATA_LAKE");
      return;
    }
    await this.selectNode(nodes.includes(previous) ? previous : nodes[0]);
  }
  dispose() {
    this.disposed = true;
    ++this.generation;
    this.resizeObserver?.disconnect();
    clearTimeout(this.saveTimer);
    cancelAnimationFrame(this.resizeFrame);
    document.removeEventListener(
      "commodities-theme-change",
      this.themeListener,
    );
    window.removeEventListener("mousemove", this.panMove);
    window.removeEventListener("mouseup", this.panEnd);
    this.chart?.destroy();
    this.chart = null;
  }
}
