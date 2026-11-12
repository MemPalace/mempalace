import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../commonplace/web/static/', import.meta.url);
// Liang–Barsky clipping: touching an edge counts as an intersection too.
function intersects(s, r) {
  let low = 0, high = 1;
  const dx = s.x2 - s.x1, dy = s.y2 - s.y1;
  for (const [p, q] of [[-dx, s.x1-r.x], [dx, r.x+r.width-s.x1], [-dy, s.y1-r.y], [dy, r.y+r.height-s.y1]]) {
    if (p === 0) { if (q < 0) return false; }
    else { const t = q / p; if (p < 0) low = Math.max(low, t); else high = Math.min(high, t); if (low > high) return false; }
  }
  return true;
}
const overlaps = (a, b) => a.x < b.x+b.width && a.x+a.width > b.x && a.y < b.y+b.height && a.y+a.height > b.y;

for (const count of [1, 2, 5, 8, 12]) for (const long of [false, true]) for (const phone of [false, true]) {
  test(`label geometry: ${count} machines, ${long ? 'long' : 'short'} names, ${phone ? 'phone' : 'desktop'}`, async () => {
    const { layout } = await import(new URL('deck-topology.js', STATIC));
    const nodes = [{ id: 'hub', name: 'Commonplace', role: 'Hub', type: 'hub' }, ...Array.from({ length: count }, (_, i) => ({ id: `m${i}`, name: long ? 'A very long machine name WWWWWW 🧰 '.repeat(6) : `Machine ${i}`, role: 'Waiting for attestation' }))];
    const links = nodes.slice(1).map(n => ({ from: 'hub', to: n.id, state: 'waiting' }));
    const result = layout({ nodes, links, width: phone ? 358 : 860, height: phone ? 440 : 620, phone });
    assert.equal(result.nodes.length, count + 1);
    assert.equal(result.labels.length, count + 1);
    assert.equal(result.dots.length, count * 2);
    for (const box of result.labels) {
      assert.ok(box.x >= 0 && box.y >= 0 && box.x+box.width <= result.width && box.y+box.height <= result.height, `inside field: ${box.id}`);
      for (const segment of result.segments) assert.ok(!intersects(segment, box), `line crosses ${box.id}`);
      for (const dot of result.dots) {
        // Stronger than centre containment: the entire painted dot stays clear.
        const dx = Math.max(box.x-dot.x, 0, dot.x-box.x-box.width), dy = Math.max(box.y-dot.y, 0, dot.y-box.y-box.height);
        assert.ok(Math.hypot(dx, dy) > dot.r, `dot crosses ${box.id}`);
      }
    }
    for (let i = 0; i < result.labels.length; i++) for (const b of result.labels.slice(i+1)) assert.ok(!overlaps(result.labels[i], b), 'labels overlap');
    if (long) assert.ok(result.nodes.slice(1).every(n => n.displayName.endsWith('…')));
    assert.deepEqual(result, layout({ nodes, links, width: phone ? 358 : 860, height: phone ? 440 : 620, phone }), 'deterministic and DOM-free');
  });
}

const { escape: e } = await import(new URL('api.js', STATIC));
const compute = await import(new URL('deck-compute.js', STATIC));
const machines = [
  { id: 'build', label: 'Build box', enabled: true, binding_state: 'owner_attested', projects: ['atlas'], topics: ['build'], wings: ['project'], max_runtime_mode: 'approval-required', verified: false },
  { id: 'studio', label: 'Studio machine', enabled: true, binding_state: 'unbound' },
  { id: 'old', label: 'Old tower', enabled: false, binding_state: 'owner_attested' },
  { id: 'gone', label: 'Revoked machine', enabled: true, revoked_at: '2026-01-01' },
];
function fakeRoot() {
  const regions = new Map(), handlers = new Map(), classes = new Set();
  const doc = { activeElement: null, adoptedStyleSheets: [] };
  const header = { hidden: false }, phoneBar = { hidden: false }, phoneTabs = { style: { position: '', bottom: '', left: '', right: '', zIndex: '' } };
  const root = {
    ownerDocument: doc, parentElement: { querySelector: selector => ({ '.head': header, '#phone-bar': phoneBar, '#phone-tabs': phoneTabs })[selector] },
    classList: { add: x => classes.add(x), remove: x => classes.delete(x) },
    addEventListener: (type, handler) => handlers.set(type, handler),
    removeEventListener: type => handlers.delete(type),
    querySelector(selector) {
      const name = /data-compute-region="([^"]+)"/.exec(selector)?.[1];
      return regions.get(name) || null;
    },
    set innerHTML(html) {
      this.html = html; regions.clear();
      for (const [, name] of html.matchAll(/data-compute-region="([^"]+)"/g)) regions.set(name, {
        innerHTML: '', textContent: '', clientWidth: 860, clientHeight: 620,
        querySelector: () => ({ focus() {} }), querySelectorAll: () => [],
      });
    },
  };
  const event = (data, key) => ({ key, prevented: false, preventDefault() { this.prevented = true; }, target: { closest(selector) { return Object.keys(data).some(k => selector === `[data-${k.replace(/[A-Z]/g, c => '-'+c.toLowerCase())}]`) ? { dataset: data } : null; } } });
  return { root, doc, regions, handlers, classes, header, phoneBar, phoneTabs, click: data => handlers.get('click')(event(data)), key(data, key) { const ev = event(data, key); handlers.get('keydown')(ev); return ev; } };
}
const flush = async () => { for (let i=0; i<8; i++) await Promise.resolve(); };
function harness({ bots = [{ id: 'general', kind: 'general' }], rows = machines, error = null } = {}) {
  const f = fakeRoot(), calls = [], nav = [], data = { rows, bots, error };
  const api = async (path, options) => {
    calls.push([path, options]);
    assert.ok(['/api/runners', '/api/bots'].includes(path), path);
    if (path === '/api/runners') { if (data.error) throw data.error; return { machines: data.rows }; }
    return { bots: data.bots };
  };
  const ui = compute.mount({ root: f.root, api, e, me: { name: 'Maya', workspace: 'Northwind' }, go: hash => nav.push(hash) });
  return { ...f, calls, nav, data, ui };
}

