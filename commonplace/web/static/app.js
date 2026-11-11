import { mountWorkspaceOrchestration } from "./projects.js";
import { api, escape as e, actor, elapsed, safeLink, pullRequestUrl, setPeople } from "./api.js";
import { digestSpeech } from "./digest.js";
import { voiceInput } from "./voice.js";
import { needItems } from "./home.js";
import { eventItem, ASKED_KEY, COPY as DECIDE_COPY, createSession, decideQueue, leftCount, offlineNow, queueKey, RELOAD_ERRORS, reduce, renderDecide, statusHTML, toolsHTML } from "./decide.js";
import { bindSwipe, SWIPE } from "./decide-swipe.js";
import { planView } from "./plan.js";
import { rulesView, rulesHandlers } from "./rules.js";
import { signinView, signinMessage } from "./signin.js";
import { childProgress } from "./children.js";
import { agentsView, createAgent, agentAction, changeAgentClient, clearAgentSecret } from "./agents.js";
import { createAdmin, boundedRequest } from "./admin.js";
import { createRunnerBlock } from "./adminrunners.js";
import { createConsole } from "./console.js";
import { createChat } from "./chat.js";
import { createDock } from "./chatdock.js";
import { deriveChatState, seenStore, clearSeen } from "./chatstate.js";
import { renderRail, renderPhoneBar, renderPhoneTabs } from "./rail.js";
import { boardView as renderBoard, bindBoard, cardId, chatTag, loadChatTimes, roveTo } from "./board.js";
import { createDetails } from "./details.js";
import { resolveRoute } from "./routes.js";
import { createNeeds } from "./needs.js";
import { createMemory } from "./memory.js";
import { createPalette } from "./palette.js";
import { mountOnboarding } from "./onboarding.js";
import { createCardMenu } from "./cardmenu.js";
// Shell layout rules. The CSP (style-src 'self') refuses inline style elements, so they live in a constructed sheet.
const shellStyles = `
    .shell #content { flex:1; min-height:0; overflow:auto; }
    .shell .head { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 16px; border-bottom:1px solid var(--line); }
    .shell .head h1 { font-size:18px; } .shell .head .right { display:flex; gap:8px; }
    #chat-content .chat-view { border-left:0; }
    #chat-content .chat-header > h2, #chat-content .chat-header > [data-close-chat] { display:none; }
    .shell #card-panel { overflow:auto; } .shell .workbench-board { min-height:100%; }
    #shell-dialog { background:#FEFEFC; color:var(--text); border:1px solid var(--line-strong); border-radius:var(--r); padding:24px; max-width:min(640px,calc(100vw - 32px)); max-height:90vh; overflow:auto; }
    #shell-dialog::backdrop { background:rgba(23,24,28,.42); } #shell-dialog nav { display:flex; gap:8px; flex-wrap:wrap; margin:16px 0; }
    #shell-dialog dl { display:grid; grid-template-columns:auto 1fr; gap:8px 16px; margin:16px 0; }
    .shell .needs-me > .bar > h1 { display:none; } .shell .digest-captions { margin:12px 16px 0; }
    @media(min-width:700px) { .shell .card-more { display:none; } }
    @media(max-width:699px) { .shell.panel-open #card-panel { position:fixed; inset:0; width:100%; height:100dvh; z-index:31; display:flex; } .shell .head { flex-wrap:wrap; } .shell .head [data-action="coordinator"] { display:none; } #shell-dialog button { min-height:44px; } }
`;
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(shellStyles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

const signinParams = new URLSearchParams(location.search);
const signinCode = signinParams.get("signin"), signinNext = signinParams.get("next") || "";
if (signinParams.has("signin") || signinParams.has("next")) {
  signinParams.delete("signin"); signinParams.delete("next");
  const search = signinParams.toString();
  history.replaceState(history.state, "", `${location.pathname}${search ? `?${search}` : ""}${location.hash}`);
}
let providersPromise;

const root = document.querySelector("#root");
const state = {
  me: null, boards: [], needs: {}, cards: [], children: [], open: [], card: null, route: "#/", base: "#/",
  selected: null, backlog: false, drafts: new Map(), pending: new Set(), seq: 0, eventEpoch: "", panelRequest: 0, refreshRequest: 0,
  screen: 0, renderPending: null, error: "", pollError: false, loading: false,
  chat: [], linked: [], chatBoard: null, chatCursor: 0, spaceTab: "chat", chatTasks: new Map(),
  chatHasMore: false, chatLoading: false, chatObserver: null, chatVisible: new Set(), chatTaskRequests: new Map(),
  toggled: new Set(), expandedChildren: new Set(), rulesWaiting: 0, chatTimes: new Map(),
  phonePushPending: false, underChat: null, fromChat: null, detailsSheet: false, rebase: null,
  queue: null, pollFails: 0, queueFocus: "", queueShownAt: 0, queueField: false, queueDirty: false,
};
let adminUI = null;
let consoleUI = null, chatUI = null, detailsUI = null, boardDispose = null;
const statuses = ["backlog", "todo", "doing", "review", "done"];
const enc = encodeURIComponent;
const data = (form) => Object.fromEntries(new FormData(form));
const message = (text, retry = false) => `<div class="message" role="status">${e(text)}${retry ? '<button data-action="retry">Retry</button>' : ""}</div>`;
// The board lists child cards (an approval's linked cards) too; each card once.
const boardCards = () => [...new Map([...state.cards, ...(state.children || [])].map((card) => [card.number, card])).values()];
const phone = () => matchMedia("(max-width:699px)").matches;
const options = (items, selected) => items.map((s) => `<option value="${e(s)}" ${s === selected ? "selected" : ""}>${e(s)}</option>`).join("");
let speechState = { active: false, text: "" };
const speech = digestSpeech(() => api("/api/needs-me/digest"), (next) => { speechState = next; speechView(); }, showError);
const voice = voiceInput(root, api, showError);
const rulesUI = rulesHandlers(root, api, refresh, showError);
function speechView() {
  const button = root.querySelector('[data-action="speak-digest"]');
  if (button) {
    button.textContent = speechState.active ? "Stop" : "Speak";
    button.dataset.active = String(speechState.active);
    button.setAttribute("aria-label", speechState.active ? "Stop speaking" : "Speak what needs you today");
  }
  const captions = root.querySelector("#digest-captions");
  if (captions) { captions.hidden = !speechState.text; captions.textContent = speechState.text; }
}
const prLink = (card) => {
  const href = pullRequestUrl(card);
  return href ? `<a href="${e(href)}" target="_blank" rel="noopener noreferrer">PR #${e(card.pr_number || "↗")}</a>` : card.pr_number ? `<span>PR #${e(card.pr_number)}</span>` : "";
};
const needLabels = { you: "Needs you", plan: "Awaiting plan review", merge: "Ready to merge", checks: "Checks running", working: "Working", expired: "Expired", idle: "Idle", done: "Done" };
function dot(card) {
  const need = needLabels[card.need] ? card.need : "idle";
  return `<i class="status-dot ${need}" data-need="${need}" role="img" aria-label="${needLabels[need]}"></i>`;
}
const children = (card, scope) => childProgress(card, state.expandedChildren.has(card.number), dot, scope);
// One task row: dot, title, elapsed; then #N, context, branch and PR.
const taskRow = (card, context = "") => `<div class="trow">${dot(card)}<a class="tt" href="#/card/${card.number}">${e(card.title)}</a><time data-elapsed="${e(card.updated_at)}" datetime="${e(card.updated_at)}">${elapsed(card.updated_at)}</time><div class="tm"><a href="#/card/${card.number}">#${card.number}</a>${card.flags?.length ? '<span class="flag" aria-label="Flagged">!</span>' : ""}${context}${card.branch ? `<span class="branch">${e(card.branch)}</span>` : ""}${prLink(card)}</div></div>`;
const rowFoot = (card) => `${card.claim_until ? `<div class="task-meta">${claim(card)}</div>` : ""}${card.status_line ? `<p class="status-line">${e(card.status_line)}</p>` : ""}`;
function claim(card) {
  if (!card.claim_until) return "";
  if (card.claim_expired || Date.parse(card.claim_until) <= Date.now()) return '<span class="claim low">Expired</span>';
  const end = Date.parse(card.claim_until), start = Date.parse(card.heartbeat_at || card.updated_at);
  const ratio = Math.max(0, Math.min(1, (end - Date.now()) / (end - start || 1800000)));
  return `<span class="claim ${ratio < .2 ? "low" : ""}" data-claim-until="${e(card.claim_until)}" data-claim-start="${e(card.heartbeat_at || card.updated_at)}" role="img" aria-label="Claim ${Math.round(ratio * 100)}% remaining">${Array.from({ length: 6 }, (_, i) => `<i class="${i < Math.ceil(ratio * 6) ? "on" : ""}"></i>`).join("")}</span>`;
}
const cardMeta = (card) => `<div class="task-meta"><span class="actor">${e(actor(card.assignee || card.return_to || card.created_by))}</span>${card.branch ? `<span class="branch">${e(card.branch)}</span>` : ""}${prLink(card)}<time data-elapsed="${e(card.updated_at)}" datetime="${e(card.updated_at)}">${elapsed(card.updated_at)}</time>${claim(card)}</div>${card.status_line ? `<p class="status-line">${e(card.status_line)}</p>` : ""}`;
// Live transitions: rows keep their identity across a re-render, so a row that changed
// group slides there and a dot whose need changed pops into its new state.
function capture(view) {
  const content = root.querySelector("#content");
  const before = new Map();
  if (content.dataset.view === view) content.querySelectorAll("[data-key]").forEach((row) => {
    const d = row.querySelector(".status-dot");
    if (!before.has(row.dataset.key)) before.set(row.dataset.key, { rect: row.getBoundingClientRect(), need: d?.dataset.need, color: d && getComputedStyle(d).backgroundColor });
  });
  return () => {
    content.dataset.view = view;
    const phase = `${-(Date.now() % 1000)}ms`;
    content.querySelectorAll(".status-dot.checks").forEach((d) => { d.style.animationDelay = phase; });
    if (!before.size || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const seen = new Set();
    content.querySelectorAll("[data-key]").forEach((row) => {
      const key = row.dataset.key, old = before.get(key);
      if (seen.has(key)) return;
      seen.add(key);
      if (!old) { row.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" }); return; }
      const rect = row.getBoundingClientRect(), dx = old.rect.left - rect.left, dy = old.rect.top - rect.top;
      if (Math.abs(dx) + Math.abs(dy) > 1) row.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 240, easing: "cubic-bezier(.2, .7, .3, 1)" });
      const d = row.querySelector(".status-dot");
      if (d && old.need && old.need !== d.dataset.need) d.animate([{ transform: "scale(1.75)", backgroundColor: old.color }, { transform: "scale(1)" }], { duration: 240, easing: "ease-out" });
    });
  };
}
const changes = ["new", "changed", "unchanged", "deleted"];
const csDot = (change) => { const kind = changes.includes(change) ? change : "unchanged"; return `<i class="cs-dot ${kind}" role="img" aria-label="${kind}"></i>`; };
const csSize = (n) => (n.children || []).reduce((sum, child) => sum + 1 + csSize(child), n.more || 0);
// The PR as a call tree (from the agent's `python -m commonplace.callstack`); nodes fold, files start folded.
function callTree(card) {
  const stack = card.callstack;
  if (!stack || !(card.pr_url || card.pr_number)) return "";
  const open = (path, folded = false) => folded === state.toggled.has(`${card.number}:${path}`);
  const node = (n, path, parent) => {
    const kids = n.children || [], expanded = open(path), cut = n.name.lastIndexOf(".");
    const where = `${n.file && n.file !== parent ? n.file.split("/").pop() : ""}${n.line ? `:${n.line}` : ""}`;
    const row = `${csDot(n.change)}<span class="cs-name">${cut > 0 ? `<span class="cs-q">${e(n.name.slice(0, cut + 1))}</span>` : ""}<b>${e(n.name.slice(cut + 1))}</b><span class="cs-q">(${e(n.params || "")})${n.seen ? " …" : ""}${kids.length && !expanded ? ` +${csSize(n)}` : ""}</span></span><span class="cs-loc" title="${e(n.file || "")}${n.line ? `:${n.line}` : ""}">${e(where)}</span>`;
    const more = n.more ? `<li><span class="cs-row cs-q">+${n.more}</span></li>` : "";
    return `<li>${kids.length ? `<button class="cs-row" data-action="cs-toggle" data-path="${path}" aria-expanded="${expanded}">${row}</button>` : `<div class="cs-row">${row}</div>`}${expanded && (kids.length || more) ? `<ul>${kids.map((k, i) => node(k, `${path}.${i}`, n.file)).join("")}${more}</ul>` : ""}</li>`;
  };
  const stats = stack.stats || {}, files = stack.files || [], filesOpen = open("files", true);
  const head = `${stats.new ? `<span>${csDot("new")}${e(stats.new)} new</span>` : ""}${stats.changed ? `<span>${csDot("changed")}${e(stats.changed)} changed</span>` : ""}<span>${e(stats.files ?? files.length)} files</span>${stack.head ? `<span class="cs-sha" title="Head commit">${e(stack.head)}</span>` : ""}`;
  const rest = files.length ? `<li><button class="cs-row" data-action="cs-toggle" data-path="files" aria-expanded="${filesOpen}"><span class="cs-name cs-q">${e(files.length)} other files${filesOpen ? "" : " +"}</span></button>${filesOpen ? `<ul>${files.map((f) => `<li><div class="cs-row">${csDot(f.change)}<span class="cs-name ${f.change === "deleted" ? "cs-gone" : ""}" title="${e(f.file)}">${e(f.file)}</span></div></li>`).join("")}</ul>` : ""}</li>` : "";
  const cut = stack.truncated || stack.more ? `<li><span class="cs-row cs-q">${stack.more ? `+${e(stack.more)}` : "…"}</span></li>` : "";
  return `<section class="cs" aria-label="Call tree"><div class="cs-head">${head}</div><div class="cs-body"><ul class="cs-tree">${(stack.roots || []).map((n, i) => node(n, String(i), "")).join("")}${cut}${rest}</ul></div></section>`;
}

// Stable selectors survive a re-render, so focus can return to the same control.
function focusKey(el) {
  if (el.id) return `#${CSS.escape(el.id)}`;
  for (const name of ["data-action", "data-details-tab", "data-details-title"]) if (el.hasAttribute?.(name)) return `[${name}="${CSS.escape(el.getAttribute(name))}"]`;
  return null;
}
function remember() {
  voice.remember();
  root.querySelectorAll("[data-draft]").forEach((input) => state.drafts.set(input.dataset.draft, input.value));
  const active = document.activeElement, ask = active?.closest?.("[data-ask]"), cols = root.querySelector(".cols");
  return {
    key: active?.dataset?.draft, start: active?.selectionStart, end: active?.selectionEnd,
    scope: active?.closest?.("#card-panel, #content")?.id,
    ask: ask && !active.dataset.draft ? { card: ask.dataset.ask, option: active.dataset.option } : null,
    focus: !ask && !active?.dataset?.draft && active?.closest?.("#card-panel") ? focusKey(active) : null,
    queue: root.querySelector(".queue")?.scrollTop, panel: root.querySelector("#card-panel")?.scrollTop,
    tree: root.querySelector(".cs-body")?.scrollTop,
    view: root.querySelector("#content")?.dataset.view, cols: cols && [cols.scrollTop, cols.scrollLeft],
    stacks: Object.fromEntries([...root.querySelectorAll(".col[data-status] .stack")].map((stack) => [stack.parentElement.dataset.status, stack.scrollTop])),
    spaceTasks: root.querySelector(".space-tasks .stack")?.scrollTop,
    chat: root.querySelector(".space-stream")?.scrollTop,
    chatBottom: (() => { const el = root.querySelector(".space-stream"); return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 60; })(),
  };
}
function restore(saved) {
  root.querySelectorAll('[data-draft^="then-"]').forEach((input) => {
    const number = input.dataset.draft.slice(5);
    const version = input.closest("[data-version]")?.dataset.version;
    if (String(state.drafts.get(`then-version-${number}`)) !== version) {
      state.drafts.delete(`then-${number}`);
      state.drafts.delete(`then-version-${number}`);
      input.value = "return";
    }
  });
  root.querySelectorAll("[data-draft]").forEach((input) => {
    if (state.drafts.has(input.dataset.draft)) input.value = state.drafts.get(input.dataset.draft);
  });
  const scope = (saved?.scope && root.querySelector(`#${saved.scope}`)) || root;
  if (saved?.key) {
    const input = scope.querySelector(`[data-draft="${CSS.escape(saved.key)}"]`);
    input?.focus({ preventScroll: true });
    if (input?.setSelectionRange && (input.tagName === "TEXTAREA" || ["text", "search", "tel", "password"].includes(input.type))) input.setSelectionRange(saved.start, saved.end);
  }
  if (saved?.ask) {
    const ask = scope.querySelector(`[data-ask="${CSS.escape(saved.ask.card)}"]`);
    (saved.ask.option == null ? ask : ask?.querySelector(`[data-option="${CSS.escape(saved.ask.option)}"]`))?.focus({ preventScroll: true });
  }
  const lost = !document.activeElement || document.activeElement === document.body;
  if (saved?.focus && lost) root.querySelector("#card-panel")?.querySelector(saved.focus)?.focus({ preventScroll: true });
  const queue = root.querySelector(".queue"), panel = root.querySelector("#card-panel");
  if (queue && saved?.queue != null) queue.scrollTop = saved.queue;
  if (panel && saved?.panel != null) panel.scrollTop = saved.panel;
  const chat = root.querySelector(".space-stream");
  if (chat) chat.scrollTop = saved?.view === state.base && saved.chatBottom === false ? saved.chat || 0 : chat.scrollHeight;
  const tree = root.querySelector(".cs-body");
  if (tree && saved?.tree != null) tree.scrollTop = saved.tree;
  voice.view();
  if (saved?.view !== state.base) return; // Board scroll belongs to the board it came from.
  const cols = root.querySelector(".cols");
  if (cols && saved.cols) [cols.scrollTop, cols.scrollLeft] = saved.cols;
  root.querySelectorAll(".col[data-status] .stack").forEach((stack) => { const top = saved.stacks[stack.parentElement.dataset.status]; if (top) stack.scrollTop = top; });
  const tasks = root.querySelector(".space-tasks .stack");
  if (tasks && saved.spaceTasks != null) tasks.scrollTop = saved.spaceTasks;
}
async function signin(code = null) {
  queueStop(); state.queue = null; state.queueFocus = "";
  speech.stop(); voice.reset();
  clearWorkbenchChat();
  state.needsUI?.stop(); state.needsUI = null; state.memoryUI?.stop(); state.memoryUI = null;
  state.palette?.ui.stop(); state.palette = null; state.cardMenu?.ui.stop(); state.cardMenu = null;
  state.onboarding?.destroy(); state.onboarding = null;
  state.chatObserver?.disconnect(); state.chatVisible.clear(); state.chatTaskRequests.clear();
  state.chat = []; state.linked = []; state.chatBoard = null; state.chatCursor = 0; state.chatTasks.clear(); state.chatTimes.clear();
  providersPromise ||= api("/app/auth/providers").catch(() => ({}));
  const { providers = [], paused = [] } = await providersPromise;
  if (state.me) return;
  const cookie = (document.cookie || "").split(";").map((part) => part.trim()).find((part) => part.startsWith("cmmn_via="));
  const value = cookie ? cookie.slice("cmmn_via=".length) : "";
  const via = /^[a-z]{1,20}$/.test(value) ? value : "";
  if (code !== null) state.error = signinMessage(code, paused, via);
  const path = location.pathname + location.search + location.hash;
  root.innerHTML = signinView({ providers, paused, via, error: state.error, next: signinNext || (path === "/app/" ? "" : path) });
}
function currentBoard() {
  const routeSlug = /^#\/(?:board|space)\/([^/?]+)/.exec(state.base)?.[1];
  const selected = needsCards().find((card) => card.number === state.selected);
  let last;
  try { last = localStorage.getItem(boardPreference()); } catch { /* This visit still works without storage. */ }
  const memory = state.base.startsWith("#/memory") ? state.memoryBoard : null;
  const slug = routeSlug ? decodeURIComponent(routeSlug) : state.base.startsWith("#/chat/") ? decodeURIComponent(state.base.slice(7)) : state.card?.board || memory || selected?.board || last;
  return state.boards.find((board) => board.slug === slug) || state.boards[0] || null;
}
function boardPreference() { return `cmmn.board.last.${state.me?.workspace || ""}`; }
function closeCardPanel() {
  detailsUI?.stop(); detailsUI = null;
  if (state.route.startsWith("#/card/")) location.hash = state.base;
  else root.querySelector("#card-panel").hidden = true;
}
function openSelectedCard(action) {
  const number = state.route.startsWith("#/card/") ? Number(state.route.slice(7)) : state.selected;
  if (!number) return;
  if (state.card?.number === number && !root.querySelector("#card-panel").hidden) {
    if (action === "edit-card") root.querySelector('[data-action="edit-card"]')?.click();
    else if (action === "move-card") root.querySelector("#card-status")?.focus();
  } else {
    state.cardKey = action;
    location.hash = `#/card/${number}`;
  }
}
// One palette per shell dialog; it owns the shortcuts sheet and the single-key switch.
function paletteUI() {
  const dialog = root.querySelector("#shell-dialog");
  if (state.palette?.dialog !== dialog) {
    state.palette?.ui.stop();
    state.palette = { dialog, ui: createPalette({ dialog, onCommand: paletteCommand, onSingleKeysChange(enabled) {
      state.singleKeys = enabled; root.querySelector(".shell")?.setAttribute("data-single-keys", enabled ? "on" : "off");
    } }) };
  }
  return state.palette.ui;
}
function shortcutsView() { paletteUI().shortcuts(); }
function shellMenu() {
  const board = currentBoard(), cards = boardCards().filter((card) => !card.board || card.board === board?.slug);
  paletteUI().open({ can_admin: state.me?.can_admin === true, board, boards: state.boards, cards, selected: state.card || cards.find((card) => card.number === state.selected) || null });
}
async function paletteCommand(item) {
  const action = item.action;
  if (action === "new-card") newCardForm();
  else if (action === "open-chat") {
    const slug = item.board || currentBoard()?.slug, target = slug && `#/board/${enc(slug)}/chat`;
    if (target && location.hash === target) void state.workbenchChat?.dock.open(); else if (target) location.hash = target;
  } else if (action === "new-board") openOnboarding();
  else if (action === "coordinator" || action === "logout") shellAction(action, item.board);
  else if (action === "move-card" || action === "edit-card") { state.selected = item.number; openSelectedCard(action); }
  else if (action === "copy-card-link") {
    try { await navigator.clipboard.writeText(new URL(`${location.pathname}${item.href}`, location.origin).href); }
    catch { showError("Could not copy the link."); }
  } else if (item.href) location.hash = item.href;
}
// Palette commands that already live in the shell's click handler go through it.
function shellAction(action, board) {
  const button = document.createElement("button");
  button.hidden = true; button.dataset.action = action;
  if (board) button.dataset.board = board;
  root.append(button); button.click(); button.remove();
}
function openOnboarding() {
  root.querySelector("#shell-dialog")?.close();
  if (state.onboarding) return;
  state.onboarding = mountOnboarding(document.body, { api, first: !state.boards.length,
    onCreated(board) { location.hash = `#/board/${enc(board.slug)}`; },
    onClose() { state.onboarding = null; },
  });
}
function cardMenuUI() {
  const mount = root.querySelector("#card-menu-root");
  if (state.cardMenu?.mount !== mount) {
    state.cardMenu?.ui.stop();
    state.cardMenu = { mount, ui: createCardMenu({ mount, api,
      async onChange() { await refresh(); },
      // Review needs a question and options, so it opens the shell's existing review form.
      async onReview(card) {
        try { state.card = await api(`/api/cards/${card.number}`); reviewForm(); } catch (error) { showError(error.message); }
      },
    }) };
  }
  return state.cardMenu.ui;
}
function shell() {
  state.workbenchChat?.dispose();
  state.palette?.ui.stop(); state.palette = null; state.cardMenu?.ui.stop(); state.cardMenu = null;
  state.shellSync = null;
  root.innerHTML = `<a class="skip" href="#content">Skip to content</a><div class="shell two"><nav class="rail" id="board-nav" aria-label="Workspace"><a href="#/memory">Memory</a><a href="#/chat">Chat</a><a href="#/console">Console</a></nav><main class="center"><div id="phone-bar"></div><header class="head bar"><h1 id="title">Projects</h1><div class="right" id="head-actions"></div></header><div id="connection-state" role="status" hidden>Disconnected</div><div id="screen-error" role="alert" hidden></div><div id="content" tabindex="-1"></div><div id="phone-tabs"></div></main><aside class="context" id="card-panel" role="dialog" aria-label="Card" tabindex="-1" hidden></aside><button class="tablet-scrim" data-action="close-panel" aria-label="Close card"></button></div><dialog id="shell-dialog" aria-labelledby="shell-dialog-h"></dialog><div id="card-menu-root"></div>`;
  navigation();
  const panel = root.querySelector("#card-panel"), layout = root.querySelector(".shell");
  if (!panel || !layout) return;
  const narrow = matchMedia("(max-width:1099px)");
  let opener = null, wasOpen = false;
  const sync = () => {
    // Needs me lends the shared panel to project settings; it draws there again once the panel closes.
    if (panel.hidden && state.needsUI?.paused?.()) state.needsUI.pause(false);
    if (layout.classList.contains("chat-open") || layout.classList.contains("ctx-pushed")) return;
    // Project settings are a page from 700 px up (10), never a dimmed sheet; on a phone they are the m13 sheet.
    const page = panel.querySelector("[data-project-settings]") && !matchMedia("(max-width:699px)").matches;
    const open = !panel.hidden, modal = open && narrow.matches && !page;
    root.querySelector("#details-btn")?.setAttribute("aria-expanded", String(open));
    if (open && !wasOpen) opener = document.activeElement;
    layout.classList.toggle("two", !open); layout.classList.toggle("panel-open", open);
    if (modal) panel.setAttribute("aria-modal", "true"); else panel.removeAttribute("aria-modal");
    for (const target of [root.querySelector("#board-nav"), root.querySelector(".center")]) if (target) target.inert = modal;
    if (open && !wasOpen && modal) panel.focus();
    // A header redraw replaces the opener (Details, Settings): find it again by id.
    const back = opener?.isConnected ? opener : opener?.id ? root.querySelector(`#${CSS.escape(opener.id)}`) : null;
    if (!open && wasOpen && back) back.focus();
    wasOpen = open;
  };
  const observer = new MutationObserver(sync);
  observer.observe(panel, { attributes:true, attributeFilter:["hidden"] });
  narrow.addEventListener("change", sync);
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || !narrow.matches) return;
    const items = [...panel.querySelectorAll('button:not(:disabled),a[href],input,textarea,select,[tabindex="0"]')].filter((el) => el.getClientRects().length);
    const index = items.indexOf(document.activeElement);
    if (!items.length) { event.preventDefault(); panel.focus(); }
    else if (event.shiftKey && index <= 0) { event.preventDefault(); items.at(-1).focus(); }
    else if (!event.shiftKey && (index === items.length - 1 || index < 0)) { event.preventDefault(); items[0].focus(); }
  });
  state.shellStop?.();
  state.shellStop = () => { observer.disconnect(); narrow.removeEventListener("change", sync); };
  // navigation() replaces the rail; it re-applies the sheet's inert state through this.
  state.shellSync = sync;
}
function navigation() {
  const boardNav = root.querySelector("#board-nav");
  if (!boardNav) return;
  const board = currentBoard();
  // Rules, Console, Settings, Decide and Projects are their own pages: they keep their heading on a phone.
  const view = state.base === "#/needs" || state.base === "#/" ? "needs" : state.base.startsWith("#/memory") ? "memory" : state.base.startsWith("#/space/") ? "space"
    : /^#\/board\/[^/?]+\/chat$|^#\/chat\//.test(state.base) ? "chat" : /^#\/board\/[^/?]+$/.test(state.base) ? "board" : "page";
  const needsByBoard = {};
  for (const card of needsCards()) needsByBoard[card.board] = (needsByBoard[card.board] || 0) + 1;
  boardNav.outerHTML = renderRail({ can_admin: state.me.can_admin === true, workspace: state.me.workspace_name || "Commonplace", user: state.me.people?.[state.me.actor] || state.me.email, boards: state.boards, board, view, needs: state.needs.count || 0, needsByBoard, rulesWaiting: state.rulesWaiting }).replace('<nav class="rail"', '<nav class="rail" id="board-nav"');
  root.querySelector("#phone-bar").innerHTML = renderPhoneBar({ board });
  root.querySelector("#phone-tabs").innerHTML = renderPhoneTabs({ board, view, needs: state.needs.count || 0 });
  root.querySelector(".shell")?.setAttribute("data-screen", state.base === "#/decide" ? "decide" : "");
  // The phone top bar already names the project, so the board hides its own title there (m1).
  root.querySelector(".shell")?.setAttribute("data-view", view);
  state.shellSync?.();
}
function showError(text) {
  state.error = text;
  const box = root.querySelector("#screen-error");
  if (box) { box.hidden = !text; box.innerHTML = `<div class="message error">${e(text)}<button data-action="retry">Retry</button></div>`; }
}
function title(text, actions = "") {
  root.querySelector("#title").textContent = text;
  // A redraw keeps focus on the same header control (Details, Chat).
  const head = root.querySelector("#head-actions"), active = document.activeElement, keep = active?.closest?.("#head-actions") && active.id;
  head.innerHTML = actions;
  if (keep) head.querySelector(`#${CSS.escape(keep)}`)?.focus({ preventScroll: true });
  document.title = `${text} · Commonplace`;
}
function decision(card, panel = false, answered = null) {
  if (answered) return answered.options.length ? `<div class="opts">${answered.options.map((choice) => `<button class="opt ${choice === answered.choice ? "go" : ""}" aria-pressed="${choice === answered.choice}" disabled>${e(choice)}</button>`).join("")}</div>` : "";
  const busy = state.pending.has(`card-${card.number}`);
  return `<div class="opts" data-decisions="${card.number}" data-version="${e(card.version)}">${(card.ask_options || []).map((choice, i) => `<button class="opt" data-action="decide" data-card="${card.number}" data-option="${i}" data-choice="${e(choice)}" ${busy ? "disabled" : ""}>${e(choice)}${i < 6 ? `<span class="k">${i + 1}</span>` : ""}</button>`).join("")}<form class="reply-form" data-form="reply" data-card="${card.number}"><input name="reply" aria-label="Reply to card ${card.number}" data-draft="reply-${card.number}" autocomplete="off" required><button class="opt" aria-label="Send reply" ${busy ? "disabled" : ""}>↵</button>${panel ? `<select name="then" aria-label="After decision" data-draft="then-${card.number}"><option value="return">Return to ${e(actor(card.return_to))}</option><option value="todo">Anyone</option><option value="done">Done</option><option value="backlog">Backlog</option></select>` : ""}</form></div>`;
}
const clock = (at) => `<time datetime="${e(at)}" title="${e(at)}">${e(at?.slice(11, 16) || "")}</time>`;
// An ask inside the conversation: the question verbatim and its choices. Answered asks keep
// their place in time with the pick marked and who answered when; only the open ask is live.
function askCard(card, ask, { answer = null, open = false, compact = false, controls = true } = {}) {
  const offered = [...(open ? card.ask_options || [] : ask.options || [])];
  if (answer?.choice && !offered.includes(answer.choice)) offered.push(answer.choice);
  const question = ask.text ?? (open ? card.ask : null);
  return `<li class="ev ask ${open ? "open" : "answered"}" ${open && controls ? `data-ask="${card.number}" tabindex="0"` : ""}>${clock(ask.at)}<span><span class="actor">${e(actor(ask.actor))}</span> <span class="v">ask${ask.target ? ` → ${e(actor(ask.target))}` : ""}</span></span><div class="hit">${question != null ? `<p class="q">${e(question)}</p>` : ""}${open ? (controls ? decision(card, !compact) : "") : decision(card, false, { options: offered, choice: answer?.choice })}${answer?.text != null ? `<p class="said">${e(answer.text)}</p>` : ""}${answer ? `<p class="stamp">${e(actor(answer.actor))} · ${clock(answer.at)}</p>` : ""}</div></li>`;
}
function conversation(card, { compact = false, controls = true } = {}) {
  const items = [];
  let ask = null;
  for (const event of card.events || []) {
    if (event.options) items.push(ask = { ask: event });
    else if (event.kind === "decide" && ask) { ask.answer = event; ask = null; }
    else items.push({ event });
  }
  if (card.ask != null) {
    if (ask) ask.open = true;
    else items.push({ ask: { kind: "ask", actor: card.return_to, target: card.ask_to, text: card.ask, at: card.updated_at }, open: true });
  }
  return items.map(({ event, ...item }) => event ? eventItem(event, actor) : askCard(card, item.ask, { ...item, compact, controls })).join("");
}
function needsCards() {
  return needItems(state.needs).map((item) => item.card);
}
function needsView() {
  // Keep the Speak/Stop node (and its focus) through live refreshes.
  const speak = root.querySelector('[data-action="speak-digest"]'), focused = speak && document.activeElement === speak;
  title("Needs me", `${speech.supported ? '<button class="btn speak" data-action="speak-digest" aria-controls="digest-captions">Speak</button>' : ""}<button id="chat-toggle" class="chat-toggle" aria-controls="chat" aria-expanded="false">Chat</button>`);
  if (speak) { root.querySelector('[data-action="speak-digest"]')?.replaceWith(speak); if (focused) speak.focus({ preventScroll: true }); }
  speechView();
  // An open card owns the shared side panel; the list comes back with the route.
  if (state.route.startsWith("#/card/")) return;
  const content = root.querySelector("#content"), panel = root.querySelector("#card-panel");
  if (!state.needsUI || !content.querySelector("[data-needs-mount]")) {
    state.needsUI?.stop();
    content.innerHTML = '<div id="digest-captions" class="digest-captions" role="region" aria-label="Spoken digest" hidden></div><div data-needs-mount></div>';
    state.needsShown = "";
    state.needsUI = createNeeds({ mount: content.querySelector("[data-needs-mount]"), detailMount: panel, api, singleKeys: singleKeysEnabled,
      // Decline goes straight to the existing versioned route; approvals and answers wait as holds.
      onAction: (action, card, extra) => action === "need-reject" ? api(`/api/cards/${card.number}/approve`, { method: "POST", body: { approve: false, version: card.version, ...extra } }) : undefined,
      onSelect(card) {
        state.selected = card.number;
        // On a phone the first item is not opened over the list by itself.
        if (!state.needsLoading || !matchMedia("(max-width:699px)").matches) panel.hidden = false;
        if (state.workbenchChat) mountWorkbenchChat();
      },
      onClose() { closeCardPanel(); },
      onAsk(card, about) {
        state.selected = card.number; mountWorkbenchChat();
        void state.workbenchChat?.dock.open({ from: document.activeElement, strip: askStrip(card, about) });
      },
      onChange() { void refresh(); },
      onError() { /* The component shows its own failure text. */ },
    });
  }
  const shown = JSON.stringify([state.needs, state.boards.map((board) => board.slug)]);
  if (shown === state.needsShown) return;
  state.needsShown = shown; state.needsLoading = state.githubCard == null;
  const ui = state.needsUI;
  // A card that left the queue (declined, answered) is not reopened by the refresh.
  const selected = state.githubCard ?? (needsCards().some((card) => card.number === state.selected) ? state.selected : undefined);
  state.githubCard = null;
  void ui.show({ needs: state.needs, boards: state.boards, selected }).finally(() => { if (state.needsUI === ui) state.needsLoading = false; });
}
const hhmm = (at) => { const date = new Date(at); return Number.isFinite(date.getTime()) ? `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}` : ""; };
// The strip over a chat opened from a Needs me item names what is being asked about (m10b, m10c).
function askStrip(card, about) {
  const hold = about.hold, released = hold?.state === "released" || Boolean(hold?.tooLate);
  if (released) {
    const at = hhmm(hold.updated_at || hold.release_at);
    return { text: `Released · You are asking about ${about.title}${at ? `, released at ${at}` : ""}. It can't be recalled here.`, backLabel: "Back to the approval" };
  }
  if (card.proposal_status === "pending" || hold) return { text: `Approval · You are asking about ${about.title} Approve or decline it in Needs me.`, backLabel: "Back to the approval" };
  return { text: `Decision · You are asking about ${about.title}`, backLabel: "Back to Needs me" };
}
function boardView() {
  const board = currentBoard();
  if (!board) { boardsView(); return; }
  const panelOpen = !root.querySelector("#card-panel").hidden;
  title(board.name || board.slug, `<button id="chat-toggle" class="chat-toggle" aria-controls="chat" aria-expanded="false">Chat</button><button class="btn only-narrow" id="details-btn" data-action="details" aria-controls="card-panel" aria-expanded="${panelOpen}">Details</button><button class="btn ghost" data-action="coordinator" data-board="${e(board.slug)}" aria-label="Settings"><svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M2 4.5h7M12.5 4.5H14M2 11.5h1.5M6.5 11.5H14"/><circle cx="10.8" cy="4.5" r="1.6"/><circle cx="5" cy="11.5" r="1.6"/></svg><span class="wl">Settings</span></button><button class="btn primary" data-action="new-card"><svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>Add card <kbd>C</kbd></button>`);
  // The redrawn header gets the chat control back without waiting for the next refresh.
  if (state.workbenchChat?.board.slug === board.slug) state.workbenchChat.dock.mount(root.querySelector("#head-actions"), { board, label: "Chat", unread: state.workbenchChat.unread });
  const content = root.querySelector("#content");
  // Keep the active phone column and a parked-card disclosure through polling.
  const column = content.querySelector('[data-board-column][aria-pressed="true"]')?.dataset.boardColumn || "todo";
  const backlog = content.querySelector(".backlog")?.open, doneAll = Boolean(content.querySelector("#col-done[data-all]"));
  boardDispose?.();
  const messages = [...(state.chatBoard === board.slug ? state.chat : []), ...[...(state.chatTimes.get(board.slug)?.values() || [])].filter(Boolean)];
  const cards = boardCards(), from = state.fromChat && cards.find((card) => card.number === state.fromChat.number);
  const made = from && chatTag(from, messages).split(" · ")[1];
  const fromBar = from ? `<div class="m-frombar"><span role="status">Showing <b>${e(cardId(board, from.number))}</b>${made ? `, made in chat at ${e(made)}` : ", from chat"}</span><a class="btn" href="#/board/${enc(board.slug)}/chat" data-action="from-chat-back">Back to chat</a></div>` : "";
  content.innerHTML = fromBar + renderBoard({ board, cards, me: state.me.actor, filter: state.boardFilter || "all", selected: state.selected, messages, adding: state.addingCard, sheet: phone(), doneAll, people: Object.entries(state.me.people || {}).map(([ref, name]) => ({ ref, name })), column });
  if (backlog) content.querySelector(".backlog").open = true;
  // Each card carries its actions button (m9); the menu returns focus to it by id.
  content.querySelectorAll(".workbench-board .card[data-card] .card-top").forEach((top) => {
    const id = top.querySelector(".cid")?.textContent || "";
    top.insertAdjacentHTML("beforeend", `<button type="button" class="iconbtn card-more" id="more-${e(id)}" data-action="card-menu" data-card="${e(top.closest("[data-card]").dataset.card)}" aria-label="Actions for ${e(id)}: move, edit"><svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="4" cy="8" r=".9"/><circle cx="8" cy="8" r=".9"/><circle cx="12" cy="8" r=".9"/></svg></button>`);
  });
  boardDispose = bindBoard(content, { api, board,
    onFilter(filter) { const saved = remember(); state.boardFilter = filter; boardView(); restore(saved); },
    onAdded(card) { state.addingCard = false; state.drafts.delete("board-title"); state.drafts.delete("board-body"); state.drafts.delete("board-assignee"); state.drafts.delete("board-label"); location.hash = `#/card/${card.number}`; },
    onSelect(number) { state.selected = number; markOpenCard(); },
    onOpen(number) { state.selected = number; location.hash = `#/card/${number}`; },
    onCancel() { cancelCard(); },
  });
  content.querySelectorAll("[data-board-add] [name]").forEach((input) => { input.dataset.draft = `board-${input.name}`; });
  markOpenCard();
  void fillChatTimes(board);
}
function markOpenCard() {
  root.querySelectorAll(".workbench-board .card[data-card]").forEach((card) => {
    const selected = Number(card.dataset.card) === state.selected || state.route === `#/card/${card.dataset.card}`;
    card.classList.toggle("is-selected", selected);
    if (selected) { card.setAttribute("aria-current", "true"); roveTo(card); } else card.removeAttribute("aria-current");
  });
}
// "From chat" shows the source message's time, never the card's; each unknown message is read once.
async function fillChatTimes(board) {
  if (!state.chatTimes.has(board.slug)) state.chatTimes.set(board.slug, new Map());
  const known = state.chatTimes.get(board.slug), chat = state.chatBoard === board.slug ? state.chat : [];
  const missing = boardCards().filter((card) => card.message_id != null && !known.has(String(card.message_id)) && !chat.some((m) => String(m.id) === String(card.message_id)));
  if (!missing.length) return;
  for (const card of missing) known.set(String(card.message_id), null);
  const found = await loadChatTimes({ api, board, cards: missing });
  for (const message of found) known.set(String(message.id), message);
  if (!found.length || !state.me || currentBoard()?.slug !== board.slug) return;
  for (const card of missing) {
    root.querySelectorAll(`.workbench-board .card[data-card="${card.number}"] .tag:not(.lbl)`).forEach((tag) => { if (tag.textContent.startsWith("From chat")) tag.textContent = chatTag(card, found); });
  }
}
function spaceTask(card, inline) {
  return `<article class="space-task" data-key="${card.number}">${taskRow(card, `<span class="actor">${e(actor(card.assignee || card.return_to || card.created_by))}</span>`)}${rowFoot(card)}${children(card, inline ? "chat" : "space")}</article>`;
}
function syncSpaceTasks(parent, cards, inline = false) {
  const nodes = new Map([...parent.children].filter((n) => n.dataset.task).map((n) => [Number(n.dataset.task), n]));
  let next = parent.firstElementChild;
  for (const card of cards) {
    let node = nodes.get(card.number);
    if (!node) { node = document.createElement("div"); node.dataset.task = card.number; }
    nodes.delete(card.number);
    const cached = state.chatTasks.get(card.number), detail = cached?.version === card.version;
    const controls = state.route !== `#/card/${card.number}`;
    const signature = JSON.stringify([card.version, card.need, card.children, card.children_need_you, card.child_cards, state.expandedChildren.has(card.number), inline && detail, inline && controls, state.pending.has(`card-${card.number}`)]);
    if (node.dataset.signature !== signature) {
      const full = inline && detail ? { ...cached, ...card } : card;
      node.innerHTML = spaceTask(full, inline) + (inline ? `<ol class="space-asks">${conversation({ ...full, events: (full.events || []).filter((ev) => ev.options || ev.kind === "decide") }, { compact: true, controls })}</ol>` : "");
      node.dataset.signature = signature;
    }
    if (node !== next) parent.insertBefore(node, next);
    next = node.nextElementSibling;
  }
  nodes.forEach((node) => node.remove());
}
async function hydrateSpaceTasks() {
  const epoch = state.screen, board = state.chatBoard;
  const current = () => epoch === state.screen && state.chatBoard === board && state.base === `#/space/${enc(board)}`;
  const changed = await Promise.all(state.linked.filter((c) => state.chatVisible.has(c.message_id)).map(async (card) => {
    if (state.chatTasks.get(card.number)?.version === card.version) return false;
    const pending = state.chatTaskRequests.get(card.number);
    if (pending?.version === card.version && pending.epoch === epoch) return false;
    const request = { version: card.version, epoch };
    state.chatTaskRequests.set(card.number, request);
    try {
      const detail = await api(`/api/cards/${card.number}`);
      if (!current() || state.linked.find((c) => c.number === card.number)?.version !== detail.version) return false;
      state.chatTasks.set(card.number, detail);
      return true;
    } catch (error) { if (current()) showError(error.message); return false; }
    finally { if (state.chatTaskRequests.get(card.number) === request) state.chatTaskRequests.delete(card.number); }
  }));
  if (current() && changed.some(Boolean)) {
    const saved = remember(), play = capture(state.base);
    spaceView(); restore(saved); play();
  }
}
function mergeChat(messages) {
  state.chat = [...new Map([...state.chat, ...messages].map((m) => [m.id, m])).values()].sort((a, b) => a.id - b.id);
}
async function olderMessages() {
  if (!state.chatHasMore || state.chatLoading || !state.chat.length) return;
  const epoch = state.screen, board = state.chatBoard, before = state.chat[0].id;
  state.chatLoading = true;
  try {
    const page = await api(`/api/boards/${enc(board)}/chat?before=${before}&last=50`);
    if (epoch !== state.screen || board !== state.chatBoard) return;
    const saved = remember(), stream = root.querySelector(".space-stream"), height = stream.scrollHeight, top = stream.scrollTop;
    mergeChat(page.messages); state.chatHasMore = page.has_more;
    spaceView(); restore(saved);
    stream.scrollTop = top + stream.scrollHeight - height;
  } catch (error) { if (epoch === state.screen) showError(error.message); }
  finally { if (board === state.chatBoard) state.chatLoading = false; }
}
function spaceView() {
  const slug = decodeURIComponent(state.base.slice("#/space/".length));
  const board = state.boards.find((b) => b.slug === slug);
  title(board?.name || slug, `<a class="opt" href="#/board/${enc(slug)}">Board</a>${board?.can_configure ? '<button class="opt" data-action="coordinator">Coordinator</button>' : ""}`);
  const content = root.querySelector("#content");
  if (content.querySelector(".space")?.dataset.board !== slug) {
    state.chatObserver?.disconnect(); state.chatVisible.clear();
    content.innerHTML = `<section class="space" data-board="${e(slug)}"><nav class="space-tabs" aria-label="Space view"><button data-action="space-tab" data-tab="chat">Chat</button><button data-action="space-tab" data-tab="tasks">Tasks <span data-task-count></span></button></nav><section class="space-chat" aria-label="Space chat"><header class="space-bar"><h2>Chat</h2><span data-coordinator></span></header><ol class="space-stream" aria-label="Messages"><li data-older hidden><button class="opt" data-action="older-messages">Older messages</button></li></ol><form class="space-composer" data-form="space-message" data-board="${e(slug)}"><textarea name="text" rows="2" aria-label="Message" data-draft="space-${e(slug)}" required></textarea><button class="blockbtn">Send</button></form></section><section class="space-tasks" aria-label="Tasks"><header class="space-bar"><h2>Tasks <span data-task-count></span></h2><button class="opt" data-action="new-card">New card</button></header><div class="stack"></div></section></section>`;
    const stream = content.querySelector(".space-stream");
    stream.addEventListener("scroll", () => { if (stream.clientHeight && stream.scrollTop < 40) void olderMessages(); });
    state.chatObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const id = Number(entry.target.dataset.message);
        if (entry.isIntersecting) state.chatVisible.add(id); else state.chatVisible.delete(id);
      }
      void hydrateSpaceTasks();
    }, { root: stream, rootMargin: "100px" });
  }
  content.querySelector(".space").dataset.tab = state.spaceTab;
  content.querySelectorAll(".space-tabs button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tab === state.spaceTab));
  content.querySelectorAll("[data-task-count]").forEach((n) => { n.textContent = state.cards.length; });
  content.querySelector("[data-coordinator]").textContent = board?.coordinator ? actor(board.coordinator) : "No coordinator";
  const stream = content.querySelector(".space-stream"), older = stream.querySelector("[data-older]");
  older.hidden = !state.chatHasMore;
  const linked = new Map();
  state.linked.forEach((c) => { if (!linked.has(c.message_id)) linked.set(c.message_id, []); linked.get(c.message_id).push(c); });
  let next = older.nextElementSibling;
  for (const m of state.chat) {
    let node = stream.querySelector(`[data-message="${m.id}"]`);
    if (!node) {
      node = document.createElement("li"); node.dataset.message = m.id;
      node.className = `space-message ${m.actor.startsWith("user:") ? "from-person" : "from-agent"}`;
      node.innerHTML = `<div class="space-stamp"><span class="actor">${e(actor(m.actor))}</span>${clock(m.at)}${m.flags.length ? '<span class="flag" aria-label="Flagged">!</span>' : ""}</div><p class="space-text">${e(m.text)}</p><div class="space-linked-tasks"></div>`;
      state.chatObserver.observe(node);
    }
    syncSpaceTasks(node.querySelector(".space-linked-tasks"), linked.get(m.id) || [], true);
    if (node !== next) stream.insertBefore(node, next);
    next = node.nextElementSibling;
  }
  syncSpaceTasks(content.querySelector(".space-tasks .stack"), state.cards);
}
function boardsView() {
  title("Projects", '<button class="btn primary" data-action="new-board">New project</button>');
  root.querySelector("#content").innerHTML = `<section class="queue">${state.boards.map((b) => `<article class="item"><h3><a href="#/board/${enc(b.slug)}">${e(b.name || b.slug)}</a></h3><div class="meta">${Object.entries(b.counts || {}).map(([key, value]) => `${e(key)} ${e(value)}`).join(" · ")}</div></article>`).join("") || '<section class="empty-strip"><h2>Create your first project</h2><p>A project is one board, one chat and one memory.</p><button class="btn primary" data-action="new-board">Create project</button></section>'}</section>`;
  // A workspace without projects starts with the create dialog, once per visit.
  if (!state.boards.length && state.base === "#/start" && !state.onboardingOffered) { state.onboardingOffered = true; openOnboarding(); }
}
async function cardPanel(number, epoch) {
  const panel = root.querySelector("#card-panel");
  panel.hidden = false;
  const opening = state.card?.number !== number;
  const tab = opening ? "details" : panel.querySelector('[data-details-tab][aria-selected="true"]')?.dataset.detailsTab || "details";
  if (opening) panel.innerHTML = message("Loading card…");
  const request = ++state.panelRequest;
  const chatOrigin = state.chatDetail?.number === number ? state.chatDetail.row : null;
  try {
    const card = await api(`/api/cards/${number}`);
    if (request !== state.panelRequest || epoch !== state.screen || state.route !== `#/card/${number}`) return;
    state.card = card;
    // A shared card link draws its own board behind the card, once.
    const rebase = state.rebase === number && card.board && state.base !== `#/board/${enc(card.board)}`;
    if (state.rebase === number) state.rebase = null;
    if (rebase) { state.base = `#/board/${enc(card.board)}`; void refresh(); }
    const saved = remember();
    // A refresh of the open card reuses the component, so focus stays where the user left it.
    if (opening || !detailsUI) { detailsUI?.stop(); detailsUI = createDetails({ mount: panel, api }); }
    const board = state.boards.find((board) => board.slug === card.board) || { slug: card.board };
    const shown = detailsUI.show(card, { board, me: state.me.actor, messages: state.chatBoard === card.board ? state.chat : [], strip: chatOrigin ? { kind: "chat", row: chatOrigin.id, source: chatOrigin.dataset.src } : state.base.startsWith("#/board/") && state.workbenchChat?.dock.isOpen() ? { kind: "board" } : null });
    if (tab !== "details") detailsUI.selectTab(tab);
    panel.querySelector('form[data-form="comment"]')?.setAttribute("aria-busy", String(state.pending.has(`card-${number}`)));
    panel.querySelectorAll('[data-action="edit-card"], [data-action="panel-move"], [data-action="handoff"]').forEach((button) => { button.disabled = state.pending.has(`card-${number}`); });
    // Keep the existing versioned ask actions and call tree reachable on card routes.
    // Only an ask waiting on this person needs these versioned answer controls; Activity already lists the rest (m10).
    const asks = card.ask != null && card.ask_to === state.me.actor ? `<ol class="log">${conversation({ ...card, events: (card.events || []).filter((ev) => ev.options || ev.kind === "decide") })}</ol>` : "";
    if (tab === "details") panel.querySelector(".ctx-body")?.insertAdjacentHTML("beforeend", `${asks}${callTree(card)}`);
    restore(saved);
    await shown;
    if (request !== state.panelRequest || epoch !== state.screen) return;
    if (state.workbenchChat?.dock.isOpen()) state.workbenchChat.dock.close({ toDetails: true, row: chatOrigin });
    // The Details button opens the sheet with focus on Hide (04c).
    if (state.detailsSheet) { state.detailsSheet = false; panel.querySelector(".hide")?.focus(); }
    if (state.cardKey) { const action = state.cardKey; state.cardKey = null; openSelectedCard(action); }
  } catch (error) {
    if (request === state.panelRequest && epoch === state.screen) panel.innerHTML = message(error.message, true) + '<button class="opt" data-action="close-panel">Close</button>';
  }
}
function newCardForm(edit = null) {
  voice.stop();
  if (!edit && state.base.startsWith("#/board/") && !state.base.endsWith("/chat")) {
    const saved = remember(); state.addingCard = true; boardView(); restore(saved);
    root.querySelector('[data-board-add] [name="title"]')?.focus();
    return;
  }
  detailsUI?.stop(); detailsUI = null;
  const panel = root.querySelector("#card-panel");
  panel.hidden = false;
  const slug = edit?.board || currentBoard()?.slug;
  if (!slug) return;
  panel.innerHTML = `<form class="form-tile" data-form="${edit ? "edit-card" : "new-card"}" data-board="${e(slug)}"><div class="bar"><h2>${edit ? "Edit card" : "New card"}</h2><button class="close" type="button" data-action="close-panel" aria-label="Close">×</button></div><label>Title<input name="title" value="${e(edit?.title || "")}" required></label><label>Body<textarea name="body">${e(edit?.body || "")}</textarea></label>${edit ? "" : `<label>Status<select name="status">${options(["backlog", "todo", "doing"], "todo")}</select></label>`}<button class="blockbtn">Save</button></form>`;
  panel.querySelector("input").focus();
}
// Phone: a chat row shows its card on the board (m11), with Back to chat to the same row.
function showFromChat(number, row) {
  state.fromChat = { number, row: row.id || "" };
  state.workbenchChat?.dock.close();
  const saved = remember(); boardView(); restore(saved);
  const status = boardCards().find((card) => card.number === number)?.status;
  if (status === "backlog") { const backlog = root.querySelector(".workbench-board .backlog"); if (backlog) backlog.open = true; }
  else root.querySelector(`.workbench-board [data-board-column="${status}"]`)?.click();
  const node = root.querySelector(`.workbench-board .card[data-card="${number}"]`);
  if (node) { roveTo(node); node.focus(); node.scrollIntoView({ block: "nearest" }); }
}
// The no-answer error's restart link opens project settings in the chat's Details slot; Back returns to the link.
async function openRestart(link) {
  const service = state.workbenchChat, board = service?.board, epoch = state.screen;
  if (!board) return;
  voice.stop();
  service.dock.close({ toDetails: true, row: link });
  const { mountProject } = await import("./projects.js");
  if (epoch !== state.screen) return;
  const panel = root.querySelector("#card-panel");
  detailsUI?.stop(); detailsUI = null;
  panel.hidden = false;
  panel.innerHTML = '<section class="form-tile"><button type="button" class="close" data-action="settings-back-to-chat" aria-label="Back to the chat">×</button><div data-project-settings></div></section>';
  await mountProject(panel.querySelector("[data-project-settings]"), { slug: board.slug, api, e, settings: true, restart: true, origin: `#/board/${enc(board.slug)}/chat`,
    current: () => epoch === state.screen, onBack() { void state.workbenchChat?.dock.backToChat(); } });
  if (epoch === state.screen && !panel.hidden) panel.querySelector("[data-project-back]")?.focus();
}
function cancelCard() {
  state.addingCard = false; boardView();
  root.querySelector('#head-actions [data-action="new-card"]')?.focus();
}
function reviewForm() {
  const panel = root.querySelector("#card-panel"); panel.hidden = false;
  panel.innerHTML = `<form class="form-tile" data-form="review"><h2>Ask for a decision</h2><label>Question<textarea name="question" minlength="24" required></textarea></label><label>Context<textarea name="context" minlength="20" required>${e(state.card.status_line || state.card.body)}</textarea></label><label>Options (one per line)<textarea name="options" required></textarea></label><button class="blockbtn">Ask</button><button type="button" class="opt" data-action="retry">Cancel</button></form>`;
}
function handoffForm(card, prefill = {}) {
  voice.stop();
  const panel = root.querySelector("#card-panel"); panel.hidden = false;
  panel.innerHTML = `<form class="form-tile" data-form="handoff" data-card="${card.number}"${prefill.connectionsOnly ? ' data-connections-only="true"' : ""}><input type="hidden" name="version" value="${e(card.version)}"><h2>Hand off #${card.number}</h2><label>To<input name="to" value="${e(prefill.to || "")}"${prefill.connectionsOnly ? ' pattern="conn:.+" title="Use conn: followed by the agent name"' : ""} required></label><label>Note<textarea name="note" required></textarea></label><label>Context<textarea name="context">${e(card.status_line || card.body)}</textarea></label><label>Options<input name="options"></label><button class="blockbtn">Hand off</button><button class="opt" type="button" data-action="retry">Cancel</button></form>`;
}
function phonePushView(data) {
  const home = "Phone notifications cover only your home workspace. Cards in other workspaces never notify this phone.";
  const pause = "Notifications pause while you are signed out everywhere, including when a sign-in expires after 30 days. Nothing you miss is sent later.";
  const off = '<button data-action="phone-push-off">Turn off</button>';
  let body;
  if (!data.available) body = `<p>${e("Not set up on this server.")}</p>${data.subscription ? off : ""}`;
  else if (!data.subscription && !data.eligible) body = `<p>${e(home)}</p>`;
  else if (!data.subscription) body = `<button data-action="phone-push-on">Turn on</button><p>${e(home)}</p><p>${e(pause)}</p>`;
  else {
    const status = data.test == null ? "" : data.test?.status === "accepted" ? "Accepted by your ntfy server" : data.test?.status === "pending" ? "Waiting" : "Not accepted";
    body = `<p>Server: ${e(data.subscription.server)}</p><p>Topic: <span data-phone-push-topic>${e(data.subscription.topic)}</span> <button data-action="phone-push-copy">Copy</button></p><button data-action="phone-push-test">Test</button>${status ? `<p role="status">${e(status)}</p>` : ""}${off}<p>${e(home)}</p><p>${e(pause)}</p>`;
  }
  return `<section class="settings-section" id="phone-push"><h2>Phone notifications</h2>${body}</section>`;
}
async function phonePushAction(action) {
  if (state.pending.has("phone-push")) return;
  state.pending.add("phone-push");
  root.querySelectorAll("#phone-push button").forEach((button) => { button.disabled = true; });
  const epoch = state.screen;
  try {
    if (action === "phone-push-copy") {
      await navigator.clipboard.writeText(root.querySelector("[data-phone-push-topic]").textContent);
      return;
    }
    const method = action === "phone-push-off" ? "DELETE" : "POST";
    const path = action === "phone-push-test" ? "/api/phone-push/test" : "/api/phone-push";
    await api(path, { method, ...(method === "POST" ? { body: {} } : {}) });
    if (state.me && epoch === state.screen) await settingsView(epoch);
  } finally {
    state.pending.delete("phone-push");
    root.querySelectorAll("#phone-push button").forEach((button) => { button.disabled = false; });
  }
}
async function settingsView(epoch) {
  title("Settings");
  const content = root.querySelector("#content");
  if (!content.querySelector(".settings")) content.innerHTML = message("Loading settings…");
  const [connections, hooks, plan, phonePush, chatSettings, orchestration] = await Promise.allSettled(["/api/connections", "/api/webhooks", "/api/plan", "/api/phone-push", "/api/chat-settings", "/api/settings/orchestration"].map((path) => api(path, { timeoutMs: 10000 })));
  if (epoch !== state.screen) return;
  const saved = remember();
  const failed = (name, result) => message(`Could not load ${name}: ${result.reason?.message || "Request failed"}`, true);
  state.phonePushPending = phonePush.status === "fulfilled" && phonePush.value.test?.status === "pending";
  content.innerHTML = `<div class="settings">${plan.status === "fulfilled" ? planView(plan.value) : failed("Plan", plan)}${orchestration.status === "fulfilled" ? '<section class="settings-section"><h2>Agent orchestration</h2><div data-workspace-orchestration></div></section>' : failed("Agent orchestration", orchestration)}${voice.settings()}${chatSettings.status === "fulfilled" ? `<section class="settings-section"><h2>Chat</h2><p>${e(chatSettings.value.front.enabled && chatSettings.value.front.provider !== "none" ? `Answers by ${chatSettings.value.front.provider}/${chatSettings.value.front.model}` : chatSettings.value.front.enabled ? "Basic replies (no model)" : "Front replies off")}</p><p>Model answers today: ${e(chatSettings.value.usage.used_today)} · Yours: ${e(chatSettings.value.usage.user_used_today)}</p>${chatSettings.value.usage.last_error ? `<p role="status">Last model error: ${e(chatSettings.value.usage.last_error)}</p>` : ""}</section>` : failed("Chat", chatSettings)}${connections.status === "fulfilled" ? agentsView(connections.value) : `<section class="settings-section"><h2>Agents</h2>${failed("Agents", connections)}</section>`}${phonePush.status === "fulfilled" ? phonePushView(phonePush.value) : `<section class="settings-section" id="phone-push"><h2>Phone notifications</h2>${failed("Phone notifications", phonePush)}</section>`}<section class="settings-section"><h2>Webhooks</h2><div id="hook-secret"></div>${hooks.status === "fulfilled" ? (hooks.value.webhooks || []).map((h) => `<div class="setting-row"><span>${e(h.url)}<br><small>${e(h.id)}</small></span><button data-action="deliveries" data-hook="${e(h.id)}">Deliveries</button><button data-action="test-hook" data-hook="${e(h.id)}">Test</button><button data-action="remove-hook" data-hook="${e(h.id)}">Remove</button></div>`).join("") || message("No webhooks") : failed("Webhooks", hooks)}<form class="webhook-form" data-form="webhook"><input name="url" type="url" data-draft="webhook-url" aria-label="Webhook URL" required><input name="board" data-draft="webhook-board" aria-label="Board" placeholder="Board"><input name="waiting_on" data-draft="webhook-waiting" aria-label="Waiting on" placeholder="Waiting on"><select name="events" aria-label="Events" data-draft="webhook-events"><option value="*">All events</option><option value="review_needed">Review needed</option><option value="card_changed">Card changed</option><option value="waiting_on_me">Waiting on me</option></select><button class="blockbtn">Add</button></form><div id="deliveries"></div></section><section class="settings-section"><button class="opt" data-action="logout">Sign out</button></section></div>`;
  if (orchestration.status === "fulfilled") mountWorkspaceOrchestration(content.querySelector("[data-workspace-orchestration]"), {api, e, enabled: orchestration.value.enabled, current: () => epoch === state.screen});
  restore(saved);
}

