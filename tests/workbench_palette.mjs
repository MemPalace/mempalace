import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import vm from 'node:vm';

const staticURL = new URL('../commonplace/web/static/', import.meta.url);
assert.ok(existsSync(new URL('palette.js', staticURL)), 'Palette module missing');
const dataURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const apiURL = dataURL(readFileSync(new URL('api.js', staticURL), 'utf8'));
const boardURL = dataURL(readFileSync(new URL('board.js', staticURL), 'utf8').replace("'./api.js'", JSON.stringify(apiURL)));
const source = readFileSync(new URL('palette.js', staticURL), 'utf8');
const ui = await import(dataURL(source.replace("'./api.js'", JSON.stringify(apiURL)).replace("'./board.js'", JSON.stringify(boardURL))));
for (const name of ['paletteItems', 'createPalette']) assert.equal(typeof ui[name], 'function', `${name} missing`);
const board = {slug: 'atlas', name: 'Atlas'};
const cards = [
  {number: 121, board: 'atlas', title: 'Offline queue', status: 'review', ask: 'Which limit? <exact>', ask_to: 'user:leon'},
  {number: 134, board: 'atlas', title: 'Draft release notes', status: 'todo', message_id: 7},
  {number: 135, board: 'atlas', title: 'Check mail', status: 'todo'},
];
const options = {board, boards: [board, {slug: 'harbor', name: 'Harbor'}], cards};
const items = (query = '', extra = {}) => ui.paletteItems({...options, query, ...extra});
const has = (list, id) => list.some((item) => item.id === id);

// A small event-capable dialog double keeps these tests independent of a browser.
class Node {
  constructor(attrs = {}) { this.attrs = attrs; this.handlers = new Map(); this.dataset = {}; this.isConnected = true; this.value = ''; this.textContent = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() { document.activeElement = this; }
  addEventListener(k, fn) { if (!this.handlers.has(k)) this.handlers.set(k, new Set()); this.handlers.get(k).add(fn); }
  removeEventListener(k, fn) { this.handlers.get(k)?.delete(fn); }
  emit(type, extra = {}) {
    const event = {type, target: this, prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {}, ...extra};
    for (const fn of this.handlers.get(type) || []) fn(event);
    return event;
  }
  closest(selector) {
    if (selector.includes('[data-palette-index]') && this.dataset.paletteIndex != null) return this;
    if (selector.includes('[data-palette-action]') && this.dataset.paletteAction) return this;
    return null;
  }
  scrollIntoView() {}
}
class Dialog extends Node {
  constructor() { super(); this.open = false; this.classes = new Set(); this.classList = {add: (...xs) => xs.forEach(x => this.classes.add(x)), remove: (...xs) => xs.forEach(x => this.classes.delete(x))}; }
  set innerHTML(value) {
    this.html = value;
    this.nodes = new Map();
    if (value.includes('role="combobox"')) this.nodes.set('[role="combobox"]', new Node());
    if (value.includes('role="listbox"')) this.nodes.set('[role="listbox"]', new Node());
    if (value.includes('role="switch"')) this.nodes.set('[role="switch"]', new Node({'aria-checked': /role="switch" aria-checked="(true|false)"/.exec(value)[1]}));
    for (const [, action] of value.matchAll(/data-palette-action="([^"]+)"/g)) {
      const node = new Node(); node.dataset.paletteAction = action;
      this.nodes.set(`[data-palette-action="${action}"]`, node);
    }
    this.nodes.set('.state', new Node());
  }
  get innerHTML() { return this.html; }
  querySelector(selector) {
    if (selector === '.state') return this.nodes.get(selector);
    if (selector.startsWith('[data-palette-index="')) {
      const node = new Node(); node.dataset.paletteIndex = /"(\d+)"/.exec(selector)[1]; return node;
    }
    return this.nodes.get(selector) || null;
  }
  querySelectorAll(selector) {
    if (selector.includes('button') || selector.includes('input')) return [...this.nodes].filter(([key]) => key === '[role="combobox"]' || key === '[role="switch"]' || key.startsWith('[data-palette-action=')).map(([, node]) => node);
    return [];
  }
  contains(node) { return [...this.nodes.values()].includes(node); }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit('close'); }
}
const opener = new Node();
globalThis.document = {activeElement: opener};
const store = new Map();
const storage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v)};
const dialog = new Dialog(), commands = [], changes = [];
const palette = ui.createPalette({dialog, storage, onCommand: item => commands.push(item), onSingleKeysChange: enabled => changes.push(enabled)});
for (const name of ['open', 'shortcuts', 'close', 'stop']) assert.equal(typeof palette[name], 'function', `${name} missing`);
const input = () => { const node = dialog.querySelector('[role="combobox"]'); assert.ok(node, 'Search input missing'); return node; };
const type = (value) => { input().value = value; dialog.emit('input', {target: input()}); };
const key = (key, extra = {}) => dialog.emit('keydown', {key, target: document.activeElement, ...extra});
const click = (action) => { const node = dialog.querySelector(`[data-palette-action="${action}"]`); assert.ok(node, `${action} button missing`); return dialog.emit('click', {target: node}); };
const listHTML = () => { const list = dialog.querySelector('[role="listbox"]'); assert.ok(list, 'Results missing'); return list.innerHTML; };

