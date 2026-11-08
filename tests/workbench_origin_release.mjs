import assert from 'node:assert/strict';
import { needsItems, renderNeedDetail, createNeeds } from '../commonplace/web/static/needs.js';
const which = process.argv[2];
const configuration = which === 'configuration';
const card = { number: 7, board: 'atlas', title: 'Synthetic origin', status: 'doing', version: 4,
  proposal_status: which === 'origin-pending' ? 'pending' : 'approved',
  approval_kind: configuration ? 'origin_configuration' : 'origin',
  actions: configuration ? ['configure_coordinator'] : ['approve'], can_approve: !configuration };
const needs = { approvals: [card] };
assert.equal(needsItems(needs)[0].kind, 'approval');
assert.equal(needsItems(needs, 'approvals').length, 1);
let html = renderNeedDetail(card);
assert.ok(html.includes(configuration ? 'Coordinator configuration' : 'Origin release'));
assert.ok(!html.includes('#/decide/') && !html.includes('60 seconds') && !html.includes('Approve and send'));
if (configuration) {
  assert.ok(html.includes('data-action="coordinator" data-board="atlas"'));
  assert.ok(html.includes('Configure coordinator'));
  assert.ok(!html.includes('data-needs-approve'));
} else {
  assert.ok(html.includes('Release origin') && html.includes('data-needs-note'));
  const requests = [], handlers = new Map(), note = { value: '' };
  const node = () => ({ innerHTML: '', addEventListener: (k, f) => handlers.set(k, f), removeEventListener() {}, contains: () => true,
    querySelector: (s) => s === '[data-needs-note]' ? note : null, querySelectorAll: () => [] });
  const mount = node(), detailMount = node();
  let refreshed = 0;
  const ui = createNeeds({ mount, detailMount, api: async (path, opts = {}) => {
    requests.push({ path, ...opts });
    return path === '/api/holds' ? { holds: [] } : path === '/api/cards/7' ? card : {};
  }, onChange: () => { refreshed++; }, document: { addEventListener() {}, removeEventListener() {} }, setInterval: () => 1, clearInterval() {} });
  await ui.show({ needs, selected: 7 });
  const button = { dataset: { needsApprove: 'origin' }, hasAttribute: (k) => k === 'data-needs-approve' };
  const click = () => handlers.get('click')({ target: { closest: () => button }, preventDefault() {}, stopPropagation() {} });
  await click();
  assert.equal(requests.filter(r => r.method === 'POST').length, 0, 'empty reason must not submit');
  note.value = 'Verified independent origin';
  await click();
  assert.deepEqual(requests.filter(r => r.method === 'POST'), [{ path: '/api/cards/7/approve', method: 'POST', body: { approve: true, version: 4, reason: note.value } }]);
  assert.equal(refreshed, 1);
  ui.stop();
}
