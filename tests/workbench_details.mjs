import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const path = new URL('../commonplace/web/static/details.js', import.meta.url);
// No new module is imported until its existence has been asserted.
assert.ok(await readFile(path, 'utf8').catch(() => null), 'Details module missing');
const details = await import(path);
for (const name of ['renderDetails', 'detailsContext', 'detailsAuthor', 'originMessage', 'loadDetailsOrigin', 'createDetails']) {
  assert.equal(typeof details[name], 'function', `${name} missing`);
}
const { renderDetails, detailsContext, originMessage, loadDetailsOrigin, createDetails } = details;
const board = { name: 'Atlas', slug: 'atlas', coordinator: 'conn:lead' };
const card = { number: 21, board: 'atlas', version: 4, title: 'Queue offline edits', body: 'Original <description>\n  unchanged', status: 'review', ask: 'Which limit? & why\n  exactly?', ask_to: 'user:leon', assignee: 'conn:otto', message_id: 17,
  events: [
    { kind: 'add', actor: 'user:maya', text: 'Created', at: '2026-10-01T10:00:00Z' },
    { kind: 'ask', actor: 'conn:otto', options: ['Yes', 'No'], context: 'old context', at: '2026-10-01T11:00:00Z' },
    { kind: 'handoff', actor: 'conn:otto', options: ['Batch', 'Single'], context: 'Exact <context>\n\t  & words', at: '2026-10-01T12:40:00Z' },
    { kind: 'comment', actor: 'user:leon', text: 'Checking', at: '2026-10-01T14:00:00Z' },
  ] };
