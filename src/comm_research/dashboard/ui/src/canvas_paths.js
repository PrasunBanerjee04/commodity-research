// Keep uPlot's axes, scales and cursor while bounding each Canvas stroke.
// Dense zigzags in one large path make software rasterization disproportionately
// expensive. Short strokes retain all vertices and avoid that path complexity.
export const emptyPaths = () => null;

export function drawBoundedSeries(plot, index) {
  const ctx = plot.ctx, series = plot.series[index];
  const times = plot.data[0], values = plot.data[index];
  const ratio = ctx.canvas.width / plot.width;
  const dash = (series.dash || []).map(value => value * ratio);
  const period = dash.reduce((sum, value) => sum + value, 0);
  const bounds = plot.bbox;
  ctx.save();
  try {
    ctx.beginPath(); ctx.rect(bounds.left, bounds.top, bounds.width, bounds.height); ctx.clip();
    ctx.strokeStyle = series.stroke(plot, index);
    ctx.lineWidth = series.width * ratio;
    ctx.lineJoin = "round"; ctx.lineCap = "butt";
    ctx.setLineDash(dash);
    ctx.beginPath();
    let previous = null, segments = 0, distance = 0, observations = 0;
    const flush = () => {
      if (segments) ctx.stroke();
      ctx.beginPath(); segments = 0;
      ctx.lineDashOffset = period ? -(distance % period) : 0;
    };
    for (let position = 0; position < times.length; position++) {
      if (values[position] == null) {
        if (!series.spanGaps) { flush(); previous = null; distance = 0; ctx.lineDashOffset = 0; }
        continue;
      }
      const x = plot.valToPos(times[position], "x", true);
      const y = plot.valToPos(values[position], series.scale, true);
      observations++;
      if (!previous) ctx.moveTo(x, y);
      else {
        ctx.lineTo(x, y); segments++;
        distance += Math.hypot(x - previous.x, y - previous.y);
        if (segments === 16) { flush(); ctx.moveTo(x, y); }
      }
      previous = { x, y };
    }
    flush();
    if (observations === 1 && previous) {
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath(); ctx.arc(previous.x, previous.y, 2 * ratio, 0, 2 * Math.PI); ctx.fill();
    }
  } finally { ctx.restore(); }
}
