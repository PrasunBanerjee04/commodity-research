import { ChartPanel } from "./chart_panel.js";
import { CaisoPanel } from "./caiso_panel.js";

export function canonicalDatasetPath(key) {
  const parts = String(key).replaceAll("\\", "/").split("/").filter(part => part && part !== ".");
  if (!parts.length || parts.includes("..") || String(key).startsWith("/")) throw new Error("Invalid dataset path.");
  return parts.join("/");
}

const LAYOUT_STORAGE_KEY = "commodity-dock-layout";

function readLayout(key) {
  try {
    return localStorage.getItem(key);
  } catch (error) {
    console.warn("Unable to read the saved workspace layout.", error);
    return null;
  }
}

export class DockManager {
  constructor({ mount, emptyState, onError, layoutKey = LAYOUT_STORAGE_KEY, datasets = [] }) {
    this.mount = mount;
    this.emptyState = emptyState;
    this.onError = onError;
    this.layoutKey = layoutKey;
    this.datasets = datasets;
    this.openPanels = new Map();
    this.chartPanels = new Map();
    this.rebuilding = false;
    if (typeof window.dockview?.DockviewComponent !== "function") {
      throw new Error("ERR_DEPENDENCY_LOAD_FAILED: Dockview missing");
    }
    this.view = new window.dockview.DockviewComponent(mount, {
      theme: window.dockview.themeDark,
      createComponent: ({ id }) => {
        let chartPanel;
        const element = document.createElement("div");
        return {
          element,
          init: ({ params }) => {
            const key = canonicalDatasetPath(params.dataset.key);
            const dataset = { ...params.dataset, ...this.datasets.find(item => item.key === key), key };
            element.addEventListener("panel-popout-request", () => {
              const panel = this.openPanels.get(key);
              if (panel) void this.view.addPopoutGroup(panel).then(opened => {
                if (!opened) this.onError(new Error("Popout window could not be opened. Allow popups for this workstation."));
              }).catch(this.onError);
            });
            const Controller = dataset.transport === "node-history" ? CaisoPanel : ChartPanel;
            chartPanel = new Controller(dataset, element, this.onError, params.settingsKey || id);
            if (!this.chartPanels.has(dataset.key)) this.chartPanels.set(dataset.key, chartPanel);
            return chartPanel.init();
          },
          layout: () => chartPanel?.resizeChart(),
          dispose: () => {
            if (chartPanel) {
              if (this.chartPanels.get(chartPanel.dataset.key) === chartPanel) this.chartPanels.delete(chartPanel.dataset.key);
              chartPanel.dispose();
            }
          },
        };
      },
    });
    this.view.onDidMutateLayout(() => {
      this.updateEmptyState();
      this.saveLayout();
    });
    this.view.onDidLayoutChange(() => this.saveLayout());
    this.view.onDidAddPanel((panel) => {
      const key = canonicalDatasetPath(panel.params.dataset.key);
      if (!this.openPanels.has(key)) this.openPanels.set(key, panel);
      this.updateEmptyState();
    });
    this.view.onDidRemovePanel((panel) => {
      const key = canonicalDatasetPath(panel.params.dataset.key);
      if (this.openPanels.get(key) === panel) this.openPanels.delete(key);
      this.updateEmptyState();
    });
    this.view.onDidActivePanelChange(() => this.updateEmptyState());
    this.restore();
    this.updateEmptyState();
  }

  setDatasets(datasets, refresh = false) {
    this.datasets = datasets;
    for (const [key, panel] of this.chartPanels) {
      const dataset = datasets.find(item => item.key === key);
      if (dataset && panel.metadata && (refresh || dataset.revision !== panel.dataset.revision)) void panel.reload(dataset).catch(error => panel.showError(error));
      else if (!dataset) panel.showError(new Error("Dataset is no longer available. Rescan the lake."));
    }
  }

  openPanel(dataset, position) {
    const key = canonicalDatasetPath(dataset.key);
    const existing = this.openPanels.get(key);
    if (existing) {
      existing.api.setActive();
      existing.focus();
      return existing;
    }
    if (!position && this.view.activePanel) {
      position = { referencePanel: this.view.activePanel.id, direction: "within" };
    }
    const panel = this.view.addPanel({
      id: `dataset:${encodeURIComponent(key)}`,
      title: (dataset.title || dataset.name || key).replace(" / ", ": "),
      component: "market",
      params: { dataset: { ...dataset, key }, settingsKey: key },
      position,
    });
    this.openPanels.set(key, panel);
    return panel;
  }

