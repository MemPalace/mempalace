// Structured decision blocks in Needs me: options side by side, answered with their label.
import assert from 'node:assert/strict';
const { decisionBlocks, decisionBlocksHTML } = await import('../commonplace/web/static/decision-blocks.js');
const { renderNeedDetail, createNeeds } = await import('../commonplace/web/static/needs.js');

const board = { slug: 'atlas', name: 'Atlas' };
const blocks = [
  { label: 'Compact <b>', detail: 'One row & a menu.', preview: '[logo]  Boards\r\n<script>alert(1)</script>' },
  { label: 'Tall', preview: '[logo]\nBoards' },
  { label: 'Keep' },
];
const card = { number: 31, board: 'atlas', title: 'Choose a header', status: 'review', version: 4, ask: 'Which header?',
  ask_options: blocks.map((b) => b.label), ask_blocks: blocks, actions: ['answer'], events: [] };
const plain = { ...card, ask_blocks: [] };
const count = (html, text) => html.split(text).length - 1;

function setup(loaded) {
  const element = () => {
    const handlers = new Map();
    return { innerHTML: '', handlers, contains: () => true, addEventListener: (type, fn) => handlers.set(type, fn),
      removeEventListener: (type) => handlers.delete(type), querySelector: () => null, querySelectorAll: () => [] };
  };
  const mount = element(), detailMount = element(), requests = [];
  const api = async (path, options = {}) => {
    requests.push({ path, ...options });
    if (path === '/api/holds') return { holds: [] };
    if (path === `/api/cards/${loaded.number}`) { const { actions, ...rest } = loaded; return rest; }
    return {};
  };
  const doc = { hidden: false, addEventListener() {}, removeEventListener() {} };
  const ui = createNeeds({ mount, detailMount, api, now: () => Date.parse('2026-10-01T14:21:12Z'), document: doc,
    setInterval: () => 1, clearInterval() {} });
  const press = (index) => {
    const button = { disabled: false, dataset: { needsChoice: String(index) }, hasAttribute: (name) => name === 'data-needs-choice' };
    return detailMount.handlers.get('click')({ target: { closest: () => button }, preventDefault() {}, stopPropagation() {} });
  };
  return { ui, detailMount, requests, press };
}

const cases = {
  render() {
    const html = renderNeedDetail(card, { board, kind: 'decision' });
    assert.equal(count(html, '<li class="dblock">'), 3, 'one block per option');
    assert.ok(html.includes('data-decision-blocks="3"'));
    // One button per option, in the offered order, and no second set in the footer.
    assert.deepEqual([...html.matchAll(/data-needs-choice="(\d)"/g)].map((m) => m[1]), ['0', '1', '2']);
    assert.equal(count(html, '<pre class="dblock-preview"'), 2); assert.equal(count(html, '<p class="dblock-detail">'), 1);
    assert.ok(html.indexOf('Which header?') < html.indexOf('class="dblocks"'), 'the question comes first');
    assert.ok(html.indexOf('class="dblocks"') < html.indexOf('<footer'), 'blocks sit in the body');
    assert.ok(renderNeedDetail(card, { board, kind: 'decision', busy: true }).includes('data-needs-choice="0" disabled'));
    // A card the viewer may not answer shows no answer buttons at all.
    assert.ok(!renderNeedDetail({ ...card, actions: [] }, { board, kind: 'decision' }).includes('data-needs-choice'));
  },
  plain() {
    const html = renderNeedDetail(plain, { board, kind: 'decision' });
    assert.ok(!html.includes('dblock'), 'a plain ask draws no blocks');
    assert.equal(count(html, 'data-needs-choice='), 3, 'plain buttons are unchanged');
    assert.ok(html.indexOf('<footer') < html.indexOf('data-needs-choice='), 'plain buttons stay in the footer');
    assert.equal(decisionBlocksHTML(plain), ''); assert.equal(decisionBlocksHTML({}), ''); assert.deepEqual(decisionBlocks(null), []);
  },
  escape() {
    const html = decisionBlocksHTML(card);
    assert.ok(!html.includes('<script>') && !html.includes('<b>'), 'option text is never markup');
    assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;') && html.includes('Compact &lt;b&gt;</button>'));
    assert.ok(html.includes('One row &amp; a menu.') && html.includes('Boards&#13;\n'), 'the preview keeps its exact text');
  },
  async answer() {
    const s = setup(card); await s.ui.show({ needs: { asks: [card] }, boards: [board], selected: 31 });
    assert.ok(s.detailMount.innerHTML.includes('class="dblocks"'));
    await s.press(1);
    assert.deepEqual(s.requests.filter((r) => r.method === 'POST'), [{ path: '/api/cards/31/decide', method: 'POST', body: { choice: 'Tall', version: 4 } }]);
    s.ui.stop();
    // An index outside the offered options answers nothing.
    const outside = setup(card); await outside.ui.show({ needs: { asks: [card] }, boards: [board], selected: 31 });
    await outside.press(7); await outside.ui.answer('Compact');
    assert.equal(outside.requests.filter((r) => r.method === 'POST').length, 0); outside.ui.stop();
  },
  stale() {
    // Blocks that do not match the offered labels are not shown; the offered labels stay answerable.
    for (const ask_blocks of [blocks.slice(0, 2), [blocks[1], blocks[0], blocks[2]], [{ label: 'Other' }, blocks[1], blocks[2]], 'x', [null, null, null]]) {
      const html = renderNeedDetail({ ...card, ask_blocks }, { board, kind: 'decision' });
      assert.ok(!html.includes('dblock')); assert.equal(count(html, 'data-needs-choice='), 3);
    }
  },
};
const name = process.argv[2];
assert.ok(cases[name], `unknown case ${name}`);
await cases[name]();
