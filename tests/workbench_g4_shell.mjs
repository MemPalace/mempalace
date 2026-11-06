// Workbench shell fixes: the board's Details button and child cards, the phone add-card
// sheet, the board strip only beside an open chat, chat row sources, ask strips, the
// restart link, direct card links and project settings over Needs me.
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
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const apiUrl = url(await read('api.js'));
const module = async (name) => import(url((await read(`${name}.js`)).replace(/(['"])\.\/api\.js\1/g, JSON.stringify(apiUrl))));
const e = (v) => String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const board = { slug: 'alpha', name: 'Alpha' };
const state = { me: { actor: 'user:one', people: { 'user:one': 'One' } }, boards: [board], needs: {}, base: '#/board/alpha', route: '#/board/alpha',
  cards: [{ number: 5, board: 'alpha', title: 'Top card', status: 'todo', parent: null }], children: [{ number: 134, board: 'alpha', title: 'Child card', status: 'todo', parent: 5 }],
  selected: null, drafts: new Map(), pending: new Set(), linked: [], chat: [], chatTimes: new Map(), chatVisible: new Set(), screen: 0, panelRequest: 0 };
function node(extra = {}) { return { innerHTML: '', hidden: true, dataset: {}, children: [], focused: 0, classList: { toggle() {}, remove() {}, add() {}, contains: () => false }, setAttribute() {}, removeAttribute() {}, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}, focus() { this.focused++; }, ...extra }; }
const nodes = {};
for (const selector of ['#content', '#card-panel', '#title', '#head-actions', '.shell']) nodes[selector] = node();
const root = { querySelector: (s) => nodes[s] || null, querySelectorAll: () => [], addEventListener() {} };
let titleActions = '';
const context = { root, state, e, enc: encodeURIComponent, document: { activeElement: null }, location: { hash: state.route }, history: { state: null, replaceState(_s, _t, hash) { context.location.hash = hash; } },
  matchMedia: () => ({ matches: false }), remember: () => ({}), restore() {}, markOpenCard() {}, title(_text, actions) { titleActions = actions; }, voice: { stop() {} }, showError() {}, message: (text) => text };
const run = (s) => runInNewContext(s, context);
// The shell's board helpers (child cards, phone width); a shell without them only lists state.cards.
function helpers() {
  if (app.includes('// The board lists child cards')) run(slice('// The board lists child cards', 'const options ='));
  else run('var boardCards = () => state.cards, phone = () => false;');
}
const which = process.argv[2];
if (which === 'details-button') {
  const ui = await module('board');
  assert.equal(typeof ui.boardView, 'function');
  helpers();
  Object.assign(context, { api() {}, renderBoard: ui.boardView, cardId: ui.cardId, chatTag: ui.chatTag, bindBoard: () => () => {}, boardDispose: null, currentBoard: () => board, fillChatTimes() {}, cancelCard() {} });
  run(slice('function boardView()', 'function markOpenCard()') + '\nboardView();');
  // 04c: the tablet board header has Details for the modal card sheet.
  assert.match(titleActions, /<button[^>]*id="details-btn"[^>]*>Details<\/button>/);
  assert.match(titleActions, /id="details-btn"[^>]*aria-controls="card-panel"/);
  assert.match(titleActions, /id="details-btn"[^>]*aria-expanded="false"/);
  // An approval's child cards are ordinary board cards (A-134 in Todo).
  assert.match(nodes['#content'].innerHTML, /data-card="134"/);
} else if (which === 'add-sheet') {
  const ui = await module('board');
  assert.equal(typeof ui.addCardForm, 'function');
  const sheet = ui.addCardForm();
  assert.match(sheet, /<form data-board-add[^>]*role="dialog"/);
  assert.match(sheet, /aria-modal="true"/);
  assert.match(sheet, /aria-labelledby="add-card-h"/);
  assert.match(sheet, /<h2 id="add-card-h">Add card<\/h2>/);
  assert.doesNotMatch(ui.addCardForm({ sheet: false }), /aria-modal/);
  // Esc in the sheet cancels it.
  let cancelled = 0, keydown = null;
  const mount = { addEventListener: (type, fn) => { if (type === 'keydown') keydown = fn; }, removeEventListener() {} };
  ui.bindBoard(mount, { api() {}, board, onCancel: () => cancelled++ });
  const form = {};
  keydown({ key: 'Escape', target: { closest: (s) => s.includes('data-board-add') ? form : null }, preventDefault() {}, stopPropagation() {} });
  assert.equal(cancelled, 1);
} else if (which === 'board-strip') {
  const ui = await module('details');
  assert.equal(typeof ui.createDetails, 'function');
  const strips = [], hide = node();
  nodes['#card-panel'].querySelector = (s) => s === '.hide' ? hide : null;
  Object.assign(context, { detailsUI: null, createDetails: () => ({ show: (_card, options) => { strips.push(options.strip); return Promise.resolve(); }, selectTab() {}, stop() {} }),
    api: async () => ({ number: 5, board: 'alpha', title: 'Top card', status: 'todo', version: 1, events: [] }), conversation: () => '', callTree: () => '', openSelectedCard() {}, refresh() {} });
  state.route = '#/card/5';
  run(slice('async function cardPanel(', 'function newCardForm('));
  // 06c: a board card picked with the chat closed shows its Details without a strip.
  await run('cardPanel(5, 0)');
  assert.equal(strips.at(-1), null);
  // With the chat open the strip says the card was picked on the board (06g).
  let closed = 0;
  state.workbenchChat = { dock: { isOpen: () => true, close: () => { closed++; } } };
  state.card = null; await run('cardPanel(5, 0)');
  assert.equal(strips.at(-1)?.kind, 'board'); assert.equal(closed, 1);
  // 04c: the Details button opens the sheet with focus on Hide.
  state.workbenchChat = null; state.detailsSheet = true; state.card = null;
  await run('cardPanel(5, 0)');
  assert.equal(hide.focused, 1); assert.equal(state.detailsSheet, false);
} else if (which === 'row-src') {
  const body = slice('onCard(number, row) {', '\n    const dock = createDock(').replace(/\}\);\s*$/, '');
  helpers();
  run(`var callbacks = { ${body} };`);
  assert.equal(typeof context.callbacks.onCard, 'function');
  // I3: a row's own source (from the chat pane) is never overwritten by the stamp text.
  const row = { id: 'chat-row-answer-3-5', dataset: { src: "the orchestrator's 14:03 answer" }, closest: () => ({ querySelector: () => ({ textContent: 'Quick reply · local model · openai_compatible/fast-model' }) }) };
  context.callbacks.onCard(5, row);
  assert.equal(row.dataset.src, "the orchestrator's 14:03 answer");
  assert.equal(context.location.hash, '#/card/5');
  const bare = { dataset: {}, closest: () => ({ querySelector: () => ({ textContent: 'You · 14:01' }) }) };
  context.callbacks.onCard(5, bare);
  assert.equal(bare.dataset.src, 'You · 14:01');
} else if (which === 'ask-strip') {
  assert.ok(app.includes('function askStrip('), 'Ask strip copy missing');
  run(slice('const hhmm =', 'function boardView()'));
  const approval = context.askStrip({ proposal_status: 'pending' }, { title: 'Send beta 0.9 to 40 testers?', hold: null });
  assert.equal(approval.text, 'Approval · You are asking about Send beta 0.9 to 40 testers? Approve or decline it in Needs me.');
  assert.equal(approval.backLabel, 'Back to the approval');
  const at = new Date(2026, 10, 5, 14, 7).toISOString();
  const released = context.askStrip({ proposal_status: 'approved' }, { title: 'Publish the privacy page', hold: { state: 'released', updated_at: at } });
  assert.equal(released.text, "Released · You are asking about Publish the privacy page, released at 14:07. It can't be recalled here.");
  assert.equal(released.backLabel, 'Back to the approval');
} else if (which === 'restart-link') {
  const handlers = {}, opened = [];
  root.addEventListener = (type, fn) => { handlers[type] = fn; };
  Object.assign(context, { consoleUI: null, openRestart: async (link) => { opened.push(link); } });
  run(slice('root.addEventListener("click",', 'root.addEventListener("submit",'));
  assert.equal(typeof handlers.click, 'function');
  // "Show how to restart the agent" opens the restart steps, never the unknown settings route.
  const link = { href: '#/board/ledger/settings' };
  let prevented = 0;
  await handlers.click({ defaultPrevented: false, preventDefault() { prevented++; }, target: { closest: (s) => s.includes('.chat-recovery') ? link : null } });
  assert.deepEqual(opened, [link]); assert.equal(prevented, 1);
} else if (which === 'direct-link') {
  const routes = await module('routes');
  Object.assign(context, routes, { clearAgentSecret() {}, decideStart: () => undefined, speech: { stop() {} }, consoleUI: null, detailsUI: null, boardDispose: null, refresh: async () => {},
    api: async () => ({ boards: state.boards }), boardPreference: () => 'last', localStorage: { getItem: () => null, setItem() {} } });
  run(slice('async function route()', '// The current view'));
  // A shared card link draws its board behind the card, not a Loading Needs me.
  state.screen = 0; state.base = '#/'; state.route = '#/'; context.location.hash = '#/card/121';
  await run('route()');
  assert.equal(state.base, '#/board/alpha');
  assert.equal(state.rebase, 121);
  assert.equal(state.route, '#/card/121');
} else if (which === 'needs-settings') {
  const needs = await import(new URL('../commonplace/web/static/needs.js', import.meta.url));
  assert.equal(typeof needs.createNeeds, 'function');
  const mount = node(), detail = node();
  const ui = needs.createNeeds({ mount, detailMount: detail, api: async () => ({ holds: [] }), document: null, setInterval: () => 0, clearInterval() {} });
  assert.equal(typeof ui.pause, 'function', 'Needs me cannot lend its panel');
  // Paused, Needs me never draws over what the shell put in the shared panel.
  detail.innerHTML = 'settings'; ui.pause(true); ui.setChatOpen(true);
  assert.equal(detail.innerHTML, 'settings');
  ui.pause(false); assert.notEqual(detail.innerHTML, 'settings');
  ui.stop();
  // The shell pauses Needs me while project settings use the panel on #/needs.
  const handlers = {}, mounted = [];
  let paused = null;
  root.addEventListener = (type, fn) => { handlers[type] = fn; };
  state.base = '#/needs'; state.needsUI = { pause: (on) => { paused = on; } };
  Object.assign(context, { consoleUI: null, api() {}, currentBoard: () => board, injected: async (host, options) => mounted.push(options) });
  run(slice('root.addEventListener("click",', 'root.addEventListener("submit",').replaceAll('await import("./projects.js")', '({mountProject: injected})'));
  await handlers.click({ target: { closest: (s) => s.includes('.chat-recovery') ? null : { dataset: { action: 'coordinator', board: 'alpha' } } } });
  assert.equal(paused, true);
  assert.equal(mounted.length, 1); assert.equal(typeof mounted[0].onBack, 'function');
} else {
  throw new Error(`unknown case ${which}`);
}