  restore() {
    const serialized = readLayout(this.layoutKey);
    if (!serialized) return false;
    try {
      this.rebuilding = true;
      this.view.fromJSON(JSON.parse(serialized));
      // Repair layouts saved before singleton enforcement without losing the first panel.
      for (const panel of [...this.view.panels]) {
        const key = canonicalDatasetPath(panel.params.dataset.key);
        if (this.openPanels.get(key) !== panel) this.view.removePanel(panel);
      }
      this.rebuilding = false;
      this.saveLayout();
      return this.view.totalPanels > 0;
    } catch (error) {
      this.rebuilding = false;
      this.onError(new Error(`Saved workspace layout could not be restored: ${error.message}`));
      return false;
    }
  }

  saveLayout() {
    if (this.rebuilding) return;
    try {
      if (!this.view.totalPanels) {
        localStorage.removeItem(this.layoutKey);
        return;
      }
      // Startup overviews are transient data, not persistent layout settings.
      localStorage.setItem(this.layoutKey, JSON.stringify(this.view.toJSON(), (key,value) => key === "preview" ? undefined : value));
    } catch (error) {
      this.onError(new Error(`Workspace layout could not be saved: ${error.message}`));
    }
  }

  setTheme(theme) {
    this.view.updateOptions({
      theme: theme === "light" ? window.dockview.themeLight : window.dockview.themeDark,
    });
    document.body.classList.toggle("light", theme === "light");
    document.dispatchEvent(new Event("commodities-theme-change"));
    this.saveLayout();
  }

  arrange(count) {
    if (!this.datasets.length) return;
    const activeDataset = this.view.activePanel?.params?.dataset;
    const candidates = [...this.openPanels.values()].map(panel => panel.params.dataset);
    for (const dataset of this.datasets) {
      if (candidates.length >= count) break;
      if (!candidates.some(item => item.key === dataset.key)) candidates.push(dataset);
    }
    if (activeDataset) candidates.sort((a, b) => (a.key === activeDataset.key ? -1 : b.key === activeDataset.key ? 1 : 0));
    // Keep the institutional two-pane preset ordered DAM left, RTM right.
    if (count === 2 && this.datasets.some(item => item.transport === "node-history")) {
      const prices = ["DAM", "RTM"].map(market => this.datasets.find(item => item.market === market));
      if (prices.every(Boolean)) {
        for (const price of prices) if (!candidates.some(item => item.key === price.key)) candidates.push(price);
        candidates.sort((a,b) => {
          const rank = item => item.key === prices[0].key ? -2 : item.key === prices[1].key ? -1 : 0;
          return rank(a)-rank(b);
        });
      }
    }
    const datasets = [...new Map(candidates.map(dataset => [canonicalDatasetPath(dataset.key), dataset])).values()];
    this.rebuilding = true;
    const panels = datasets.map(dataset => this.openPanel(dataset));
    // Reparent the existing panel/canvas; rebuilding the dock remounted every chart.
    for (const panel of panels.slice(1)) {
      panel.api.moveTo({ group: panels[0].api.group, position: "center", skipSetActive: true });
    }
    for (let index = 1; index < Math.min(count, panels.length); index++) {
      const reference = index === 3 ? panels[1] : panels[0];
      panelPosition(panels[index], reference, index === 1 ? "right" : "bottom");
    }
    if (activeDataset) this.openPanel(activeDataset);
    this.rebuilding = false;
    this.updateEmptyState();
    this.saveLayout();
  }

  updateEmptyState() {
    const hasPanels = this.view.totalPanels > 0;
    this.emptyState.hidden = hasPanels;
    const active = this.view.activePanel;
    this.mount.dataset.activePanel = active?.id || "";
    for (const button of document.querySelectorAll("[data-layout]")) {
      const expectedCount = Number(button.dataset.layout);
      button.classList.toggle("active", hasPanels && this.view.totalPanels === expectedCount);
    }
  }
}

function panelPosition(panel, reference, position) {
  panel.api.moveTo({ group: reference.api.group, position, skipSetActive: true });
  panel.api.setActive();
}
