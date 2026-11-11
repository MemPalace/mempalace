import assert from 'node:assert/strict';
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const { panelHTML, mountProjectPanel, forgetProjectPanels } = await load('projectpanel.js');

const PANEL = {
  format: 'commonplace.panel/1', title: 'Atlas', highlights: ['Beta ships Friday', '3 open risks'],
  links: [{ label: 'Runbook', url: 'https://example.com/runbook' }],
};

// A fake board: keeps the inserted panel, the style tag and a document, which is all the mount touches.
function fakeContent() {
  const board = {
    html: '', removed: 0,
    querySelector: (selector) => (selector === '[data-project-panel]' && board.html ? { remove: () => { board.html = ''; board.removed += 1; } } : null),
    insertAdjacentHTML: (_where, html) => { board.html = html; },
    ownerDocument: { styles: 0, getElementById: () => (board.ownerDocument.styles ? {} : null), head: { insertAdjacentHTML: () => { board.ownerDocument.styles += 1; } } },
  };
  return { board, content: { querySelector: (selector) => (selector === '.workbench-board' ? board : null) } };
}
const wait = () => new Promise((done) => setTimeout(done, 0));

const cases = {
  shows_highlights_and_links() {
    const html = panelHTML(PANEL);
    assert.match(html, /<aside class="project-panel" data-project-panel aria-label="Atlas">/);
    assert.match(html, /<li>Beta ships Friday<\/li>/);
    assert.match(html, /<a href="https:\/\/example\.com\/runbook" target="_blank" rel="noopener noreferrer nofollow">Runbook<\/a>/);
  },
  no_panel_renders_nothing() {
    for (const value of [null, undefined, {}, { highlights: [], links: [] }, 'text', []]) assert.equal(panelHTML(value), '');
  },
  text_is_escaped_never_markup() {
    const html = panelHTML({ title: '<img src=x onerror=alert(1)>', highlights: ['<script>alert(1)</script>', '"quoted" & more'], links: [{ label: '<b>x</b>', url: 'https://example.com/?a="b"&c=<d>' }] });
    assert.doesNotMatch(html, /<script|<img|<b>/);
    assert.match(html, /&lt;script&gt;/);
    assert.match(html, /&quot;quoted&quot; &amp; more/);
    assert.doesNotMatch(html, /href="[^"]*"[^>]*"[^>]*onerror/);
  },
  unsafe_links_are_dropped() {
    const bad = ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'data:text/html,x', 'file:///etc/passwd', 'ftp://example.com', '//example.com', 'example.com', 'https://user:pw@example.com/', '', null, 5];
    const html = panelHTML({ highlights: ['kept'], links: bad.map((url) => ({ label: 'Go', url })).concat([{ label: 'Good', url: 'http://example.com/' }]) });
    assert.equal((html.match(/<a /g) || []).length, 1);
    assert.match(html, /Good/);
    assert.doesNotMatch(html, /javascript|data:|file:|ftp:|user:pw/i);
  },
  only_unsafe_links_and_no_lines_renders_nothing() {
    assert.equal(panelHTML({ links: [{ label: 'x', url: 'javascript:1' }] }), '');
  },
  malformed_entries_do_not_throw() {
    const html = panelHTML({ highlights: [1, null, 'ok', {}], links: [null, 'x', { label: 5, url: 'https://e.com' }, { url: 'https://e.com' }] });
    assert.match(html, /<li>ok<\/li>/);
    assert.doesNotMatch(html, /<a /);
  },
  async mount_draws_a_stored_panel() {
    forgetProjectPanels();
    const { board, content } = fakeContent();
    const calls = [];
    await mountProjectPanel(content, { slug: 'a b', api: async (path) => { calls.push(path); return { panel: PANEL }; } });
    assert.deepEqual(calls, ['/api/projects/a%20b/panel']);
    assert.match(board.html, /Beta ships Friday/);
    assert.equal(board.ownerDocument.styles, 1);
  },
  async mount_leaves_a_project_without_a_panel_alone() {
    forgetProjectPanels();
    const { board, content } = fakeContent();
    await mountProjectPanel(content, { slug: 'plain', api: async () => ({ panel: null }) });
    assert.equal(board.html, '');
    assert.equal(board.ownerDocument.styles, 0);
  },
  async mount_survives_a_failed_read() {
    forgetProjectPanels();
    const { board, content } = fakeContent();
    await mountProjectPanel(content, { slug: 'broken', api: async () => { throw new Error('offline'); } });
    assert.equal(board.html, '');
  },
  async mount_skips_a_screen_that_moved_on() {
    forgetProjectPanels();
    const { board, content } = fakeContent();
    await mountProjectPanel(content, { slug: 'gone', current: () => false, api: async () => ({ panel: PANEL }) });
    assert.equal(board.html, '');
  },
  async redraw_uses_the_cache_then_rereads_when_old() {
    forgetProjectPanels();
    let clock = 1000, reads = 0;
    const api = async () => { reads += 1; return { panel: { ...PANEL, highlights: [`read ${reads}`] } }; };
    const first = fakeContent();
    await mountProjectPanel(first.content, { slug: 'c', api, now: () => clock });
    const second = fakeContent();
    await mountProjectPanel(second.content, { slug: 'c', api, now: () => clock + 1000 });
    assert.match(second.board.html, /read 1/);
    assert.equal(reads, 1);
    const third = fakeContent();
    await mountProjectPanel(third.content, { slug: 'c', api, now: () => clock + 120000 });
    assert.equal(reads, 2);
    assert.match(third.board.html, /read 2/);
  },
};

const name = process.argv[2];
if (!cases[name]) { console.error(`unknown case ${name}`); process.exit(2); }
await cases[name]();
await wait();
