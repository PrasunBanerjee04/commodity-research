import test from 'node:test';
import assert from 'node:assert/strict';
import { DockManager, canonicalDatasetPath } from '../src/comm_research/dashboard/ui/src/dock_manager.js';
import { traceStyle } from '../src/comm_research/dashboard/ui/src/chart_panel.js';

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
    panel.api = { setActive: () => { this.activePanel = panel; } };
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
  dock.arrange(4); assert.equal(dock.view.totalPanels, 2); assert.equal(dock.openPanels.size, 2);
  dock.arrange(1); assert.equal(dock.view.totalPanels, 2); assert.equal(dock.openPanels.size, 2);
});
test('node hue is stable across components and congestion is dashed', () => {
  const rows = ['LMP','ENERGY','CONG','LOSS'].map(component => ({ node:'TH_NP15_GEN-APND', component, series: component }));
  const styles = rows.map(traceStyle);
  assert.equal(new Set(styles.map(style => style.stroke.slice(0,7))).size, 1);
  assert.equal(styles[0].stroke.slice(0,7), '#2962FF'); assert.deepEqual(styles[2].dash, [6,4]);
  assert.equal(traceStyle({ node:'TH_SP15_GEN-APND', component:'LMP' }).stroke.slice(0,7), '#089981');
});
