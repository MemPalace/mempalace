import { escape as e } from './api.js';

// After Done: Archive and restore, a carry-on follow-up and the Pocket of pinned facts.
// The server decides who may act (the can_* flags); archiving hides a card, it never deletes it.
const has = (card) => card && typeof card.final === 'boolean';

const POCKET_NOTE = 'Key facts to keep with this card. Unpinning takes a fact off the card; it stays in storage.';

export function pocketList(card) {
  const facts = card.pocket || [];
  if (!facts.length) return '<p class="muted">Nothing pinned yet.</p>';
  return `<ul class="pocket">${facts.map((f) => `<li data-pin="${e(f.id)}"><span class="pocket-text">${e(f.text)}</span>${card.can_pocket ? ` <button type="button" class="btn ghost sm" data-action="unpin" data-pin="${e(f.id)}" aria-label="Unpin this fact">Unpin</button>` : ''}</li>`).join('')}</ul>`;
}

function stateLine(card) {
  if (card.archived) return `<p class="muted" data-lifecycle-state>Archived${card.archived_at ? ' · ' + e(card.archived_at) : ''}. Hidden from the board; still readable here.</p>`;
  if (card.final) return '<p class="muted" data-lifecycle-state>Done. This card is final: archive it, or carry on in a new card.</p>';
  return '';
}

function links(card) {
  const rows = [];
  if (card.follow_up_of != null) rows.push(`Carries on from <a href="#/card/${e(card.follow_up_of)}">#${e(card.follow_up_of)}</a>`);
  if ((card.follow_ups || []).length) rows.push(`Carried on in ${card.follow_ups.map((n) => `<a href="#/card/${e(n)}">#${e(n)}</a>`).join(', ')}`);
  return rows.length ? `<p class="muted" data-lifecycle-links>${rows.join(' · ')}</p>` : '';
}

export function lifecycleSection(card) {
  if (!has(card)) return '';
  const buttons = [
    card.can_archive ? '<button type="button" class="btn sm" data-action="archive">Archive</button>' : '',
    card.can_restore ? '<button type="button" class="btn sm" data-action="restore">Restore</button>' : '',
    card.can_follow_up ? '<button type="button" class="btn ghost sm" data-action="carry-on">Carry on</button>' : '',
  ].join('');
  const form = card.can_pocket ? `<form class="reply" data-form="pocket" data-card="${e(card.number)}"><label class="sr" for="pocket-text">Fact to pin to #${e(card.number)}</label><input id="pocket-text" name="text" autocomplete="off" maxlength="2000" placeholder="Pin a key fact" required><button class="btn sm">Pin</button></form>` : '';
  const empty = !(card.pocket || []).length && !form;
  return `<section data-details-lifecycle><div class="sec-h"><h3>Done and Pocket</h3>${buttons}</div>${stateLine(card)}${links(card)}${empty ? '' : `<h4 class="muted">Pocket</h4><p class="muted">${POCKET_NOTE}</p>${pocketList(card)}${form}`}</section>`;
}

// Action name to the REST call the app makes; the card version keeps a stale view from winning.
export function lifecycleRequest(card, action, extra = {}) {
  const base = `/api/cards/${card.number}`, body = { version: card.version };
  if (action === 'archive') return { path: `${base}/archive`, body };
  if (action === 'restore') return { path: `${base}/restore`, body };
  if (action === 'carry-on') return { path: `${base}/follow-up`, body };
  if (action === 'unpin') return { path: `${base}/unpocket`, body: { ...body, pin: Number(extra.pin) } };
  if (action === 'pin') return { path: `${base}/pocket`, body: { ...body, text: extra.text } };
  return null;
}

// details.js calls this after each draw: the section sits above the description.
export function placeLifecycle(mount, card) {
  const body = mount.querySelector('.ctx-body');
  if (!body || mount.querySelector('[data-details-lifecycle]')) return;
  const html = lifecycleSection(card);
  if (!html) return;
  const description = body.querySelector('[data-details-description]');
  if (description) description.insertAdjacentHTML('beforebegin', html);
  else body.insertAdjacentHTML('beforeend', html);
}
