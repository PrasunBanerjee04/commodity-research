import { loadCatalog, rescanCatalog } from "./api.js";
import { DockManager } from "./dock_manager.js";
import { TreeNavigator } from "./tree_navigator.js";

const status = document.querySelector("#status-message");
const datasetTree = document.querySelector("#dataset-tree");
const datasetCount = document.querySelector("#dataset-count");
const navigator = document.querySelector("#navigator");

function showStatus(error) {
  status.textContent = error.message;
}

const dock = new DockManager({
  mount: document.querySelector("#dockview"),
  emptyState: document.querySelector("#empty-workspace"),
  onError: showStatus,
});
const tree = new TreeNavigator({
  root: datasetTree,
  search: document.querySelector("#dataset-search"),
  count: datasetCount,
  onOpen: (dataset) => {
    dock.openPanel(dataset);
  },
});

function setTheme(theme) {
  localStorage.setItem("commodity-theme", theme);
  dock.setTheme(theme);
  document.querySelector("#theme-toggle").textContent = theme === "light" ? "DARK" : "LIGHT";
}

async function updateCatalog(datasets) {
  dock.setDatasets(datasets);
  tree.setDatasets(datasets);
  if (!datasets.length) {
    showStatus(new Error("No data lake feeds were discovered."));
    return;
  }
  if (dock.view.totalPanels === 0) dock.openPanel(datasets[0]);
}

try {
  const theme = localStorage.getItem("commodity-theme") || "dark";
  dock.setTheme(theme);
  document.querySelector("#theme-toggle").addEventListener("click", () => {
    setTheme(document.body.classList.contains("light") ? "dark" : "light");
  });
  document.querySelectorAll("[data-layout]").forEach((button) => {
    button.addEventListener("click", () => dock.arrange(Number(button.dataset.layout)));
  });
  document.querySelector("#navigator-toggle").addEventListener("click", () => {
    navigator.classList.toggle("collapsed");
  });
  document.querySelector("#rescan-button").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    status.textContent = "RESCANNING DATA LAKE…";
    try {
      await updateCatalog(await rescanCatalog());
      status.textContent = "";
    } catch (error) {
      showStatus(error);
    } finally {
      event.currentTarget.disabled = false;
    }
  });
  await updateCatalog(await loadCatalog());
} catch (error) {
  showStatus(error);
}
