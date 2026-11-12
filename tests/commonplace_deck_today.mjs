// Today is driven through its real mount and DOM events with a small, persistent form DOM.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { escape as e } from '../commonplace/web/static/api.js';
import { mount, questionsFrom, questionHTML, workingHTML, summary } from '../commonplace/web/static/deck-today.js';
import { createDeck, SCREENS, deckHome, deckSections, UI_KEY } from '../commonplace/web/static/deck.js';

const question = (extra = {}) => ({ type: 'decision', card: 60, version: 3, open: true, question: 'Ship the login fix today?', context: 'This changes the public site.', options: ['Ship today', 'Not now'], ...extra });
const bot = (id, extra = {}) => ({ id, name: id === 'atlas' ? 'Atlas' : id === 'general' ? 'General' : 'Billing', job: 'Website and sign-in', kind: 'project', waiting: 1, state: 'idle', last: null, ...extra });
const bots = [bot('general', { kind: 'general' }), bot('atlas'), bot('billing')];
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function dom() {
  const regions = new Map(['summary', 'waiting', 'working', 'draft-error'].map((name) => [name, { innerHTML: '', textContent: '', hidden: true }]));
  const text = { value: '', selectionStart: 0, selectionEnd: 0 };
  const picker = { innerHTML: '', value: '', disabled: true }, send = { disabled: true };
  const form = { elements: { bot: picker, text }, querySelector: () => send };
  const listeners = new Map(), classes = new Set();
  const root = {
    html: '', paints: 0, classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    set innerHTML(value) { this.html = value; this.paints++; },
    querySelector(q) { return q === 'form' ? form : regions.get(/data-today="([^"]+)"/.exec(q)?.[1]); },
    addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: (type, fn) => { assert.equal(listeners.get(type), fn); listeners.delete(type); },
  };
  return { root, regions, text, picker, form, send, listeners, classes,
    click(n, choice) { listeners.get('click')?.({ preventDefault() {}, target: { closest: (q) => q === '[data-today-decide]' ? { dataset: { todayDecide: String(n), choice } } : null } }); },
    submit() { listeners.get('submit')?.({ target: form, preventDefault() {} }); },
  };
}
function fixture({ api: custom, roster = bots, pages = {}, boards = [] } = {}) {
  const d = dom(), calls = [], routes = [];
  const api = async (path, options = {}) => {
    calls.push({ path, ...options });
    if (custom) return custom(path, options);
    if (path === '/api/bots') return { bots: roster };
    if (path.includes('/messages?limit=1')) return { waiting: pages[path.split('/')[3]] || [question()] };
    return { ok: true };
  };
  const ui = mount({ root: d.root, api, e, me: { name: 'Maya' }, boards, needs: {}, go: (hash) => routes.push(hash) });
  return { ...d, ui, calls, routes };
}
const waiting = (f) => f.regions.get('waiting').innerHTML;

