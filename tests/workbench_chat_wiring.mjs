import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), 'utf8');
const app = await read('app.js');
const source = (start, end) => {
  const a = app.indexOf(start), b = app.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing slice: ${start}`);
  return app.slice(a, b);
};
const e = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const url = (s) => `data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const chatstate = await import(url(await read('chatstate.js')));
for (const name of ['deriveChatState', 'seenStore', 'clearSeen']) assert.equal(typeof chatstate[name], 'function');
const which = process.argv[2];
const alpha = {slug: 'atlas', name: 'Atlas', coordinator: 'conn:iris'};
const harbor = {slug: 'harbor', name: 'Harbor', coordinator: null};
const state = {me: {user: 'maya', actor: 'user:maya', people: {}}, boards: [alpha, harbor], base: '#/board/atlas', route: '#/board/atlas', cards: [], linked: [], chat: [], chatTimes: new Map(), open: [], selected: 1, needs: {count: 2}, drafts: new Map(), pending: new Set(), panelRequest: 0, screen: 0};
const nodes = new Map();
const make = () => ({innerHTML: '', hidden: true, dataset: {}, classList: {contains: () => false, toggle() {}, add() {}, remove() {}}, setAttribute(k,v) {this[k] = v;}, querySelector: () => null, querySelectorAll: () => [], focus() {}});
for (const selector of ['.shell', '#head-actions', '#title', '#content', '#phone-tabs', '#card-panel']) nodes.set(selector, make());
const root = {querySelector: (s) => nodes.get(s) || null, querySelectorAll: () => [], addEventListener() {}};
let selectedBoard = alpha, titleActions = '', creates = 0, chatOptions, dockOptions, dock, stops = 0, cleared = 0;
let requests = [], replies = [], rows = [], messageCursor = 20, replyCursor = 0, observers = [];
let apiHook = null;
const handlers = new Map(), timers = new Map(), storage = new Map();
const storageAPI = {getItem: (k) => storage.get(k) ?? null, setItem: (k,v) => storage.set(k,v), removeItem: (k) => storage.delete(k), key: (i) => [...storage.keys()][i], get length() {return storage.size;}};
const document = {hidden: false, activeElement: null, addEventListener: (k,v) => handlers.set(k,v), removeEventListener: (k,v) => {if (handlers.get(k) === v) handlers.delete(k);}, dispatchEvent(event) {handlers.get(event.type)?.(event);}};
const pane = {show: async (...args) => {pane.shown = args;}, stop: () => stops++, clearUnsent: () => cleared++, focusComposer() {}, setQuote() {}, destroyed: 0, destroy() {pane.destroyed++;}};
const api = async (path, options) => {
  requests.push({path, options});
  if (apiHook) return apiHook(path, options);
  if (path.endsWith('/status')) return {orchestrator: {visible: false, last_seen: 'private'}};
  return {me: {user: 'maya'}, board: selectedBoard.slug, messages: rows, replies, cursor: messageCursor, reply_cursor: replyCursor};
};
const context = {state, root, e, enc: encodeURIComponent, api, adminUI: null, ...chatstate,
  seenStore: (user, slug) => chatstate.seenStore(user, slug, storageAPI), clearSeen: (user) => chatstate.clearSeen(user, storageAPI),
  currentBoard: () => selectedBoard, singleKeysEnabled: () => true, chatUI: null,
  createChat: (options) => {chatOptions = options; creates++; return pane;},
  createDock: (options) => {
    dockOptions = options;
    dock = {opened: false, mounts: [], updates: [], destroyed: 0,
      mount(header, settings) {this.mounts.push({header, settings}); this.settings = settings;},
      async open(options) {this.opened = true; this.openedWith = options; await pane.show(this.settings.board.slug, options);},
      close(options) {this.opened = false; this.closedWith = options;},
      isOpen() {return this.opened;}, update(options) {this.updates.push(options);},
      destroy() {this.destroyed++; pane.stop();},
    }; return dock;
  },
  renderPhoneTabs: (options) => {context.phoneOptions = options; return 'tabs';},
  document, location: {hash: state.route}, history: {state: null, replaceState(_s,_t,hash) {context.location.hash = hash;}},
  MutationObserver: class {constructor(fn) {this.fn=fn; observers.push(this);} observe() {} disconnect() {this.disconnected = true;}},
  Event: class {constructor(type) {this.type=type;}},
  setTimeout(fn, ms) {const key = {}; timers.set(key, {fn, ms}); return key;}, clearTimeout(key) {timers.delete(key);},
  title(_text, actions) {titleActions = actions;}, cardId: () => '', chatTag: () => '', cancelCard() {}, renderBoard: () => 'board', bindBoard: () => () => {}, boardDispose: null,
  boardsView() {}, markOpenCard() {}, fillChatTimes() {}, remember: () => ({}), restore() {}, refresh() {},
};
const run = (s) => runInNewContext(s, context);
// The shell's board helpers (child cards, phone width) come from app.js itself.
run(source('// The board lists child cards', 'const options =').replace('matchMedia("(max-width:699px)").matches', 'false'));
const flush = async () => {for (let i = 0; i < 15; i++) await Promise.resolve();};
function mount() {
  assert.match(app, /function mountWorkbenchChat\(\)/, 'Dock integration missing');
  run(source('function mountWorkbenchChat()', 'function consoleScreen()'));
  assert.equal(typeof context.mountWorkbenchChat, 'function');
  run('mountWorkbenchChat()');
  assert.ok(state.workbenchChat, 'Mounted service missing');
  assert.equal(typeof state.workbenchChat.pollUnread, 'function');
}
const message = (id, actor = 'conn:iris') => ({id, actor, text: 'Exact words', at: '2026-11-05T12:00:00Z'});
if (which === 'board_header') {
  run(source('function boardView()', 'function markOpenCard()') + '\nboardView();');
  assert.match(titleActions, /id="chat-toggle"/);
  assert.match(titleActions, /aria-controls="chat"/);
} else if (which === 'needs_header') {
  Object.assign(context, {needsCards: () => [], speech: {}, homeView: () => '', dot() {}, taskRow() {}, children() {}, speechView() {}, createNeeds: () => ({show: async () => {}, stop() {}})});
  run(source('function needsView()', 'function boardView()') + '\nneedsView();');
  assert.match(titleActions, /id="chat-toggle"/);
} else if (which === 'memory_header') {
  state.mapDispose = () => {}; Object.assign(context, {URLSearchParams, createMemory: () => ({stop() {}, search: async () => {}})});
  run(source('async function memoryView(', '\n// The shell keeps one dock'));
  await run('memoryView(0, true)');
  assert.match(titleActions, /id="chat-toggle"/);
} else if (which === 'styles') {
  const html = await read('index.html');
  assert.match(html, /href="\/app\/chatdock.css"/);
  assert.ok(html.indexOf('/app/chatdock.css') > html.indexOf('/app/workbench.css'));
  assert.match(html, /href="\/app\/chat.css"/);
} else if (which === 'anchors') {
  for (const [start,end] of [['function remember()', 'async function signin('], ['function navigation()', 'function showError'], ['function title(', 'function decision('], ['function needsView()', 'function boardView()'], ['function spaceView()', 'function boardsView()'], ['async function hydrateSpaceTasks()', 'function mergeChat('], ['function reviewForm()', 'function handoffForm('], ['function handoffForm(', 'async function settingsView('], ['async function approveProposal(', 'function clearDecisionDraft('], ['async function sendDecide(', 'async function route()']]) source(start,end);
} else if (which === 'board_guards') {
  run(source('function boardView()', 'function markOpenCard()') + '\nboardView();');
  assert.match(titleActions, /data-action="coordinator"/);
  assert.match(titleActions, /data-action="new-card"/);
  assert.match(app, /kind === "space-message"/);
} else {
  if (which === 'seen_pages') {
    // 250 agent messages; this device last read 100. The server pages at most 100 rows.
    const all = Array.from({length: 250}, (_, i) => message(i + 1));
    storage.set('cmmn.chat.seen.maya.atlas', '{"message":100,"reply":0}');
    apiHook = (path) => {
      if (path.endsWith('/status')) return {};
      const after = path.match(/after=(\d+)/);
      const list = after ? all.filter((m) => m.id > Number(after[1])).slice(0, 100) : all.slice(-20);
      return {me: {user: 'maya'}, messages: list, replies: [], cursor: list.at(-1)?.id ?? Number(after?.[1] || 0), reply_cursor: 0};
    };
  }
  mount(); await flush();
  const service = state.workbenchChat;
  if (which === 'mount') {
    assert.equal(dockOptions.root, nodes.get('.shell')); assert.equal(dockOptions.chat, pane);
    nodes.set('#chat-content', make()); assert.equal(chatOptions.mount(), nodes.get('#chat-content'));
    assert.equal(dock.settings.board, alpha); assert.equal(dock.settings.label, 'Chat');
    assert.match(app, /from "\.\/chatdock.js"/); assert.match(app, /from "\.\/chatstate.js"/);
  } else if (which === 'reuse') {
    run('mountWorkbenchChat(); mountWorkbenchChat();'); await flush();
    assert.equal(creates, 1); assert.equal(dock.mounts.length, 3); assert.equal(dock.destroyed, 0);
  } else if (which === 'needs_project' || which === 'memory_project') {
    state.base = which === 'needs_project' ? '#/needs' : '#/memory?q=exact'; selectedBoard = harbor;
    run('mountWorkbenchChat()'); await flush();
    assert.equal(dock.settings.label, 'Harbor chat'); assert.equal(dock.settings.board, harbor);
    assert.equal(context.phoneOptions.board, harbor);
  } else if (which === 'deep_route') {
    state.base = state.route = '#/board/atlas/chat'; run('mountWorkbenchChat()'); await flush();
    assert.equal(dock.isOpen(), true); assert.equal(pane.shown[0], 'atlas');
    const refresh = source('async function refresh(', 'function queueDraft(');
    assert.match(refresh, /if \(state.base.startsWith\("#\/board\/"\)\) boardView\(\)/);
    assert.doesNotMatch(refresh, /await chatScreen/);
    dock.close(); observers[0].fn(); await flush();
    assert.equal(state.base, '#/board/atlas'); assert.equal(context.location.hash, '#/board/atlas');
  } else if (which === 'baseline') {
    // Fresh session with old history establishes a baseline, rather than attention.
    service.clear(); rows = Array.from({length:20}, (_, i) => message(i+1)); await service.pollUnread();
    assert.equal(service.unread, 0);
    const value = JSON.parse(storage.get('cmmn.chat.seen.maya.atlas'));
    assert.deepEqual(value, {message:20, reply:0});
  } else if (['arrivals', 'deduplicate', 'private_replies'].includes(which)) {
    rows = [message(21), message(22, 'user:leon')]; messageCursor = 22;
    replies = [{id:1, kind:'receipt'}, {id:2, kind:'answer', for_user:'maya'}, {id:3, kind:'handoff', for_user:'maya'}, {id:4, kind:'answer', for_user:'leon'}]; replyCursor = 4;
    await service.pollUnread();
    assert.equal(service.unread, 3);
    assert.ok(requests.some(({path}) => path === '/api/chat/atlas?after=20&replies_after=0'));
    if (which === 'deduplicate') {await service.pollUnread(); assert.equal(service.unread, 3);}
    if (which === 'private_replies') assert.equal(dock.updates.at(-1).unread, 3);
  } else if (which === 'visibility') {
    assert.equal(typeof handlers.get('visibilitychange'), 'function');
    document.hidden = true; handlers.get('visibilitychange')(); requests = [];
    await service.pollUnread(); assert.equal(requests.length, 0); assert.equal(timers.size, 0);
    document.hidden = false; handlers.get('visibilitychange')(); await flush();
    assert.equal(requests.length, 2);
  } else if (which === 'cadence') {
    assert.equal(timers.size, 1); const timer = [...timers.values()][0];
    assert.equal(timer.ms, 15000); requests = []; await timer.fn(); await flush();
    assert.equal(requests.length, 2);
  } else if (which === 'events') {
    assert.match(app, /events\.some\(\(event\) => event.kind === "space_message" \|\| event.kind === "space_replied"\)\) void state.workbenchChat\?\.pollUnread\(\)/);
  } else if (which === 'closed_pane') {
    requests = []; let done = false;
    const pending = chatOptions.api('/api/chat/atlas?after=20').then(() => {done = true;});
    await flush(); assert.equal(requests.length, 0); assert.equal(done, false);
    dock.opened = true; observers[0].fn(); await pending;
    assert.equal(requests.length, 1);
  } else if (which === 'inflight_close') {
    dock.opened = true; let resolve;
    apiHook = () => new Promise((r) => {resolve=r;});
    const pending = chatOptions.api('/api/chat/atlas?after=20'); await flush();
    assert.equal(typeof resolve, 'function'); dock.opened = false;
    resolve({messages:[message(21)], cursor:21}); let done=false; pending.then(() => {done=true;});
    await flush(); assert.equal(done, false);
    dock.opened = true; observers[0].fn(); await pending;
  } else if (which === 'visible_read') {
    dock.opened = true; rows=[message(21)]; messageCursor=21;
    await chatOptions.api('/api/chat/atlas?after=20'); chatOptions.onState({unread:1});
    assert.equal(JSON.parse(storage.get('cmmn.chat.seen.maya.atlas')).message, 21);
    assert.equal(service.unread, 0); assert.equal(context.phoneOptions.unread, 0);
    dock.opened=false; chatOptions.onState({unread:10});
    assert.equal(JSON.parse(storage.get('cmmn.chat.seen.maya.atlas')).message, 21);
  } else if (which === 'details') {
    const row = {id:'chat-row-request-20-5', dataset:{}, closest: () => ({querySelector: () => ({textContent:'Atlas orchestrator · 14:03'})})};
    assert.equal(typeof chatOptions.onCard, 'function'); chatOptions.onCard(5,row);
    assert.equal(state.chatDetail.row, row); assert.equal(context.location.hash, '#/card/5');
    assert.equal(row.dataset.src, 'Atlas orchestrator · 14:03');
    state.route = '#/card/5'; dock.opened = true; observers[0].fn();
    assert.equal(state.route, '#/board/atlas'); assert.equal(context.location.hash, '#/board/atlas');
    assert.match(app, /dock.close\(\{ toDetails: true, row: chatOrigin \}\)/);
    assert.match(app, /row: chatOrigin.id, source: chatOrigin.dataset.src/);
  } else if (which === 'signout') {
    storage.set('cmmn.chat.seen.leon.atlas', '{"message":99,"reply":0}');
    assert.equal(typeof context.clearWorkbenchChat, 'function'); run('clearWorkbenchChat()');
    assert.equal(cleared, 1); assert.equal(storage.has('cmmn.chat.seen.maya.atlas'), false);
    assert.equal(storage.has('cmmn.chat.seen.leon.atlas'), true); assert.equal(state.workbenchChat, null);
    let adminStops = 0;
    context.adminUI = {stop() { adminStops++; }};
    run('clearWorkbenchChat()');
    assert.equal(adminStops, 1, 'sign-out stops an existing Admin controller');
    assert.match(app, /action === "logout"[\s\S]*?clearWorkbenchChat\(\)/);
    assert.match(source("async function signin(", "function currentBoard()"), /clearWorkbenchChat\(\)/);
  } else if (which === 'dispose') {
    requests=[]; service.dispose(); await service.pollUnread();
    assert.equal(dock.destroyed, 1); assert.equal(handlers.has('visibilitychange'), false);
    assert.equal(timers.size, 0); assert.equal(observers[0].disconnected, true); assert.equal(requests.length, 0);
  } else if (which === 'board_switch') {
    let resolve; apiHook = (path) => path.endsWith('/status') ? {} : new Promise((r) => {resolve=r;});
    const pending = service.pollUnread(); await flush(); assert.equal(typeof resolve, 'function');
    selectedBoard = harbor; run('mountWorkbenchChat()');
    resolve({messages:[message(99)], replies:[], cursor:99}); await pending; await flush();
    assert.equal(service.board.slug, 'harbor'); assert.equal(service.unread, 0);
    service.dispose();
  } else if (which === 'seen_pages') {
    for (let i = 0; i < 5; i++) await flush();
    assert.ok(requests.some(({path}) => path === '/api/chat/atlas?after=100&replies_after=0'), 'first closed poll must read after the stored seen id');
    assert.ok(requests.some(({path}) => path === '/api/chat/atlas?after=200&replies_after=0'), 'a full page must be followed');
    assert.equal(service.unread, 150);
  } else if (which === 'chat_destroy') {
    service.dispose(); assert.equal(pane.destroyed, 1);
  } else assert.fail(`Unknown case: ${which}`);
  state.workbenchChat?.dispose();
}
console.log(`${which}: PASS`);
