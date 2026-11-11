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
const linksUrl = url(await read('cardlinks.js'));
// A data: module cannot resolve "./x.js": every other sibling import loads from its file.
const siblings = (source) => source.replace(/(from\s*)(['"])\.\/([\w-]+\.js)\2/g, (_all, from, _quote, name) => from + JSON.stringify(new URL(`../commonplace/web/static/${name}`, import.meta.url).href));
async function module(name) {
  assert.match(app, new RegExp(`from ["']\\./${name}\\.js["']`), `${name} import missing`);
  return import(url(siblings((await read(`${name}.js`)).replace(/(['"])\.\/api\.js\1/g, JSON.stringify(apiUrl)).replace(/(['"])\.\/cardlinks\.js\1/g, JSON.stringify(linksUrl)))));
}
const e = (v) => String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const board = { slug: 'alpha', name: 'Alpha', counts: { review: 9 }, agents_working: 2 };
const state = { me: { actor: 'user:one', workspace: 'workspace', email: 'one@example.com', people: { 'user:one': 'One' } }, boards: [board], needs: { count: 1 }, base: '#/board/alpha', route: '#/board/alpha', cards: [{ number: 5, board: 'alpha', title: 'Exact <words>', status: 'todo' }, { number: 6, board: 'alpha', title: 'Parked', status: 'backlog' }], selected: 5, drafts: new Map(), pending: new Set(), linked: [], chat: [], chatTimes: new Map(), chatVisible: new Set(), chatTaskRequests: new Map(), screen: 0, panelRequest: 0 };
const nodes = {};
function node() { return { innerHTML: '', hidden: true, dataset: {}, children: [{}], classList: { toggle() {}, remove() {}, add() {}, contains: () => false }, setAttribute() {}, removeAttribute() {}, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}, focus() {}, getClientRects: () => [{}] }; }
for (const selector of ['#board-nav', '#phone-bar', '#phone-tabs', '#content', '#card-panel', '.shell', '#title', '#head-actions']) nodes[selector] = node();
const root = { innerHTML: '', querySelector: (s) => nodes[s] || null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {} };
const storage = new Map();
const context = { root, state, e, enc: encodeURIComponent, document: { activeElement: null, title: '' }, location: { hash: state.route }, localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k,v) => storage.set(k,v) }, URLSearchParams, remember: () => ({}), restore() {}, title() {}, markOpenCard() {}, voice: { stop() {} }, showError: (s) => assert.equal(s, ''), needsCards: () => [], message: (text) => text, matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}}), MutationObserver: class {observe() {} disconnect() {}}, setTimeout, clearTimeout };
const run = (s) => runInNewContext(s, context);
const which = process.argv[2];
if (which === 'shell') {
  Object.assign(context, await module('rail'));
  context.navigation = () => {};
  run(slice('function shell()', 'function navigation()') + '\nshell();');
  assert.match(root.innerHTML, /class="rail"/);
  assert.match(root.innerHTML, /class="shell two"/);
  assert.match(root.innerHTML, /id="phone-bar"/);
  assert.match(root.innerHTML, /id="phone-tabs"/);
  assert.match(root.innerHTML, /id="card-panel"/);
} else if (which === 'navigation') {
  const rail = await module('rail');
  for (const name of ['renderRail','renderPhoneBar','renderPhoneTabs']) assert.equal(typeof rail[name], 'function');
  Object.assign(context, rail);
  run(slice('function currentBoard()', 'function shell()'));
  run(slice('function navigation()', 'function showError') + '\nnavigation();');
  const html = nodes['#board-nav'].outerHTML;
  assert.equal(typeof html, 'string');
  assert.match(html, /2 agents working/);
  assert.doesNotMatch(html, /9 need you/);
  assert.equal((nodes['#phone-tabs'].innerHTML.match(/<a /g) || []).length, 3);
  state.base = '#/needs'; state.selected = 12;
  context.needsCards = () => [{number: 12, board: 'alpha'}];
  run('navigation();');
  assert.match(nodes['#phone-bar'].innerHTML, /data-board="alpha"/);
} else if (which === 'board') {
  const ui = await module('board');
  assert.equal(typeof ui.boardView, 'function'); assert.equal(typeof ui.bindBoard, 'function');
  let mounted = 0, cleaned = 0;
  Object.assign(context, { fillChatTimes() {}, cardId: ui.cardId, chatTag: ui.chatTag, cancelCard() {}, renderBoard: ui.boardView, bindBoard: (mount, options) => { assert.equal(mount, nodes['#content']); assert.equal(options.board.slug, 'alpha'); mounted++; return () => cleaned++; }, boardDispose: null, currentBoard: () => board, api() {}, refresh() {} });
  run(slice('// The board lists child cards', 'const options ='));
  run(slice('function boardView()', 'function markOpenCard()') + '\nboardView(); boardView();');
  assert.equal(mounted, 2); assert.equal(cleaned, 1);
  assert.match(nodes['#content'].innerHTML, /class="workbench-board"/);
  assert.match(nodes['#content'].innerHTML, /A-5/);
  assert.match(nodes['#content'].innerHTML, /Backlog \(1\)/);
  assert.match(nodes['#content'].innerHTML, /Parked/);
} else if (which === 'details') {
  const ui = await module('details');
  assert.equal(typeof ui.createDetails, 'function');
  let stopped = 0;
  Object.assign(context, { detailsUI: null, createDetails: (options) => { assert.equal(options.mount, nodes['#card-panel']); const result = ui.createDetails(options); const stop = result.stop; result.stop = () => { stopped++; stop(); }; return result; }, api: async () => ({ number: 5, board: 'alpha', title: 'Exact <words>', body: 'Body\r\noriginal', version: 1, status: 'todo', events: [] }) });
  state.route = '#/card/5';
  run(slice('async function cardPanel(', 'function newCardForm('));
  await run('cardPanel(5, 0)');
  assert.match(nodes['#card-panel'].innerHTML, /class="workbench-details"/);
  assert.match(nodes['#card-panel'].innerHTML, /Exact &lt;words&gt;/);
  assert.match(nodes['#card-panel'].innerHTML, /Body&#13;\noriginal/);
  assert.match(nodes['#card-panel'].innerHTML, /data-form="comment"/);
  // A refresh of the open card keeps its component (and the user's focus).
  await run('cardPanel(5, 0)'); assert.equal(stopped, 0);
} else if (which === 'routes') {
  const routes = await module('routes'); assert.equal(typeof routes.resolveRoute, 'function');
  Object.assign(context, routes, { clearAgentSecret() {}, decideStart: () => undefined, speech: {stop() {}}, chatUI: {stop() {}}, consoleUI: {stop() {}}, detailsUI: null, boardDispose: null, markOpenCard() {}, refresh: async () => {}, history: { state: null, replaceState(_s,_t,hash) { context.location.hash = hash; } }, api: async () => ({boards: state.boards}), boardPreference: () => 'last', singleKeysEnabled: () => true });
  run(slice('async function route()', '// The current view'));
  for (const [hash, expected] of [['#/', '#/board/alpha'], ['#/chat/alpha', '#/board/alpha/chat'], ['#/space/alpha', '#/space/alpha'], ['#/needs', '#/needs'], ['#/rules', '#/rules'], ['#/console', '#/console'], ['#/settings', '#/settings'], ['#/decide', '#/decide']]) {
    context.decideStart = () => hash === '#/decide' ? null : undefined;
    Object.assign(context, {createSession: () => ({}), askedRead: () => ({}), queueArmSlow() {}, queueStop() {}, queueFlush() {}});
    context.location.hash = hash; await run('route()'); assert.equal(state.base, expected);
  }
  state.boards = []; context.location.hash = '#/'; context.api = async () => ({boards: []}); context.decideStart = () => undefined;
  await run('route()'); assert.equal(state.base, '#/start');
} else if (which === 'settings') {
  assert.match(app, /b\.dataset\.board\s*\|\|/, 'Settings must honor its originating board');
  const handlers = {}, mounted = [];
  root.addEventListener = (type, handler) => { handlers[type] = handler; };
  state.base = '#/needs';
  Object.assign(context, {consoleUI: null, api() {}, currentBoard: () => ({slug: 'wrong-project'}), injectedMountProject: async (_mount, options) => mounted.push(options.slug)});
  run(slice('root.addEventListener("click",', 'root.addEventListener("submit",').replaceAll('await import("./projects.js")', '({mountProject: injectedMountProject})'));
  await handlers.click({target: {closest: (s) => s.includes('chat-recovery') ? null : ({dataset: {action: 'coordinator', board: 'origin-project'}})}});
  assert.deepEqual(mounted, ['origin-project']);
  let edited = null; state.card = {number: 5, version: 1, board: 'alpha'};
  context.newCardForm = (card) => { edited = card; };
  await handlers.click({target: {closest: (s) => s.includes('chat-recovery') ? null : ({dataset: {action: 'edit-card'}})}});
  assert.equal(edited, state.card);
} else if (which === 'keys' || which === 'switch' || which === 'switch-enter' || which === 'chat-key' || which === 'panel-enter') {
  assert.ok(app.includes('function singleKeysEnabled()'), 'Single-key preference missing');
  const handlers = {};
  Object.assign(context, { Element: class {}, document: {activeElement: null, addEventListener: (name, fn) => { handlers[name] = fn; }}, performance: {now: () => 0}, detailsUI: null });
  const rows = [5, 7].map((number) => ({...node(), dataset: {card: String(number)}, focus() {state.selected = number;}, scrollIntoView() {}}));
  root.querySelectorAll = () => rows;
  let added = 0, edited = 0, moved = 0, hidden = 0;
  Object.assign(context, { newCardForm: () => added++, openSelectedCard: (action) => { if (action === 'edit-card') edited++; else if (action === 'move-card') moved++; }, closeCardPanel: () => hidden++, shortcutsView() {}, shellMenu() {}, boardView() {}, restore() {}, needsCards: () => [] });
  run(slice('function singleKeysEnabled()', 'let timer = null'));
  const key = async (key, extra = {}) => handlers.keydown({key, target: {}, preventDefault() {}, ...extra});
  if (which === 'keys') {
    await key('c'); assert.equal(added, 1);
    await key('j'); assert.equal(state.selected, 7);
    await key('k'); assert.equal(state.selected, 5);
    await key('e'); assert.equal(edited, 1);
    await key('m'); assert.equal(moved, 1);
    await key('Enter'); assert.equal(context.location.hash, '#/card/5');
    nodes['#card-panel'].hidden = false;
    await key(']'); assert.equal(hidden, 1);
    await key('c', {ctrlKey: true}); await key('c', {repeat: true}); assert.equal(added, 1);
  } else if (which === 'switch') {
    storage.set('cmmn.keys.single', 'false');
    await key('c'); await key('e'); await key('m'); await key('j'); await key(']');
    assert.equal(added + edited + moved + hidden, 0); assert.equal(state.selected, 5);
    let palette = 0; context.shellMenu = () => palette++;
    await key('k', {ctrlKey: true}); assert.equal(palette, 1);
    nodes['#card-panel'].hidden = false; await key('Escape'); assert.equal(hidden, 1);
    storage.set('cmmn.keys.single', 'true'); await key('c'); assert.equal(added, 1);
    // The visible switch (now in the palette's shortcuts sheet) has the same stored preference and ARIA state.
    const paletteSource = await read('palette.js');
    assert.match(paletteSource, /role="switch"/); assert.match(paletteSource, /aria-checked=/);
    assert.match(paletteSource, /'cmmn\.keys\.single'/); assert.match(app, /onSingleKeysChange/);
    const field = new context.Element(); field.closest = (selector) => selector.includes('input') ? {} : null;
    await key('c', {target: field}); assert.equal(added, 1);
  }
  if (which === 'switch-enter') {
    // With single keys off, Enter still opens the selected card; only printable keys are gated.
    storage.set('cmmn.keys.single', 'false');
    await key('Enter'); assert.equal(context.location.hash, '#/card/5');
    context.location.hash = '#/board/alpha'; await key('ArrowDown'); await key('c');
    assert.equal(context.location.hash, '#/board/alpha'); assert.equal(added, 0);
  } else if (which === 'panel-enter') {
    // Enter on the open card's heading (card 7, opened by its link) never jumps to the stale selection (5).
    const heading = new context.Element(); heading.closest = (selector) => selector === '#card-panel' ? nodes['#card-panel'] : null;
    state.route = context.location.hash = '#/card/7'; nodes['#card-panel'].hidden = false;
    for (const single of ['false', 'true']) {
      storage.set('cmmn.keys.single', single);
      await key('Enter', {target: heading});
      assert.equal(context.location.hash, '#/card/7', `Enter inside the card panel moved to another card (single keys ${single})`);
    }
  } else if (which === 'chat-key') {
    // ] on the bare legacy chat route has no board to return to.
    state.base = state.route = context.location.hash = '#/chat';
    await key(']'); assert.equal(context.location.hash, '#/chat');
    state.base = state.route = context.location.hash = '#/board/alpha/chat';
    await key(']'); assert.equal(context.location.hash, '#/board/alpha');
  }
} else if (which === 'chat-time-label') {
  // The source time patches only the From chat tag, never a label chip with the same words.
  const ui = await module('board');
  assert.equal(typeof ui.chatTag, 'function');
  const tags = [{className: 'tag lbl', textContent: 'From chat review'}, {className: 'tag', textContent: 'From chat'}];
  root.querySelectorAll = (selector) => tags.filter((tag) => selector.endsWith('.tag') || (selector.endsWith('.tag:not(.lbl)') && !tag.className.includes('lbl')));
  state.cards = [{number: 5, board: 'alpha', message_id: 40}];
  Object.assign(context, {chatTag: ui.chatTag, currentBoard: () => board, api() {}, loadChatTimes: async () => [{id: 40, at: '2026-11-05T14:03:00'}]});
  run(slice('// The board lists child cards', 'const options ='));
  run(slice('async function fillChatTimes(', 'function spaceTask('));
  context.board = board; await run('fillChatTimes(board)');
  assert.equal(tags[0].textContent, 'From chat review', 'Label chip was overwritten');
  assert.equal(tags[1].textContent, 'From chat · 14:03');
} else if (which === 'inert') {
  Object.assign(context, await module('rail'));
  nodes['#card-panel'].hidden = false;
  nodes['.shell'].classList.contains = () => false;
  context.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  context.navigation = () => {};
  run(slice('function shell()', 'function navigation()') + '\nshell();');
  run(slice('function currentBoard()', 'function shell()'));
  run(slice('function navigation()', 'function showError'));
  // navigation() swaps in a fresh rail; the open sheet must make it inert again.
  nodes['#board-nav'].inert = false;
  run('navigation();');
  assert.equal(nodes['#board-nav'].inert, true, 'Rail behind the open sheet is not inert');
} else if (which === 'rail-once') {
  Object.assign(context, await module('rail'));
  context.navigation = () => {};
  run(slice('function shell()', 'function navigation()') + '\nshell();');
  // Two re-renders of the rail leave no stylesheet behind; the rules live in workbench.css.
  run(slice('function currentBoard()', 'function shell()'));
  run(slice('function navigation()', 'function showError') + '\nnavigation(); navigation();');
  assert.doesNotMatch(nodes['#board-nav'].outerHTML, /<style>/);
} else if (which === 'rules-count') {
  Object.assign(context, await module('rail'));
  run(slice('function currentBoard()', 'function shell()'));
  state.rulesWaiting = 2;
  run(slice('function navigation()', 'function showError') + '\nnavigation();');
  const rules = nodes['#board-nav'].outerHTML.match(/<a href="#\/rules">[\s\S]*?<\/a>/);
  assert.ok(rules, 'Rules entry missing');
  assert.match(rules[0], />2</);
} else if (which === 'bare-chat') {
  const routes = await module('routes');
  Object.assign(context, routes, { clearAgentSecret() {}, decideStart: () => undefined, speech: {stop() {}}, chatUI: {stop() {}}, consoleUI: {stop() {}}, detailsUI: null, boardDispose: null, markOpenCard() {}, refresh: async () => {}, history: { state: null, replaceState(_s,_t,hash) { context.location.hash = hash; } }, api: async () => ({boards: state.boards}), boardPreference: () => 'last', singleKeysEnabled: () => true });
  run(slice('async function route()', '// The current view'));
  // Bare #/chat opens the default project's chat; no slug is read from the path.
  context.location.hash = '#/chat'; await run('route()');
  assert.equal(state.base, '#/board/alpha/chat');
  assert.equal(context.location.hash, '#/board/alpha/chat');
} else if (which === 'panel-focus') {
  const ui = await module('details');
  let created = 0;
  Object.assign(context, { detailsUI: null, createDetails: (options) => { created++; return ui.createDetails(options); }, api: async () => ({ number: 5, board: 'alpha', title: 'Same card', body: '', version: 1, status: 'todo', events: [] }) });
  state.route = '#/card/5';
  run(slice('async function cardPanel(', 'function newCardForm('));
  await run('cardPanel(5, 0)'); await run('cardPanel(5, 0)');
  assert.equal(created, 1, 'A background refresh must keep the open card component');
  // Focus returns to the control that had it, found again by a stable selector.
  const panel = nodes['#card-panel'];
  const fresh = { focused: 0, focus() { this.focused++; } };
  const select = { id: 'card-status', dataset: {}, closest: (s) => s.includes('#card-panel') ? panel : null, hasAttribute: () => false };
  panel.id = 'card-panel'; panel.querySelector = (s) => s === '#card-status' ? fresh : null;
  Object.assign(context, { voice: { remember() {}, view() {}, stop() {} }, CSS: { escape: (v) => v } });
  run(slice('function focusKey(', 'async function signin('));
  context.document.activeElement = select; context.saved = run('remember()');
  context.document.activeElement = null; run('restore(saved)');
  assert.equal(fresh.focused, 1);
} else if (which === 'anchors') {
  for (const [start,end] of [['function remember()', 'async function signin('], ['function navigation()', 'function showError'], ['function title(', 'function decision('], ['function needsView()', 'function boardView()'], ['function spaceView()', 'function boardsView()'], ['async function hydrateSpaceTasks()', 'function mergeChat('], ['function reviewForm()', 'function handoffForm('], ['function handoffForm(', 'async function settingsView('], ['async function approveProposal(', 'function clearDecisionDraft('], ['async function sendDecide(', 'async function route()']]) slice(start,end);
} else if (which === 'legacy') {
  assert.match(app, /if \(status === "review"\) reviewForm\(\)/);
  assert.match(app, /\/api\/cards\/\$\{number\}\/approve/);
  assert.match(app, /data-form="comment"/);
  assert.match(app, /kind === "space-message"/);
} else if (which === 'shell-nostyle') {
  // The app's CSP (style-src 'self') refuses inline style elements: the shell's rules travel as a constructed sheet.
  Object.assign(context, await module('rail'));
  context.navigation = () => {};
  run(slice('function shell()', 'function navigation()') + '\nshell();');
  assert.match(root.innerHTML, /class="shell two"/);
  assert.doesNotMatch(root.innerHTML, /<style/i);
  const sheet = /const shellStyles = `([^`]*)`;/.exec(app);
  assert.ok(sheet, 'Shell sheet missing');
  assert.match(sheet[1], /#shell-dialog \{/); assert.match(sheet[1], /\.shell \.needs-me > \.bar > h1 \{ display:none; \}/);
  assert.match(app, /sheet\.replaceSync\(shellStyles\)/);
} else assert.fail(`Unknown case: ${which}`);
console.log(`${which}: PASS`);
