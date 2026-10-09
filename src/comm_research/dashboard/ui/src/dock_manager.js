import { ChartPanel } from "./chart_panel.js";

export function canonicalDatasetPath(key) {
  const parts = String(key).replaceAll("\\", "/").split("/").filter(part => part && part !== ".");
  if (!parts.length || parts.includes("..") || String(key).startsWith("/")) throw new Error("Invalid dataset path.");
  return parts.join("/");
}

const LAYOUT_STORAGE_KEY = "commodity-dock-layout";

function readLayout() {
  try {
    return localStorage.getItem(LAYOUT_STORAGE_KEY);
  } catch (error) {
    console.warn("Unable to read the saved workspace layout.", error);
    return null;
  }
}

export class DockManager {
  constructor({ mount, emptyState, onError }) {
    this.mount = mount;
    this.emptyState = emptyState;
    this.onError = onError;
    this.datasets = [];
    this.openPanels = new Map();
    this.rebuilding = false;
    this.view = new window.dockview.DockviewComponent(mount, {
      theme: window.dockview.themeDark,
      createComponent: ({ id }) => {
        let chartPanel;
        const element = document.createElement("div");
        return {
          element,
          init: ({ params }) => {
            const dataset = { ...params.dataset, key: canonicalDatasetPath(params.dataset.key) };
            chartPanel = new ChartPanel(dataset, element, this.onError, params.settingsKey || id);
            return chartPanel.init();
          },
          dispose: () => chartPanel?.dispose(),
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

  setDatasets(datasets) {
    this.datasets = datasets;
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
    const serialized = readLayout();
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
        localStorage.removeItem(LAYOUT_STORAGE_KEY);
        return;
      }
      localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(this.view.toJSON()));
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
    const datasets = [...new Map(candidates.map(dataset => [canonicalDatasetPath(dataset.key), dataset])).values()];
    this.rebuilding = true;
    this.view.clear();
    this.openPanels.clear();
    const panels = [];
    for (let index = 0; index < datasets.length; index += 1) {
      let position;
      if (index === 1 && count >= 2) position = { referencePanel: panels[0].id, direction: "right" };
      else if (index === 2 && count >= 4) position = { referencePanel: panels[0].id, direction: "below" };
      else if (index === 3 && count >= 4) position = { referencePanel: panels[1].id, direction: "below" };
      else if (index > 0) position = { referencePanel: panels[0].id, direction: "within" };
      panels.push(this.openPanel(datasets[index], position));
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
