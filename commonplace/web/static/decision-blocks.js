// Structured decision blocks: the options of one ask side by side, each with its optional
// detail line and plain-text preview above the button that answers with its label.
import { escape as e } from "./api.js";

const exact = (text) => e(text).replaceAll("\r", "&#13;");

// The blocks of the open ask, one per offered label in order. A plain ask, or blocks that
// are out of step with the offered labels, give [] so the caller draws plain buttons.
export function decisionBlocks(card) {
  const blocks = Array.isArray(card?.ask_blocks) ? card.ask_blocks : [], offered = card?.ask_options || [];
  if (!blocks.length || blocks.length !== offered.length) return [];
  return blocks.every((block, index) => block && block.label === offered[index]) ? blocks : [];
}

// The button carries the option's index, like the plain buttons, so the same handler answers.
export function decisionBlocksHTML(card, { disabled = false } = {}) {
  const blocks = decisionBlocks(card);
  if (!blocks.length) return "";
  const items = blocks.map((block, index) => {
    const preview = typeof block.preview === "string" ? `<pre class="dblock-preview" tabindex="0" aria-label="${e(`Preview of ${block.label}`)}">${exact(block.preview)}</pre>` : "";
    const detail = typeof block.detail === "string" ? `<p class="dblock-detail">${exact(block.detail)}</p>` : "";
    return `<li class="dblock">${preview}<button type="button" class="btn primary" data-needs-choice="${index}"${disabled ? " disabled" : ""}>${e(block.label)}</button>${detail}</li>`;
  }).join("");
  return `<ul class="dblocks" data-decision-blocks="${blocks.length}" aria-label="Options">${items}</ul>`;
}