test('registered Today is first and home; an off switch never imports or mounts it', async () => {
  assert.equal(deckHome(), '#/today');
  assert.equal(deckSections()[0][0], 'today');
  assert.equal((await SCREENS.today()).TITLE, 'Today');
  let loads = 0;
  const ui = createDeck({ screens: { today: () => { loads++; } }, storage: { getItem: () => null }, doc: {} });
  assert.equal(ui.owns('#/today'), false);
  assert.equal(loads, 0);
  const app = readFileSync(new URL('../commonplace/web/static/app.js', import.meta.url), 'utf8');
  assert.match(app, /if \(\/\^#\\\/\(\?:today\|compute\)\$\/\.test\(state.base\) && !state.deck\?\.owns\(state.base\)\) state.base = "#\/"/);
});

test('one open question per task, project owner preferred in either order', async () => {
  const pages = bots.map((b) => ({ bot: b, waiting: [question()] }));
  for (const list of [pages, pages.toReversed()]) {
    const result = questionsFrom(list);
    assert.equal(result.length, 1);
    assert.equal(result[0].bot.kind, 'project');
  }
  assert.deepEqual(questionsFrom([{ bot: bots[0], waiting: [question({ open: false }), question({ gone: true }), question({ type: 'work' }), question({ card: 0 })] }]), []);
  const f = fixture({ roster: bots.slice(0, 2) }); await flush();
  assert.equal((waiting(f).match(/data-question="60"/g) || []).length, 1);
  assert.match(waiting(f), /<b>Atlas<\/b>/);
  assert.equal(f.picker.value, 'atlas', 'first project, even when General is first');
  assert.deepEqual(f.calls.map((c) => c.path), ['/api/bots', '/api/bots/general/messages?limit=1', '/api/bots/atlas/messages?limit=1']);
  f.ui.stop();
});

test('one tap posts one exact versioned decision, locks duplicate taps, settles until refresh', async () => {
  const post = deferred();
  const f = fixture({ api: (path) => path === '/api/bots' ? { bots: [bot('atlas')] } : path.includes('/messages') ? { waiting: [question()] } : post.promise });
  await flush();
  f.click(60, 'Ship today'); f.click(60, 'Ship today');
  const writes = f.calls.filter((c) => c.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/api/cards/60/decide');
  assert.deepEqual(writes[0].body, { version: 3, choice: 'Ship today' });
  assert.match(waiting(f), / disabled/);
  post.resolve({ ok: true }); await flush();
  assert.match(waiting(f), /data-state="settled"/);
  assert.match(waiting(f), /Answered: Ship today/);
  f.click(60, 'Ship today');
  assert.equal(f.calls.filter((c) => c.method === 'POST').length, 1);
  await f.ui.refresh({ boards: [] });
  assert.doesNotMatch(waiting(f), /data-question="60"/);
  assert.match(waiting(f), /Nothing waits on you/);
  f.ui.stop();
});

test('a failed answer stays open, shows escaped error and can be retried', async () => {
  let fail = true;
  const f = fixture({ api: (path) => {
    if (path === '/api/bots') return { bots: [bot('atlas')] };
    if (path.includes('/messages')) return { waiting: [question()] };
    if (fail) throw new Error('Offline <try again>');
    return { ok: true };
  } });
  await flush(); f.click(60, 'Ship today'); await flush();
  assert.match(waiting(f), /data-state="open"/);
  assert.match(waiting(f), /role="alert">Offline &lt;try again&gt;/);
  assert.doesNotMatch(waiting(f), / disabled|data-state="settled"/);
  fail = false; f.click(60, 'Not now'); await flush();
  assert.match(waiting(f), /Answered: Not now/);
  f.ui.stop();
});

test('409 refetches the latest question and its version before another answer', async () => {
  let version = 3;
  const f = fixture({ api: (path, options) => {
    if (path === '/api/bots') return { bots: [bot('atlas')] };
    if (path.includes('/messages')) return { waiting: [question({ version, question: version === 3 ? 'Ship?' : 'Wait for the extra check?' })] };
    if (options.body.version === 3) { version = 4; throw Object.assign(new Error('Conflict'), { status: 409 }); }
    return { ok: true };
  } });
  await flush(); f.click(60, 'Ship today'); await flush();
  assert.match(waiting(f), /That question changed. Showing the latest./);
  assert.match(waiting(f), /Wait for the extra check/);
  assert.doesNotMatch(waiting(f), /data-state="settled"/);
  f.click(60, 'Not now'); await flush();
  assert.deepEqual(f.calls.filter((c) => c.method === 'POST').map((c) => c.body.version), [3, 4]);
  f.ui.stop();
});

test('refresh preserves the live composer, caret and recipient; submit hands off its draft', async () => {
  const data = new Map(); globalThis.sessionStorage = { setItem: (k, v) => data.set(k, v) };
  const f = fixture(); await flush();
  f.text.value = 'Please fix the footer'; f.text.selectionStart = 4; f.text.selectionEnd = 8;
  f.picker.value = 'billing';
  await f.ui.refresh({ boards: [{ slug: 'atlas', name: 'Atlas', agents_working: 2 }] });
  assert.equal(f.root.paints, 1, 'the form and focused input are never replaced');
  assert.equal(f.form.elements.text, f.text);
  assert.deepEqual([f.text.value, f.text.selectionStart, f.text.selectionEnd, f.picker.value], ['Please fix the footer', 4, 8, 'billing']);
  f.submit();
  assert.deepEqual(JSON.parse(data.get('cmmn.deck.draft')), { bot: 'billing', text: 'Please fix the footer' });
  assert.deepEqual(f.routes, ['#/bots/billing']);
  assert.equal(f.calls.filter((c) => c.method === 'POST').length, 0);
  f.ui.stop(); delete globalThis.sessionStorage;
});

test('unavailable draft storage keeps the composer and reports a plain error', async () => {
  globalThis.sessionStorage = { setItem() { throw new Error('denied'); } };
  const f = fixture(); await flush(); f.text.value = 'Please help'; f.submit();
  assert.equal(f.text.value, 'Please help'); assert.deepEqual(f.routes, []);
  assert.equal(f.regions.get('draft-error').hidden, false);
  f.ui.stop(); delete globalThis.sessionStorage;
});

test('working bots and project agent counts use only reported data, with honest empty states', async () => {
  const bs = [bot('harbor', { waiting: 0, state: 'working', last: { text: 'Fixing push tokens', at: new Date(Date.now() - 120000).toISOString() } }), bot('atlas', { waiting: 0 })];
  const boards = [{ slug: 'harbor', name: 'Harbor', agents_working: 2 }];
  const f = fixture({ roster: bs, boards }); await flush();
  assert.match(f.regions.get('working').innerHTML, /Fixing push tokens/);
  assert.match(f.regions.get('working').innerHTML, /2 agents working/);
  assert.match(f.regions.get('working').innerHTML, /<time[^>]+>2m<\/time>/);
  assert.equal(f.calls.length, 1, 'no thread read for a bot with no waiting questions');
  assert.equal(summary([], bs, boards), '0 things wait on you. 2 agents are working.');
  assert.equal(summary([question()], bs, []), '1 thing waits on you. 1 bot is working.');
  assert.match(workingHTML([], [], e), /No agents are working right now./);
  assert.doesNotMatch(workingHTML([], [], e), /Finished/);
  f.ui.stop();
  const empty = fixture({ roster: [] }); await flush();
  assert.match(waiting(empty), /Nothing waits on you./);
  assert.equal(empty.send.disabled, true); empty.ui.stop();
});

test('server strings are escaped, ids encoded and at most one affirmative option is inverted', () => {
  const q = { ...question({ question: '<img onerror="bad">', context: '<script>bad</script>', options: ['Accept', 'Approve <all>'] }), bot: bot('a/b', { name: '<Atlas>', job: '"job"' }) };
  const html = questionHTML(q, {}, e);
  assert.doesNotMatch(html, /<img|<script|<Atlas>/);
  assert.match(html, /&lt;img/); assert.match(html, /#\/bots\/a%2Fb/);
  assert.equal((html.match(/class="today-option inverted"/g) || []).length, 1);
  assert.doesNotMatch(questionHTML({ ...q, options: ['Euros', 'Dollars'] }, {}, e), /inverted/);
});

test('late reads never overwrite a newer refresh, stop aborts reads and writes and unbinds listeners', async () => {
  const old = deferred(); let reads = 0;
  const f = fixture({ api: (path) => {
    if (path === '/api/bots') return ++reads === 1 ? old.promise : { bots: [bot('atlas')] };
    return { waiting: [question()] };
  } });
  await f.ui.refresh(); old.resolve({ bots: [] }); await flush();
  assert.match(waiting(f), /Ship the login/);
  const pending = deferred();
  const g = fixture({ api: (path) => path === '/api/bots' ? { bots: [bot('atlas')] } : path.includes('/messages') ? { waiting: [question()] } : pending.promise });
  await flush(); g.click(60, 'Ship today');
  const before = waiting(g); g.ui.stop();
  assert.ok(g.calls.every((c) => c.signal.aborted));
  assert.equal(g.listeners.size, 0); assert.equal(g.classes.has('deck-today'), false);
  pending.resolve({ ok: true }); await flush(); assert.equal(waiting(g), before);
  const count = g.calls.length; await g.ui.refresh(); assert.equal(g.calls.length, count);
  f.ui.stop();
});

test('a partial or failed load shows a retry and keeps previously loaded questions', async () => {
  let fail = false;
  const f = fixture({ api: (path) => {
    if (path === '/api/bots') return { bots: [bot('atlas')] };
    if (fail) throw new Error('Offline');
    return { waiting: [question()] };
  } });
  await flush(); fail = true; await f.ui.refresh();
  assert.match(waiting(f), /data-question="60"/);
  assert.match(waiting(f), /Could not load every question/);
  assert.match(waiting(f), /data-today-retry/); f.ui.stop();
});

test('stop ignores a pending read; one constructed sheet scopes every rule and adds no inline styles', async () => {
  const pending = deferred();
  const sheets = [];
  let css = '';
  globalThis.document = { adoptedStyleSheets: sheets };
  globalThis.CSSStyleSheet = class { replaceSync(text) { css = text; } };
  const f = fixture({ api: () => pending.promise });
  const before = waiting(f);
  f.ui.stop();
  pending.resolve({ bots }); await flush();
  assert.equal(waiting(f), before);
  assert.equal(f.calls[0].signal.aborted, true);
  const g = fixture({ roster: [] }); await flush();
  assert.equal(document.adoptedStyleSheets.length, 1);
  for (const line of css.split('\n').map((s) => s.trim()).filter(Boolean)) {
    if (line.startsWith('@media') || line === '}') continue;
    for (const selector of line.split('{')[0].split(',')) assert.match(selector.trim(), /^\.deck-today\b/);
  }
  assert.doesNotMatch(f.root.html, /<style|style="/);
  assert.doesNotMatch(css, /#[\da-f]{3,8}\b/i);
  g.ui.stop(); delete globalThis.document; delete globalThis.CSSStyleSheet;
});