test('readiness is exactly enabled plus owner_attested, never verified or last-used time', () => {
  assert.equal(compute.machineState({ enabled: true, binding_state: 'owner_attested', verified: false }), 'ready');
  assert.equal(compute.machineState({ enabled: true, binding_state: 'unbound', verified: true, last_seen: 'now' }), 'waiting');
  assert.equal(compute.machineState({ enabled: false, binding_state: 'owner_attested' }), 'off');
});

test('real screen: counts, selection by click/keyboard, tab arrows, refresh and cleanup', async () => {
  const f = harness(); await flush();
  assert.match(f.root.html, /1 of 3 machines ready/);
  const map = f.regions.get('field').innerHTML;
  for (const word of ['Commonplace', 'Build box', 'Studio machine', 'Old tower', 'Ready', 'Waiting for attestation', 'Off']) assert.ok(map.includes(word));
  assert.doesNotMatch(map, /Revoked machine|online|agents working|proxy|CDN/i);
  assert.match(f.regions.get('detail').innerHTML, /Build box/);
  f.click({ computeTab: 'Config' });
  for (const word of ['atlas', 'project', 'approval-required', 'Attested by the owner, not verified']) assert.ok(f.regions.get('detail').innerHTML.includes(word));
  f.key({ computeTab: 'Config' }, 'ArrowRight');
  assert.match(f.regions.get('detail').innerHTML, /data-compute-tab="Agents"[^>]*aria-selected="true"/);
  assert.match(f.regions.get('detail').innerHTML, /Running work is not reported/);
  assert.ok(f.key({ computeNode: 'studio' }, ' ').prevented);
  assert.match(f.regions.get('detail').innerHTML, /Studio machine/);
  await f.ui.refresh({ me: { name: 'Maya' } });
  assert.match(f.regions.get('detail').innerHTML, /Studio machine/);
  assert.match(f.regions.get('detail').innerHTML, /data-compute-tab="Agents"[^>]*aria-selected="true"/);
  f.key({ computeTab: 'Agents' }, 'End');
  assert.match(f.regions.get('detail').innerHTML, /No sites are recorded/);
  f.key({ computeTab: 'Sites' }, 'ArrowRight');
  assert.match(f.regions.get('detail').innerHTML, /No physical details/);
  f.key({ computeNode: 'old' }, 'Enter');
  assert.match(f.regions.get('detail').innerHTML, /Old tower/);
  f.click({ computeNode: 'build' });
  f.data.rows = machines.filter(m => m.id !== 'build');
  await f.ui.refresh();
  assert.match(f.regions.get('detail').innerHTML, /Studio machine/);
  assert.equal(f.header.hidden, true); assert.equal(f.phoneBar.hidden, true); assert.equal(f.phoneTabs.style.position, 'fixed');
  f.ui.stop(); f.ui.stop();
  assert.equal(f.handlers.size, 0); assert.equal(f.header.hidden, false); assert.equal(f.classes.size, 0);
  assert.ok(f.calls.at(-1)[1].signal.aborted); assert.equal(f.phoneBar.hidden, false); assert.equal(f.phoneTabs.style.position, '');
});

test('a refusal is a plain owner-only sentence with no map or raw error', async () => {
  const f = harness({ error: { status: 403, message: 'internal detail' } }); await flush();
  assert.match(f.root.html, /Only workspace owners can see the machines/);
  assert.doesNotMatch(f.root.html, /<svg|internal detail|role="alert"/);
  assert.equal(f.calls.length, 1); f.ui.stop();
});

