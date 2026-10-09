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

let dock;
let tree;

function initializeWorkspace() {
  dock = new DockManager({
    mount: document.querySelector("#dockview"),
    emptyState: document.querySelector("#empty-workspace"),
    onError: showStatus,
  });
  tree = new TreeNavigator({
    root: datasetTree,
    search: document.querySelector("#dataset-search"),
    count: datasetCount,
    onOpen: (dataset) => dock.openPanel(dataset),
  });
}

function setTheme(theme) {
  localStorage.setItem("commodity-theme", theme);
  dock.setTheme(theme);
  document.querySelector("#theme-toggle").textContent = theme === "light" ? "DARK" : "LIGHT";
}

async function updateCatalog(datasets, refresh = false) {
  dock.setDatasets(datasets, refresh);
  tree.setDatasets(datasets);
  if (!datasets.length) {
    showStatus(new Error("No data lake feeds were discovered."));
    return;
  }
  if (dock.view.totalPanels === 0) {
    const dam = datasets.find(dataset => /\/caiso\/(dam_lmp|da_lmp)$/.test(dataset.key));
    const rtm = datasets.find(dataset => /\/caiso\/(rtm_lmp|rt_lmp)$/.test(dataset.key));
    const first = dock.openPanel(dam || rtm || datasets[0]);
    if (dam && rtm) dock.openPanel(rtm, { referencePanel: first.id, direction: "right" });
  }
}

try {
  initializeWorkspace();
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
    const button = event.currentTarget;
    button.disabled = true;
    status.textContent = "RESCANNING DATA LAKE…";
    try {
      await updateCatalog(await rescanCatalog(), true);
      status.textContent = "";
    } catch (error) {
      showStatus(error);
    } finally {
      button.disabled = false;
    }
  });
  await updateCatalog(await loadCatalog());
} catch (error) {
  console.error("Workspace initialization failed", error);
  showStatus(error);
  if (!dock || dock.view.totalPanels === 0) {
    const emptyState = document.querySelector("#empty-workspace");
    emptyState.replaceChildren(document.createTextNode(`WORKSPACE_LOAD_ERROR: ${error.message}`));
    emptyState.hidden = false;
  }
}
