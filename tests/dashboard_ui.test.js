import test from 'node:test';
import assert from 'node:assert/strict';
import { DockManager, canonicalDatasetPath } from '../src/comm_research/dashboard/ui/src/dock_manager.js';
import { traceStyle } from '../src/comm_research/dashboard/ui/src/chart_panel.js';
import { request } from '../src/comm_research/dashboard/ui/src/api.js';
import { QueryCache, viewStatistics } from '../src/comm_research/dashboard/ui/src/query_cache.js';

class View {
  constructor() { this.panels = []; this.callbacks = {}; }
  onDidMutateLayout(fn) { this.callbacks.mutate = fn; }
  onDidLayoutChange(fn) { this.callbacks.layout = fn; }
  onDidAddPanel(fn) { this.callbacks.add = fn; }
  onDidRemovePanel(fn) { this.callbacks.remove = fn; }
  onDidActivePanelChange(fn) { this.callbacks.active = fn; }
  get totalPanels() { return this.panels.length; }
  addPanel({ id, params }) {
    const panel = { id, params, focusCount: 0 };
    panel.api = { setActive: () => { this.activePanel = panel; }, group: {}, moveTo: ({group}) => { panel.api.group = group; } };
    panel.focus = () => { panel.focusCount++; };
    this.panels.push(panel); this.activePanel = panel; this.callbacks.add(panel); return panel;
  }
  removePanel(panel) { this.panels = this.panels.filter(item => item !== panel); this.callbacks.remove(panel); }
  clear() { for (const panel of [...this.panels]) this.removePanel(panel); }
  toJSON() { return { panels: this.panels.map(({ id, params }) => ({ id, params })) }; }
  fromJSON(layout) { for (const panel of layout.panels) this.addPanel(panel); }
}
function setup(layout) {
  const saved = new Map(layout ? [['commodity-dock-layout', JSON.stringify(layout)]] : []);
  global.localStorage = { getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  global.window = { dockview: { DockviewComponent: View } };
  global.document = { querySelectorAll: () => [] };
  return new DockManager({ mount: { dataset: {} }, emptyState: {}, onError: error => { throw error; } });
}
const dam = { key: 'power_gas/napg/caiso/dam_lmp' };
const rtm = { key: 'power_gas/napg/caiso/rtm_lmp' };
test('repeated and canonical-equivalent opens focus one panel', () => {
  const dock = setup(); const panel = dock.openPanel(dam);
  assert.equal(dock.openPanel({ ...dam, key: './power_gas//napg/caiso/dam_lmp/' }), panel);
  dock.openPanel(dam); assert.equal(dock.openPanels.size, 1); assert.equal(dock.view.totalPanels, 1); assert.equal(panel.focusCount, 2);
  assert.throws(() => canonicalDatasetPath('../secret'));
});
test('close unregisters; reopen mounts one fresh panel', () => {
  const dock = setup(); const panel = dock.openPanel(dam); dock.openPanel(rtm);
  dock.view.removePanel(panel); assert.equal(dock.openPanels.has(dam.key), false);
  const reopened = dock.openPanel(dam); assert.notEqual(reopened, panel); assert.equal(dock.view.totalPanels, 2);
});
test('old duplicate layouts are repaired during restore', () => {
  const dock = setup({ panels: [{ id: 'old-1', params: { dataset: dam } }, { id: 'old-2', params: { dataset: { ...dam, key: './'+dam.key } } }, { id: 'rtm', params: { dataset: rtm } }] });
  assert.equal(dock.view.totalPanels, 2); assert.equal(dock.openPanels.size, 2); assert.equal(dock.openPanel(dam).id, 'old-1');
});
test('layout presets preserve all open datasets and do not clone scarce feeds', () => {
  const dock = setup(); dock.setDatasets([dam, rtm]); dock.openPanel(dam); dock.openPanel(rtm);
  const instances = [...dock.openPanels.values()];
  dock.arrange(4); assert.equal(dock.view.totalPanels, 2); assert.equal(dock.openPanels.size, 2);
  dock.arrange(1); assert.equal(dock.view.totalPanels, 2); assert.equal(dock.openPanels.size, 2);
  assert.deepEqual(new Set(dock.openPanels.values()), new Set(instances));
});

function cacheRow(day, node, signal = 'value:LMP', value = 20) {
  return { timestamp:`2024-01-${String(day).padStart(2,'0')}T00:00:00Z`, value,
    node, signal, series:`${signal} · ${node}`, dimensions:{node, market_run_id:'RTM'} };
}
test('cached date, node and component filters slice locally with inclusive UTC days', () => {
  const cache = new QueryCache();
  cache.add({start:'2024-01-01',end:'2024-01-30',signals:['value:LMP','value:CONG'],filters:{node:['NP15','SP15']}}, {
    plot:[cacheRow(1,'NP15'),cacheRow(2,'NP15'),cacheRow(2,'SP15','value:CONG'),cacheRow(3,'NP15')]
  });
  assert.equal(cache.select('2024-01-02','2024-01-02',['value:LMP'],{node:['NP15']}).plot.length,1);
  assert.equal(cache.select('2024-01-02','2024-01-02',['value:CONG'],{node:['SP15']}).plot.length,1);
  assert.deepEqual(cache.select('2024-01-01','2024-01-30',['value:LMP'],{node:[]}).plot,[]);
  assert.equal(cache.select('2024-01-01','2024-01-31',['value:LMP'],{node:['NP15']}),null);
});
test('partial cache batches do not claim a missing node/component combination', () => {
  const cache = new QueryCache();
  cache.add({start:'2024-01-01',end:'2024-01-30',signals:['value:LMP'],filters:{node:['NP15']}},{plot:[cacheRow(1,'NP15')]});
  cache.add({start:'2024-01-01',end:'2024-01-30',signals:['value:CONG'],filters:{node:['SP15']}},{plot:[cacheRow(1,'SP15','value:CONG')]});
  assert.equal(cache.select('2024-01-01','2024-01-30',['value:LMP'],{node:['SP15']}),null);
});
test('invalid payloads and point-cap violations cannot poison a cached window', () => {
  const cache = new QueryCache();
  const query = {start:'2024-01-01',end:'2024-01-30',signals:['value:LMP'],filters:{node:['NP15']}};
  assert.throws(()=>cache.add(query,{plot:[{...cacheRow(1,'NP15'),timestamp:'bad'}]}));
  assert.throws(()=>cache.add(query,{plot:Array.from({length:1501},()=>cacheRow(1,'NP15'))}),/1,500/);
  assert.equal(cache.windows.length,0);
});
test('display statistics use actual timestamps and sample standard deviation', () => {
  const rows = [cacheRow(1,'NP15','value:LMP',-10),cacheRow(2,'NP15','value:LMP',-5)].map(row=>({...row,_epoch:Date.parse(row.timestamp)/1000}));
  const [stats]=viewStatistics(rows);
  assert.equal(stats.change_24h,50); assert.equal(stats.mean,-7.5);
  assert.equal(stats.std,Math.sqrt(12.5));
});
test('node hue is stable across components and congestion is dashed', () => {
  const rows = ['LMP','ENERGY','CONG','LOSS'].map(component => ({ node:'TH_NP15_GEN-APND', component, series: component }));
  const styles = rows.map(traceStyle);
  assert.equal(new Set(styles.map(style => style.stroke.slice(0,7))).size, 1);
  assert.equal(styles[0].stroke.slice(0,7), '#2962FF'); assert.deepEqual(styles[2].dash, [6,4]);
  assert.equal(traceStyle({ node:'TH_SP15_GEN-APND', component:'LMP' }).stroke.slice(0,7), '#089981');
});

test('HTTP status survives an HTML error response', async () => {
  const original = global.fetch;
  try {
    global.fetch = async () => new Response('<h1>Not Found</h1>', { status: 404, statusText: 'Not Found' });
    await assert.rejects(request('/api/series'), /HTTP 404: Not Found/);
  } finally { global.fetch = original; }
});
test('JSON errors retain both the status and API explanation', async () => {
  const original = global.fetch;
  try {
    global.fetch = async () => new Response('{"error":"Lake unavailable"}', { status: 503 });
    await assert.rejects(request('/api/series'), /HTTP 503: Lake unavailable/);
  } finally { global.fetch = original; }
});
test('a malformed successful response produces a clear data error', async () => {
  const original = global.fetch;
  try {
    global.fetch = async () => new Response('not JSON');
    await assert.rejects(request('/api/series'), /Invalid JSON response from the data API/);
  } finally { global.fetch = original; }
});
