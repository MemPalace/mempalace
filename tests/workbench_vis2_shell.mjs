// Card history in Details (choices, reasons, an open ask for every viewer) and hidden Done cards.
import assert from 'node:assert/strict';
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const board = { slug: 'atlas', name: 'Atlas' };
// One case by name (the pytest wrapper), or every case when run bare (node --test).
async function run(which) {
if (which === 'history') {
  const d = await load('details.js');
  assert.equal(typeof d.renderDetails, 'function');
  const card = { number: 5, board: 'atlas', title: 'T', status: 'doing', version: 3, body: '', events: [
    { kind: 'ask', actor: 'conn:otto', target: 'user:leon', options: ['Ship', 'Wait'], text: 'Ship now?', at: '2026-11-05T12:00:00Z' },
    { kind: 'decide', actor: 'user:leon', choice: 'Wait', at: '2026-11-05T12:05:00Z' },
    { kind: 'release', actor: 'conn:otto', reason: 'Blocked on <review>', at: '2026-11-05T12:06:00Z' },
  ] };
  const html = d.renderDetails(card, { board });
  const activity = html.slice(html.indexOf('<ol class="activity">'));
  // A choice without a text reply still says which choice answered the ask.
  assert.match(activity, /Chose <b>Wait<\/b>/, 'the selected choice is missing');
  // The ask names who it went to and the choices offered; reasons stay verbatim.
  assert.match(activity, /asked → [^<]+<\/p><ul class="act-opts"[^>]*><li>Ship<\/li><li>Wait<\/li><\/ul>/);
  assert.match(activity, /Reason: Blocked on &lt;review&gt;/);
} else if (which === 'open-ask') {
  const d = await load('details.js');
  assert.equal(typeof d.renderDetails, 'function');
  // Another viewer (not the asked person) still sees the open ask and its choices.
  const card = { number: 6, board: 'atlas', title: 'T', status: 'review', version: 4, body: '', ask: 'Which limit?', ask_to: 'user:leon', return_to: 'conn:otto', ask_options: ['Per user', 'Per workspace'], updated_at: '2026-11-05T12:00:00Z',
    events: [{ kind: 'ask', actor: 'conn:otto', target: 'user:leon', options: ['Per user', 'Per workspace'], text: 'Which limit?', at: '2026-11-05T12:00:00Z' }] };
  const html = d.renderDetails(card, { board, me: 'user:maya' });
  const activity = html.slice(html.indexOf('<ol class="activity">'));
  assert.match(activity, /aria-label="Choices, waiting for an answer"><li>Per user<\/li><li>Per workspace<\/li>/, 'open ask choices missing');
  // No answer controls for this viewer: the history only lists the choices.
  assert.doesNotMatch(activity, /data-action="decide"/);
  // An open ask that has no event yet still shows.
  const bare = d.renderDetails({ ...card, events: [] }, { board, me: 'user:maya' });
  assert.match(bare.slice(bare.indexOf('<ol class="activity">')), /Which limit\?[\s\S]*<li>Per workspace<\/li>/);
} else if (which === 'done-selected') {
  const b = await load('board.js');
  assert.equal(typeof b.boardView, 'function');
  const done = [1, 2, 3, 4, 5, 6].map((n) => ({ number: n, title: `Card ${n}`, status: 'done' }));
  // A selected Done card past the first four opens the list instead of staying hidden.
  const html = b.boardView({ board, me: 'user:maya', cards: done, selected: 6 });
  assert.match(html, /id="col-done"[^>]*data-all/, 'Done stays collapsed over the selected card');
  assert.doesNotMatch(html, /Show 2 more/);
  assert.match(b.boardView({ board, me: 'user:maya', cards: done, selected: 2 }), /Show 2 more/);
} else if (which === 'done-reveal') {
  const b = await load('board.js');
  assert.equal(typeof b.roveTo, 'function');
  // Focusing or roving to a hidden Done card (a chat link, a route) opens the list first.
  const attrs = new Set(); let removed = 0;
  const cards = [];
  const group = { querySelectorAll: () => cards };
  const col = { hasAttribute: (n) => attrs.has(n), setAttribute: (n) => attrs.add(n), querySelectorAll: () => cards, querySelector: (s) => s === '[data-board-more]' ? { remove() { removed++; } } : null };
  for (let i = 0; i < 6; i++) cards.push({ tabIndex: -1, closest: (s) => s === '.col' ? col : s === '.cards' ? group : null });
  b.roveTo(cards[1]);
  assert.equal(attrs.has('data-all'), false, 'a visible card must not open the list');
  b.roveTo(cards[5]);
  assert.equal(attrs.has('data-all'), true, 'hidden Done card stays hidden');
  assert.equal(removed, 1); assert.equal(cards[5].tabIndex, 0);
} else if (which === 'show-more-focus') {
  const b = await load('board.js');
  assert.equal(typeof b.bindBoard, 'function');
  // Show N more hands focus to the first revealed card instead of dropping it.
  const handlers = {}, attrs = new Set();
  const cards = [];
  const group = { querySelectorAll: () => cards };
  const col = { hasAttribute: (n) => attrs.has(n), setAttribute: (n) => attrs.add(n), querySelectorAll: () => cards, querySelector: () => null };
  for (let i = 0; i < 6; i++) cards.push({ tabIndex: -1, focused: false, focus() { this.focused = true; }, closest: (s) => s === '.col' ? col : s === '.cards' ? group : null });
  const more = { closest: (s) => s === '[data-board-more]' ? more : s === '.col' ? col : null, remove() {} };
  b.bindBoard({ addEventListener: (t, fn) => { handlers[t] = fn; }, removeEventListener() {} }, { api() {}, board });
  handlers.click({ target: more });
  assert.equal(attrs.has('data-all'), true);
  assert.equal(cards[4].focused, true, 'focus was dropped');
} else {
  throw new Error(`unknown case ${which}`);
}
}
const CASES = ['history', 'open-ask', 'done-selected', 'done-reveal', 'show-more-focus'];
for (const which of process.argv[2] ? [process.argv[2]] : CASES) await run(which);
