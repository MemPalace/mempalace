// Links from a card to what its agent did. Nothing here decides who may read what:
// the lists below come from routes that already filter by viewer, so a link exists only
// for an agent the viewer's console lists and a transcript the viewer's thread read allows.

// Board actors are "conn:<name>" and a connection name is unique per workspace.
export function agentForAssignee(assignee, agents = []) {
  const ref = String(assignee || "");
  if (!ref.startsWith("conn:")) return null;
  const matches = agents.filter((agent) => `conn:${agent.name}` === ref);
  return matches.length === 1 ? matches[0].id : null;
}

export async function loadCardLinks(card, api) {
  const [agents, handoffs] = await Promise.all([
    String(card.assignee || "").startsWith("conn:") ? api("/api/console").catch(() => null) : null,
    api(`/api/handoffs?task=${encodeURIComponent(`#${card.number}`)}`).catch(() => null),
  ]);
  return {
    agent: agentForAssignee(card.assignee, agents?.agents),
    transcript: Array.isArray(handoffs) && handoffs.length > 0,
    card: card.number,
  };
}

export function cardLinksHTML(links) {
  if (!links || (!links.agent && !links.transcript)) return "";
  const agent = links.agent ? `<a class="btn sm" href="#/console?agent=${encodeURIComponent(links.agent)}">Agent activity</a>` : "";
  // The stored transcript lives on a server-rendered page, so it opens beside the app.
  const transcript = links.transcript ? `<a class="btn sm" href="/app/cards/${encodeURIComponent(`#${links.card}`)}" target="_blank" rel="noopener">Transcript and handoffs</a>` : "";
  return `<div class="acts card-links" data-card-links>${agent}${transcript}</div>`;
}

export function placeCardLinks(mount, links) {
  mount.querySelector("[data-card-links]")?.remove();
  const html = cardLinksHTML(links);
  if (html) mount.querySelector(".fields")?.insertAdjacentHTML("afterend", html);
}