async function memoryView(epoch, refresh = false) {
  title("Memory", '<button id="chat-toggle" class="chat-toggle" aria-controls="chat" aria-expanded="false">Chat</button><span class="head-sub">Exact words from chats, cards and notes. Never summarised.</span>');
  const content = root.querySelector("#content");
  const query = new URLSearchParams(state.base.split("?")[1] || "").get("q") || "";
  if (state.memoryUI && content.querySelector("[data-memory-mount]") && (refresh || state.memoryQuery === query)) return;
  state.memoryUI?.stop();
  content.innerHTML = "<div data-memory-mount></div>";
  state.memoryQuery = query;
  // The dock is mounted after the view; the component reaches it through the shell's service.
  const dock = { open: (options) => state.workbenchChat?.dock.open(options), close: (options) => state.workbenchChat?.dock.close(options), isOpen: () => Boolean(state.workbenchChat?.dock.isOpen()) };
  const ui = state.memoryUI = createMemory({ mount: content.querySelector("[data-memory-mount]"), panel: root.querySelector("#card-panel"), api, boards: state.boards, me: state.me, dock, singleKeys: singleKeysEnabled,
    // The selected source's project becomes the chat destination named in the header.
    onSelect(result, board) { state.memoryBoard = board?.slug || result.board; mountWorkbenchChat(); },
  });
  if (query && epoch === state.screen) await ui.search({ query });
}
// The shell keeps one dock and one pane across header refreshes. Only the pane's
// successful visible reads advance seen ids; closed polling merely accumulates rows.
function mountWorkbenchChat() {
  const board = currentBoard(), layout = root.querySelector(".shell");
  if (!board || !layout) { state.workbenchChat?.dispose(); return; }
  let service = state.workbenchChat;
  if (!service) {
    const pages = new Map(), stores = new Map(), waiters = new Set();
    let disposed = false, timer = null, running = false, rerun = false, lastRead = null;
    const user = state.me.user || state.me.actor?.replace(/^user:/, "");
    const store = (slug) => {
      if (!stores.has(slug)) stores.set(slug, seenStore(user, slug));
      return stores.get(slug);
    };
    const visible = () => !disposed && !document.hidden && service.dock.isOpen();
    const wake = () => { for (const resolve of waiters) resolve(); waiters.clear(); };
    async function whenVisible(slug) {
      while (!disposed && service.board.slug === slug && !visible()) await new Promise((resolve) => waiters.add(resolve));
      if (disposed || service.board.slug !== slug) throw new Error("Chat view changed");
    }
    const paneApi = async (path, options) => {
      const slug = service.board.slug;
      // Offline delivery belongs to the pane, even after it has been closed.
      if (!options?.method || options.method === "GET") await whenVisible(slug);
      const result = await api(path, options);
      if (!options?.method || options.method === "GET") {
        await whenVisible(slug);
        if (path.startsWith(`/api/chat/${enc(slug)}?`)) lastRead = { slug, page: result };
      }
      return result;
    };
    chatUI ??= createChat({ mount: () => root.querySelector("#chat-content"), api: paneApi, e, me: state.me,
      onState() {
        const heading = root.querySelector("#chat-content #chat-h"), view = root.querySelector("#chat-content .chat-view");
        if (heading && view) { heading.id = "chat-content-h"; view.setAttribute("aria-labelledby", heading.id); }
        if (!visible() || lastRead?.slug !== service.board.slug) return;
        store(lastRead.slug).markSeen(lastRead.page);
        service.unread = 0; service.dock.update({ unread: 0 }); service.phone();
      },
      onCard(number, row) {
        // The pane names the row's source (I3); the stamp is only a fallback.
        if (!row.dataset.src) {
          const item = row.closest(".chat-message"), reply = row.closest(".chat-reply");
          row.dataset.src = (reply || item)?.querySelector(".chat-stamp")?.textContent?.trim() || "";
        }
        state.selected = number;
        if (phone() && state.base.startsWith("#/board/") && boardCards().some((card) => card.number === number)) { showFromChat(number, row); return; }
        state.chatDetail = { number, row };
        location.hash = `#/card/${number}`;
      },
    });
    const dock = createDock({ root: layout, api, e, chat: chatUI });
    function schedule() {
      clearTimeout(timer);
      if (!disposed && !document.hidden) timer = setTimeout(pollUnread, 15000);
    }
    async function pollUnread() {
      clearTimeout(timer);
      if (disposed || document.hidden) return;
      if (running) { rerun = true; return; }
      if (dock.isOpen()) { schedule(); return; }
      running = true;
      const slug = service.board.slug;
      try {
        const old = pages.get(slug), seen = old ? null : store(slug).peek();
        const merge = (a = [], b = []) => [...new Map([...a, ...b].map((row) => [row.id, row])).values()];
        // A stored seen entry reads everything after it; only a first sight uses the last 20.
        const after = old || (seen && { cursor: seen.message, reply_cursor: seen.reply });
        const read = (from) => api(`/api/chat/${enc(slug)}?${from ? `after=${from.cursor || 0}&replies_after=${from.reply_cursor || 0}` : "last=20"}`);
        let [page, status] = await Promise.all([read(after), api(`/api/chat/${enc(slug)}/status`)]), last = page;
        // A full page (100 rows) may have more behind it.
        while (after && ((last.messages || []).length >= 100 || (last.replies || []).length >= 100)) {
          if (disposed || service.board.slug !== slug) return;
          last = await read(page);
          if ((last.cursor || 0) <= (page.cursor || 0) && (last.reply_cursor || 0) <= (page.reply_cursor || 0)) break;
          page = { ...page, ...last, messages: merge(page.messages, last.messages), replies: merge(page.replies, last.replies) };
        }
        if (disposed || document.hidden || service.board.slug !== slug) return;
        const merged = { ...old, ...page, messages: merge(old?.messages, page.messages), replies: merge(old?.replies, page.replies),
          cursor: Math.max(old?.cursor || 0, page.cursor || 0), reply_cursor: Math.max(old?.reply_cursor || 0, page.reply_cursor || 0) };
        pages.set(slug, merged);
        const derived = deriveChatState(merged, status, service.board, Date.now(), store(slug).read(merged));
        service.unread = derived.unread;
        dock.update({ unread: derived.unread, hasOrchestrator: !!service.board.coordinator });
        service.phone();
      } catch { /* Keep the last honest count; the next event or visible tick retries. */ }
      finally {
        running = false;
        if (rerun) { rerun = false; void pollUnread(); } else schedule();
      }
    }
    function visibility() { clearTimeout(timer); wake(); if (!document.hidden) void pollUnread(); }
    const observer = new MutationObserver(() => {
      wake();
      state.needsUI?.setChatOpen(dock.isOpen());
      if (dock.isOpen()) {
        if (state.route.startsWith("#/card/")) {
          // The card under the chat comes back when the chat closes (README: closing restores the panel).
          state.underChat = { number: Number(state.route.slice(7)), card: state.card };
          state.route = state.base; state.card = null; state.chatDetail = null;
          history.replaceState(history.state, "", state.base);
        }
        if (state.fromChat) { state.fromChat = null; root.querySelector(".m-frombar")?.remove(); }
        service.phone();
      }
      else {
        if (state.base.endsWith("/chat")) {
          const base = state.base.slice(0, -5);
          if (state.route === state.base) { state.route = base; history.replaceState(history.state, "", base); }
          state.base = base;
        }
        const under = state.underChat;
        state.underChat = null;
        // Only a side-column panel comes back; below 1100 px it was a sheet, and a closed chat leaves no sheet open.
        if (under && !matchMedia("(max-width:1099px)").matches && !layout.classList.contains("ctx-pushed") && !state.route.startsWith("#/card/")) {
          // Same card, same component: no strip (the chat is closed) and focus stays on the control.
          state.route = `#/card/${under.number}`; state.card = under.card;
          history.replaceState(history.state, "", state.route);
          markOpenCard();
          void cardPanel(under.number, state.screen);
        }
        void pollUnread();
      }
    });
    observer.observe(layout, { attributes: true, attributeFilter: ["class"] });
    document.addEventListener("visibilitychange", visibility);
    service = state.workbenchChat = { dock, board, unread: 0, pollUnread,
      phone() {
        const mount = root.querySelector("#phone-tabs");
        if (!mount) return;
        const view = state.base === "#/needs" ? "needs" : state.base.startsWith("#/memory") ? "memory" : dock.isOpen() ? "chat" : "board";
        mount.innerHTML = renderPhoneTabs({ board: service.board, view, needs: state.needs.count || 0, unread: dock.isOpen() ? 0 : service.unread });
        const tab = mount.querySelector("#m-chat-tab");
        if (tab) {
          tab.setAttribute("aria-controls", "chat"); tab.setAttribute("aria-expanded", String(dock.isOpen()));
          tab.onclick = (event) => {
            event.preventDefault(); event.stopPropagation();
            void dock.open({ from: tab, strip: view === "needs" ? { text: `${service.board.name || service.board.slug} chat`, backLabel: "Back to Needs me" } : null });
          };
        }
      },
      dispose() {
        disposed = true; clearTimeout(timer); observer.disconnect(); wake();
        document.removeEventListener("visibilitychange", visibility);
        dock.destroy(); chatUI?.destroy(); chatUI = null; state.workbenchChat = null;
      },
      clear() { for (const value of stores.values()) value.clearSeen(); clearSeen(user); pages.clear(); stores.clear(); },
    };
  }
  const changed = service.board.slug !== board.slug;
  service.board = board;
  if (changed) service.unread = 0;
  layout.dataset.singleKeys = singleKeysEnabled() ? "on" : "off";
  service.dock.mount(root.querySelector("#head-actions"), { board, label: state.base === "#/needs" || state.base.startsWith("#/memory") ? `${board.name || board.slug} chat` : "Chat", unread: service.unread });
  service.phone();
  if (state.base.endsWith("/chat") && !service.dock.isOpen()) void service.dock.open();
  else void service.pollUnread();
}
// The signed-in user's seen ids live on the device: clear them on sign-out and session expiry
// even when no board service exists (Settings, Memory, …). Other users' ids stay.
function clearWorkbenchChat() {
  adminUI?.stop();
  const user = state.me?.user || state.me?.actor?.replace(/^user:/, "");
  if (user) clearSeen(user);
  chatUI?.clearUnsent();
  state.workbenchChat?.clear();
  state.workbenchChat?.dispose();
  chatUI?.stop(); chatUI = null;
  if (typeof Event === "function") document.dispatchEvent?.(new Event("signout"));
}
function adminScreen() {
  adminUI ??= createAdmin({ mount: () => root.querySelector("#content"), api, e, current: () => state.me, runners: createRunnerBlock({ api, e }) });
  adminUI.show();
  title("Workspace administration");
  navigation();
}
function consoleScreen() {
  consoleUI ??= createConsole({ mount: () => root.querySelector("#content"), api, e });
  consoleUI.show();
  title("Console");
}
async function refresh({ background = false, events = [] } = {}) {
  if (!state.me) return;
  if (state.route === "#/admin") return;
  const epoch = state.screen, request = ++state.refreshRequest;
  if (!background) state.renderPending = epoch;
  const current = () => epoch === state.screen && request === state.refreshRequest;
  const read = (path) => api(path, state.base === "#/settings" ? { timeoutMs: 10000 } : {}).catch((error) => {
    if (state.base === "#/settings") throw new Error(`Could not load ${path}: ${error.message}`);
    throw error;
  });
  try {
    const [boards, needs, rules] = await Promise.all([read("/api/boards"), read("/api/needs-me"), read(state.base === "#/rules" ? "/api/rules" : "/api/rules/count").catch((error) => { if (["#/rules", "#/settings"].includes(state.base)) throw error; return null; })]);
    if (!current()) return;
    state.boards = boards.boards; state.needs = needs;
    if (rules) state.rulesWaiting = rules.waiting;
    if (/^#\/(board|space)\//.test(state.base)) {
      const slug = decodeURIComponent(state.base.split("/")[2]);
      const result = await api(`/api/boards/${enc(slug)}/cards`);
      if (!current()) return;
      state.cards = result.cards;
      state.children = result.children || [];
      state.linked = result.linked;
      if (state.base.startsWith("#/space/")) {
        if (state.chatBoard !== slug) {
          const page = await api(`/api/boards/${enc(slug)}/chat?last=50`);
          if (!current()) return;
          state.chat = page.messages; state.chatCursor = page.cursor; state.chatHasMore = page.has_more;
          state.chatBoard = slug; state.chatLoading = false; state.chatTasks.clear(); state.chatTaskRequests.clear();
        } else {
          let cursor = state.chatCursor, batch;
          const messages = [];
          do {
            batch = await api(`/api/boards/${enc(slug)}/chat?after=${cursor}`);
            if (!current()) return;
            messages.push(...batch.messages); cursor = batch.cursor;
          } while (batch.messages.length === 100);
          mergeChat(messages); state.chatCursor = cursor;
        }
      }
    }
    // The latest refresh inherits any foreground render it superseded on this screen.
    if (state.renderPending === epoch) { background = false; state.renderPending = null; }
    const saved = remember(), play = capture(state.base);
    navigation();
    if (state.base.startsWith("#/board/")) boardView();
    else if (state.base.startsWith("#/space/")) spaceView();
    else if (state.base === "#/boards" || state.base === "#/start") boardsView();
    else if (state.base === "#/rules") {
      if (!background || rulesUI.force || !rulesUI.busy()) {
        rulesUI.update(rules, { owner: rules.can_edit });
        root.querySelector("#content").innerHTML = rulesView(rules, { owner: rules.can_edit });
      } else {
        rulesUI.stale = true;
        const marker = root.querySelector("[data-rule-pending]");
        if (marker) marker.hidden = false;
      }
      title("Rules", rules.can_edit ? `<button type="button" class="blockbtn" data-rule-action="new" aria-controls="rule-new" aria-expanded="${!!root.querySelector('.rule-new:not([hidden])')}">New rule</button>` : "");
    } else if (state.base.startsWith("#/memory")) {
      // Results come from the person's own search; background events never redraw them.
      await memoryView(epoch, background);
    } else if (/^#\/chat(?:\/|$)/.test(state.base)) {
      boardView();
    } else if (state.base === "#/console") {
      if (!background) consoleScreen();
    } else if (state.base === "#/settings") {
      if (!background || events.some((event) => event.kind.startsWith("webhook."))) await settingsView(epoch);
    } else if (state.base === "#/decide") {
      if (state.queue) queueApply(state.queue.ready ? { type: "sync", cards: decideQueue(state.needs) } : { type: "load", cards: decideQueue(state.needs) });
      queueNoteOffline();
    } else needsView();
    if (!current()) return true;
    if (state.base.startsWith("#/board/") || state.base === "#/needs" || state.base.startsWith("#/memory")) {
      mountWorkbenchChat();
      if (events.some((event) => event.kind === "space_message" || event.kind === "space_replied")) void state.workbenchChat?.pollUnread();
    } else state.workbenchChat?.dispose();
    restore(saved); play();
    if (state.base.startsWith("#/space/")) void hydrateSpaceTasks();
    if (state.route.startsWith("#/card/") && !(background && root.querySelector('form[data-form="edit-card"], form[data-form="handoff"]'))) await cardPanel(Number(state.route.slice(7)), epoch);
    showError("");
    return true;
  } catch (error) {
    if (!current() || !state.me) return;
    state.renderPending = null;
    if (state.base === "#/rules") rulesUI.force = false;
    showError(error.message);
    const content = root.querySelector("#content");
    if ((state.base === "#/settings" && (!background || !content.querySelector(".settings"))) || !content.children.length) content.innerHTML = message(error.message, true);
    return false;
  }
}
function decideStart(hash) {
  const match = /^#\/decide(?:\/([1-9]\d*))?(\?list=1)?$/.exec(hash);
  if (!match) return undefined;
  return match[1] ? Number(match[1]) : null;
}
function askedRead() {
  try { return JSON.parse(localStorage.getItem(ASKED_KEY) || "{}") || {}; } catch { return {}; }
}
function askedWrite(asked) {
  try {
    if (Object.keys(asked).length) localStorage.setItem(ASKED_KEY, JSON.stringify(asked));
    else localStorage.removeItem(ASKED_KEY);
  } catch { /* private mode: the waiting state lasts this visit only */ }
}
const stillMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
let queueTimer = null, queueSlowTimer = null;
function queueStop() {
  clearTimeout(queueTimer); queueTimer = null;
  clearTimeout(queueSlowTimer); queueSlowTimer = null;
}
function queueArmSlow() {
  clearTimeout(queueSlowTimer);
  queueSlowTimer = setTimeout(() => {
    if (state.base === "#/decide" && state.queue && !state.queue.ready) queueApply({ type: "slow" });
  }, 10000);
}
function queueArm() {
  const holding = state.queue?.items.some((item) => item.kind === "hold");
  if (holding && !queueTimer) queueTimer = setInterval(() => queueApply({ type: "tick" }), 1000);
  else if (!holding && queueTimer) { clearTimeout(queueTimer); queueTimer = null; }
}
function queueDraft(number) {
  const field = root.querySelector(`[data-draft="reply-${number}"]`);
  if (field) return field.value;
  return state.drafts.get(`reply-${number}`) || "";
}
function focusMark() {
  const el = document.activeElement;
  if (!(el instanceof HTMLElement) || !el.closest(".decide, #head-actions")) return null;
  if (el.dataset.draft) return { kind: "draft", key: el.dataset.draft, start: el.selectionStart, end: el.selectionEnd, value: el.value };
  if (el.dataset.question) return { kind: "question" };
  if (el.dataset.action) return { kind: "action", action: el.dataset.action, option: el.dataset.option || "", card: el.dataset.card || "" };
  if (el.classList.contains("close")) return { kind: "close" };
  return null;
}
function focusRestore(mark, questionChanged) {
  if (mark?.kind === "draft") {
    const input = root.querySelector(`[data-draft="${CSS.escape(mark.key)}"]`);
    if (!input) return;
    input.focus({ preventScroll: true });
    if (mark.value != null && input.value !== mark.value) input.value = mark.value;
    if (mark.start != null) input.setSelectionRange(mark.start, mark.end);
    return;
  }
  if (!questionChanged && mark?.kind === "action") {
    const selector = mark.option !== "" ? `[data-action="${CSS.escape(mark.action)}"][data-option="${CSS.escape(mark.option)}"]` : mark.card ? `[data-action="${CSS.escape(mark.action)}"][data-card="${CSS.escape(mark.card)}"]` : `[data-action="${CSS.escape(mark.action)}"]`;
    root.querySelector(selector)?.focus({ preventScroll: true });
    return;
  }
  if (!questionChanged && mark?.kind === "close") { root.querySelector(".decide .close, .decide-tools .close")?.focus({ preventScroll: true }); return; }
  if (questionChanged || mark?.kind === "question") root.querySelector("[data-question]")?.focus({ preventScroll: true });
}
function paintQueue() {
  const content = root.querySelector("#content");
  if (!content || !state.queue) return;
  // Never rebuild the card under a finger: repaint once the drag settles.
  if (content.querySelector(".decide-card.dragging")) { state.queueDirty = true; queueArm(); return; }
  state.queueDirty = false;
  const mark = focusMark();
  const previous = state.queueFocus;
  content.innerHTML = renderDecide(state.queue, { drafts: Object.fromEntries(state.drafts) });
  const count = leftCount(state.queue);
  title("Decide", toolsHTML(state.queue));
  if (count != null) {
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = String(count);
    root.querySelector("#title")?.append(document.createTextNode(" "), n);
  }
  const question = content.querySelector("[data-question]");
  const nextFocus = question?.dataset.question || "";
  const changed = Boolean(previous && nextFocus && previous !== nextFocus);
  const card = content.querySelector(".decide-card:not(.under)");
  if (changed && card) card.classList.add(previous.split(":")[0] === nextFocus.split(":")[0] ? "flip" : "rise");
  if (changed || !previous) state.queueShownAt = performance.now();
  state.queueFocus = nextFocus;
  if (state.queueField) {
    state.queueField = false;
    content.querySelector(".decide-field input")?.focus({ preventScroll: true });
  } else focusRestore(mark, changed || (!mark && Boolean(nextFocus) && previous !== nextFocus));
  if (card?.dataset.swipe === "on" && !stillMotion()) {
    // The swipe answers the question it was drawn from, as shown, or nothing.
    const item = state.queue.items[state.queue.cursor];
    const number = Number(card.dataset.card), shown = item ? { question: item.question, options: JSON.stringify(item.card.ask_options || []) } : {};
    bindSwipe(card, content.querySelector(".decide-dock"), {
      armedAt: state.queueShownAt + SWIPE.arm,
      labels: { yes: DECIDE_COPY.releaseYes, no: DECIDE_COPY.releaseNo },
      onCommit: (side) => queueApply({ type: side, number, ...shown }),
      onSettle: () => { if (state.queueDirty) paintQueue(); },
    });
  }
  queueArm();
}
// The hold counts down in the status line only, so the card, the field and focus stay put.
function patchStatus() {
  const old = root.querySelector(".decide-status .blocks");
  const template = document.createElement("template");
  template.innerHTML = statusHTML(state.queue);
  const fresh = template.content.querySelector(".blocks");
  if (!old || !fresh) return false;
  old.replaceWith(fresh);
  const label = root.querySelector(".decide-status .sr-only");
  if (label) label.textContent = template.content.querySelector(".sr-only")?.textContent || "";
  return true;
}
function queueApply(event) {
  if (!state.queue) return;
  const { session, effects } = reduce(state.queue, event);
  state.queue = session;
  for (const effect of effects) {
    if (effect.type === "post") void sendDecide(effect, false);
    else if (effect.type === "get") void loadQueueCard(effect.number);
    else if (effect.type === "get-asked") void loadAskedCard(effect.number);
    else if (effect.type === "asked") askedWrite(effect.asked);
    else if (effect.type === "home") location.hash = "#/";
  }
  if (state.base === "#/decide" && state.queue && state.queue.phase !== "empty") {
    if (event.type === "tick" && !effects.length && patchStatus()) queueArm();
    else paintQueue();
  } else queueArm();
}
function queueFlush(keepalive) {
  if (!state.queue) return;
  const { session, effects } = reduce(state.queue, { type: "flush" });
  state.queue = session;
  for (const effect of effects) if (effect.type === "post") void sendDecide(effect, keepalive);
  for (const effect of effects) if (effect.type === "asked") askedWrite(effect.asked);
  if (state.base === "#/decide" && state.queue.phase !== "empty") paintQueue();
  else queueArm();
}
function queueNoteOffline() {
  if (state.base !== "#/decide" || !state.queue?.ready) return;
  const offline = offlineNow(navigator.onLine, state.pollFails);
  if (offline !== Boolean(state.queue.offline)) queueApply({ type: "offline", offline });
}
async function loadQueueCard(number) {
  try {
    const card = await api(`/api/cards/${number}`);
    if (state.base !== "#/decide" || !state.queue) return;
    queueApply({ type: "card", number, card });
  } catch (error) {
    if (error.status === 401 || state.base !== "#/decide" || !state.queue) return;
    queueApply({ type: "result", number, status: error.status || 0, error: error.message });
  }
}
async function loadAskedCard(number) {
  let card = null;
  try { card = await api(`/api/cards/${number}`); } catch (error) { if (error.status !== 404) return; }
  if (state.base !== "#/decide" || !state.queue) return;
  queueApply({ type: "asked-card", number, card });
}
async function sendDecide(effect, keepalive) {
  const { number, body } = effect;
  try {
    const card = await api(`/api/cards/${number}/decide`, { method: "POST", body, keepalive: Boolean(keepalive) });
    clearDecisionDraft(number);
    if (state.base !== "#/decide" || !state.queue) {
      // Sent on the way out: the session is gone, so keep the waiting state for the next visit here.
      const asked = askedRead();
      if (effect.asking) asked[number] = { at: card?.updated_at || new Date().toISOString(), rejected: effect.rejected || [] };
      else delete asked[number];
      askedWrite(asked);
      return;
    }
    queueApply({ type: "result", number, status: 200, card });
  } catch (error) {
    if (error.status === 401 || state.base !== "#/decide" || !state.queue) return;
    const reload = error.status === 409 || (error.status === 403 && RELOAD_ERRORS.has(error.message));
    if (reload) {
      try {
        const card = await api(`/api/cards/${number}`);
        if (state.base !== "#/decide" || !state.queue) return;
        queueApply({ type: "result", number, status: error.status, error: error.message, card });
        return;
      } catch (loadError) {
        if (loadError.status === 401 || state.base !== "#/decide" || !state.queue) return;
        queueApply({ type: "result", number, status: loadError.status || 0, error: loadError.message });
        return;
      }
    }
    queueApply({ type: "result", number, status: error.status || 0, error: error.message });
  }
}
async function route() {
  if (!state.me) return;
  remember();
  let hash = location.hash || "#/";
  // A shared card link (first screen of the visit) needs a screen behind the card.
  const direct = state.screen === 0 && /^#\/card\/[1-9]\d*$/.test(hash);
  if (hash === "#/" || hash === "#/chat" || direct) {
    const result = await api("/api/boards");
    if (!state.me) return;
    state.boards = result.boards;
  }
  let last;
  try { last = localStorage.getItem(boardPreference()); } catch { /* The first readable board is the fallback. */ }
  // Bare #/chat names no project: open the default project's chat, never a slug read from the path.
  const resolved = hash === "#/chat" ? resolveRoute("#/", { boards: state.boards, last }).replace(/^#\/board\/[^/?]+$/, "$&/chat") : resolveRoute(hash, { boards: state.boards, last });
  if (resolved !== hash) { history.replaceState(history.state, "", resolved); hash = resolved; }
  if (/^#\/board\/[^/?]+(?:\/chat)?$/.test(hash)) {
    try { localStorage.setItem(boardPreference(), decodeURIComponent(hash.split("/")[2])); } catch { /* Optional preference. */ }
  }
  if (hash !== state.route) clearAgentSecret(root);
  if (hash !== state.route) voice.stop();
  const previousBase = state.base, previousRoute = state.route;
  state.screen++; state.route = hash; state.underChat = null; state.fromChat = null;
  if (direct) { state.base = resolveRoute("#/", { boards: state.boards, last }); state.rebase = Number(hash.slice(7)); }
  const start = decideStart(hash);
  if (!/^#\/card\/[1-9]\d*$/.test(hash)) {
    state.base = start !== undefined ? "#/decide" : (/^#\/((?:board\/[^/?]+(?:\/chat)?|space\/[^/?]+)|start|needs|boards|rules|memory(?:\?.*)?|chat(?:\/[^/?]+)?|console|settings|admin)$/.test(hash) ? hash : "#/");
    state.card = null;
  } else if (state.base === "#/admin") {
    state.base = resolveRoute("#/", { boards: state.boards, last });
  }
  if (state.base !== previousBase) state.workbenchChat?.dispose();
  if (previousBase === "#/admin") adminUI?.stop();
  if (previousBase === "#/console" && state.base !== "#/console") consoleUI?.stop();
  if (previousBase === "#/decide" && state.base !== "#/decide") {
    queueFlush(true); queueStop(); state.queue = null; state.queueFocus = "";
  }
  if (state.base === "#/decide" && !hash.startsWith("#/card/")) {
    // Back from a card opened inside the queue is the same visit, whatever deep link started it.
    const sameVisit = previousBase === "#/decide" && state.queue && (state.queue.start === start || (previousRoute.startsWith("#/card/") && start === null));
    if (!sameVisit) {
      queueFlush(true); queueStop(); state.queueFocus = "";
      state.queue = createSession(start, state.me.actor || "", { asked: askedRead(), list: /\?list=1$/.test(hash) });
      queueArmSlow();
    }
  }
  // Needs me and Memory share the side panel with cards: a card route or another screen stops them.
  if (state.base !== "#/needs" || hash.startsWith("#/card/")) { state.needsUI?.stop(); state.needsUI = null; }
  if (!state.base.startsWith("#/memory") || hash.startsWith("#/card/")) { state.memoryUI?.stop(); state.memoryUI = null; }
  if (state.base !== previousBase) {
    boardDispose?.(); boardDispose = null; state.addingCard = false; state.boardFilter = "all";
    state.chatObserver?.disconnect(); state.chatVisible.clear();
  }
  if (state.base !== "#/" && state.base !== "#/needs") speech.stop();
  if (!hash.startsWith("#/card/")) { detailsUI?.stop(); detailsUI = null; state.panelRequest++; }
  root.querySelector("#shell-dialog")?.close();
  root.querySelector("#card-panel").hidden = !hash.startsWith("#/card/");
  markOpenCard();
  if (!root.querySelector("#content").children.length || state.base !== previousBase) root.querySelector("#content").innerHTML = message("Loading…");
  showError("");
  if (hash === "#/admin") { adminScreen(); return; }
  await refresh();
}
// The current view's list first, so a write never sends a version left over from another view.
function findCard(number) {
  if (state.card?.number === number) return state.card;
  const lists = /^#\/(board|space)\//.test(state.base) ? [state.cards, state.children, state.linked, state.open, needsCards()] : [state.open, needsCards(), state.cards, state.children, state.linked];
  return lists.flat().find((c) => c.number === number);
}
async function mutate(key, action) {
  if (state.pending.has(key)) return;
  state.pending.add(key);
  root.querySelectorAll(`button[data-card="${key.replace("card-", "")}"], [data-card="${key.replace("card-", "")}"] button`).forEach((b) => { b.disabled = true; });
  try { await action(); await refresh(); }
  catch (error) {
    if (error.status === 409) { await refresh(); showError("Card changed. Review it and try again."); }
    else showError(error.message);
  } finally {
    state.pending.delete(key);
    root.querySelectorAll(`button[data-card="${key.replace("card-", "")}"], [data-card="${key.replace("card-", "")}"] button`).forEach((b) => { b.disabled = false; });
  }
}
async function approveProposal(number, version, approve) {
  await mutate(`card-${number}`, () => api(`/api/cards/${number}/approve`, { method: "POST", body: { approve, version } }));
}
function clearDecisionDraft(number) {
  for (const key of [`reply-${number}`, `then-${number}`, `then-version-${number}`]) state.drafts.delete(key);
  root.querySelectorAll(`[data-draft="reply-${number}"]`).forEach((input) => { input.value = ""; });
  root.querySelectorAll(`[data-draft="then-${number}"]`).forEach((input) => { input.value = "return"; });
}
function decisionThen(number, version) {
  if (String(state.drafts.get(`then-version-${number}`)) === String(version)) return state.drafts.get(`then-${number}`) || "return";
  state.drafts.delete(`then-${number}`);
  state.drafts.delete(`then-version-${number}`);
  return "return";
}
async function decide(number, payload, from) {
  // The version the person saw: if the card was re-asked since, the server answers 409.
  const version = Number(from.closest("[data-version]")?.dataset.version);
  await mutate(`card-${number}`, async () => {
    await api(`/api/cards/${number}/decide`, { method: "POST", body: { ...payload, version } });
    clearDecisionDraft(number);
  });
}
async function move(number, status, extra = {}) {
  const card = findCard(number);
  if (!card) return;
  if (status === "review" && !extra.question) {
    state.card = await api(`/api/cards/${number}`);
    reviewForm();
    return;
  }
  await mutate(`card-${number}`, () => api(`/api/cards/${number}`, { method: "PATCH", body: { version: card.version, status, ...extra } }));
}
root.addEventListener("input", (event) => {
  if (event.target.dataset.draft) {
    const key = event.target.dataset.draft;
    state.drafts.set(key, event.target.value);
    if (/^then-\d+$/.test(key)) state.drafts.set(`then-version-${key.slice(5)}`, event.target.closest("[data-version]")?.dataset.version);
    root.querySelectorAll(`[data-draft="${CSS.escape(key)}"]`).forEach((input) => { if (input !== event.target) input.value = event.target.value; });
  }
});
root.addEventListener("change", (event) => { if (event.target.matches("[data-agent-client]")) changeAgentClient(event.target); });
root.addEventListener("focusin", (event) => {
  const row = event.target.closest("[data-row], .workbench-board .card[data-card]");
  // Needs me keeps its own selection; focus there never moves the shell's.
  if (row && !row.closest(".needs-me") && Number(row.dataset.card || row.dataset.row) !== state.selected) {
    state.selected = Number(row.dataset.card || row.dataset.row);
    markOpenCard();
    root.querySelectorAll("[data-row]").forEach((r) => r.classList.toggle("sel", Number(r.dataset.row) === state.selected));
  }
});
root.addEventListener("click", async (event) => {
  if (event.defaultPrevented) return;
  const b = event.target.closest("[data-action]");
  if (!b) {
    const restart = event.target.closest('#chat .chat-recovery a[href$="/settings"]');
    if (restart) { event.preventDefault(); await openRestart(restart).catch((error) => showError(error.message)); return; }
    const card = event.target.closest(".workbench-board .card[data-card]");
    if (card && !event.target.closest("a,button,input,select,textarea")) { state.selected = Number(card.dataset.card); location.hash = `#/card/${state.selected}`; }
    if (event.target.closest("#shell-dialog a")) root.querySelector("#shell-dialog").close();
    return;
  }
  const number = Number(b.dataset.card), action = b.dataset.action;
  if (consoleUI?.click(action, b)) return;
  if (action.startsWith("queue-")) {
    // A double tap must not answer the next question too: answers wait out the arm delay.
    if (["queue-yes", "queue-no", "queue-answer", "queue-ask-new"].includes(action) && performance.now() < state.queueShownAt + SWIPE.arm) return;
    const current = state.queue?.items[state.queue.cursor]?.number;
    if (action === "queue-answer") {
      const reply = queueDraft(current);
      if (current) state.drafts.set(`reply-${current}`, reply);
      queueApply({ type: "answer", optionIndex: Number(b.dataset.option), reply });
    } else if (action === "queue-yes") queueApply({ type: "yes" });
    else if (action === "queue-no") queueApply({ type: "no" });
    else if (action === "queue-back") queueApply({ type: "back" });
    else if (action === "queue-reply-open") { state.queueField = true; queueApply({ type: "reply-open" }); }
    else if (action === "queue-ask-new") {
      const reply = queueDraft(current);
      if (current) state.drafts.set(`reply-${current}`, reply);
      queueApply({ type: "ask-new", reply });
    } else if (action === "queue-later") queueApply({ type: "later" });
    else if (action === "queue-undo") queueApply({ type: "undo" });
    else if (action === "queue-retry") queueApply({ type: "retry" });
    else if (action === "queue-next") queueApply({ type: "next" });
    else if (action === "queue-show-later") queueApply({ type: "show-later" });
    else if (action === "queue-jump") queueApply({ type: "jump", number });
    return;
  }
  try {
    if (action === "shortcuts") shortcutsView();
    else if (["palette", "switch-project", "switch-workspace"].includes(action)) shellMenu(action);
    else if (action === "cancel-card") cancelCard();
    else if (action === "card-menu") {
      const card = findCard(number), board = currentBoard();
      if (card && board) cardMenuUI().show(card, { board, from: b });
    }
    else if (action === "open-chat" || action === "back-to-chat") { const board = currentBoard(); if (board) location.hash = `#/board/${enc(board.slug)}/chat`; }
    else if (action === "connect-agent") {
      const slug = currentBoard()?.slug, epoch = state.screen;
      if (!slug) return;
      const { openConnectDialog } = await import("./projects.js");
      if (epoch !== state.screen) return;
      openConnectDialog({ slug, api, e, opener: b, current: () => epoch === state.screen, onUpdate() { void refresh(); } });
    }
    else if (action === "details") {
      const number = state.selected || Number(root.querySelector(".workbench-board .card[data-card]")?.dataset.card);
      if (!number) return;
      state.selected = number; state.detailsSheet = true;
      if (state.route === `#/card/${number}`) root.querySelector("#card-panel .hide")?.focus();
      else location.hash = `#/card/${number}`;
    } else if (action === "from-chat-back") {
      event.preventDefault();
      const from = state.fromChat;
      state.fromChat = null; root.querySelector(".m-frombar")?.remove();
      const row = (from?.row && document.getElementById(from.row)) || root.querySelector(`#chat [data-chat-card="${from?.number}"]`);
      void state.workbenchChat?.dock.open({ from: root.querySelector("#m-chat-tab") || undefined, focus: row, resume: true });
    }
    else if (action.startsWith("phone-push-")) await phonePushAction(action);
    else if (action.startsWith("agent-")) await agentAction(b);
    else if (action === "speak-digest") await speech.toggle();
    else if (action === "need-accept") {
      state.drafts.set(`then-${number}`, "done");
      state.drafts.set(`then-version-${number}`, Number(b.dataset.version));
      root.querySelectorAll(`[data-draft="then-${number}"]`).forEach((input) => { input.value = "done"; });
      location.hash = `#/card/${number}`;
    } else if (action === "need-approve" || action === "need-reject") await approveProposal(number, Number(b.dataset.version), action === "need-approve");
    else if (action === "need-requeue" || action === "need-reassign") {
      const card = { ...findCard(number), number, version: Number(b.dataset.version) };
      handoffForm(card, { to: action === "need-requeue" ? "anyone" : "", connectionsOnly: action === "need-reassign" });
    } else if (action === "need-park") await mutate(`card-${number}`, () => api(`/api/cards/${number}`, { method: "PATCH", body: { version: Number(b.dataset.version), status: "backlog" } }));
    else if (action === "decide") {
      const reply = state.drafts.get(`reply-${number}`);
      const version = Number(b.closest("[data-version]")?.dataset.version);
      await decide(number, { choice: b.dataset.choice, ...(reply ? { reply } : {}), then: decisionThen(number, version) }, b);
    } else if (action === "move") await move(number, b.dataset.status);
    else if (action === "panel-move") {
      const status = root.querySelector("#card-status").value;
      if (status === "review") reviewForm(); else await move(state.card.number, status);
    } else if (action === "approve-proposal" || action === "reject-proposal") await approveProposal(state.card.number, state.card.version, action === "approve-proposal");
    else if (action === "children-toggle") {
      const card = findCard(number);
      if (!card) return;
      if (!state.expandedChildren.delete(number)) state.expandedChildren.add(number);
      const saved = remember();
      b.closest(".task-children").outerHTML = children(card, b.dataset.scope);
      restore(saved);
      root.querySelector(`.child-toggle[data-card="${number}"][data-scope="${b.dataset.scope}"]`)?.focus({ preventScroll: true });
    } else if (action === "cs-toggle") {
      const key = `${state.card.number}:${b.dataset.path}`, saved = remember();
      if (!state.toggled.delete(key)) state.toggled.add(key);
      b.closest(".cs").outerHTML = callTree(state.card);
      restore(saved);
      root.querySelector(`.cs [data-path="${CSS.escape(b.dataset.path)}"]`)?.focus({ preventScroll: true });
    } else if (action === "backlog") { state.backlog = !state.backlog; const saved = remember(); boardView(); restore(saved); }
    else if (action === "retry") await refresh({ background: state.base === "#/rules" && rulesUI.busy() });
    else if (action === "older-messages") await olderMessages();
    else if (action === "space-tab") {
      voice.stop();
      const saved = remember(); state.spaceTab = b.dataset.tab; spaceView(); restore(saved);
    } else if (action === "coordinator") {
      voice.stop();
      const slug = b.dataset.board || currentBoard()?.slug, epoch = state.screen;
      if (!slug) return;
      const { mountProject } = await import("./projects.js");
      if (epoch !== state.screen) return;
      const panel = root.querySelector("#card-panel");
      // On Needs me the panel holds the item's detail: it waits while settings use the panel.
      const needs = state.base === "#/needs" ? state.needsUI : null;
      needs?.pause(true);
      panel.hidden = false;
      panel.innerHTML = '<section class="form-tile"><button type="button" class="close" data-action="close-panel" aria-label="Close project settings">×</button><div data-project-settings></div></section>';
      await mountProject(panel.querySelector("[data-project-settings]"), { slug, api, e, settings: true, current: () => epoch === state.screen,
        ...(needs ? { onBack() { panel.hidden = true; } } : {}) });
    }
    else if (action === "close-panel") {
      closeCardPanel();
    }
    // The restart settings' × reads "Back to the chat": it takes the same path as its Back link.
    else if (action === "settings-back-to-chat") {
      if (state.workbenchChat) void state.workbenchChat.dock.backToChat(); else closeCardPanel();
    } else if (action === "new-card") newCardForm();
    else if (action === "edit-card") newCardForm(state.card);
    else if (action === "handoff") handoffForm(state.card);
    else if (action === "new-board") openOnboarding();
    else if (action === "logout") {
      state.needsUI?.stop(); state.needsUI = null;
      voice.stop();
      clearWorkbenchChat();
      await api("/app/logout", { method: "POST" }); state.me = null; state.drafts.clear(); signin();
    } else if (action === "remove-hook") await mutate(`hook-${b.dataset.hook}`, () => api(`/api/webhooks/${enc(b.dataset.hook)}`, { method: "DELETE" }));
    else if (action === "test-hook") await mutate(`hook-${b.dataset.hook}`, () => api(`/api/webhooks/${enc(b.dataset.hook)}/test`, { method: "POST", body: {} }));
    else if (action === "deliveries") {
      const result = await api(`/api/webhooks/${enc(b.dataset.hook)}/deliveries`);
      root.querySelector("#deliveries").innerHTML = `<h3>Deliveries</h3>${(result.deliveries || []).map((d) => `<div class="setting-row"><span>${e(d.status)} · ${e(d.http_status || "—")} · ${e(d.error || "")}</span><time>${e(d.delivered_at || d.next_at || "")}</time></div>`).join("") || message("No deliveries")}`;
    }
  } catch (error) { showError(error.message); }
});
root.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-form]"); if (!form) return;
  event.preventDefault();
  if (form.dataset.form === "queue-reply") {
    const cardNumber = Number(form.dataset.card);
    const reply = queueDraft(cardNumber);
    if (!reply.trim()) { showError("Enter a reply."); return; }
    state.drafts.set(`reply-${cardNumber}`, reply);
    queueApply({ type: "reply", reply });
    return;
  }
  const payload = data(form), kind = form.dataset.form;
  if (kind === "reply" && !payload.reply?.trim()) { showError("Enter a reply."); return; }
  if (kind === "handoff" && (!payload.note?.trim() || !payload.to?.trim())) { showError("Enter a recipient and a note."); return; }
  if (kind === "handoff" && form.dataset.connectionsOnly === "true" && !/^conn:\S+$/.test(payload.to)) {
    showError("Choose an agent using conn: followed by its name.");
    return;
  }
  const formKey = `form-${kind}-${form.dataset.card || ""}`;
  if (state.pending.has(formKey)) return;
  const submit = form.querySelector('button:not([type="button"])');
  if (submit?.disabled) return;
  if (submit) submit.disabled = true;
  state.pending.add(formKey);
  try {
    if (kind === "login") {
      let token = payload.token.trim();
      try { token = new URL(token).searchParams.get("t") || token; } catch { /* A bare token is valid. */ }
      location.assign(`/app/login?t=${enc(token)}`);
    } else if (kind === "agent") await createAgent(form);
    else if (kind === "email-login") {
      const result = await api("/app/auth/email", { method: "POST", body: { email: payload.email, next: payload.next || signinNext } });
      let status = root.querySelector(".signin [role=status]");
      if (!status) { status = document.createElement("div"); status.className = "message"; status.setAttribute("role", "status"); form.after(status); }
      status.textContent = result.message;
    } else if (kind === "reply") await decide(Number(form.dataset.card), { ...payload, then: payload.then || "return" }, form);
    else if (kind === "comment") await mutate(`card-${state.card.number}`, async () => {
      await api(`/api/cards/${state.card.number}/comments`, { method: "POST", body: payload });
      state.drafts.delete(`comment-${state.card.number}`); form.reset();
    });
    else if (kind === "new-card") await mutate("new-card", async () => {
      const card = await api(`/api/boards/${enc(form.dataset.board)}/cards`, { method: "POST", body: payload });
      location.hash = `#/card/${card.number}`;
    });
    else if (kind === "edit-card") await mutate(`card-${state.card.number}`, () => api(`/api/cards/${state.card.number}`, { method: "PATCH", body: { ...payload, version: state.card.version } }));
    else if (kind === "review") await move(state.card.number, "review", { ...payload, options: payload.options.split("\n").map((v) => v.trim()).filter(Boolean) });
    else if (kind === "handoff") await mutate(`card-${Number(form.dataset.card)}`, () => api(`/api/cards/${Number(form.dataset.card)}/handoff`, { method: "POST", body: { ...payload, version: Number(payload.version), options: payload.options.split(",").map((s) => s.trim()).filter(Boolean) } }));
    else if (kind === "board") await mutate("new-board", async () => {
      const slug = payload.name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 100) || `space-${Date.now().toString(36)}`;
      await api("/api/boards", { method: "POST", body: { ...payload, slug } }); location.hash = `#/board/${enc(slug)}`;
      root.querySelector("#card-panel").hidden = true;
    });
    else if (kind === "space-message") await mutate(`chat-${form.dataset.board}`, async () => {
      await api(`/api/boards/${enc(form.dataset.board)}/chat`, { method: "POST", body: payload });
      state.drafts.delete(`space-${form.dataset.board}`); form.reset();
      const stream = root.querySelector(".space-stream"); if (stream) stream.scrollTop = stream.scrollHeight;
    });
    else if (kind === "coordinator") await mutate(`coordinator-${form.dataset.board}`, async () => {
      const result = await api(`/api/boards/${enc(form.dataset.board)}`, { method: "PATCH", body: { coordinator: payload.coordinator || null } });
      state.drafts.delete(`coordinator-${form.dataset.board}`);
      form.querySelector("[data-hook-warning]")?.remove();
      if (result.warning) {
        const warning = document.createElement("p"); warning.dataset.hookWarning = ""; warning.className = "message"; warning.setAttribute("role", "alert");
        warning.textContent = result.warning; form.append(warning);
      } else root.querySelector("#card-panel").hidden = true;
    });
    else if (kind === "webhook") {
      const result = await api("/api/webhooks", { method: "POST", body: { ...payload, events: [payload.events] } });
      await settingsView(state.screen);
      const secret = root.querySelector("#hook-secret");
      secret.textContent = result.secret ? `Secret: ${result.secret}` : "Webhook added";
      secret.className = "secret";
    }
  } catch (error) { showError(error.message); }
  finally { state.pending.delete(formKey); if (submit?.isConnected) submit.disabled = false; }
});
let drag = null;
root.addEventListener("dragstart", (event) => {
  const card = event.target.closest(".card[data-card]"); if (!card) return;
  drag = Number(card.dataset.card); event.dataTransfer.setData("text/plain", String(drag)); event.dataTransfer.effectAllowed = "move"; card.classList.add("dragging");
});
root.addEventListener("dragover", (event) => { const column = event.target.closest(".col[data-status]"); if (column && drag) { event.preventDefault(); column.classList.add("drop"); } });
root.addEventListener("dragleave", (event) => { event.target.closest(".col")?.classList.remove("drop"); });
root.addEventListener("dragend", () => { drag = null; root.querySelectorAll(".dragging,.drop").forEach((el) => el.classList.remove("dragging", "drop")); });
root.addEventListener("drop", async (event) => {
  const column = event.target.closest(".col[data-status]"); if (!column || !drag) return;
  event.preventDefault();
  const number = drag, before = event.target.closest(".card[data-card]")?.dataset.card; drag = null;
  await move(number, column.dataset.status, before && Number(before) !== number ? { before: Number(before) } : {});
});
function singleKeysEnabled() {
  if (state.singleKeys != null) return state.singleKeys;
  try { return localStorage.getItem("cmmn.keys.single") !== "false"; } catch { return true; }
}
function workbenchKey(event) {
  const key = event.key.toLowerCase();
  if (key === "?") { shortcutsView(); event.preventDefault(); return true; }
  if (key === "]") {
    if (!root.querySelector("#card-panel").hidden) closeCardPanel();
    else if (/^#\/board\/[^/?]+\/chat$/.test(state.base)) location.hash = state.base.slice(0, -5);
    event.preventDefault(); return true;
  }
  if (!state.base.startsWith("#/board/") || state.base.endsWith("/chat")) return false;
  const onControl = event.target instanceof Element && event.target.closest("button,a,summary,[role=tab]");
  if (onControl && event.key === "Enter") return false;
  // Enter inside the open card belongs to that card, never to the board selection behind it.
  if (event.key === "Enter" && event.target instanceof Element && event.target.closest("#card-panel")) return false;
  if (key === "c") newCardForm();
  else if (["j", "k"].includes(key) && root.querySelector("#card-panel").hidden) {
    const cards = [...root.querySelectorAll(".workbench-board .card[data-card]")].filter((card) => card.getClientRects().length);
    if (!cards.length) return false;
    const index = cards.findIndex((card) => Number(card.dataset.card) === state.selected);
    const card = cards[Math.max(0, Math.min(cards.length - 1, index + (key === "j" ? 1 : -1)))];
    state.selected = Number(card.dataset.card); markOpenCard();
    card.focus({preventScroll:true}); card.scrollIntoView({block:"nearest"});
  } else if (key === "e") openSelectedCard("edit-card");
  else if (key === "m") openSelectedCard("move-card");
  else if (event.key === "Enter" && state.selected) location.hash = `#/card/${state.selected}`;
  else return false;
  event.preventDefault(); return true;
}
document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  if (["/", "]", "Escape", "Tab"].includes(event.key) && root.querySelector(".shell.chat-open, .shell.ctx-pushed")) return;
  if (!state.me) return;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") { event.preventDefault(); shellMenu(); return; }
  const dialog = root.querySelector("#shell-dialog");
  if (dialog?.open) { if (event.key === "Escape") { event.preventDefault(); dialog.close(); } return; }
  // Project dialogs sit on document.body and keep their own keys; no board shortcut runs behind them.
  if (document.querySelector?.("dialog[open]")) return;
  const typing = event.target instanceof Element && event.target.closest("input,textarea,select,[contenteditable]");
  if (event.altKey || event.ctrlKey || event.metaKey || typing) return;
  const panel = root.querySelector("#card-panel");
  const panelOpen = panel && !panel.hidden;
  if (event.key === "Escape") {
    if (panelOpen) { event.preventDefault(); closeCardPanel(); return; }
    if (state.base === "#/decide") { location.hash = "#/"; return; }
    root.querySelector('[data-action="close-panel"]')?.click();
    return;
  }
  if (event.repeat) return;
  // Only printable single keys follow the switch; Enter and the arrows always work.
  if (!singleKeysEnabled()) { if (event.key.length > 1) workbenchKey(event); return; }
  if (workbenchKey(event)) return;
  const ask = event.target instanceof Element && event.target.closest("[data-ask]");
  if (ask && /^[1-6]$/.test(event.key)) {
    ask.querySelector(`[data-action="decide"][data-option="${Number(event.key) - 1}"]:not(:disabled)`)?.click();
    event.preventDefault(); return;
  }
  if (state.base === "#/decide" && state.queue && !panelOpen) {
    const onControl = event.target instanceof Element && event.target.closest("button, a, summary, [role=button]");
    const queueAction = event.repeat || (event.key === "Enter" && onControl) ? null : queueKey(event.key, false);
    if (queueAction) {
      event.preventDefault();
      if (queueAction.type === "answer") {
        if (performance.now() < state.queueShownAt + SWIPE.arm) return;
        const current = state.queue.items[state.queue.cursor]?.number;
        queueApply({ type: "answer", optionIndex: queueAction.optionIndex, reply: current ? queueDraft(current) : "" });
      } else if (queueAction.type === "yes") root.querySelector('[data-action="queue-yes"]:not(:disabled)')?.click();
      else if (queueAction.type === "no") root.querySelector('[data-action="queue-no"]:not(:disabled)')?.click();
      else if (queueAction.type === "ask-new") root.querySelector('[data-action="queue-ask-new"]:not(:disabled)')?.click();
      else if (queueAction.type === "back") root.querySelector('[data-action="queue-back"]')?.click();
      else if (queueAction.type === "later") root.querySelector('[data-action="queue-later"]')?.click();
      else if (queueAction.type === "undo") root.querySelector('[data-action="queue-undo"]')?.click();
      else if (queueAction.type === "open") {
        const item = state.queue.items[state.queue.cursor];
        if (item) location.hash = `#/card/${item.number}`;
      } else if (queueAction.type === "focus-reply") {
        const field = root.querySelector(".decide-field input");
        if (field) field.focus();
        else root.querySelector('[data-action="queue-reply-open"]')?.click();
      }
      else if (queueAction.type === "home") location.hash = "#/";
      else if (queueAction.type === "move") queueApply({ type: "move", delta: queueAction.delta });
      return;
    }
  }
  // Needs me owns J/K/R inside its list and details; elsewhere on the screen they are forwarded to it.
  if (state.base !== "#/needs" || !["j", "k", "r"].includes(event.key.toLowerCase())) return;
  const needs = root.querySelector("[data-needs-mount]");
  if (needs && state.needsUI) { event.preventDefault(); needs.dispatchEvent(new KeyboardEvent("keydown", { key: event.key })); }
});
let timer = null, polling = false, retryDelay = 2000;
function schedule(delay) { clearTimeout(timer); timer = setTimeout(poll, delay ?? (document.hidden ? 30000 : 2000)); }
async function poll() {
  if (polling || !state.me) { schedule(); return; }
  polling = true;
  try {
    const feed = await api(`/api/events?after=${state.seq}&epoch=${encodeURIComponent(state.eventEpoch || "")}`);
    if (!state.me) return;
    if (feed.reset && await refresh({ background: false }) === false) throw new Error("Refresh failed");
    if (!feed.reset && feed.events.length && await refresh({ background: true, events: feed.events }) === false) throw new Error("Refresh failed");
    if (state.base === "#/settings" && state.phonePushPending) {
      const epoch = state.screen;
      const phonePush = await api("/api/phone-push");
      if (state.me && epoch === state.screen && !state.pending.has("phone-push")) {
        state.phonePushPending = phonePush.test?.status === "pending";
        const section = root.querySelector("#phone-push");
        if (section) section.outerHTML = phonePushView(phonePush);
      }
    }
    state.seq = feed.seq; state.eventEpoch = feed.epoch || "";
    retryDelay = 2000; state.pollError = false; state.pollFails = 0;
  } catch { state.pollError = true; state.pollFails += 1; retryDelay = Math.min(30000, retryDelay * 2); }
  finally {
    polling = false;
    const indicator = root.querySelector("#connection-state"); if (indicator) indicator.hidden = !state.pollError;
    queueNoteOffline();
    if (state.me) schedule(state.pollError ? retryDelay : undefined);
  }
}
document.addEventListener("visibilitychange", () => { if (document.hidden) queueFlush(true); schedule(document.hidden ? 30000 : 0); });
document.addEventListener("session-expired", () => { adminUI?.stop(); state.needsUI?.stop(); state.needsUI = null; clearWorkbenchChat(); state.me = null; state.drafts.clear(); clearTimeout(timer); state.queue = null; state.error = ""; signin(); });
window.addEventListener("hashchange", route);
window.addEventListener("pagehide", () => { adminUI?.stop(); clearAgentSecret(root); queueFlush(true); speech.stop(); voice.stop(); });
window.addEventListener("online", () => { state.pollFails = 0; queueNoteOffline(); if (state.me) schedule(0); });
window.addEventListener("offline", queueNoteOffline);
setInterval(() => {
  root.querySelectorAll("[data-elapsed]").forEach((time) => { time.textContent = elapsed(time.dataset.elapsed); });
  root.querySelectorAll("[data-claim-until]").forEach((bar) => {
    const end = Date.parse(bar.dataset.claimUntil), start = Date.parse(bar.dataset.claimStart);
    const ratio = Math.max(0, Math.min(1, (end - Date.now()) / (end - start || 1800000)));
    bar.classList.toggle("low", ratio < .2);
    bar.setAttribute("aria-label", `Claim ${Math.round(ratio * 100)}% remaining`);
    [...bar.children].forEach((block, i) => block.classList.toggle("on", i < Math.ceil(ratio * 6)));
  });
}, 15000);
async function boot() {
  try { state.me = await boundedRequest(api, "/api/me"); setPeople(state.me.people); }
  catch (error) { state.error = error.status === 401 ? "" : error.message; await signin(signinCode); return; }
  if (!state.me) return;
  shell(); void voice.load();
  if (!state.me) return;
  let moved = false;
  const params = new URLSearchParams(location.search);
  if (/^#\/card\/[1-9]\d*$/.test(location.hash) && params.has("w")) {
    moved = params.get("w") !== state.me.workspace;
    params.delete("w");
    const search = params.toString();
    history.replaceState(history.state, "", `${location.pathname}${search ? `?${search}` : ""}${moved ? "#/" : location.hash}`);
  }
  if (location.hash === "#/needs" && /^[1-9]\d*$/.test(params.get("github_card") || "")) {
    state.githubCard = Number(params.get("github_card"));
    params.delete("github_card");
    const search = params.toString();
    history.replaceState(history.state, "", `${location.pathname}${search ? `?${search}` : ""}${location.hash}`);
  }
  await route();
  if (moved) showError("This card is in another workspace. Sign out, then sign in again to open it.");
  if (state.me) schedule(0);
}
boot();
