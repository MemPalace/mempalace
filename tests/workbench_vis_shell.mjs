// Board, Details and Needs me markup that the visual check found missing (design 03, 04, 05, 06g, 07).
import assert from 'node:assert/strict';
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const board = { slug: 'atlas', name: 'Atlas' }, me = 'user:maya';
const card = (number, status, extra = {}) => ({ number, title: `Card ${number}`, status, ...extra });
// One case by name (the pytest wrapper), or every case when run bare (node --test).
async function run(which) {
if (which === 'empty-board') {
  const b = await load('board.js');
  assert.equal(typeof b.boardView, 'function');
  const html = b.boardView({ board, me, cards: [] });
  // Three numbered steps, each with its button (03).
  assert.equal((html.match(/<li[^>]*><span class="num"/g) || []).length, 3, 'numbered steps missing');
  for (const action of ['new-card', 'connect-agent', 'open-chat']) assert.match(html, new RegExp(`<ol class="steps">[\\s\\S]*data-action="${action}"`));
  // Empty columns explain themselves; Todo offers Add card; no empty Backlog entry.
  assert.match(html, /Work waiting to be picked up\./);
  assert.equal((html.match(/class="empty-col">Nothing yet/g) || []).length, 3);
  assert.match(html, /class="add-ghost" data-action="new-card"/);
  assert.doesNotMatch(html, /Backlog \(0\)/);
} else if (which === 'columns') {
  const b = await load('board.js');
  assert.equal(typeof b.boardView, 'function');
  const done = [1, 2, 3, 4, 5, 6].map((n) => card(n, 'done'));
  const html = b.boardView({ board, me, cards: [...done, card(7, 'doing'), card(8, 'todo')] });
  // The column switcher carries every column's count (r-06c-chat-1024).
  assert.match(html, /data-board-column="doing"[^>]*>Doing <span class="n">1<\/span>/);
  // Done is a compact check list with Show N more (04).
  assert.equal((html.match(/class="card is-done/g) || []).length, 6);
  assert.match(html, /data-board-more>Show 2 more</);
  assert.doesNotMatch(b.boardView({ board, me, cards: done, doneAll: true }), /Show 2 more/);
} else if (which === 'composer') {
  const b = await load('board.js');
  assert.equal(typeof b.boardView, 'function');
  const html = b.boardView({ board, me, cards: [card(8, 'todo')], adding: true, sheet: false });
  // The composer opens at the top of Todo (05), before its cards.
  const todo = html.slice(html.indexOf('id="col-todo"'), html.indexOf('id="col-doing"'));
  assert.ok(todo.includes('data-board-add'), 'composer is not in Todo');
  assert.ok(todo.indexOf('data-board-add') < todo.indexOf('data-card="8"'));
  assert.match(todo, /Enter adds · Esc cancels/);
} else if (which === 'activity') {
  const d = await load('details.js');
  assert.equal(typeof d.renderDetails, 'function');
  const html = d.renderDetails({ number: 5, board: 'atlas', title: 'T', status: 'todo', version: 1, body: 'b', events: [{ kind: 'add', actor: 'user:maya', at: '2026-11-05T12:00:00Z' }, { kind: 'claim', actor: 'conn:otto', at: '2026-11-05T12:01:00Z' }] }, { board });
  // Activity reads as sentences (06g), not raw event kinds.
  assert.match(html, /<b>[^<]*<\/b> created the card/);
  assert.match(html, /started working on it/);
  assert.doesNotMatch(html, / · add · /);
  assert.match(html, /<div class="sec-h"><h3>Description<\/h3><button[^>]*data-action="edit-card"/);
} else if (which === 'needs-meta') {
  const n = await load('needs.js');
  assert.equal(typeof n.renderNeeds, 'function');
  const now = Date.parse('2026-11-05T14:21:00Z');
  const ask = { number: 22, board: 'atlas', title: 'Pick', status: 'review', version: 1, ask: 'Which?', ask_options: ['A'], actions: ['answer'], updated_at: '2026-11-05T14:09:00Z', events: [{ kind: 'ask', actor: 'conn:lead', options: ['A'], at: '2026-11-05T14:09:00Z' }] };
  const list = n.renderNeeds({ needs: { asks: [ask] }, boards: [board], now });
  // Rows carry their age (07).
  assert.match(list, /<time class="ago" datetime="2026-11-05T14:09:00Z">12 min<\/time>/);
  const detail = n.renderNeedDetail(ask, { board, now, kind: 'decision' });
  assert.match(detail, / · asked \d\d:\d\d/);
  assert.match(detail, /placeholder="e\.g\. Send at 09:00 tomorrow"/);
} else {
  throw new Error(`unknown case ${which}`);
}
}
const CASES = ['empty-board', 'columns', 'composer', 'activity', 'needs-meta'];
for (const which of process.argv[2] ? [process.argv[2]] : CASES) await run(which);