switch (process.argv[2]) {
  case 'new': {
    const found = items('new');
    assert.ok(has(found, 'new-card')); assert.ok(has(found, 'new-request')); assert.ok(has(found, 'new-project'));
    assert.match(found.find(x => x.id === 'new-card').label, /^New card/);
    assert.match(found.find(x => x.id === 'new-request').label, /^New request/);
    assert.ok(has(items(' NEW '), 'new-request'));
    break;
  }
  case 'forbidden': {
    for (const query of ['', 'approve', 'decline', 'remind', 'due', 'starts after']) {
      const found = items(query, {selected: cards[0]});
      assert.ok(!found.some(x => /approve|decline|remind|due|starts.after/i.test(x.action || '')));
    }
    palette.open({...options, selected: cards[0]});
    assert.doesNotMatch(listHTML(), /Approve|Decline|Starts after|Change due|Remind/);
    break;
  }
  case 'navigation': {
    const found = items();
    for (const href of ['#/rules', '#/console', '#/settings', '#/needs', '#/memory', '#/board/atlas', '#/board/harbor', '#/space/atlas', '#/decide']) assert.ok(found.some(x => x.href === href), href);
    assert.equal(found.find(x => x.id === 'sign-out').action, 'logout');
    assert.ok(has(items('shortcuts'), 'shortcuts'));
    assert.ok(!has(items('', {board: null, boards: [], cards: []}), 'new-card'));
    break;
  }
  case 'ids': {
    for (const q of ['A-121', 'a121', '#121', '121']) {
      const match = items(q).find(x => x.number === 121);
      assert.ok(match, q); assert.equal(match.href, '#/card/121'); assert.match(match.label, /^A-121/);
    }
    assert.ok(items('offline queue').some(x => x.number === 121));
    assert.ok(!items('H-121').some(x => x.number === 121));
    assert.ok(!items('1121').some(x => x.number === 121));
    assert.ok(!items('121', {cards: [...cards, {number: 121, board: 'private', title: 'Hidden'}]}).some(x => x.label.includes('Hidden')));
    break;
  }
  case 'context': {
    const found = items('', {selected: cards[0]});
    assert.deepEqual(found.filter(x => x.group === 'Selected card').map(x => x.action), ['move-card', 'edit-card', 'search-memory', 'copy-card-link']);
    assert.equal(found[0].number, 121);
    assert.equal(found.find(x => x.action === 'search-memory').query, cards[0].ask);
    assert.ok(!items('', {selected: cards[1]}).some(x => x.id === 'search-blocker'));
    assert.ok(!items('', {selected: {...cards[0], board: 'private'}}).some(x => x.group === 'Selected card'));
    break;
  }
  case 'render': {
    palette.open({...options, query: 'new'});
    assert.equal(dialog.open, true); assert.equal(dialog.getAttribute('aria-modal'), 'true');
    assert.equal(dialog.getAttribute('aria-label'), 'Command palette');
    assert.equal(document.activeElement, input()); assert.match(dialog.innerHTML, /role="combobox"/);
    assert.match(listHTML(), /New card in Atlas/); assert.match(listHTML(), /New request/);
    type('zzzz'); assert.match(listHTML(), /Search memory for/); assert.doesNotMatch(listHTML(), /New card/);
    palette.open({...options, board: {...board, name: '<img onerror=bad>'}, cards: [{number: 3, board: 'atlas', title: '<script>bad</script>'}]});
    assert.doesNotMatch(dialog.innerHTML + listHTML(), /<img|<script>/);
    assert.match(listHTML(), /&lt;script&gt;/);
    break;
  }
  case 'keys': {
    palette.open({...options, query: 'new'});
    const first = input().getAttribute('aria-activedescendant'); assert.ok(first);
    assert.equal(key('ArrowDown').prevented, true);
    assert.notEqual(input().getAttribute('aria-activedescendant'), first);
    key('ArrowUp'); assert.equal(input().getAttribute('aria-activedescendant'), first);
    key('ArrowUp'); assert.notEqual(input().getAttribute('aria-activedescendant'), first);
    key('Home'); assert.equal(input().getAttribute('aria-activedescendant'), first);
    assert.equal(key('Tab', {shiftKey: true}).prevented, true);
    assert.notEqual(document.activeElement, input());
    assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, input());
    key('Escape'); assert.equal(dialog.open, false); assert.equal(document.activeElement, opener);
    break;
  }
  case 'dispatch': {
    palette.open({...options, query: 'new'}); key('ArrowDown'); key('Enter');
    assert.equal(commands.length, 1); assert.equal(commands[0].action, 'open-chat'); assert.equal(commands[0].board, 'atlas');
    assert.equal(dialog.open, false);
    palette.open({...options, query: 'rules'});
    const option = new Node(); option.dataset.paletteIndex = '0'; dialog.emit('click', {target: option});
    assert.equal(commands.length, 2); assert.equal(commands[1].href, '#/rules');
    palette.open({...options, query: 'zzzz'}); key('Enter'); assert.equal(commands.length, 3); assert.equal(commands[2].query, 'zzzz');
    palette.open({...options, query: 'new'}); key('Enter', {isComposing: true}); assert.equal(commands.length, 3);
    break;
  }
  case 'switch': {
    palette.shortcuts();
    const toggle = dialog.querySelector('[role="switch"]'); assert.ok(toggle, 'Switch missing');
    assert.equal(document.activeElement, toggle); assert.equal(toggle.getAttribute('aria-checked'), 'true');
    click('single-keys'); assert.equal(toggle.getAttribute('aria-checked'), 'false');
    assert.equal(store.get('cmmn.keys.single'), 'false'); assert.deepEqual(changes, [false]);
    const app = readFileSync(new URL('app.js', staticURL), 'utf8');
    assert.ok(app.includes('function singleKeysEnabled()'));
    const fn = app.slice(app.indexOf('function singleKeysEnabled()'), app.indexOf('function workbenchKey('));
    const state = {singleKeys: changes.at(-1)};
    assert.equal(vm.runInNewContext(`${fn}; singleKeysEnabled()`, {state, localStorage: storage}), false);
    palette.close(); palette.shortcuts(); assert.equal(dialog.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
    click('reset'); assert.equal(store.get('cmmn.keys.single'), 'true'); assert.equal(changes.at(-1), true);
    assert.match(dialog.innerHTML, /Ctrl K and Esc keep working/);
    assert.doesNotMatch(dialog.innerHTML, /Change…|Undo/);
    break;
  }
  case 'lifecycle': {
    palette.open(options); dialog.emit('cancel'); assert.equal(dialog.open, false); assert.equal(document.activeElement, opener);
    palette.shortcuts(); click('done'); assert.equal(dialog.open, false);
    palette.open(options); palette.stop();
    assert.equal(dialog.open, false); assert.equal([...dialog.handlers.values()].reduce((n, set) => n + set.size, 0), 0);
    const broken = {getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); }};
    const second = ui.createPalette({dialog, storage: broken, onCommand() {}, onSingleKeysChange: x => changes.push(x)});
    second.shortcuts(); click('single-keys'); assert.equal(changes.at(-1), false);
    second.close(); second.shortcuts(); assert.equal(dialog.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
    second.stop();
    break;
  }
  case 'nostyle': {
    // The app's CSP (style-src 'self') refuses inline style elements.
    palette.open(options); assert.doesNotMatch(dialog.innerHTML, /<style/i);
    palette.shortcuts(); assert.doesNotMatch(dialog.innerHTML, /<style/i);
    break;
  }
  case 'focus-race': {
    // Browsers fire the dialog's close event after the command ran; a focus the command set must stay.
    class LateDialog extends Dialog { close() { this.open = false; setTimeout(() => this.emit('close'), 0); } }
    const late = new LateDialog(), target = new Node();
    const ui2 = ui.createPalette({dialog: late, storage, onCommand: (item) => { if (item.action === 'new-card') target.focus(); }});
    const keyOn = (k) => late.emit('keydown', {key: k, target: late.querySelector('[role="combobox"]')});
    opener.focus(); ui2.open({...options, query: 'new card'}); keyOn('Enter');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(document.activeElement, target, 'Focus moved by the command was taken back');
    opener.focus(); ui2.open({...options, query: 'rules'}); keyOn('Enter');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(document.activeElement, opener, 'Unclaimed focus must return to the opener');
    ui2.stop();
    break;
  }
  default: assert.fail('Unknown test case');
}
