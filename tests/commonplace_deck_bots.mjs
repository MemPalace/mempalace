import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { createBots, stripHTML, rosterHTML, projectWork, projectHTML } from '../commonplace/web/static/bots.js';
import { escape as e } from '../commonplace/web/static/api.js';

const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };
const bot = (id, extra = {}) => ({ id, name: id === 'atlas' ? 'Atlas' : 'Harbor', job: 'Website and sign-in', state: 'idle', ...extra });
function harness({ deck = true, bots = [bot('atlas', { board: 'northwind' }), bot('harbor')], readProject = async () => ({ cards: [], children: [] }), readThread } = {}) {
  const data = new Map();
  globalThis.sessionStorage = { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: (k) => data.delete(k) };
  globalThis.document = { documentElement: { dataset: deck ? { ui: 'deck' } : {} } };
  const box = { value: '', style: {}, scrollHeight: 24, focuses: 0, focus() { this.focuses++; } };
  const regions = Object.fromEntries(['roster', 'model', 'head', 'messages', 'waiting', 'notice', 'composer', 'send', 'sheet'].map((name) => [name, { innerHTML: '', querySelector: () => null }]));
  regions.composer.querySelector = () => box;
  const root = { isConnected: true, setAttribute() {}, addEventListener() {} };
  const mount = { innerHTML: '', querySelector(q) { return q === '.bots' ? root : regions[/data-region="(\w+)"/.exec(q)?.[1]]; } };
  const calls = [], timers = new Map();
  const ui = createBots({ mount: () => mount, e, hidden: () => false, viewport: null,
    setTimer: (fn, delay) => { timers.set(delay, fn); return delay; }, clearTimer: (id) => timers.delete(id),
    api: async (path, options = {}) => {
      calls.push([path, options.method || 'GET']);
      if (path === '/api/bots') return { bots };
      if (path.startsWith('/api/boards/')) return readProject(path);
      if (path.includes('/messages')) return readThread ? readThread(path) : { bot: bots.find((b) => path.includes(`/${b.id}/`)), messages: [], waiting: [] };
      throw new Error(`Unexpected call ${path}`);
    }, send: () => { throw new Error('A draft must never send'); },
  });
  const draft = (id, text) => data.set('cmmn.deck.draft', JSON.stringify({ bot: id, text }));
  return { ui, regions, box, data, draft, calls, timers };
}

for (const deck of [true, false]) test(`draft hand-off fills once, preserves exact text and never sends (${deck ? 'Deck' : 'classic'})`, async () => {
  const h = harness({ deck });
  const text = '  Please fix this\n<exactly as written>  ';
  h.draft('atlas', text);
  await h.ui.show('atlas');
  assert.equal(h.box.value, text);
  assert.equal(h.box.focuses, 1);
  assert.equal(h.data.has('cmmn.deck.draft'), false);
  h.box.value = 'My edit';
  await h.ui.show('atlas');
  assert.equal(h.box.value, 'My edit');
  assert.equal(h.box.focuses, 1);
  assert.ok(h.calls.every(([, method]) => method === 'GET'));
  h.draft('atlas', 'A second hand-off');
  await h.ui.show('atlas');
  assert.equal(h.box.value, 'A second hand-off', 'same-route hand-offs are consumed too');
  assert.equal(h.box.focuses, 2);
  h.ui.stop();
});

test('a draft for another bot survives until that thread opens', async () => {
  const h = harness(); h.draft('harbor', 'Mobile question');
  await h.ui.show('atlas');
  assert.equal(h.data.has('cmmn.deck.draft'), true);
  assert.equal(h.box.value, '');
  await h.ui.show('harbor');
  assert.equal(h.box.value, 'Mobile question');
  assert.equal(h.data.has('cmmn.deck.draft'), false);
  h.ui.stop();
});

test('malformed drafts are silently removed; unavailable storage is harmless', async () => {
  for (const raw of ['{bad', 'null', '[]', '{"bot":"atlas","text":1}']) {
    const h = harness(); h.data.set('cmmn.deck.draft', raw);
    await h.ui.show('atlas');
    assert.equal(h.data.has('cmmn.deck.draft'), false);
    assert.equal(h.box.value, ''); h.ui.stop();
  }
  const h = harness();
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() { throw new Error('Denied'); } });
  await h.ui.show('atlas'); h.ui.stop();
  delete globalThis.sessionStorage;
});

