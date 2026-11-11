import { api as request, escape as e, safeLink } from "./api.js";

// The project's own panel: a few highlight lines and action links the project declares as data
// (format commonplace.panel/1, stored by PUT /api/projects/<slug>/panel). The document is untrusted:
// every string is escaped, and a link is only drawn when it is an absolute http(s) address without
// credentials; anything else is dropped, never rendered as a link. A project without a panel gets nothing.

const MAX_AGE_MS = 60_000;
const cache = new Map();
const styles = `.project-panel { margin: 12px 14px 0; padding: 10px 12px; border: 1px solid var(--line-strong); border-radius: var(--r); background: var(--surface); font-size: 13px; line-height: 18px; }
.project-panel h2 { margin: 0 0 4px; font-size: 12.5px; font-weight: 650; }
.project-panel ul { margin: 0; padding-left: 18px; }
.project-panel .panel-links { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; padding: 0; list-style: none; }
.project-panel .panel-links a { overflow-wrap: anywhere; }`;
// The app's CSP (style-src 'self') refuses inline style elements, so the rules are adopted as a sheet.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function panelHTML(panel) {
  if (!panel || typeof panel !== "object") return "";
  const lines = (Array.isArray(panel.highlights) ? panel.highlights : []).filter((line) => typeof line === "string" && line);
  const links = (Array.isArray(panel.links) ? panel.links : []).map((link) => ({ label: link && link.label, href: safeLink(link && link.url) }))
    .filter((link) => typeof link.label === "string" && link.label && link.href);
  if (!lines.length && !links.length) return "";
  const title = typeof panel.title === "string" && panel.title ? panel.title : "Project panel";
  return `<aside class="project-panel" data-project-panel aria-label="${e(title)}"><h2>${e(title)}</h2>`
    + (lines.length ? `<ul>${lines.map((line) => `<li>${e(line)}</li>`).join("")}</ul>` : "")
    + (links.length ? `<ul class="panel-links">${links.map((link) => `<li><a href="${e(link.href)}" target="_blank" rel="noopener noreferrer nofollow">${e(link.label)}</a></li>`).join("")}</ul>` : "")
    + "</aside>";
}

// Mount at the top of the board in `content`; the board redraws often, so a fresh panel is cached
// and drawn at once while an old one is read again. `current()` says whether the screen is still this board.
export async function mountProjectPanel(content, { slug, api = request, current = () => true, now = Date.now } = {}) {
  const draw = (panel) => {
    const board = content.querySelector?.(".workbench-board");
    if (!board || !current()) return;
    board.querySelector("[data-project-panel]")?.remove();
    const html = panelHTML(panel);
    if (!html) return;
    board.insertAdjacentHTML("afterbegin", html);
  };
  const known = cache.get(slug);
  if (known) draw(known.panel);
  if (known && now() - known.at < MAX_AGE_MS) return;
  try {
    const result = await api(`/api/projects/${encodeURIComponent(slug)}/panel`);
    cache.set(slug, { panel: result?.panel || null, at: now() });
    draw(result?.panel || null);
  } catch {
    // The panel is optional: a failed read leaves the project as it was.
  }
}

export const forgetProjectPanels = () => cache.clear();
