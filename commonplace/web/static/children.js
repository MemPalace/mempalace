import { escape as e } from "./api.js";

// The parent owns the disclosure; its key stays stable while children change.
export function childProgress(card, expanded, dot, scope = "board") {
  if (!card.children?.total) return "";
  const id = `children-${scope}-${card.number}`;
  return `<div class="task-children"><button class="child-toggle" data-action="children-toggle" data-card="${card.number}" data-scope="${scope}" aria-expanded="${expanded}" aria-controls="${id}">${card.children_need_you ? dot({ need: "you" }) : ""}<span>${e(card.children.done)}/${e(card.children.total)} done</span><span aria-hidden="true">${expanded ? "−" : "+"}</span></button>${expanded ? `<ul class="child-list" id="${id}">${(card.child_cards || []).map((child) => `<li>${dot(child)}<a href="#/card/${child.number}">#${child.number}</a><a class="child-title" href="#/card/${child.number}" title="${e(child.title)}">${e(child.title)}</a><span>${e(child.status)}</span></li>`).join("")}</ul>` : `<ul id="${id}" hidden></ul>`}</div>`;
}
