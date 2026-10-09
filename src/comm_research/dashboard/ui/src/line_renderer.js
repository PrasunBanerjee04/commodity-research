import { drawBoundedSeries } from "./canvas_paths.js";

const VERTEX = `
attribute vec2 position;
attribute vec4 color;
attribute vec3 distance;
uniform vec2 viewport;
varying vec4 lineColor;
varying vec3 lineDistance;
void main() {
  gl_Position = vec4(position / viewport * vec2(2., -2.) + vec2(-1., 1.), 0., 1.);
  lineColor = color; lineDistance = distance;
}`;
const FRAGMENT = `
precision highp float;
varying vec4 lineColor;
varying vec3 lineDistance;
void main() {
  float period = lineDistance.y + lineDistance.z;
  if (period > 0. && mod(lineDistance.x, period) > lineDistance.y) discard;
  gl_FragColor = lineColor;
}`;

// uPlot supplies scale coordinates and paints axes. Rasterize bounded line
// segments on an offscreen WebGL canvas, then composite that bitmap once.
// This avoids expensive dashed Path2D strokes without reducing observations.
export class LineRenderer {
  constructor() {
    this.canvas = document.createElement("canvas");
    try {
      this.gl = this.canvas.getContext("webgl", {
        alpha: true, antialias: false, premultipliedAlpha: true,
        preserveDrawingBuffer: true,
      });
      if (!this.gl) return;
      const gl = this.gl;
      const shader = (type, source) => {
        const result = gl.createShader(type);
        gl.shaderSource(result, source); gl.compileShader(result);
        if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) {
          const error = gl.getShaderInfoLog(result); gl.deleteShader(result); throw new Error(error);
        }
        return result;
      };
      this.program = gl.createProgram();
      const vertex = shader(gl.VERTEX_SHADER, VERTEX), fragment = shader(gl.FRAGMENT_SHADER, FRAGMENT);
      gl.attachShader(this.program, vertex); gl.attachShader(this.program, fragment);
      gl.linkProgram(this.program); gl.deleteShader(vertex); gl.deleteShader(fragment);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(this.program));
      this.buffer = gl.createBuffer();
      this.viewport = gl.getUniformLocation(this.program, "viewport");
      this.attributes = ["position", "color", "distance"].map(name => gl.getAttribLocation(this.program, name));
      this.vertices = new Float32Array(0);
      this.lineWidthLimit = gl.getParameter(gl.ALIASED_LINE_WIDTH_RANGE)[1];
      // A restored context has invalid old handles; retain the safe Canvas
      // fallback until this panel next creates a chart.
      this.canvas.addEventListener("webglcontextlost", () => { this.lost = true; });
    } catch (error) {
      console.warn("WebGL lines unavailable; using bounded Canvas strokes.", error);
      this.dispose(); this.gl = null;
    }
  }

  draw(plot) {
    const gl = this.gl;
    if (!gl || this.lost || gl.isContextLost()) {
      plot.root.dataset.renderer = "canvas";
      for (let index = 1; index < plot.series.length; index++)
        if (plot.series[index].show) drawBoundedSeries(plot, index);
      return;
    }
    plot.root.dataset.renderer = "webgl";
    const width = plot.ctx.canvas.width, height = plot.ctx.canvas.height;
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width; this.canvas.height = height;
    }
    const ratio = width / plot.width, bounds = plot.bbox;
    gl.viewport(0, 0, width, height);
    gl.disable(gl.SCISSOR_TEST); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(Math.round(bounds.left), Math.round(height - bounds.top - bounds.height),
      Math.round(bounds.width), Math.round(bounds.height));
    gl.useProgram(this.program); gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    this.attributes.forEach((attribute, index) => {
      gl.enableVertexAttribArray(attribute);
      gl.vertexAttribPointer(attribute, [2,4,3][index], gl.FLOAT, false, 36, [0,8,24][index]);
    });
    gl.uniform2f(this.viewport, width, height);
    gl.lineWidth(Math.min(ratio, this.lineWidthLimit));
    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const required = plot.series.reduce((size, series, index) => size + (index && series.show
      ? Math.max(0, plot.data[index].reduce((n, value) => n + (value != null), 0) - 1) * 18 : 0), 0);
    if (required > this.vertices.length) this.vertices = new Float32Array(2 ** Math.ceil(Math.log2(required)));
    let offset = 0;
    const singlePoints = [];
    for (let index = 1; index < plot.series.length; index++) {
      const series = plot.series[index];
      if (!series.show) continue;
      const times = plot.data[0], values = plot.data[index];
      // The aligned table may contain gaps from other traces; each trace still
      // has <=1,500 observations, so at most 1,499 line segments are emitted.
      let previous = null, distance = 0;
      const firstOffset = offset;
      const channels = series.stroke(plot, index).slice(1).match(/../g).map(value => parseInt(value, 16) / 255);
      const alpha = (channels[3] ?? 1) * series.alpha;
      const on = (series.dash?.[0] || 0) * ratio, off = (series.dash?.[1] || 0) * ratio;
      const push = (x, y, along) => {
        this.vertices[offset++] = x; this.vertices[offset++] = y;
        this.vertices[offset++] = channels[0]; this.vertices[offset++] = channels[1];
        this.vertices[offset++] = channels[2]; this.vertices[offset++] = alpha;
        this.vertices[offset++] = along; this.vertices[offset++] = on; this.vertices[offset++] = off;
      };
      for (let position = 0; position < times.length; position++) {
        if (values[position] == null) {
          if (!series.spanGaps) { previous = null; distance = 0; }
          continue;
        }
        const x = plot.valToPos(times[position], "x", true);
        const y = plot.valToPos(values[position], series.scale, true);
        if (previous) {
          const dx = x - previous.x, dy = y - previous.y, length = Math.hypot(dx, dy);
          if (length) {
            push(previous.x, previous.y, distance);
            push(x, y, distance + length);
            distance += length;
          }
        }
        previous = { x, y };
      }
      if (offset === firstOffset && previous) singlePoints.push({ ...previous, stroke: series.stroke(plot, index) });
    }
    gl.bufferData(gl.ARRAY_BUFFER, this.vertices.subarray(0, offset), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.LINES, 0, offset / 9);
    plot.ctx.drawImage(this.canvas, 0, 0);
    // One observation has no line segment; keep that valid price visible.
    for (const point of singlePoints) {
      plot.ctx.save(); plot.ctx.fillStyle = point.stroke;
      plot.ctx.beginPath(); plot.ctx.arc(point.x, point.y, 2 * ratio, 0, 2 * Math.PI);
      plot.ctx.fill(); plot.ctx.restore();
    }
  }

  dispose() {
    if (!this.gl) return;
    if (this.buffer) this.gl.deleteBuffer(this.buffer);
    if (this.program) this.gl.deleteProgram(this.program);
    this.gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}
