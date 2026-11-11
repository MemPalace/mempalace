import { escape as e } from './api.js';

// The sentence comes from the server (boards/why.py), so the board, the details and the API agree.
export const waitingText = (card) => card?.waiting?.text || '';

export function waitingSection(card) {
  const text = waitingText(card);
  return text ? `<section class="callout wait" role="note" data-details-waiting><h3>Why this card waits</h3><p>${e(text)}</p></section>` : '';
}

// Board-level summary: why cards wait and why few agents are working. Empty when nothing waits.
export function whyStrip(board) {
  const notes = board?.waiting_notes || [];
  return notes.length
    ? `<section class="why-strip" role="note" aria-label="Why cards are waiting" data-why-strip><h2>Why cards are waiting</h2><ul>${notes.map((n) => `<li data-why="${e(n.code)}">${e(n.text)}</li>`).join('')}</ul></section>`
    : '';
}
