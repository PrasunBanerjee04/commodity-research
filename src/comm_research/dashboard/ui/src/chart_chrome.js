// Presentation shared by the history workstation and generic research panes.
export const METRIC_TOKENS = {
  LMP: { stroke: "#2962FF", width: 1.75, dash: [] },
  ENERGY: { stroke: "#00897B", width: 1.25, dash: [] },
  CONG: { stroke: "#E53935", width: 1.25, dash: [6, 4] },
  LOSS: { stroke: "#FB8C00", width: 1.25, dash: [] },
  GHG: { stroke: "#8E24AA", width: 1.25, dash: [] },
};

export const utcTick = (epoch) =>
  new Date(epoch * 1000).toISOString().slice(5, 16).replace("T", " ");

export function utcTicks(plot, ticks) {
  const ratio = plot.ctx.canvas.width / plot.width;
  return ticks.map(epoch => {
    const center = plot.bbox.left / ratio + plot.valToPos(epoch, "x");
    // Keep the fixed-width timestamp labels wholly inside each viewport.
    return center < 34 || center > plot.width - 34 ? "" : utcTick(epoch);
  });
}

export function popoutButton(container) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "panel-popout";
  button.textContent = "⤢";
  button.setAttribute("aria-label", "Pop out node chart");
  button.title = "Pop out node chart";
  button.addEventListener("click", () => container.dispatchEvent(
    new CustomEvent("panel-popout-request", { bubbles: true }),
  ));
  return button;
}

export function drawRulers(plot) {
  const { ctx, bbox } = plot;
  const ratio = ctx.canvas.width / plot.width;
  ctx.save();
  ctx.lineWidth = ratio;
  const right = Math.round(bbox.left + bbox.width) + ratio / 2;
  ctx.strokeStyle = "#2A2E39";
  ctx.beginPath();
  ctx.moveTo(right, bbox.top);
  ctx.lineTo(right, bbox.top + bbox.height);
  ctx.stroke();
  if (plot.scales.y.min <= 0 && plot.scales.y.max >= 0) {
    const zero = Math.round(plot.valToPos(0, "y", true)) + ratio / 2;
    ctx.strokeStyle = "#363A45";
    ctx.setLineDash([ratio, ratio * 3]);
    ctx.beginPath();
    ctx.moveTo(bbox.left, zero);
    ctx.lineTo(bbox.left + bbox.width, zero);
    ctx.stroke();
  }
  ctx.restore();
}

export function updateAxisPills(plot) {
  if (!plot.bbox) return;
  if (!plot.axisPills) {
    plot.axisPills = ["x", "y"].map(axis => {
      const label = document.createElement("div");
      label.className = `axis-pill axis-pill-${axis}`;
      label.setAttribute("aria-hidden", "true");
      plot.root.append(label);
      return label;
    });
  }
  const [x, y] = plot.axisPills;
  const { left, top } = plot.cursor;
  x.hidden = y.hidden = left < 0 || top < 0;
  if (x.hidden) return;
  const ratio = plot.ctx.canvas.width / plot.width;
  const bounds = plot.bbox;
  x.textContent = utcTick(plot.posToVal(left, "x"));
  y.textContent = plot.posToVal(top, "y").toFixed(2);
  x.style.left = `${Math.max(0, Math.min(plot.width - x.offsetWidth,
    bounds.left / ratio + left - x.offsetWidth / 2))}px`;
  x.style.top = `${(bounds.top + bounds.height) / ratio + 4}px`;
  y.style.right = "0px";
  y.style.top = `${bounds.top / ratio + top - y.offsetHeight / 2}px`;
}