test('a failed thread load keeps its draft for a retry', async () => {
  let fail = true;
  const h = harness({ readThread: async () => { if (fail) throw new Error('Offline'); return { bot: bot('atlas'), messages: [], waiting: [] }; } });
  h.draft('atlas', 'Keep me');
  await h.ui.show('atlas'); assert.equal(h.data.has('cmmn.deck.draft'), true);
  fail = false; h.ui.click({ dataset: { botsAction: 'retry' } }); await settle();
  assert.equal(h.box.value, 'Keep me'); assert.equal(h.data.has('cmmn.deck.draft'), false);
  h.ui.stop();
});

test('strip links every bot, highlights the open bot, and offers only a project bot the glance', () => {
  const bots = [bot('atlas', { board: 'northwind', state: 'needs_you' }), bot('harbor')];
  const html = stripHTML(bots, bots[0], e);
  for (const id of ['atlas', 'harbor']) assert.match(html, new RegExp(`href="#/bots/${id}"`));
  assert.equal((html.match(/aria-current="page"/g) || []).length, 1);
  assert.match(html, /aria-label="Waiting on you"/);
  assert.match(html, /What Atlas is doing/);
  assert.doesNotMatch(stripHTML(bots, bots[1], e), /deck-project-link/);
  const bad = bot('a"<>', { name: '<img>', board: 'p' });
  assert.doesNotMatch(stripHTML([bad], bad, e), /<img>|style="|<style/);
  assert.match(stripHTML([], bad, e), /What &lt;img&gt; is doing/);
});

test('tile additions are hidden in classic and expose job and state words for Deck', () => {
  const html = rosterHTML({ bots: [bot('atlas', { state: 'needs_you' }), bot('harbor', { state: 'working' }), bot('general')] }, e);
  assert.match(html, /class="deck-bot-job" hidden>Website and sign-in/);
  for (const word of ['Waiting on you', 'Working', 'Idle']) assert.ok(html.includes(word));
});

test('project status mapping follows bot work blocks, including precedence over waiting', () => {
  const cases = [
    [{ status: 'review', review_kind: 'decision', need: 'you' }, 'needs_you'],
    [{ status: 'review', review_kind: 'completion', waiting: { text: 'Checks' } }, 'ready'],
    [{ status: 'review', review_kind: 'decision', need: 'other' }, 'blocked'],
    [{ status: 'doing' }, 'working'], [{ status: 'backlog' }, 'queued'], [{ status: 'todo' }, 'queued'],
    [{ status: 'doing', waiting: { text: 'Waiting for checks' } }, 'blocked'], [{ status: 'done', waiting: {} }, 'done'],
  ];
  for (const [input, expected] of cases) assert.equal(projectWork(input).status, expected);
  assert.equal(projectWork({ status: 'review', review_kind: 'decision', need: 'other', status_line: 'Old note' }).note, 'Waiting for an answer');
  assert.equal(projectWork({ status: 'todo', status_line: 'One\ntwo' }).note, 'One two');
  assert.equal(projectWork({ status: 'doing', waiting: { code: 'claim_expired', text: 'Claim expired; hand the card off' } }).note, 'Claim expired; hand the task off');
});

test('project groups use titles and chips, include only this week’s dated completions, and escape text', () => {
  const now = new Date(2026, 10, 11, 12);
  const items = [
    { number: 42, title: 'Approve release', status: 'review', review_kind: 'decision', need: 'you' },
    { title: 'Review changes', status: 'review', review_kind: 'completion' },
    { title: '<img src=x>', status: 'doing', status_line: '<script>\nSecond line' },
    { title: 'Waiting', status: 'todo', waiting: { text: 'Waiting for a person' } },
    { title: 'Next task', status: 'backlog' },
    { title: 'Finished Tuesday', status: 'done', done_at: new Date(2026, 10, 10, 12).toISOString() },
    { title: 'Before Monday', status: 'done', done_at: new Date(2026, 10, 8, 12).toISOString() },
    { title: 'Unknown date', status: 'done' }, { title: 'Bad date', status: 'done', done_at: 'bad' },
    { title: 'Future', status: 'done', done_at: new Date(2026, 10, 12).toISOString() },
  ];
  const html = projectHTML({ items }, bot('atlas'), e, now);
  const sections = [...html.matchAll(/<section[^>]*><h3>(.*?)<\/h3>(.*?)<\/section>/g)];
  assert.deepEqual(sections.map((m) => m[1]), ['Needs you', 'Working', 'Blocked', 'Queued', 'Done this week']);
  assert.match(sections[0][2], /Approve release[^]*chip ready">Ready[^]*Review changes/);
  assert.match(sections[4][2], /Finished Tuesday/);
  assert.doesNotMatch(html, /Before Monday|Unknown date|Bad date|Future|<img|<script|#42|>42</);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /&lt;script&gt; Second line/);
  assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /\b(card|board|ticket)s?\b/i);
  assert.doesNotMatch(projectHTML({ items: items.filter((i) => i.status !== 'done') }, bot('atlas'), e, now), /Done this week/);
  assert.match(projectHTML({ items: [] }, bot('atlas'), e), /No open work/);
});

test('glance reads the person’s project API once, includes children, and closes on Escape', async () => {
  const h = harness({ readProject: async (path) => {
    assert.equal(path, '/api/boards/northwind/cards');
    return { cards: [{ title: 'Parent work', status: 'todo' }], children: [{ title: 'Child work', status: 'doing' }] };
  } });
  await h.ui.show('atlas');
  h.ui.click({ dataset: { botsAction: 'project' } }); await settle();
  assert.ok(h.regions.sheet.innerHTML.includes('Parent work') && h.regions.sheet.innerHTML.includes('Child work'));
  assert.deepEqual(h.calls.filter(([path]) => path.startsWith('/api/boards/')), [['/api/boards/northwind/cards', 'GET']]);
  h.ui.keydown({ key: 'Escape', preventDefault() {} });
  assert.equal(h.regions.sheet.innerHTML, ''); h.ui.stop();
});

test('no glance requests in classic or without a project; errors and late responses stay in their sheet', async () => {
  for (const options of [{ deck: false }, { bots: [bot('atlas')] }]) {
    const h = harness(options); await h.ui.show('atlas');
    h.ui.click({ dataset: { botsAction: 'project' } }); await settle();
    assert.equal(h.calls.some(([path]) => path.startsWith('/api/boards/')), false); h.ui.stop();
  }
  const h = harness({ readProject: async () => { throw new Error('Private server detail'); } });
  await h.ui.show('atlas'); h.ui.click({ dataset: { botsAction: 'project' } }); await settle();
  assert.match(h.regions.sheet.innerHTML, /work is unavailable/);
  assert.doesNotMatch(h.regions.sheet.innerHTML, /Private server detail/); h.ui.stop();
  let resolve;
  const late = harness({ readProject: () => new Promise((r) => { resolve = r; }) });
  await late.ui.show('atlas'); late.ui.click({ dataset: { botsAction: 'project' } });
  assert.match(late.regions.sheet.innerHTML, /Loading/);
  late.ui.click({ dataset: { botsAction: 'close' } });
  resolve({ cards: [{ title: 'Late work', status: 'todo' }] }); await settle();
  assert.equal(late.regions.sheet.innerHTML, ''); late.ui.stop();
});

test('new CSS is entirely Deck-scoped, token-based, and linked directly after deck.css', () => {
  const read = (name) => readFileSync(new URL(`../commonplace/web/static/${name}`, import.meta.url), 'utf8');
  const css = read('deck-bots.css').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const match of css.matchAll(/([^{}]+)\{/g)) {
    if (match[1].trim().startsWith('@media')) continue;
    for (const selector of match[1].split(',')) assert.match(selector.trim(), /^:root\[data-ui="deck"\]/);
  }
  assert.doesNotMatch(css, /#[\da-f]{3,8}\b|linear-gradient|backdrop-filter/i);
  assert.match(css, /repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /max-width: 699px/);
  assert.match(css, /100% - 760px/);
  assert.match(css, /deck-bot-state\[hidden\][^}]*display: block !important/);
  assert.match(css, /bb\.decision\[data-state="open"\]::before[^]*content: "Waiting on you"/);
  assert.match(read('index.html'), /href="\/app\/deck.css">\s*<link rel="stylesheet" href="\/app\/deck-bots.css">/);
});
