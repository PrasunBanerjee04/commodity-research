import { ChartPanel } from "./chart_panel.js";

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
    this.sequence = 0;
    this.view = new window.dockview.DockviewComponent(mount, {
      theme: window.dockview.themeDark,
      createComponent: ({ id }) => {
        let chartPanel;
        const element = document.createElement("div");
        return {
          element,
          init: ({ params }) => {
            chartPanel = new ChartPanel(params.dataset, element, this.onError, params.settingsKey || id);
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
    this.view.onDidAddPanel(() => this.updateEmptyState());
    this.view.onDidRemovePanel(() => this.updateEmptyState());
    this.view.onDidActivePanelChange(() => this.updateEmptyState());
    this.restore();
    this.updateEmptyState();
  }

  setDatasets(datasets) {
    this.datasets = datasets;
  }

  nextPanelId(dataset) {
    this.sequence += 1;
    return `${dataset.key.replace(/[^a-zA-Z0-9_-]/g, "-")}-${Date.now()}-${this.sequence}`;
  }

  openPanel(dataset, position) {
    if (!position && this.view.activePanel) {
      position = { referencePanel: this.view.activePanel.id, direction: "within" };
    }
    const id = this.nextPanelId(dataset);
    return this.view.addPanel({
      id,
      title: (dataset.title || dataset.name || dataset.key).replace(" / ", ": "),
      component: "market",
      params: { dataset, settingsKey: id },
      position,
    });
  }

  restore() {
    const serialized = readLayout();
    if (!serialized) return false;
    try {
      this.view.fromJSON(JSON.parse(serialized));
      return this.view.totalPanels > 0;
    } catch (error) {
      this.onError(new Error(`Saved workspace layout could not be restored: ${error.message}`));
      return false;
    }
  }

  saveLayout() {
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
    const candidates = activeDataset
      ? [activeDataset, ...this.datasets.filter((dataset) => dataset.key !== activeDataset.key)]
      : [...this.datasets];
    while (candidates.length < count) candidates.push(candidates[candidates.length % Math.max(1, candidates.length)]);
    const datasets = candidates.slice(0, count);
    this.view.clear();
    const panels = [];
    for (let index = 0; index < datasets.length; index += 1) {
      let position;
      if (index === 1) position = { referencePanel: panels[0].id, direction: "right" };
      if (index === 2) position = { referencePanel: panels[0].id, direction: "below" };
      if (index === 3) position = { referencePanel: panels[1].id, direction: "below" };
      panels.push(this.openPanel(datasets[index], position));
    }
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
