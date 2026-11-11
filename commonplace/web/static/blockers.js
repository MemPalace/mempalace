import { escape as e } from './api.js';

// Blocker links are only present on card views when the workspace has orchestration on.
export const hasBlockers = (card) => Array.isArray(card?.blocked_by);

// Waiting blockers carry a title from the server; satisfied ones are only a number.
export function blockerRows(card) {
  const titles = new Map((card.waiting?.code === 'blocked_by' ? card.waiting.blockers : []).map((b) => [b.number, b.title]));
  return (card.blocked_by || []).map((number) => ({ number, title: titles.get(number) || '', done: !titles.has(number) }));
}

// Accepts "12", "#12" or a list like "12, #14"; anything else is not a card number.
export function parseBlockerNumbers(text) {
  const parts = String(text || '').split(/[\s,]+/).filter(Boolean);
  const numbers = parts.map((part) => (/^#?\d+$/.test(part) ? Number(part.replace('#', '')) : NaN));
  return parts.length && numbers.every((n) => Number.isSafeInteger(n) && n > 0) ? numbers : null;
}

export const addBlockers = (card, numbers) => [...new Set([...(card.blocked_by || []), ...numbers])];
export const removeBlocker = (card, number) => (card.blocked_by || []).filter((n) => n !== number);

export function blockersSection(card) {
  if (!hasBlockers(card)) return '';
  const rows = blockerRows(card);
  const list = rows.length
    ? `<ul class="blockers">${rows.map((r) => `<li data-blocker="${r.number}"><span>#${r.number}${r.title ? ' ' + e(r.title) : ''} · ${r.done ? 'Done' : 'Waiting'}</span> <button type="button" class="btn ghost sm" data-action="unblock" data-blocker="${r.number}" aria-label="Clear blocker #${r.number}">Clear</button></li>`).join('')}</ul>`
    : '<p class="muted">Nothing blocks this card.</p>';
  const blocks = (card.blocks || []).length ? `<p class="muted">Blocks ${card.blocks.map((n) => '#' + n).join(', ')}</p>` : '';
  return `<section data-details-blockers><div class="sec-h"><h3>Blocked by</h3></div>${list}${blocks}<form class="reply" data-form="blockers"><label class="sr" for="blocker-number">Card number that blocks #${card.number}</label><input id="blocker-number" name="blockers" inputmode="numeric" autocomplete="off" placeholder="Card number, e.g. 12" required><button class="btn sm">Add blocker</button></form></section>`;
}
