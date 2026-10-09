const LABELS = {
  ags: "Agricultural Commodities",
  caiso: "CAISO",
  dam_lmp: "Day-Ahead LMP",
  eupg: "Europe Power & Gas",
  napg: "North America Power & Gas",
  oil: "Crude & Refined Products",
  power_gas: "Power & Natural Gas",
  rtm_lmp: "Real-Time LMP",
  weather: "Meteorology & Numerical Models",
};

function displayName(value) {
  if (LABELS[value]) return LABELS[value];
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.length <= 4 ? part.toUpperCase() : part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

function datasetParts(dataset) {
  return (dataset.key || dataset.path || dataset.name || "other")
    .replaceAll("\\", "/")
    .split("/")
    .filter(Boolean);
}

export class TreeNavigator {
  constructor({ root, search, count, onOpen }) {
    this.root = root;
    this.search = search;
    this.count = count;
    this.onOpen = onOpen;
    this.datasets = [];
    this.search.addEventListener("input", () => this.render());
  }

  setDatasets(datasets) {
    this.datasets = datasets;
    this.count.textContent = `${datasets.length} FEEDS`;
    this.render();
  }

  render() {
    const query = this.search.value.trim().toLocaleLowerCase();
    const visible = this.datasets.filter((dataset) => (
      `${dataset.key || ""} ${dataset.name || ""} ${dataset.title || ""}`
        .toLocaleLowerCase()
        .includes(query)
    ));
    const tree = new Map();
    for (const dataset of visible) {
      let level = tree;
      const parts = datasetParts(dataset);
      for (const part of parts.slice(0, -1)) {
        if (!level.has(part)) level.set(part, new Map());
        level = level.get(part);
      }
      const leafName = parts.at(-1) || dataset.key || dataset.name;
      level.set(`\u0000${dataset.key}`, { dataset, label: displayName(leafName) });
    }

    const fragment = document.createDocumentFragment();
    fragment.append(this.renderBranch(tree, true, query.length > 0));
    this.root.replaceChildren(fragment);
  }

  renderBranch(tree, isRoot = false, expanded = false) {
    const branch = document.createElement("div");
    branch.className = `tree-branch${isRoot ? " root" : ""}`;
    for (const [key, value] of tree) {
      if (key.startsWith("\u0000")) {
        const button = document.createElement("button");
        button.className = "tree-leaf";
        button.type = "button";
        button.title = value.dataset.title || value.dataset.key;
        button.append(document.createTextNode(value.label));
        button.addEventListener("click", () => this.onOpen(value.dataset));
        branch.append(button);
        continue;
      }
      const details = document.createElement("details");
      details.className = "tree-branch";
      details.open = expanded || isRoot;
      const summary = document.createElement("summary");
      summary.append(document.createTextNode(displayName(key)));
      const childBranch = this.renderBranch(value, false, expanded);
      const badge = document.createElement("span");
      badge.className = "tree-count";
      badge.textContent = String(this.countLeaves(value));
      summary.append(badge);
      details.append(summary, childBranch);
      branch.append(details);
    }
    return branch;
  }

  countLeaves(tree) {
    let count = 0;
    for (const [key, value] of tree) {
      count += key.startsWith("\u0000") ? 1 : this.countLeaves(value);
    }
    return count;
  }
}
