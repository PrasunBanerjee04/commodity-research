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
test('metric hues and widths stay consistent across nodes, with a congestion stroke key', () => {
  const components = ['LMP','ENERGY','CONG','LOSS','GHG'];
  const expected = ['#2962FF','#00897B','#E53935','#FB8C00','#8E24AA'];
  for (const node of ['TH_NP15_GEN-APND','TH_SP15_GEN-APND']) {
    const styles = components.map(component => traceStyle({node,component}));
    assert.deepEqual(styles.map(style=>style.stroke),expected);
    assert.deepEqual(styles.map(style=>style.width),[1.75,1.25,1.25,1.25,1.25]);
    assert.deepEqual(styles[2].dash,[6,4]);
    assert.deepEqual(styles[3].dash,[]);
  }
  assert.equal(traceStyle({component:'MCE'}).stroke,'#00897B');
  assert.equal(traceStyle({component:'MGHG'}).stroke,'#8E24AA');
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

const {prepareHistory, displayData, COMPONENTS} = await import('../src/comm_research/dashboard/ui/src/node_history.js');
test('complete CAISO history retains exact prices while bounding every local display', () => {
  const count = 100000;
  const payload = {node:'NP15',timestamps:Array.from({length:count},(_,i)=>new Date(Date.UTC(2020,0,1)+i*300000).toISOString())};
  for (const {key} of COMPONENTS) payload[key] = Array.from({length:count},(_,i)=>30+Math.sin(i)*20);
  payload.congestion[45678] = 12345;
  payload.loss[10] = null;
  const history = prepareHistory(payload);
  assert.equal(history.epochs.length,count);
  assert.equal(history.values[2][45678],12345);
  assert.ok(Number.isNaN(history.values[3][10]));
  for (const days of [1,5,30,365,10000]) {
    const end = history.epochs.at(-1), data = displayData(history,end-days*86400,end);
    assert.ok(data[0].length<=1500); assert.equal(data[0].at(-1),end);
    assert.ok(data.every(column=>column.length===data[0].length));
  }
  assert.ok(displayData(history,history.epochs[0],history.epochs.at(-1))[3].includes(12345));
  assert.ok(displayData(history,history.epochs[0],history.epochs.at(-1),320)[0].length<=320);
  assert.deepEqual(COMPONENTS.map(component=>component.color),['#2962FF','#00897B','#E53935','#FB8C00']);
});
test('malformed complete history cannot poison local canvas sampling', () => {
  assert.throws(()=>prepareHistory({timestamps:['bad'],lmp:[1],energy:[1],congestion:[1],loss:[1]}));
  assert.throws(()=>prepareHistory({timestamps:[],lmp:[1],energy:[],congestion:[],loss:[]}));
});