const decode = (text) => text.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&quot;', '"').replaceAll('&#39;', "'").replaceAll('&amp;', '&').replaceAll('&#13;', '\r');
const quoted = (html, attribute) => {
  const match = html.match(new RegExp(`<p class="quote" ${attribute}>([\\s\\S]*?)</p>`));
  assert.ok(match, `${attribute} missing`);
  return decode(match[1]);
};
const opts = { board, me: 'user:maya' };
const html = renderDetails(card, opts);
const cases = {
  origin() {
    const messages = [{ id: 18, text: 'wrong' }, { id: 17, actor: 'user:maya', text: 'Request <tag> & “words”\n\t  trailing  ', at: '2026-10-01T10:01:00Z' }];
    assert.equal(originMessage(card, messages), messages[1]);
    assert.equal(quoted(renderDetails(card, { ...opts, messages }), 'data-origin-text'), messages[1].text);
    assert.equal(originMessage(card, [{ id: 17, board: 'harbor', text: 'private' }]), null);
    assert.equal(originMessage({ ...card, message_id: null }, messages), null);
    assert.ok(!html.includes('data-origin-text'));
  },
  blocked() {
    const state = detailsContext(card, opts.me);
    assert.equal(state.blocked, true);
    assert.equal(state.since, card.events[2].at);
    assert.equal(quoted(html, 'data-context-text'), card.events[2].context);
    assert.ok(html.includes('Blocked since <time datetime="2026-10-01T12:40:00Z"'));
    assert.ok(html.includes('Otto') || html.includes('otto (agent)'));
    assert.ok(html.includes(`href="#/memory?q=${encodeURIComponent(card.ask)}"`));
    assert.equal(detailsContext(card, 'user:leon').needsMe, true);
    assert.equal(detailsContext(card, 'user:leon').blocked, false);
    assert.equal(detailsContext({ ...card, review_kind: 'completion' }, opts.me).blocked, false);
    assert.ok(!renderDetails({ ...card, review_kind: 'completion' }, opts).includes('Blocked since'));
    assert.ok(!renderDetails({ ...card, status: 'done' }, opts).includes('data-context-text'));
  },
  proposal() {
    const proposal = { ...card, status: 'backlog', proposal_status: 'pending', body: '\n  Proposal <body> & exact\r\n\t', events: [{ kind: 'add', actor: 'conn:builder' }, ...card.events.slice(1)] };
    const result = renderDetails(proposal, opts);
    assert.ok(result.includes('Context (exact words)'));
    assert.equal(quoted(result, 'data-context-text'), proposal.body);
    assert.ok(result.includes('— builder (agent)'));
    assert.ok(result.includes(`<h3>${proposal.title}</h3>`));
    assert.ok(!result.includes('Blocked since'));
    assert.ok(!renderDetails({ ...proposal, events: [] }, opts).includes('— —'));
    assert.equal(details.detailsAuthor('conn:lead', board), 'Atlas orchestrator');
  },
  actions() {
    for (const marker of ['data-action="edit-card"', 'data-action="panel-move"', 'data-action="handoff"', 'data-form="comment"', 'value="review"', 'value="backlog"', 'name="text"', 'data-draft="comment-21"', 'data-version="4"']) assert.ok(html.includes(marker), marker);
    for (const forbidden of ['Remind', '<dt>Due</dt>', 'Starts after', 'data-action="approve', 'data-action="reject']) assert.ok(!html.includes(forbidden), forbidden);
    assert.ok(html.includes('Original &lt;description&gt;\n  unchanged'));
    assert.ok(!html.includes('<context>'));
  },
  tabs() {
    assert.ok(html.includes('role="tablist" aria-label="Context"'));
    assert.ok(html.includes('id="tab-details" aria-selected="true"'));
    assert.ok(html.includes('role="tabpanel" aria-labelledby="tab-details"'));
    const memory = renderDetails({ ...card, linked: [{ drawer_id: 'note/a', title: 'Exact <note>' }] }, { ...opts, tab: 'memory' });
    assert.ok(memory.includes('href="#/memory?id=note%2Fa"'));
    assert.ok(memory.includes('Exact &lt;note&gt;'));
    assert.ok(memory.includes('id="tab-memory" aria-selected="true"'));
    const handlers = new Map();
    const mount = { innerHTML: '', addEventListener: (key, fn) => handlers.set(key, fn), removeEventListener: (key) => handlers.delete(key), querySelector: () => null };
    const component = createDetails({ mount });
    assert.equal(typeof component.show, 'function');
    assert.equal(typeof component.selectTab, 'function');
    assert.equal(typeof component.stop, 'function');
    component.show({ ...card, message_id: null }, opts);
    let prevented = false;
    handlers.get('keydown')({ target: { closest: () => ({}) }, key: 'ArrowRight', preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true);
    assert.ok(mount.innerHTML.includes('aria-labelledby="tab-memory"'));
    handlers.get('click')({ target: { closest: () => ({ dataset: { detailsTab: 'details' } }) } });
    assert.ok(mount.innerHTML.includes('aria-labelledby="tab-details"'));
    component.stop();
    assert.equal(handlers.size, 0);
  },
  strips() {
    const fromChat = renderDetails(card, { ...opts, strip: { source: 'the 12:41 quick reply (local model)', row: 'A-21' } });
    assert.ok(fromChat.includes('Opened from Atlas chat: the 12:41 quick reply (local model).'));
    assert.ok(fromChat.includes('data-row="A-21"'));
    assert.ok(fromChat.includes('data-back-chat'));
    assert.ok(renderDetails(card, { ...opts, strip: { kind: 'board' } }).includes('You picked A-21 on the board. The chat is one click away.'));
    assert.ok(!html.includes('Back to chat'));
  },
  refocus() {
    // Only a newly shown card moves focus to its heading; a refresh of the same card does not.
    let focused = 0;
    const title = { focus: () => { focused++; } };
    const mount = { innerHTML: '', addEventListener() {}, removeEventListener() {}, querySelector: (s) => s === '[data-details-title]' ? title : null };
    const component = createDetails({ mount });
    component.show({ ...card, message_id: null }, opts);
    assert.equal(focused, 1);
    component.show({ ...card, message_id: null, title: 'Refreshed' }, opts);
    assert.equal(focused, 1, 'A refresh of the same card moved focus');
    component.show({ ...card, number: 22, message_id: null }, opts);
    assert.equal(focused, 2);
    component.stop();
  },
  async loading() {
    const calls = [];
    const api = async (url) => { calls.push(url); return { board: 'atlas', messages: [{ id: 17, text: 'Exact source' }] }; };
    assert.equal((await loadDetailsOrigin(card, api)).text, 'Exact source');
    assert.deepEqual(calls, ['/api/chat/atlas?before=18&last=1']);
    assert.equal(await loadDetailsOrigin({ ...card, message_id: null }, api), null);
    assert.equal(calls.length, 1);
    assert.equal(await loadDetailsOrigin(card, async () => ({ board: 'harbor', messages: [{ id: 17, text: 'wrong board' }] })), null);
    // A slow old origin must never replace a newly selected card.
    let resolve;
    const mount = { innerHTML: '', addEventListener() {}, removeEventListener() {}, querySelector: () => null };
    const component = createDetails({ mount, api: () => new Promise((done) => { resolve = done; }) });
    const pending = component.show(card, opts);
    await component.show({ ...card, number: 22, message_id: null, title: 'New selection' }, opts);
    resolve({ board: 'atlas', messages: [{ id: 17, text: 'Stale source' }] });
    await pending;
    assert.ok(mount.innerHTML.includes('New selection'));
    assert.ok(!mount.innerHTML.includes('Stale source'));
    component.stop();
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
