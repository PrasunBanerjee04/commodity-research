// Shared local navigation for uPlot panes. Drag selection is provided by
// uPlot; wheel zoom and Shift+drag pan never invoke the query layer.
export function bindChartNavigation(plot, { reset, changed = () => {} }) {
  const over = plot.over,
    view = over.ownerDocument.defaultView;
  let pan = null,
    zoomStart = null;
  over.title =
    "Drag to zoom · Wheel to zoom · Shift-drag to pan · Double-click or RESET to restore";
  const wheel = (event) => {
    event.preventDefault();
    const box = over.getBoundingClientRect();
    const center = plot.posToVal(event.clientX - box.left, "x");
    const factor = Math.exp(
      Math.max(-100, Math.min(100, event.deltaY)) * 0.004,
    );
    const scale = plot.scales.x;
    plot.setScale("x", {
      min: center + (scale.min - center) * factor,
      max: center + (scale.max - center) * factor,
    });
    changed();
  };
  const start = (event) => {
    if (event.button === 0 && !event.shiftKey)
      zoomStart = { x: event.clientX, y: event.clientY };
    if (!event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    pan = { x: event.clientX, min: plot.scales.x.min, max: plot.scales.x.max };
  };
  const move = (event) => {
    if (!pan) return;
    const width = over.getBoundingClientRect().width;
    if (!width) return;
    const shift = ((event.clientX - pan.x) / width) * (pan.max - pan.min);
    plot.setScale("x", { min: pan.min - shift, max: pan.max - shift });
    changed();
  };
  const end = (event) => {
    if (
      zoomStart &&
      Math.hypot(event.clientX - zoomStart.x, event.clientY - zoomStart.y) > 5
    )
      changed();
    zoomStart = null;
    pan = null;
  };
  const doubleClick = (event) => {
    event.preventDefault();
    reset();
  };
  over.addEventListener("wheel", wheel, { passive: false });
  over.addEventListener("mousedown", start, true);
  over.addEventListener("dblclick", doubleClick);
  view.addEventListener("mousemove", move);
  view.addEventListener("mouseup", end);
  return () => {
    over.removeEventListener("wheel", wheel);
    over.removeEventListener("mousedown", start, true);
    over.removeEventListener("dblclick", doubleClick);
    view.removeEventListener("mousemove", move);
    view.removeEventListener("mouseup", end);
  };
}