test('empty workspace keeps just the hub, and unavailable general bot hides both handoffs', async () => {
  const f = harness({ rows: [], bots: [] }); await flush();
  assert.equal((f.regions.get('field').innerHTML.match(/data-compute-node=/g) || []).length, 1);
  assert.match(f.root.html, /No machines yet/);
  assert.match(f.regions.get('actions').innerHTML, /href="#\/admin">Add machine/);
  assert.doesNotMatch(f.regions.get('actions').innerHTML, /Add site|Let a bot/);
  f.ui.stop();
});

test('one-click handoffs store the draft and navigate; never send a message', async () => {
  const previous = globalThis.sessionStorage, saved = [];
  globalThis.sessionStorage = { setItem: (key, value) => saved.push([key, JSON.parse(value)]) };
  try {
    const f = harness(); await flush();
    f.click({ computeAction: 'site' }); f.click({ computeAction: 'map' });
    assert.deepEqual(saved, [['cmmn.deck.draft', { bot: 'general', text: 'Add a site to my map.' }], ['cmmn.deck.draft', { bot: 'general', text: 'Map my machines and sites.' }]]);
    assert.deepEqual(f.nav, ['#/bots/general', '#/bots/general']);
    assert.deepEqual(f.calls.map(c => c[0]), ['/api/runners', '/api/bots']);
    f.ui.stop();
  } finally { globalThis.sessionStorage = previous; }
});

test('escaped server strings stay inert in SVG and configuration', async () => {
  const attack = '<img src=x onerror="alert(1)">';
  const f = harness({ rows: [{ ...machines[0], id: attack, label: attack, projects: [attack], topics: [attack], wings: [attack], max_runtime_mode: attack }] }); await flush();
  f.click({ computeTab: 'Config' });
  for (const html of [f.regions.get('field').innerHTML, f.regions.get('detail').innerHTML]) {
    assert.doesNotMatch(html, /<img|aria-label="<|<style|style="/);
    assert.match(html, /&lt;img/);
  }
  f.ui.stop();
});

test('late refresh and late stopped responses cannot replace the current screen', async () => {
  const f = fakeRoot(), pending = [];
  const api = path => path === '/api/bots' ? Promise.resolve({ bots: [] }) : new Promise(resolve => pending.push(resolve));
  const ui = compute.mount({ root: f.root, api, e });
  const next = ui.refresh();
  pending[1]({ machines: [machines[1]] }); await next;
  pending[0]({ machines }); await flush();
  assert.match(f.root.html, /0 of 1 machines ready/);
  const last = ui.refresh(); ui.stop(); pending[2]({ machines }); await last;
  assert.equal(f.root.html, '');
});

test('transient failures keep the map, while a later refusal clears machine data', async () => {
  const f = harness(); await flush();
  f.data.error = new Error('network'); await f.ui.refresh();
  assert.match(f.regions.get('field').innerHTML, /Build box/);
  assert.match(f.regions.get('notice').textContent, /could not be loaded/);
  f.data.error = { status: 403 }; await f.ui.refresh();
  assert.doesNotMatch(f.root.html, /Build box|data-compute-region="field"/);
  f.data.error = null; await f.ui.refresh();
  assert.match(f.root.html, /1 of 3 machines ready/); f.ui.stop();
});


test('one constructed stylesheet, all selectors scoped to the mounted root', async () => {
  const previous = globalThis.CSSStyleSheet, sheets = [];
  globalThis.CSSStyleSheet = class { replaceSync(css) { this.css = css; sheets.push(this); } };
  try {
    const a = harness(), b = harness(); await flush();
    assert.equal(sheets.length, 1);
    assert.equal(a.doc.adoptedStyleSheets[0], b.doc.adoptedStyleSheets[0]);
    const rules = sheets[0].css.replace(/\/\*[\s\S]*?\*\//g, '').split('\n').filter(line => line.includes('{') && !line.startsWith('@media'));
    for (const rule of rules) for (const selector of rule.split('{')[0].split(',')) assert.match(selector.trim(), /^\.deck-compute(?:[ #.]|$)/);
    a.ui.stop(); b.ui.stop();
  } finally { globalThis.CSSStyleSheet = previous; }
});

test('failed bot discovery leaves the real map and Add machine available', async () => {
  const f = fakeRoot();
  const ui = compute.mount({ root: f.root, e, api: async path => { if (path === '/api/bots') throw new Error('network'); return { machines }; } });
  await flush(); assert.match(f.regions.get('field').innerHTML, /Build box/);
  assert.doesNotMatch(f.regions.get('actions').innerHTML, /Add site|Let a bot/);
  assert.match(f.regions.get('actions').innerHTML, /Add machine/); ui.stop();
});
