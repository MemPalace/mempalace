import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
const file = new URL('../commonplace/web/static/needs.js', import.meta.url);
assert.ok(existsSync(file), 'Needs module missing');
const mod = await import(file);
for (const name of ['needsItems', 'renderNeeds', 'renderNeedDetail', 'holdState', 'createNeeds']) {
  assert.equal(typeof mod[name], 'function', `${name} missing`);
}
const { setPeople } = await import('../commonplace/web/static/api.js');
setPeople({ 'user:maya': 'Maya', 'conn:iris': 'Iris' });
const { needsItems, renderNeeds, renderNeedDetail, holdState, createNeeds } = mod;
const now = Date.parse('2026-10-01T14:21:12Z');
const board = { slug: 'atlas', name: 'Atlas', coordinator: 'conn:lead' };
const proposal = { number: 21, board: 'atlas', title: 'Send the beta?', status: 'backlog', proposal_status: 'pending', version: 3, actions: ['approve', 'reject'], body: '\n  Exact <body> & “words”\r\n\ttrailing  ', events: [{ kind: 'add', actor: 'conn:lead' }, { kind: 'ask', actor: 'conn:wrong', context: 'Wrong context', options: ['Yes'] }], child_cards: [{ number: 19, title: 'Check <email>', status: 'done' }] };
const decision = { number: 22, board: 'harbor', title: 'Choose a headline', status: 'review', version: 8, ask: 'Which one?', ask_options: ['A & B', 'C'], actions: ['answer'], events: [{ kind: 'ask', actor: 'user:maya', context: 'Exact ask\r\n  words', options: ['A & B', 'C'] }] };
const review = { number: 23, board: 'atlas', title: 'Review the screen', status: 'review', review_kind: 'completion', actions: ['accept', 'answer'] };
const needs = { backlog_from_agents: [proposal], asks: [decision], done_review: [review], ready_to_merge: [review] };
const waiting = { id: 'hold-one', card: 21, kind: 'approve', state: 'waiting', release_at: '2026-10-01T14:22:00Z', created_at: '2026-10-01T14:21:00Z' };
const decode = (s) => s.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&quot;', '"').replaceAll('&#39;', "'").replaceAll('&#13;', '\r').replaceAll('&amp;', '&');
const visible = (html) => decode(html.replace(/<[^>]*>/g, ''));
const context = (html) => { const match = html.match(/<p class="quote" data-context-text>([\s\S]*?)<\/p>/); assert.ok(match, 'Context missing'); return decode(match[1]); };
function element() {
  const handlers = new Map(), controls = new Map();
  const node = { innerHTML: '', handlers, controls, contains: () => true,
    addEventListener: (type, fn) => handlers.set(type, fn), removeEventListener: (type) => handlers.delete(type),
    querySelector: (selector) => controls.get(selector) || null, querySelectorAll: () => [] };
  for (const selector of ['[data-needs-title]', '[data-needs-cancel]', '[data-needs-approve]', '[data-needs-note]']) controls.set(selector, { value: '', focus() { node.focused = selector; } });
  return node;
}
function setup({ api: customApi, onAction, onAsk } = {}) {
  const mount = element(), detailMount = element(), requests = [], timers = new Map(), events = new Map();
  let holds = [], clock = now;
  const doc = { hidden: false, addEventListener: (k, fn) => events.set(k, fn), removeEventListener: (k) => events.delete(k) };
  const api = async (path, options = {}) => {
    requests.push({ path, ...options });
    if (customApi) { const answer = customApi(path, options); if (answer !== undefined) return answer; }
    if (path === '/api/holds' && options.method === 'POST') { holds = [{ ...waiting, card: options.body.card, kind: options.body.kind }]; return { id: waiting.id, release_at: waiting.release_at }; }
    if (path === '/api/holds') return { holds };
    if (path.endsWith('/stop')) { holds = holds.map((h) => ({ ...h, state: 'stopped' })); return { state: 'stopped' }; }
    if (path === '/api/cards/21') { const { actions, ...card } = proposal; return { ...card, can_approve: true }; }
    if (path === '/api/cards/22') { const { actions, ...card } = decision; return card; }
    if (path === '/api/cards/23') return review;
    return {};
  };
  const ui = createNeeds({ mount, detailMount, api, onAction, onAsk, now: () => clock, document: doc,
    setInterval: (fn) => { timers.set(1, fn); return 1; }, clearInterval: (id) => timers.delete(id) });
  for (const name of ['show', 'select', 'setFilter', 'approve', 'confirm', 'decline', 'answer', 'stopHold', 'ask', 'refreshHolds', 'setChatOpen', 'stop']) assert.equal(typeof ui[name], 'function', `${name} missing`);
  return { ui, mount, detailMount, requests, timers, events, doc, setHolds: (value) => { holds = value; }, setTime: (value) => { clock = value; } };
}
const opts = { boards: [board], now };
const cases = {
  async approve() {
    const s = setup(); await s.ui.show({ needs, boards: [board], selected: 21 });
    s.detailMount.controls.get('[data-needs-note]').value = '  Send tomorrow\n';
    assert.ok(s.detailMount.innerHTML.includes('data-needs-approve')); s.ui.approve(); assert.equal(s.requests.filter((r) => r.method === 'POST').length, 0);
    assert.ok(s.detailMount.innerHTML.includes('Yes, approve')); assert.ok(s.detailMount.innerHTML.includes('60 seconds after you confirm'));
    await Promise.all([s.ui.confirm(), s.ui.confirm()]);
    const posts = s.requests.filter((r) => r.method === 'POST'); assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], { path: '/api/holds', method: 'POST', body: { card: 21, kind: 'approve', version: 3, reason: '  Send tomorrow\n' } });
    assert.equal(s.requests.filter((r) => r.path.includes('/approve')).length, 0);
    assert.ok(s.mount.innerHTML.includes('Approved, not sent yet'));
    assert.ok(!s.detailMount.innerHTML.includes('Yes, approve')); s.ui.stop();
  },
  context() {
    let html = renderNeedDetail(proposal, { board, now });
    assert.ok(html.includes('Context (exact words)')); assert.equal(context(html), proposal.body);
    assert.ok(html.includes('— Atlas orchestrator')); assert.ok(!html.includes('Wrong context'));
    html = renderNeedDetail(decision, { board, now }); assert.equal(context(html), decision.events[0].context); assert.ok(html.includes('— Maya'));
    const agent = renderNeedDetail({ ...proposal, events: [{ kind: 'add', actor: 'conn:iris' }] }, { board }); assert.ok(agent.includes('— Iris (agent)'));
    assert.ok(!renderNeedDetail({ ...proposal, events: [] }, { board }).includes('— —'));
    assert.ok(!html.includes('Goes outside this workspace'));
  },
  async filters() {
    assert.equal(needsItems(needs).length, 3);
    assert.deepEqual(needsItems(needs, 'approvals').map((i) => i.card.number), [21]);
    assert.deepEqual(needsItems(needs, 'decisions').map((i) => i.card.number), [22]);
    assert.deepEqual(needsItems(needs, 'reviews').map((i) => i.card.number), [23]);
    const html = renderNeeds({ needs, filter: 'approvals', ...opts }); assert.ok(html.includes('aria-pressed="true"')); assert.ok(!html.includes(decision.title));
    const s = setup(); await s.ui.show({ needs, boards: [board] }); await s.ui.setFilter('approvals'); assert.ok(!s.mount.innerHTML.includes(decision.title)); s.ui.stop();
    assert.ok(renderNeeds({ needs: {}, ...opts }).includes('Nothing needs you right now'));
  },
  waiting() {
    const state = holdState(waiting, now); assert.equal(state.seconds, 48); assert.equal(state.canStop, true);
    const html = renderNeeds({ needs, holds: [waiting], ...opts }); assert.ok(visible(html).includes('Stop · 0:48')); assert.ok(html.includes('nothing has been sent'));
    const detail = renderNeedDetail(proposal, { board, hold: waiting, now }); assert.ok(visible(detail).includes('Stop · 0:48')); assert.ok(!detail.includes('data-needs-approve'));
    assert.equal(holdState(waiting, now + 47001).seconds, 1);
  },
  settling() {
    for (const hold of [{ ...waiting, state: 'settling' }, waiting]) {
      const html = renderNeedDetail(proposal, { board, hold, now: now + 48000 }); assert.ok(html.includes('Sending now…')); assert.ok(!html.includes('data-needs-stop=')); assert.ok(!html.includes('Released to the orchestrator'));
    }
    assert.equal(holdState({ ...waiting, state: 'settling' }, now).canStop, false);
  },
  async stopped() {
    const s = setup(); s.setHolds([waiting]); await s.ui.show({ needs, boards: [board], selected: 21 }); await s.ui.stopHold(waiting.id);
    const posts = s.requests.filter((r) => r.method === 'POST'); assert.deepEqual(posts, [{ path: '/api/holds/hold-one/stop', method: 'POST', body: {} }]);
    assert.ok(s.detailMount.innerHTML.includes('Stopped. Nothing was sent.')); assert.ok(s.detailMount.innerHTML.includes('data-needs-approve'));
    s.ui.stop();
  },
  async too_late() {
    const s = setup({ api: (path) => { if (path.endsWith('/stop')) throw Object.assign(new Error('too_late'), { status: 409, payload: { error: 'too_late', released_at: waiting.release_at } }); } });
    s.setHolds([waiting]); await s.ui.show({ needs, boards: [board], selected: 21 }); await s.ui.stopHold(waiting.id);
    assert.ok(s.detailMount.innerHTML.includes("this can't be stopped here anymore")); assert.ok(s.detailMount.innerHTML.includes('Ask the orchestrator'));
    assert.ok(!s.detailMount.innerHTML.includes('data-needs-stop=')); assert.ok(!s.detailMount.innerHTML.includes('Nothing was sent')); s.ui.stop();
    const released = renderNeedDetail(proposal, { board, hold: { ...waiting, state: 'released' }, now }); assert.ok(released.includes('Released to the orchestrator')); assert.ok(!released.includes('reported “'));
  },
  async decline() {
    const actions = [], s = setup({ onAction: (...args) => actions.push(args) }); await s.ui.show({ needs, selected: 21 });
    await s.ui.decline(); assert.equal(actions.length, 1); assert.equal(actions[0][0], 'need-reject'); assert.equal(actions[0][1].number, 21);
    assert.equal(s.requests.filter((r) => r.method === 'POST').length, 0); s.ui.stop();
    const direct = setup(); await direct.ui.show({ needs, selected: 21 });
    let prevented = false, stopped = false;
    const button = { disabled: false, dataset: {}, hasAttribute: () => false };
    await direct.detailMount.handlers.get('click')({ target: { closest: () => button }, preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } });
    assert.ok(prevented && stopped, 'Decline must not bubble to the shell a second time');
    assert.deepEqual(direct.requests.filter((r) => r.method === 'POST'), [{ path: '/api/cards/21/approve', method: 'POST', body: { approve: false, version: 3 } }]); direct.ui.stop();
  },
  linked() {
    const html = renderNeedDetail({ ...proposal, linked: [{ number: 99, title: 'Wrong source' }] }, { board });
    assert.ok(html.includes('Linked cards')); assert.ok(html.includes('href="#/card/19"')); assert.ok(html.includes('A-19')); assert.ok(html.includes('Check &lt;email&gt;'));
    assert.ok(!html.includes('Wrong source')); assert.ok(!renderNeedDetail({ ...proposal, child_cards: [] }, { board }).includes('Linked cards'));
  },
  async ask() {
    const asks = [], s = setup({ onAsk: (...args) => asks.push(args) }); await s.ui.show({ needs, boards: [board], selected: 21 });
    const key = { key: 'r', target: { closest: () => null }, preventDefault() {} }; s.mount.handlers.get('keydown')(key);
    assert.equal(asks.length, 1); assert.equal(asks[0][0].number, 21); assert.equal(asks[0][1].board, 'atlas'); assert.equal(asks[0][1].text, proposal.body);
    assert.ok(s.detailMount.innerHTML.includes('aria-expanded="true"'));
    s.ui.setChatOpen(false); assert.ok(s.detailMount.innerHTML.includes('aria-expanded="false"'));
    s.detailMount.handlers.get('keydown')({ ...key, target: { closest: () => ({}) } }); assert.equal(asks.length, 1);
    assert.equal(s.requests.filter((r) => r.method === 'POST').length, 0); s.ui.stop();
  },
  async reload() {
    const s = setup(); s.setHolds([waiting]); await s.ui.show({ needs: {}, boards: [board], selected: 21 });
    assert.ok(s.requests.some((r) => r.path === '/api/holds')); assert.ok(s.requests.some((r) => r.path === '/api/cards/21'));
    assert.ok(s.mount.innerHTML.includes(proposal.title)); assert.ok(visible(s.detailMount.innerHTML).includes('Stop · 0:48'));
    s.setTime(now + 48000); await s.timers.get(1)(); assert.ok(s.detailMount.innerHTML.includes('Sending now…'));
    s.setHolds([{ ...waiting, state: 'released' }]); await s.ui.refreshHolds(); assert.ok(s.detailMount.innerHTML.includes('Released to the orchestrator'));
    s.doc.hidden = true; const count = s.requests.length; await s.timers.get(1)(); assert.equal(s.requests.length, count);
    s.ui.stop(); assert.equal(s.timers.size, 0); assert.equal(s.events.size, 0); assert.equal(s.mount.handlers.size, 0); assert.equal(s.detailMount.handlers.size, 0);
  },
  async races() {
    let resolve;
    const pending = new Promise((r) => { resolve = r; });
    const s = setup({ api: (path) => path === '/api/cards/21' ? pending : undefined });
    const first = s.ui.show({ needs, selected: 21 }); await s.ui.select(22); resolve(proposal); await first;
    assert.ok(s.detailMount.innerHTML.includes(decision.title)); assert.ok(!s.detailMount.innerHTML.includes(proposal.title));
    const html = s.detailMount.innerHTML; s.ui.stop(); await s.ui.refreshHolds(); assert.equal(s.detailMount.innerHTML, html);
  },
  async errors() {
    const s = setup({ api: (path, options) => {
      if (path === '/api/holds' && options.method === 'POST') throw Object.assign(new Error('version conflict'), { status: 409 });
    } });
    await s.ui.show({ needs, selected: 21 }); s.ui.approve(); await s.ui.confirm();
    assert.ok(s.detailMount.innerHTML.includes('Could not confirm approval')); assert.ok(!s.detailMount.innerHTML.includes('nothing has been sent'));
    assert.equal(s.requests.filter((r) => r.method === 'POST').length, 1); s.ui.stop();
    const failed = renderNeedDetail(proposal, { board, hold: { ...waiting, state: 'failed', code: 'stale_version' } }); assert.ok(failed.includes('card changed')); assert.ok(!failed.includes('Released to the orchestrator'));
  },
  async decision() {
    const s = setup(); await s.ui.show({ needs, selected: 22 });
    assert.ok(s.detailMount.innerHTML.includes('A &amp; B')); assert.ok(!s.detailMount.innerHTML.includes('data-needs-approve'));
    await s.ui.answer('A & B', '  Exact reply\n');
    // Only Approve waits 60 seconds: a decision goes straight to the card's decide route, once.
    assert.deepEqual(s.requests.filter((r) => r.method === 'POST'), [{ path: '/api/cards/22/decide', method: 'POST', body: { choice: 'A & B', version: 8, reply: '  Exact reply\n' } }]);
    assert.ok(!s.mount.innerHTML.includes('Approved, not sent yet')); assert.ok(!s.mount.innerHTML.includes(decision.title)); s.ui.stop();
    assert.ok(!renderNeeds({ needs, holds: [{ ...waiting, card: 22, kind: 'decide' }], ...opts }).includes('Approved, not sent yet'));
    const failing = setup({ api: (path) => { if (path.endsWith('/decide')) throw Object.assign(new Error('card changed'), { status: 409 }); } });
    await failing.ui.show({ needs, selected: 22 }); await failing.ui.answer('C');
    assert.ok(failing.detailMount.innerHTML.includes('Could not send your answer')); assert.equal(failing.requests.filter((r) => r.path === '/api/holds' && r.method === 'POST').length, 0); failing.ui.stop();
    const noOption = setup(); await noOption.ui.show({ needs, selected: 22 }); await noOption.ui.answer('unknown'); assert.equal(noOption.requests.filter((r) => r.method === 'POST').length, 0); noOption.ui.stop();
    // A plan the server lets this viewer approve gets the direct Approve, never the held one.
    const plan = renderNeedDetail({ ...proposal, proposal_status: null, labels: ['plan'] }, { board });
    assert.ok(plan.includes('data-needs-approve="plan"')); assert.ok(!plan.includes('Approve and send')); assert.ok(!plan.includes('data-needs-confirm'));
    assert.ok(!renderNeedDetail({ ...proposal, proposal_status: null, labels: ['plan'], actions: [] }, { board }).includes('data-needs-approve'));
  },
  async focus() {
    const s = setup(); await s.ui.show({ needs, selected: 21 }); assert.equal(s.detailMount.focused, '[data-needs-title]');
    s.detailMount.controls.get('[data-needs-note]').value = 'Draft <note>';
    s.ui.approve(); assert.equal(s.detailMount.focused, '[data-needs-cancel]'); assert.ok(s.detailMount.innerHTML.includes('Draft &lt;note&gt;'));
    s.detailMount.handlers.get('keydown')({ key: 'Escape', target: { closest: () => null }, preventDefault() {}, stopPropagation() {} });
    const phone = renderNeedDetail(proposal, { board, phone: true, confirm: true });
    assert.ok(phone.includes('<dialog class="confirm" role="alertdialog" aria-modal="true"'));
    assert.ok(phone.includes('aria-describedby="needs-confirm-description"'));
    assert.equal(s.detailMount.focused, '[data-needs-approve]'); assert.ok(!s.detailMount.innerHTML.includes('Yes, approve')); s.ui.stop();
  },
  restricted() {
    // The owner approves proposals from restricted agents: the server's actions decide, not a client flag.
    const html = renderNeedDetail({ ...proposal, restricted: true, restricted_origin: 1 }, { board, now });
    assert.ok(html.includes('data-needs-approve')); assert.ok(html.includes('data-action="need-reject"'));
    assert.ok(!renderNeedDetail({ ...proposal, actions: [] }, { board, now }).includes('data-needs-approve'));
  },
  async zkey() {
    const key = { key: 'z', target: { closest: () => null }, preventDefault() {}, stopPropagation() {} };
    const s = setup(); s.setHolds([waiting]); await s.ui.show({ needs, boards: [board], selected: 21 });
    s.detailMount.handlers.get('keydown')(key); await new Promise((r) => setTimeout(r, 10));
    assert.deepEqual(s.requests.filter((r) => r.method === 'POST'), [{ path: '/api/holds/hold-one/stop', method: 'POST', body: {} }]); s.ui.stop();
    const idle = setup(); await idle.ui.show({ needs, selected: 21 });
    idle.detailMount.handlers.get('keydown')(key); await new Promise((r) => setTimeout(r, 10));
    assert.equal(idle.requests.filter((r) => r.method === 'POST').length, 0); idle.ui.stop();
  },
  async quiet() {
    // The countdown is rewritten every second; only the state copy is announced.
    assert.match(renderNeedDetail(proposal, { board, hold: waiting, now }), /data-hold-countdown="hold-one" aria-hidden="true"/);
    assert.ok(!renderNeedDetail({ ...decision, events: [{ kind: 'ask', actor: 'user:maya', context: '' }] }, { board }).includes('Context (exact words)'));
    let stored = false;
    const s = setup({ api: (path, options) => {
      if (path === '/api/holds' && options.method === 'POST') { stored = true; return undefined; }
      if (path === '/api/holds' && stored) throw new Error('timeout');
    } });
    await s.ui.show({ needs, selected: 21 }); s.ui.approve(); await s.ui.confirm();
    assert.ok(!s.detailMount.innerHTML.includes('Could not confirm approval')); assert.ok(s.detailMount.innerHTML.includes('nothing has been sent'));
    assert.equal(s.requests.filter((r) => r.method === 'POST').length, 1); s.ui.stop();
  },
  async plan_direct() {
    // A plan is approved or rejected at once on the card's approve route; no hold, no 60 seconds.
    const plan = { number: 24, board: 'atlas', title: 'Plan the launch', status: 'review', version: 5, labels: ['plan'], actions: ['approve', 'reject'] };
    const planNeeds = { plans: [plan] }, { actions, ...loaded } = plan;
    const run = async (attr, body) => {
      const s = setup({ api: (path) => path === '/api/cards/24' ? loaded : undefined });
      await s.ui.show({ needs: planNeeds, boards: [board], selected: 24 });
      const html = s.detailMount.innerHTML;
      assert.ok(html.includes('data-needs-approve="plan"'), 'plan Approve missing'); assert.ok(html.includes('>Reject</button>'), 'plan Reject missing');
      assert.ok(!html.includes('Approve and send') && !html.includes('60 seconds'), 'a plan must not be held');
      const button = { dataset: attr === 'approve' ? { needsApprove: 'plan' } : { action: 'need-reject' }, disabled: false,
        hasAttribute: (name) => name === (attr === 'approve' ? 'data-needs-approve' : 'data-action') };
      const want = attr === 'approve' ? '[data-needs-approve]' : '[data-action=need-reject]';
      await s.detailMount.handlers.get('click')({ target: { closest: (sel) => sel.split(',').includes(want) ? button : null }, preventDefault() {}, stopPropagation() {} });
      assert.deepEqual(s.requests.filter((r) => r.method === 'POST'), [{ path: '/api/cards/24/approve', method: 'POST', body }]);
      assert.ok(!s.mount.innerHTML.includes(plan.title), 'the plan must leave the queue'); s.ui.stop();
    };
    await run('approve', { approve: true, version: 5 });
    await run('reject', { approve: false, version: 5 });
    // A proposal keeps the held Approve: no direct plan button.
    assert.ok(!renderNeedDetail(proposal, { board, now }).includes('data-needs-approve="plan"'));
  },
  async shell_actions() {
    // Accept and stale-card recovery are drawn for the shell's existing need-* handlers.
    const stale = { number: 25, board: 'atlas', title: 'Stuck card', status: 'doing', version: 7, need: 'you', actions: ['requeue', 'reassign', 'park'] };
    const html = renderNeedDetail(stale, { board, kind: 'decision' });
    for (const [action, label] of [['requeue', 'Requeue'], ['reassign', 'Reassign'], ['park', 'Park']]) {
      assert.ok(html.includes(`data-action="need-${action}" data-card="25" data-version="7">${label}</button>`), `${label} missing`);
    }
    assert.ok(html.includes('<a class="btn" href="#/card/25">Answer</a>'), 'Answer link missing');
    assert.ok(!html.includes('data-needs-approve') && !html.includes('need-accept'));
    const done = renderNeedDetail({ ...review, version: 4 }, { board, kind: 'review' });
    assert.ok(done.includes('data-action="need-accept" data-card="23" data-version="4">Accept</button>'), 'Accept missing');
    assert.ok(!renderNeedDetail({ ...review, version: 4, actions: ['answer'] }, { board, kind: 'review' }).includes('need-accept'));
    assert.ok(!renderNeedDetail(proposal, { board, now }).includes('need-accept'));
    // The component leaves these clicks to the shell: no preventDefault, no stopPropagation.
    const s = setup(); await s.ui.show({ needs, boards: [board], selected: 23 });
    for (const action of ['need-accept', 'need-requeue', 'need-reassign', 'need-park']) {
      const event = { target: { closest: (sel) => sel.split(',').some((part) => part === `[data-action=${action}]`) ? { dataset: { action } } : null }, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
      await s.detailMount.handlers.get('click')(event);
      assert.ok(!event.prevented && !event.stopped, `${action} must reach the shell`);
    }
    assert.equal(s.requests.filter((r) => r.method === 'POST').length, 0); s.ui.stop();
  },
  async answer_error_stale() {
    // A failure for card A must not be drawn under card B, selected while A's request was in flight.
    const failing = [];
    const fail = () => new Promise((_, reject) => failing.push(() => reject(Object.assign(new Error('card changed'), { status: 409 }))));
    const run = async (from, to, start, path, copy) => {
      const s = setup({ api: (p, options) => options.method === 'POST' && p === path ? fail() : undefined });
      await s.ui.show({ needs, boards: [board], selected: from });
      const pending = start(s); await new Promise((r) => setTimeout(r, 0));
      assert.equal(failing.length, 1, `${path} was not sent`);
      await s.ui.select(to); failing.pop()(); await pending;
      assert.ok(s.detailMount.innerHTML.includes(to === 21 ? proposal.title : decision.title), 'the newly selected card must stay shown');
      assert.ok(!s.detailMount.innerHTML.includes(copy), `${copy} drawn under the wrong card`); s.ui.stop();
    };
    await run(22, 21, (s) => s.ui.answer('C'), '/api/cards/22/decide', 'Could not send your answer');
    await run(21, 22, (s) => s.ui.decline(), '/api/cards/21/approve', 'Could not decline');
    await run(21, 22, (s) => { s.ui.approve(); return s.ui.confirm(); }, '/api/holds', 'Could not confirm approval');
  },
  async answer_error_same_card() {
    // A failure for the card still on screen is drawn, even after that card was re-selected or the list refreshed.
    const failing = [];
    const fail = () => new Promise((_, reject) => failing.push(() => reject(Object.assign(new Error('card changed'), { status: 409 }))));
    const run = async (name, number, start, again, path, copy) => {
      const s = setup({ api: (p, options) => options.method === 'POST' && p === path ? fail() : undefined });
      await s.ui.show({ needs, boards: [board], selected: number });
      const pending = start(s); await new Promise((r) => setTimeout(r, 0));
      assert.equal(failing.length, 1, `${name}: ${path} was not sent`);
      await again(s, number); failing.pop()(); await pending;
      assert.ok(s.detailMount.innerHTML.includes(number === 21 ? proposal.title : decision.title), `${name}: card ${number} must stay shown`);
      assert.ok(s.detailMount.innerHTML.includes(copy), `${name}: the failure for the card still shown must be drawn`); s.ui.stop();
    };
    const reselect = (s, number) => s.ui.select(number);
    const refresh = (s, number) => s.ui.show({ needs, boards: [board], selected: number });
    await run('answer reselect', 22, (s) => s.ui.answer('C'), reselect, '/api/cards/22/decide', 'Could not send your answer');
    await run('answer refresh', 22, (s) => s.ui.answer('C'), refresh, '/api/cards/22/decide', 'Could not send your answer');
    await run('decline refresh', 21, (s) => s.ui.decline(), refresh, '/api/cards/21/approve', 'Could not decline');
    await run('hold reselect', 21, (s) => { s.ui.approve(); return s.ui.confirm(); }, reselect, '/api/holds', 'Could not confirm approval');
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
