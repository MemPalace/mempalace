import { readFile } from 'node:fs/promises';
const url = (s) => `data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const root = new URL('../commonplace/web/static/', import.meta.url);
const api = url(await readFile(new URL('api.js', root), 'utf8'));
const blockers = url((await readFile(new URL('blockers.js', root), 'utf8')).replace("'./api.js'", JSON.stringify(api)));
const sibling = async (name) => url((await readFile(new URL(name, root), 'utf8')).replace(/(['"])\.\/api\.js\1/, JSON.stringify(api)));
const waitingModule = await sibling('waiting.js');
const cardlinks = await sibling('cardlinks.js'), cardruns = await sibling('cardruns.js');
const detailsSource = (await readFile(new URL('details.js', root), 'utf8')).replace('"./api.js"', JSON.stringify(api)).replace('"./cardlinks.js"', JSON.stringify(cardlinks)).replace('"./cardruns.js"', JSON.stringify(cardruns)).replace('"./blockers.js"', JSON.stringify(blockers)).replace('"./waiting.js"', JSON.stringify(waitingModule));
const { parseBlockerNumbers, addBlockers, removeBlocker, blockerRows, blockersSection } = await import(blockers);
const { renderDetails } = await import(url(detailsSource));
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
