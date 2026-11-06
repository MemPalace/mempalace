import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';

// Stand-ins for the browser's constructed-sheet API, installed before any module loads.
const adopted = [];
globalThis.CSSStyleSheet = class { replaceSync(text) { this.text = text; } };
globalThis.document = { get adoptedStyleSheets() { return adopted; }, set adoptedStyleSheets(list) { adopted.splice(0, adopted.length, ...list); } };
const load = async (name) => {
  const file = new URL(`../commonplace/web/static/${name}`, import.meta.url);
  assert.ok(existsSync(file), `${name} missing`);
  return import(file);
};

const cases = {
  async nostyle() {
    // The app's CSP (style-src 'self') refuses inline style elements, so no module may render one.
    const [board, details, needs, menu] = await Promise.all(['board.js', 'details.js', 'needs.js', 'cardmenu.js'].map(load));
    await load('palette.js');
    const project = { slug: 'atlas', name: 'Atlas' };
    const card = { number: 7, board: 'atlas', title: 'Synthetic card', status: 'todo', version: 1, events: [] };
    const proposal = { ...card, status: 'backlog', proposal_status: 'pending', actions: ['approve', 'reject'], body: 'Synthetic body' };
    const markup = {
      board: board.boardView({ board: project, cards: [card], me: 'user:maya' }),
      details: details.renderDetails(card, { board: project }),
      needs: needs.renderNeeds({ needs: { backlog_from_agents: [proposal] }, boards: [project] }),
      'needs detail': needs.renderNeedDetail(proposal, { board: project }),
      'card menu': menu.renderCardMenu(card, { board: project }),
    };
    for (const [name, html] of Object.entries(markup)) {
      assert.equal(typeof html, 'string', `${name} did not render`);
      assert.doesNotMatch(html, /<style/i, `${name} renders an inline style element`);
    }
    // Each module's rules are adopted once, as a constructed sheet.
    for (const selector of ['.workbench-board', '.workbench-details', '.needs-me', '.card-menu', '.workbench-palette']) {
      assert.equal(adopted.filter((sheet) => sheet.text.includes(`${selector} `)).length >= 1, true, `${selector} sheet not adopted`);
    }
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
