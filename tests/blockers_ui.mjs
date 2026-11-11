// The modules load from their real files, so an import a module gains later resolves without wiring here.
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const { parseBlockerNumbers, addBlockers, removeBlocker, blockerRows, blockersSection } = await load('blockers.js');
const { renderDetails } = await load('details.js');
const base = { number: 9, board: 'atlas', version: 3, title: 'Ship', body: '', status: 'todo', events: [] };
const waiting = { code: 'blocked_by', blockers: [{ number: 4, title: 'Schema <b>' }] };
const withLinks = { ...base, blocked_by: [4, 5], blocks: [12], waiting };
console.log(JSON.stringify({
  parsed: ['12', '#12', '12, #14 15', '', 'x', '0', '1e3', '-4', '99999999999999999999'].map(parseBlockerNumbers),
  add: addBlockers(withLinks, [5, 6]),
  remove: removeBlocker(withLinks, 4),
  rows: blockerRows(withLinks),
  off: blockersSection(base),
  empty: blockersSection({ ...base, blocked_by: [] }).includes('Nothing blocks this card.'),
  section: blockersSection(withLinks),
  inDetails: renderDetails(withLinks, { me: 'user:a' }).includes('data-details-blockers'),
  hiddenInDetails: renderDetails(base, { me: 'user:a' }).includes('data-details-blockers'),
}));
