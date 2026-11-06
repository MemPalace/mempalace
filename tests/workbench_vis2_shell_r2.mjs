// Phone headings on every non-board page, the Memory subtitle, two-per-row memory filters,
// and Hand off beside Edit (visual recheck N1, m13, N4, N2).
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), 'utf8');
const app = await read('app.js');
function slice(start, end) {
  const at = app.indexOf(start), stop = app.indexOf(end, at);
  assert.ok(at >= 0 && stop > at, `Missing slice: ${start} → ${end}`);
  return app.slice(at, stop);
}
// One case by name (the pytest wrapper), or every case when run bare (node --test).
async function run(which) {
if (which === 'route-heading') {
  const attrs = {}, node = () => ({ innerHTML: '', outerHTML: '', setAttribute(n, v) { attrs[n] = v; } });
  const nodes = { '#board-nav': node(), '#phone-bar': node(), '#phone-tabs': node(), '.shell': node() };
  const state = { me: { people: {} }, boards: [{ slug: 'alpha', name: 'Alpha' }], needs: {}, base: '#/' };
  const context = { root: { querySelector: (s) => nodes[s] || null }, state, needsCards: () => [], currentBoard: () => state.boards[0],
    renderRail: () => '<nav class="rail">', renderPhoneBar: () => '', renderPhoneTabs: () => '' };
  runInNewContext(slice('function navigation()', 'function showError'), context);
  const view = (base) => { state.base = base; runInNewContext('navigation()', context); return attrs['data-view']; };
  // workbench.css hides the heading below 700 px only for these two views.
  const css = await read('workbench.css');
  assert.match(css, /\.shell\[data-view="board"\] #title, \.shell\[data-view="chat"\] #title/);
  assert.equal(view('#/board/alpha'), 'board');
  assert.equal(view('#/board/alpha/chat'), 'chat');
  for (const base of ['#/rules', '#/console', '#/settings', '#/decide', '#/boards', '#/start']) {
    assert.ok(!['board', 'chat'].includes(view(base)), `${base} loses its heading on a phone`);
  }
  assert.equal(view('#/needs'), 'needs'); assert.equal(view('#/memory'), 'memory'); assert.equal(view('#/space/alpha'), 'space');
} else if (which === 'memory-subtitle') {
  let actions = '';
  const content = { querySelector: () => null, innerHTML: '' };
  const state = { base: '#/memory', boards: [], me: {}, screen: 1 };
  const context = { root: { querySelector: (s) => s === '#content' ? content : {} }, state, URLSearchParams, api() {}, singleKeysEnabled: () => true,
    title(_t, a) { actions = a; }, createMemory: () => ({ stop() {}, search: async () => {} }), mountWorkbenchChat() {} };
  runInNewContext(slice('async function memoryView(', '\n// The shell keeps one dock'), context);
  await runInNewContext('memoryView(1)', context);
  assert.match(actions, /Exact words from chats, cards and notes\. Never summarised\./, 'Memory subtitle missing');
} else if (which === 'memory-chips') {
  const css = await read('map.css');
  const phone = css.slice(css.lastIndexOf('@media (max-width: 699px)'));
  assert.match(phone, /\.memory-search \.filters \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/, 'phone filters are not two per row');
  assert.match(phone, /\.memory-search \.memory-status \{[^}]*grid-column: 1 \/ -1/);
} else if (which === 'handoff') {
  const d = await import(new URL('../commonplace/web/static/details.js', import.meta.url));
  assert.equal(typeof d.renderDetails, 'function');
  const html = d.renderDetails({ number: 5, board: 'atlas', title: 'T', status: 'todo', version: 1, body: 'b', events: [] }, { board: { slug: 'atlas', name: 'Atlas' } });
  // Hand off sits with Edit in the title row, never at the scroll edge under the description.
  const row = html.slice(html.indexOf('<div class="title-row">'), html.indexOf('</header>'));
  assert.match(row, /data-action="handoff"/, 'Hand off is not beside Edit');
  assert.equal((html.match(/data-action="handoff"/g) || []).length, 1);
} else {
  throw new Error(`unknown case ${which}`);
}
}
const CASES = ['route-heading', 'memory-subtitle', 'memory-chips', 'handoff'];
for (const which of process.argv[2] ? [process.argv[2]] : CASES) await run(which);
