import { escape as e } from "./api.js";

// "Reviewer" in the card panel: who accepts this card's completion. The server decides who may
// choose and who may answer; this only shows the choice and lets a workspace owner change it.
// No chosen reviewer means today's behaviour, so the row says so instead of naming anyone.

export const DEFAULT_WORDS = "Workspace owner (default)";

const refOf = (reviewer) => (typeof reviewer === "string" && reviewer ? reviewer : "");

export function reviewerChange(card, select) {
  const next = select?.value || null;
  return next === (refOf(card.reviewer) || null) ? null : { version: card.version, reviewer: next };
}

// Rows for the card's field list. An owner gets the choice; everyone else reads it.
export function reviewerRows(card, { people = {}, author = (ref) => ref } = {}) {
  if (card.can_set_reviewer !== true && !card.reviewer) return "";
  const chosen = refOf(card.reviewer);
  const words = chosen ? e(author(chosen)) : DEFAULT_WORDS;
  if (card.can_set_reviewer !== true) return `<dt>Reviewer</dt><dd data-card-reviewer>${words}</dd>`;
  const entries = Object.entries(people);
  if (chosen && !entries.some(([ref]) => ref === chosen)) entries.push([chosen, author(chosen)]);
  const options = [["", DEFAULT_WORDS], ...entries].map(([ref, name]) => `<option value="${e(ref)}"${ref === chosen ? " selected" : ""}>${e(name)}</option>`).join("");
  return `<dt>Reviewer</dt><dd data-card-reviewer><select id="card-reviewer" aria-label="Who reviews completion">${options}</select><button class="btn sm" data-action="panel-reviewer">Set</button></dd>`;
}
