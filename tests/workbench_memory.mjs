import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const path = new URL('../commonplace/web/static/memory.js', import.meta.url);
assert.ok(await readFile(path, 'utf8').catch(() => null), 'Memory module missing');
const memory = await import(path);
const { setPeople } = await import('../commonplace/web/static/api.js');
for (const fn of ['memoryActions', 'memoryAuthor', 'quoteDraft', 'searchMemory', 'loadNeighbours', 'postMemory', 'linkMemory', 'renderMemory', 'renderMemoryPanel', 'createMemory']) {
  assert.equal(typeof memory[fn], 'function', `${fn} missing`);
}
const board = { slug: 'atlas', name: 'Atlas', wing: 'project_atlas', coordinator: 'conn:lead' };
const other = { slug: 'harbor', name: 'Harbor', wing: 'project_harbor' };
const text = '\n  <Exact> & café 😀\r\n\t"quoted"\n';
const row = { kind: 'chat', id: 17, board: 'atlas', wing: board.wing, text, actor: 'user:leon', author: 'Leon', at: '2026-10-28T16:12:00Z', sourceLabel: 'Atlas chat' };
const card = { number: 21, board: 'atlas', title: 'Sync', status: 'review', ask: 'Which limit?', ask_to: 'user:leon', version: 4 };
const me = 'user:maya';
const panel = (extra = {}) => memory.renderMemoryPanel({ result: row, destination: board, cards: [card], me, ...extra });
const decode = (value) => value.replaceAll('&#13;', '\r').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&quot;', '"').replaceAll('&#39;', "'").replaceAll('&amp;', '&');
setPeople({ 'user:maya': 'Maya', 'user:leon': 'Leon' });
function node() {
  const listeners = new Map();
  return { innerHTML: '', hidden: true, listeners, focusCount: 0,
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) { assert.equal(listeners.get(type), fn); listeners.delete(type); },
    querySelector() { return this; }, focus() { this.focusCount++; },
  };
}
function click(target, attrs) {
  const dataset = attrs;
  target.listeners.get('click')({ target: { closest(selector) { return selector.includes('data-memory-result') ? (dataset.memoryResult !== undefined ? { dataset } : null) : { dataset }; } } });
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const cases = {
  async wings() {
    const visible = panel();
    assert.ok(visible.includes('data-memory-action="chat"'));
    assert.ok(visible.includes('data-memory-post="21"'));
    assert.ok(visible.includes('Message to Atlas chat'));
    assert.ok(!visible.includes('aria-keyshortcuts="L"'));
    for (const destination of [other, {}, { ...board, wing: '' }]) {
      const hidden = panel({ destination });
      assert.ok(!hidden.includes('data-memory-action="chat"'));
      assert.ok(!hidden.includes('data-memory-post='));
      assert.ok(!hidden.includes('data-memory-action="link"'));
      assert.ok(hidden.includes('aria-keyshortcuts="Y"'));
    }
    const note = { ...row, kind: 'note' };
    assert.ok(panel({ result: note }).includes('aria-keyshortcuts="L"'));
    assert.ok(!panel({ result: note, destination: other }).includes('aria-keyshortcuts="L"'));
    const sameWing = { ...other, wing: board.wing };
    assert.equal(memory.memoryActions(row, sameWing, [card], me).quote, true);
    assert.equal(memory.memoryActions(row, sameWing, [card], me).post.length, 0);
    for (const invalid of [{ status: 'doing' }, { ask_to: me }, { review_kind: 'completion' }, { board: 'harbor' }, { ask: '' }, { restricted: true }]) {
      assert.equal(memory.memoryActions(row, board, [{ ...card, ...invalid }], me).post.length, 0);
    }
    let calls = 0;
    await assert.rejects(memory.postMemory({ result: row, destination: other, card, me, api: async () => calls++ }));
    assert.equal(calls, 0);
  },
  async exact() {
    const calls = [];
    await memory.postMemory({ result: row, destination: board, card, me, api: async (...args) => { calls.push(args); return card; } });
    assert.deepEqual(calls, [['/api/cards/21/comments', { method: 'POST', body: { text, version: 4 } }]]);
    assert.deepEqual(Buffer.from(calls[0][1].body.text), Buffer.from(text));
    assert.equal(memory.quoteDraft(row).text, text);
    assert.deepEqual(Object.keys(memory.quoteDraft(row)), ['board', 'wing', 'author', 'at', 'sourceLabel', 'text']);
    const html = panel();
    const body = html.match(/<p class="msg-b">([\s\S]*?)<\/p>/);
    assert.ok(body, 'verbatim message missing');
    assert.equal(decode(body[1]), text);
    assert.ok(!html.includes('<Exact>'));
    assert.ok(!html.includes('is notified'));
  },
  authors() {
    assert.equal(memory.memoryAuthor(row, board), 'Leon');
    assert.equal(memory.memoryAuthor({ actor: 'conn:otto' }, board), 'otto (agent)');
    assert.equal(memory.memoryAuthor({ actor: 'conn:lead' }, board), 'Atlas orchestrator');
    assert.equal(memory.memoryAuthor({ actor: 'user:maya' }, board), 'Maya');
    assert.equal(memory.memoryAuthor({ kind: 'reply', model: 'local:openai_compatible/small', actor: 'conn:lead' }, board), 'Quick reply · local model');
    for (const model of ['anthropic/model', 'openai_compatible/model', 'runner:chat']) {
      assert.equal(memory.memoryAuthor({ kind: 'reply', model }, board), 'Quick reply · hosted provider');
    }
    assert.equal(memory.memoryAuthor({}, board), '');
    assert.equal(memory.memoryAuthor({ kind: 'note', author: 'otto' }, board), 'otto (agent)');
    assert.equal(memory.memoryAuthor({ kind: 'note', author: 'lead' }, board), 'Atlas orchestrator');
  },
  async fanout() {
    const boards = Array.from({ length: 11 }, (_, i) => ({ slug: `project-${i}`, wing: `wing-${i}` }));
    let active = 0, peak = 0;
    const paths = [];
    const result = await memory.searchMemory({ boards, query: 'exact', api: async (path) => {
      active++; peak = Math.max(peak, active); paths.push(path);
      await tick(); active--;
      return { results: [], truncated: false };
    } });
    assert.equal(peak, 4);
    assert.equal(paths.length, 22);
    assert.deepEqual(result.errors, []);
    for (const project of boards) {
      assert.ok(paths.some((path) => path.startsWith(`/api/chat/${project.slug}/search?`)));
      assert.ok(paths.some((path) => new URL(path, 'https://example.test').searchParams.get('theme') === project.wing));
    }
  },
  async filters() {
    const paths = [];
    const api = async (path) => { paths.push(path); return { results: [{ ...row, wing: 'wrong', board: 'wrong' }] }; };
    const found = await memory.searchMemory({ boards: [board, other], query: 'rate limit', project: 'atlas', source: 'chat', from: 'user:leon', since: '2026-10-01T00:00:00Z', api });
    assert.equal(paths.length, 1);
    const params = new URL(paths[0], 'https://example.test').searchParams;
    assert.equal(params.get('source'), 'chat'); assert.equal(params.get('from'), 'user:leon'); assert.equal(params.get('since'), '2026-10-01T00:00:00Z');
    assert.equal(found.results[0].board, 'atlas'); assert.equal(found.results[0].wing, board.wing); assert.equal(found.results[0].author, 'Leon');
    paths.length = 0;
    const notes = [
      { id: 'note-1', theme: board.wing, author: 'user:leon', text, at: row.at },
      { id: 'note-2', theme: other.wing, author: 'user:leon', text, at: row.at },
      { id: 'note-3', theme: board.wing, author: 'user:maya', text, at: row.at },
      { id: 'note-4', theme: board.wing, author: 'user:leon', text, at: '2020-01-01T00:00:00Z' },
    ];
    const result = await memory.searchMemory({ boards: [board, { ...board, slug: 'same-wing' }], query: 'rate', source: 'note', from: 'user:leon', since: '2026-01-01T00:00:00Z', api: async (path) => { paths.push(path); return { results: notes }; } });
    assert.equal(result.results.length, 1);
    assert.equal(result.results[0].kind, 'note'); assert.equal(result.results[0].text, text);
    assert.ok(paths.every((path) => path.includes('theme=project_atlas')));
  },
  async neighbours() {
    const paths = [];
    const result = await memory.loadNeighbours(row, async (path) => {
      paths.push(path);
      return { board: 'atlas', messages: path.includes('before=') ? [{ id: 16, text: 'Before' }, { id: 17, text }] : [{ id: 18, text: 'After' }, { id: 19, text: 'Later' }] };
    });
    assert.deepEqual(paths, ['/api/chat/atlas?before=18&last=2', '/api/chat/atlas?after=17']);
    assert.deepEqual(result.neighbours.map((row) => row.id), [16, 17, 18]);
    assert.equal(result.neighbours[1].hit, true); assert.equal(result.at, 17);
    assert.deepEqual(await memory.loadNeighbours({ ...row, kind: 'note' }, () => assert.fail('No chat request for notes')), { neighbours: [], at: null });
  },
  async reply() {
    const paths = [];
    const reply = { ...row, kind: 'reply', id: 94, model: 'local:openai_compatible/model' };
    const result = await memory.loadNeighbours(reply, async (path) => {
      paths.push(path);
      if (path.includes('replies_after')) return { replies: [{ id: 94, message_id: 17 }] };
      return { board: 'atlas', messages: path.includes('before=') ? [{ id: 17, text: 'Request' }] : [{ id: 18, text: 'After' }] };
    });
    assert.equal(paths[0], '/api/chat/atlas?last=1&replies_after=93');
    assert.ok(paths.includes('/api/chat/atlas?before=18&last=2'));
    assert.equal(result.at, 17); assert.equal(result.neighbours[1].id, 94); assert.equal(result.neighbours[1].hit, true);
    await assert.rejects(memory.loadNeighbours(reply, async () => ({ replies: [] })), /source reply/);
  },
  async link() {
    const note = { ...row, kind: 'note', id: 'note-exact' };
    const calls = [];
    const args = { result: note, destination: board, card, api: async (...call) => { calls.push(call); return { result: { content: [{ type: 'text', text: 'linked' }] } }; } };
    await memory.linkMemory(args);
    assert.deepEqual(calls[0], ['/mcp', { method: 'POST', body: { jsonrpc: '2.0', id: 'memory-link', method: 'tools/call', params: { name: 'memory_link', arguments: { id: note.id, task: 21 } } } }]);
    await assert.rejects(memory.linkMemory({ ...args, result: row }));
    await assert.rejects(memory.linkMemory({ ...args, destination: other }));
    await assert.rejects(memory.linkMemory({ ...args, card: { ...card, restricted: true } }));
    assert.equal(calls.length, 1);
    await assert.rejects(memory.linkMemory({ ...args, api: async () => ({ result: { isError: true } }) }), /Could not link/);
  },
  async render() {
    const state = { boards: [board, other], results: [row], filters: { query: '<query>', project: 'atlas' }, selected: row, destination: board, searched: true };
    const html = memory.renderMemory(state);
    for (const label of ['Project:', 'Source:', 'From:', 'When:', 'Search memory', 'aria-busy="false"', 'data-memory-result="0"', 'aria-current="true"']) assert.ok(html.includes(label), label);
    assert.ok(html.includes('&lt;query&gt;'));
    assert.ok(!html.includes('<query>'));
    const literal = memory.renderMemory({ ...state, filters: { query: '[a]' }, results: [{ ...row, text: '<script> [a] .*' }] });
    assert.ok(literal.includes('<mark>[a]</mark>')); assert.ok(!literal.includes('<script>'));
    assert.ok(!html.includes('link to a card'));
    assert.ok(memory.renderMemory({ ...state, selected: { ...row, kind: 'note' } }).includes('link to a card'));
    assert.ok(!memory.renderMemory({ ...state, destination: other, selected: { ...row, kind: 'note' } }).includes('link to a card'));
    assert.ok(memory.renderMemory({ ...state, busy: true, completedQuery: 'prior' }).includes('showing results for “prior” until done'));
    assert.ok(memory.renderMemory({ ...state, results: [] }).includes('No exact words match'));
    assert.ok(memory.renderMemory({ ...state, error: 'Try later' }).includes('role="alert"'));
    const css = await readFile(new URL('../commonplace/web/static/map.css', import.meta.url), 'utf8');
    for (const selector of ['.memory-search', '.memory-panel', '.memory-panel-actions', '.memory-search .res.is-selected']) assert.ok(css.includes(selector), `${selector} missing`);
  },
  async controller() {
    const mount = node(), pane = node(), calls = [], copied = [], opened = [];
    let keys = true;
    const control = memory.createMemory({ mount, panel: pane, boards: [board, other], me: { actor: me }, singleKeys: () => keys,
      clipboard: { writeText: async (value) => copied.push(value) }, dock: { open: (args) => opened.push(args), close: () => calls.push('close') },
      api: async (path, options) => {
        if (options) { calls.push([path, options]); return { ...card, version: 5 }; }
        if (path.includes('/search')) return { results: [{ ...row, theme: board.wing }] };
        if (path.endsWith('/cards')) return { cards: [card] };
        return { board: 'atlas', messages: path.includes('before=') ? [{ id: 17, text }] : [] };
      },
    });
    for (const fn of ['search', 'select', 'openInChat', 'backToSource', 'stop', 'setDestination', 'restoreSource']) assert.equal(typeof control[fn], 'function', `${fn} missing`);
    await control.search({ query: 'exact', source: 'chat', project: 'atlas' });
    await control.select(row);
    assert.equal(pane.hidden, false);
    assert.equal(control.openInChat(), true);
    assert.equal(opened[0].at, 17); assert.equal(opened[0].quote.text, text);
    assert.equal(opened[0].composerLabel, 'Message to Atlas chat');
    assert.equal(opened[0].strip.backLabel, 'Back to the source');
    control.backToSource();
    assert.ok(calls.includes('close')); assert.ok(pane.focusCount > 0);
    control.setDestination(other);
    assert.equal(control.openInChat(), false);
    click(pane, { memoryPost: '21' }); await tick();
    assert.equal(calls.filter(Array.isArray).length, 0);
    control.setDestination(board);
    const event = { key: 'Y', target: { closest: () => null }, preventDefault() {} };
    keys = false; pane.listeners.get('keydown')(event); await tick(); assert.equal(copied.length, 0);
    keys = true; pane.listeners.get('keydown')(event); await tick(); assert.deepEqual(copied, [text]);
    click(pane, { memoryPost: '21' }); await tick();
    assert.equal(calls.filter(Array.isArray).length, 1);
    assert.equal(calls.find(Array.isArray)[1].body.text, text);
    control.stop(); assert.equal(mount.listeners.size, 0); assert.equal(pane.listeners.size, 0);
  },
  async stale() {
    const mount = node(), pane = node();
    let finish;
    const control = memory.createMemory({ mount, panel: pane, boards: [board], api: (path) => {
      if (path.includes('q=old')) return new Promise((resolve) => { finish = resolve; });
      return Promise.resolve({ results: [{ ...row, text: 'Newest result' }], cards: [] });
    } });
    const old = control.search({ query: 'old', source: 'chat' });
    await control.search({ query: 'new', source: 'chat' });
    finish({ results: [{ ...row, text: 'Stale result' }] }); await old;
    const visible = mount.innerHTML.replaceAll(/<\/?mark>/g, '');
    assert.ok(visible.includes('Newest result')); assert.ok(!visible.includes('Stale result'));
    control.stop();
  },
  async failures() {
    let calls = 0;
    await assert.rejects(memory.searchMemory({ boards: [board], query: 'one two three four five six seven eight nine', api: async () => calls++ }));
    assert.equal(calls, 0);
    const result = await memory.searchMemory({ boards: [board, other], source: 'chat', query: 'rate', api: async (path) => {
      if (path.includes('/atlas/')) throw new Error('unavailable');
      return { results: [{ ...row, board: 'harbor' }], truncated: true };
    } });
    assert.equal(result.results.length, 1); assert.equal(result.truncated, true); assert.equal(result.errors.length, 1);
    assert.equal(result.results[0].board, 'harbor');
    await assert.rejects(memory.loadNeighbours(row, async () => ({ board: 'wrong', messages: [] })), /source could not/);
  },
  source_author() {
    // After the header switches to another same-wing project, neighbours keep their own board's names.
    const harbor = { ...other, wing: board.wing, coordinator: 'conn:harbor-lead' };
    const html = memory.renderMemoryPanel({ result: { ...row, hit: true }, destination: harbor, source: board, cards: [], me, neighbours: [{ kind: 'chat', id: 16, board: 'atlas', actor: 'conn:lead', text: 'Earlier words', at: row.at }] });
    assert.ok(html.includes('Atlas orchestrator')); assert.ok(!html.includes('Harbor orchestrator'));
  },
};
assert.equal(typeof cases[process.argv[2]], 'function', 'Unknown contract case');
await cases[process.argv[2]]();
