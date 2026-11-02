import { api, escape as e, actor, elapsed, safeLink, pullRequestUrl, setPeople } from "./api.js";
import { mountMap } from "./map.js";
import { digestSpeech } from "./digest.js";
import { voiceInput } from "./voice.js";
import { homeView, needItems } from "./home.js";
import { ASKED_KEY, COPY as DECIDE_COPY, createSession, decideQueue, leftCount, offlineNow, queueKey, RELOAD_ERRORS, reduce, renderDecide, statusHTML, toolsHTML } from "./decide.js";
import { bindSwipe, SWIPE } from "./decide-swipe.js";
import { planView } from "./plan.js";
import { rulesView, rulesHandlers } from "./rules.js";
import { signinView, signinMessage } from "./signin.js";
import { childProgress } from "./children.js";
import { agentsView, createAgent, agentAction, changeAgentClient, clearAgentSecret } from "./agents.js";

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
  me: null, boards: [], needs: {}, cards: [], open: [], openTotal: null, card: null, route: "#/", base: "#/",
  selected: null, backlog: false, drafts: new Map(), pending: new Set(), seq: 0, panelRequest: 0, refreshRequest: 0,
  mapDispose: null, screen: 0, error: "", pollError: false, loading: false,
  chat: [], linked: [], chatBoard: null, chatCursor: 0, spaceTab: "chat", chatTasks: new Map(),
  chatHasMore: false, chatLoading: false, chatObserver: null, chatVisible: new Set(), chatTaskRequests: new Map(),
  toggled: new Set(), expandedChildren: new Set(), rulesWaiting: 0,
  queue: null, pollFails: 0, queueFocus: "", queueShownAt: 0, queueField: false, queueDirty: false,
};
const statuses = ["backlog", "todo", "doing", "review", "done"];
const enc = encodeURIComponent;
const data = (form) => Object.fromEntries(new FormData(form));
const message = (text, retry = false) => `<div class="message" role="status">${e(text)}${retry ? '<button data-action="retry">Retry</button>' : ""}</div>`;
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
const needLabels = { you: "Needs you", plan: "Plan to approve", merge: "Ready to merge", checks: "Checks running", working: "Working", expired: "Expired", idle: "Idle", done: "Done" };
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

function remember() {
  voice.remember();
  root.querySelectorAll("[data-draft]").forEach((input) => state.drafts.set(input.dataset.draft, input.value));
  const active = document.activeElement, ask = active?.closest?.("[data-ask]"), cols = root.querySelector(".cols");
  return {
    key: active?.dataset?.draft, start: active?.selectionStart, end: active?.selectionEnd,
    scope: active?.closest?.("#card-panel, #content")?.id,
    ask: ask && !active.dataset.draft ? { card: ask.dataset.ask, option: active.dataset.option } : null,
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
  state.mapDispose?.(); state.mapDispose = null;
  state.chatObserver?.disconnect(); state.chatVisible.clear(); state.chatTaskRequests.clear();
  state.chat = []; state.linked = []; state.chatBoard = null; state.chatCursor = 0; state.chatTasks.clear();
  providersPromise ||= api("/app/auth/providers").catch(() => ({}));
  const { providers = [], paused = [] } = await providersPromise;
  if (state.me) return;
  const cookie = (document.cookie || "").split(";").map((part) => part.trim()).find((part) => part.startsWith("cmmn_via="));
  const value = cookie ? cookie.slice("cmmn_via=".length) : "";
  const via = /^[a-z]{1,20}$/.test(value) ? value : "";
  if (code !== null) state.error = signinMessage(code, paused, via);
  const path = location.pathname + location.hash;
  root.innerHTML = signinView({ providers, paused, via, error: state.error, next: signinNext || (path === "/app/" ? "" : path) });
}
function shell() {
  root.innerHTML = `<div class="app"><aside class="side"><div class="ws"><a class="brand-home" href="#/" aria-label="Commonplace"><img class="brand-v2" src="/app/brand/logo-small-dark.svg" alt="Commonplace"></a></div><nav class="navs"><a href="#/" data-nav="needs">Home <span class="n" id="needs-count">0</span></a><a href="#/memory" data-nav="memory">Memory</a><a href="#/rules" data-nav="rules">Rules <span class="n" data-rules-count>0</span></a></nav><nav class="navs fill" id="board-nav"></nav><nav class="navs"><a href="#/settings" data-nav="settings">Settings</a><button class="logout" data-action="logout">Sign out</button></nav></aside><main class="main"><header class="head"><h1 id="title">Home</h1><div class="right" id="head-actions"></div></header><div id="connection-state" role="status" hidden>Disconnected</div><div id="screen-error" role="alert" hidden></div><div id="content"></div><aside class="panel" id="card-panel" role="dialog" aria-label="Card" hidden></aside></main><nav class="mtabs"><a href="#/" data-nav="needs">Home <b id="mobile-count">0</b></a><a href="#/boards" data-nav="boards">Spaces</a><a href="#/memory" data-nav="memory">Memory</a><a href="#/rules" data-nav="rules">Rules <span class="n" data-rules-count>0</span></a><a href="#/settings" data-nav="settings">Settings</a></nav></div>`;
  navigation();
}
function navigation() {
  const boardNav = root.querySelector("#board-nav");
  if (!boardNav) return;
  boardNav.innerHTML = state.boards.map((b) => `<a href="#/board/${enc(b.slug)}" ${["space", "board"].some((view) => state.base === `#/${view}/${enc(b.slug)}`) ? 'aria-current="page"' : ""}>${e(b.name || b.slug)}${b.counts.review ? `<span class="n">${b.counts.review}</span>` : ""}</a>`).join("") + '<button class="opt" data-action="new-board">New space</button>';
  root.querySelectorAll("#needs-count, #mobile-count").forEach((n) => { n.textContent = state.needs.count || 0; });
  root.querySelectorAll("[data-rules-count]").forEach((n) => { const count = state.rulesWaiting; n.textContent = count; n.dataset.waiting = String(count > 0); });
  root.querySelector(".app")?.setAttribute("data-screen", state.base === "#/decide" ? "decide" : "");
  const key = state.base === "#/rules" ? "rules" : state.base.startsWith("#/memory") ? "memory" : state.base === "#/settings" ? "settings" : /^#\/(board|space)/.test(state.base) ? "boards" : "needs";
  root.querySelectorAll("[data-nav]").forEach((a) => a.dataset.nav === key ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
}
function showError(text) {
  state.error = text;
  const box = root.querySelector("#screen-error");
  if (box) { box.hidden = !text; box.innerHTML = `<div class="message error">${e(text)}<button data-action="retry">Retry</button></div>`; }
}
function title(text, actions = "") {
  root.querySelector("#title").textContent = text;
  root.querySelector("#head-actions").innerHTML = actions;
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
  return items.map(({ event, ...item }) => event ? `<li class="ev">${clock(event.at)}<span><span class="actor">${e(actor(event.actor))}</span> <span class="v">${e(event.kind)}${event.target ? ` → ${e(actor(event.target))}` : ""}</span></span>${event.text != null || event.choice ? `<p class="said">${e(event.choice || "")}${event.choice && event.text ? "\n" : ""}${e(event.text || "")}</p>` : ""}</li>` : askCard(card, item.ask, { ...item, compact, controls })).join("");
}
function needsCards() {
  return needItems(state.needs).map((item) => item.card);
}
function needsView() {
  title("Home");
  const cards = needsCards();
  if (![...cards, ...state.open].some((c) => c.number === state.selected)) state.selected = cards[0]?.number;
  // Keep the Speak/Stop node (and its focus) through live refreshes.
  const speak = root.querySelector('[data-action="speak-digest"]'), focused = speak && document.activeElement === speak;
  root.querySelector("#content").innerHTML = homeView({ needs: state.needs, boards: state.boards, open: state.open, total: state.openTotal, selected: state.selected, speak: speech.supported }, { dot, taskRow, children });
  if (speak) { root.querySelector('[data-action="speak-digest"]')?.replaceWith(speak); if (focused) speak.focus({ preventScroll: true }); }
  speechView();
}
function boardView() {
  const slug = decodeURIComponent(state.base.slice("#/board/".length));
  const board = state.boards.find((b) => b.slug === slug);
  title(board?.name || slug, `<a class="opt" href="#/space/${enc(slug)}">Chat</a><button class="blockbtn" data-action="new-card">New card</button>`);
  root.querySelector("#content").innerHTML = `<div class="cols ${state.backlog ? "expanded" : ""}">${statuses.map((status) => {
    const all = state.cards.filter((c) => c.status === status);
    const list = status === "done" ? [...all].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 20) : all;
    return status === "backlog" && !state.backlog ? `<section class="col rail"><header><button data-action="backlog" aria-label="Open backlog">${all.length}</button></header><div class="stack"><button data-action="backlog">Backlog</button></div></section>` : `<section class="col ${status}" data-status="${status}"><header><button ${status === "backlog" ? 'data-action="backlog"' : 'disabled'}>${status}</button><span class="n">${all.length}</span></header><div class="stack">${list.map((card) => `<article class="card" draggable="true" data-card="${card.number}" data-key="${card.number}">${taskRow(card, `${card.assignee ? `<span class="actor">${e(actor(card.assignee))}</span>` : ""}${card.links ? `<span>${card.links} memories</span>` : ""}`)}${card.ask ? `<p class="cq">${e(card.ask)}</p>` : ""}${rowFoot(card)}${children(card, "board")}</article>`).join("")}</div></section>`;
  }).join("")}</div>`;
  markOpenCard();
}
function markOpenCard() {
  root.querySelectorAll(".cols .card[data-card]").forEach((card) => {
    card.classList.toggle("open", state.route === `#/card/${card.dataset.card}`);
  });
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
  title("Spaces", '<button class="blockbtn" data-action="new-board">New space</button>');
  root.querySelector("#content").innerHTML = `<section class="queue">${state.boards.map((b) => `<article class="item"><h3><a href="#/board/${enc(b.slug)}">${e(b.name || b.slug)}</a></h3><div class="meta">${Object.entries(b.counts || {}).map(([key, value]) => `${e(key)} ${e(value)}`).join(" · ")}</div></article>`).join("") || message("No spaces")}</section>`;
}
async function cardPanel(number, epoch) {
  const panel = root.querySelector("#card-panel");
  panel.hidden = false;
  const opening = state.card?.number !== number;
  if (opening) panel.innerHTML = message("Loading card…");
  const request = ++state.panelRequest;
  try {
    const card = await api(`/api/cards/${number}`);
    if (request !== state.panelRequest || epoch !== state.screen || state.route !== `#/card/${number}`) return;
    state.card = card;
    const saved = remember();
    panel.innerHTML = `<div class="tile top"><div class="bar"><span class="id">#${card.number}</span><a href="#/board/${enc(card.board)}">${e(card.board)}</a><button class="close" data-action="close-panel" aria-label="Close card">×</button></div><span><span class="state ${e(card.status)}">${e(card.status)}</span></span><h2>${e(card.title)}</h2>${cardMeta(card)}<p class="prose">${e(card.body)}</p>${card.proposal_status ? `<p>Proposal: ${e(card.proposal_status)} · Parent: ${e(actor(card.proposed_to))}</p>` : ""}${card.can_approve ? '<button class="opt" data-action="approve-proposal">Approve</button><button class="opt" data-action="reject-proposal">Reject</button>' : ""}<div class="panel-actions"><select aria-label="Card status" id="card-status">${options(statuses, card.status)}</select><button class="opt" data-action="panel-move">Move</button><button class="opt" data-action="edit-card">Edit</button><button class="opt" data-action="handoff">Hand off</button></div>${card.linked?.length ? `<div class="linked">${card.linked.map((link) => `<a href="#/memory?id=${enc(link.drawer_id)}" class="${link.invalidated ? "gone" : ""}">${e(link.title || "Memory")}</a>`).join("")}</div>` : ""}${callTree(card)}</div><ol class="log">${conversation(card)}</ol><form class="composer" data-form="comment"><input name="text" data-draft="comment-${card.number}" aria-label="Comment" autocomplete="off" required><button>Send</button></form>`;
    restore(saved);
    if (opening) panel.querySelector("[data-ask]")?.scrollIntoView({ block: "nearest" });
  } catch (error) {
    if (request === state.panelRequest && epoch === state.screen) panel.innerHTML = message(error.message, true) + '<button class="opt" data-action="close-panel">Close</button>';
  }
}
function newCardForm(edit = null) {
  voice.stop();
  const panel = root.querySelector("#card-panel");
  panel.hidden = false;
  const slug = edit?.board || decodeURIComponent(state.base.split("/")[2]);
  panel.innerHTML = `<form class="form-tile" data-form="${edit ? "edit-card" : "new-card"}" data-board="${e(slug)}"><div class="bar"><h2>${edit ? "Edit card" : "New card"}</h2><button class="close" type="button" data-action="close-panel" aria-label="Close">×</button></div><label>Title<input name="title" value="${e(edit?.title || "")}" required></label><label>Body<textarea name="body">${e(edit?.body || "")}</textarea></label>${edit ? "" : `<label>Status<select name="status">${options(["backlog", "todo", "doing"], "todo")}</select></label>`}<button class="blockbtn">Save</button></form>`;
  panel.querySelector("input").focus();
}
function reviewForm() {
  const panel = root.querySelector("#card-panel"); panel.hidden = false;
  panel.innerHTML = `<form class="form-tile" data-form="review"><h2>Ask for a decision</h2><label>Question<textarea name="question" minlength="24" required></textarea></label><label>Context<textarea name="context" minlength="20" required>${e(state.card.status_line || state.card.body)}</textarea></label><label>Options (one per line)<textarea name="options" required></textarea></label><button class="blockbtn">Ask</button><button type="button" class="opt" data-action="retry">Cancel</button></form>`;
}
function handoffForm() {
  voice.stop();
  const panel = root.querySelector("#card-panel");
  panel.innerHTML = `<form class="form-tile" data-form="handoff"><h2>Hand off #${state.card.number}</h2><label>To<input name="to" required></label><label>Note<textarea name="note" required></textarea></label><label>Context<textarea name="context">${e(state.card.status_line || state.card.body)}</textarea></label><label>Options<input name="options"></label><button class="blockbtn">Hand off</button><button class="opt" type="button" data-action="retry">Cancel</button></form>`;
}
async function settingsView(epoch) {
  title("Settings");
  const content = root.querySelector("#content");
  if (!content.querySelector(".settings")) content.innerHTML = message("Loading settings…");
  const [connections, hooks, plan] = await Promise.allSettled([api("/api/connections"), api("/api/webhooks"), api("/api/plan")]);
  if (epoch !== state.screen) return;
  const saved = remember();
  content.innerHTML = `<div class="settings">${plan.status === "fulfilled" ? planView(plan.value) : ""}${voice.settings()}${connections.status === "fulfilled" ? agentsView(connections.value) : `<section class="settings-section"><h2>Agents</h2>${message(connections.reason.message, true)}</section>`}<section class="settings-section"><h2>Webhooks</h2><div id="hook-secret"></div>${hooks.status === "fulfilled" ? (hooks.value.webhooks || []).map((h) => `<div class="setting-row"><span>${e(h.url)}<br><small>${e(h.id)}</small></span><button data-action="deliveries" data-hook="${e(h.id)}">Deliveries</button><button data-action="test-hook" data-hook="${e(h.id)}">Test</button><button data-action="remove-hook" data-hook="${e(h.id)}">Remove</button></div>`).join("") || message("No webhooks") : message(hooks.reason.message, true)}<form class="webhook-form" data-form="webhook"><input name="url" type="url" data-draft="webhook-url" aria-label="Webhook URL" required><input name="board" data-draft="webhook-board" aria-label="Board" placeholder="Board"><input name="waiting_on" data-draft="webhook-waiting" aria-label="Waiting on" placeholder="Waiting on"><select name="events" aria-label="Events" data-draft="webhook-events"><option value="*">All events</option><option value="review_needed">Review needed</option><option value="card_changed">Card changed</option><option value="waiting_on_me">Waiting on me</option></select><button class="blockbtn">Add</button></form><div id="deliveries"></div></section><section class="settings-section"><button class="opt" data-action="logout">Sign out</button></section></div>`;
  restore(saved);
}

async function memoryView(epoch, refresh = false) {
  title("Memory");
  const content = root.querySelector("#content");
  if (refresh && state.mapDispose) return; // Map data is refreshed through its controller.
  state.mapDispose?.(); state.mapDispose = null;
  content.innerHTML = `<section class="memory-view"><div class="memory-toolbar"><form data-form="search"><input type="search" name="q" id="memory-query" data-draft="memory-query" aria-label="Search memories" placeholder="Search"><button class="opt">Search</button></form><div class="views" role="tablist" aria-label="Layout"><button role="tab" data-view="map" aria-selected="true">Map</button><button role="tab" data-view="themes" aria-selected="false">Themes</button><button role="tab" data-view="people" aria-selected="false">People</button></div><button class="opt" data-action="new-memory">Save memory</button></div><div class="memory-results" id="memory-results" hidden></div><section class="stage" id="stage" aria-label="Memory map"><canvas id="map" aria-label="Memory connections"></canvas><ul class="legend" id="legend" aria-label="Themes"></ul><div class="when" id="when" aria-label="Memories per week"><div class="bars" id="bars"></div></div><div class="zoom"><button data-zoom="in" aria-label="Zoom in">+</button><span id="pct">100%</span><button data-zoom="out" aria-label="Zoom out">−</button><button data-zoom="fit">Fit</button></div><div class="tip" id="tip" hidden></div><div id="map-state" class="memory-empty">Loading memories…</div></section></section>`;
  restore({});
  const graph = await api("/api/memory/map?limit=400");
  if (epoch !== state.screen) return;
  root.querySelector("#map-state").textContent = graph.nodes.length ? "" : "No memories";
  state.mapDispose = mountMap(content, graph, openMemoryNode);
  const id = new URLSearchParams(state.base.split("?")[1]).get("id");
  if (id) openMemory(id);
}
async function openMemoryNode(node) {
  if (node.kind === "card") {
    const n = Number(String(node.id).replace(/^(card:|c)/, ""));
    if (Number.isInteger(n)) location.hash = `#/card/${n}`;
    return;
  }
  if (node.kind === "memory") { await openMemory(node.id); return; }
  const graph = state.mapDispose?.graph;
  const linked = (graph?.links || []).filter((l) => l.source === node.id || l.target === node.id);
  const neighbours = (graph?.nodes || []).filter((n) => linked.some((l) => l.source === n.id || l.target === n.id) && n.id !== node.id);
  const panel = root.querySelector("#card-panel"); panel.hidden = false;
  panel.innerHTML = `<div class="tile top"><div class="bar">Person<button class="close" data-action="close-panel" aria-label="Close">×</button></div><h2>${e(node.label)}</h2><ul class="linked">${linked.filter((l) => l.kind === "fact").map((l) => `<li>${e(l.label)}</li>`).join("")}${neighbours.map((n) => `<li><button class="opt" data-action="map-node" data-node="${e(n.id)}">${e(n.label)}</button></li>`).join("")}</ul></div>`;
}
async function openMemory(id) {
  const epoch = state.screen;
  const panel = root.querySelector("#card-panel"); panel.hidden = false; panel.innerHTML = message("Loading memory…");
  try {
    const memory = await api(`/api/memory/${enc(id)}`);
    if (epoch !== state.screen) return;
    panel.innerHTML = `<div class="tile top"><div class="bar">Memory<button class="close" data-action="close-panel" aria-label="Close">×</button></div><div class="hit"><p class="q">${e(memory.text)}</p><p class="stamp">${e(memory.author)} · ${e(memory.at)} · ${e(memory.theme)}</p></div><div class="linked">${(memory.cards || []).map((c) => `<a href="#/card/${c.number}">#${c.number} ${e(c.title)}</a>`).join("")}</div></div>`;
  } catch (error) { if (epoch === state.screen) panel.innerHTML = message(error.message); }
}
async function search(form) {
  const q = new FormData(form).get("q");
  const epoch = state.screen;
  const results = root.querySelector("#memory-results");
  results.hidden = false; results.innerHTML = message("Searching…");
  const found = await api(`/api/memory/search?q=${enc(q)}&limit=30`);
  if (epoch !== state.screen) return;
  results.innerHTML = found.results.map((m) => `<button data-action="memory" data-id="${e(m.id)}"><span class="meta">${e(m.theme)} · ${e(m.author)} · ${e(m.at)}</span><p>${e(m.text)}</p></button>`).join("") || message("No memories found");
}
async function refresh({ background = false, events = [] } = {}) {
  if (!state.me) return;
  const epoch = state.screen, request = ++state.refreshRequest;
  const current = () => epoch === state.screen && request === state.refreshRequest;
  try {
    const home = state.base === "#/";
    const [boards, needs, open, rules] = await Promise.all([api("/api/boards"), api("/api/needs-me"), home ? api("/api/cards") : null, api(state.base === "#/rules" ? "/api/rules" : "/api/rules/count").catch((error) => { if (state.base === "#/rules") throw error; return null; })]);
    if (!current()) return;
    state.boards = boards.boards; state.needs = needs;
    if (rules) state.rulesWaiting = rules.waiting;
    if (open) { state.open = open.cards; state.openTotal = open.total; }
    if (/^#\/(board|space)\//.test(state.base)) {
      const slug = decodeURIComponent(state.base.split("/")[2]);
      const result = await api(`/api/boards/${enc(slug)}/cards`);
      if (!current()) return;
      state.cards = result.cards;
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
    const saved = remember(), play = capture(state.base);
    navigation();
    if (state.base.startsWith("#/board/")) boardView();
    else if (state.base.startsWith("#/space/")) spaceView();
    else if (state.base === "#/boards") boardsView();
    else if (state.base === "#/rules") {
      if (!background || rulesUI.force || !rulesUI.busy()) {
        rulesUI.update(rules, { owner: rules.can_edit });
        root.querySelector("#content").innerHTML = rulesView(rules, { owner: rules.can_edit });
      } else rulesUI.stale = true;
      title("Rules", rules.can_edit ? `<button type="button" class="blockbtn" data-rule-action="new" aria-controls="rule-new" aria-expanded="${!!root.querySelector('.rule-new:not([hidden])')}">New rule</button>` : "");
    } else if (state.base.startsWith("#/memory")) {
      const changed = events.some((event) => event.kind.startsWith("memory."));
      if (!background || changed) await memoryView(epoch, background && !changed);
    } else if (state.base === "#/settings") {
      if (!background || events.some((event) => event.kind.startsWith("webhook."))) await settingsView(epoch);
    } else if (state.base === "#/decide") {
      if (state.queue) queueApply(state.queue.ready ? { type: "sync", cards: decideQueue(state.needs) } : { type: "load", cards: decideQueue(state.needs) });
      queueNoteOffline();
    } else needsView();
    if (!current()) return true;
    restore(saved); play();
    if (state.base.startsWith("#/space/")) void hydrateSpaceTasks();
    if (state.route.startsWith("#/card/") && !(background && root.querySelector('form[data-form="edit-card"], form[data-form="handoff"]'))) await cardPanel(Number(state.route.slice(7)), epoch);
    showError("");
    return true;
  } catch (error) {
    if (!current() || !state.me) return;
    showError(error.message);
    if (!root.querySelector("#content").children.length) root.querySelector("#content").innerHTML = message(error.message, true);
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
    state.drafts.delete(`reply-${number}`);
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
  const hash = location.hash || "#/";
  if (hash !== state.route) clearAgentSecret(root);
  if (hash !== state.route) voice.stop();
  const previousBase = state.base, previousRoute = state.route;
  state.screen++; state.route = hash;
  const start = decideStart(hash);
  if (!/^#\/card\/[1-9]\d*$/.test(hash)) {
    state.base = start !== undefined ? "#/decide" : (/^#\/((?:board|space)\/[^/?]+|boards|rules|memory(?:\?.*)?|settings)$/.test(hash) ? hash : "#/");
    state.card = null;
  }
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
  if (!state.base.startsWith("#/memory")) { state.mapDispose?.(); state.mapDispose = null; }
  if (state.base !== previousBase) { state.chatObserver?.disconnect(); state.chatVisible.clear(); }
  if (state.base !== "#/") speech.stop();
  root.querySelector("#card-panel").hidden = !hash.startsWith("#/card/");
  markOpenCard();
  if (!root.querySelector("#content").children.length || state.base !== previousBase) root.querySelector("#content").innerHTML = message("Loading…");
  showError("");
  await refresh();
}
// The current view's list first, so a write never sends a version left over from another view.
function findCard(number) {
  if (state.card?.number === number) return state.card;
  const lists = /^#\/(board|space)\//.test(state.base) ? [state.cards, state.linked, state.open, needsCards()] : [state.open, needsCards(), state.cards, state.linked];
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
async function decide(number, payload, from) {
  // The version the person saw: if the card was re-asked since, the server answers 409.
  const version = Number(from.closest("[data-version]")?.dataset.version);
  await mutate(`card-${number}`, async () => {
    await api(`/api/cards/${number}/decide`, { method: "POST", body: { ...payload, version } });
    state.drafts.delete(`reply-${number}`);
    root.querySelectorAll(`[data-draft="reply-${number}"]`).forEach((input) => { input.value = ""; });
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
    root.querySelectorAll(`[data-draft="${CSS.escape(key)}"]`).forEach((input) => { if (input !== event.target) input.value = event.target.value; });
  }
});
root.addEventListener("change", (event) => { if (event.target.matches("[data-agent-client]")) changeAgentClient(event.target); });
root.addEventListener("focusin", (event) => {
  const row = event.target.closest("[data-row]");
  if (row && Number(row.dataset.row) !== state.selected) {
    state.selected = Number(row.dataset.row);
    root.querySelectorAll("[data-row]").forEach((r) => r.classList.toggle("sel", Number(r.dataset.row) === state.selected));
  }
});
root.addEventListener("click", async (event) => {
  const b = event.target.closest("[data-action]"); if (!b) return;
  const number = Number(b.dataset.card), action = b.dataset.action;
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
    if (action.startsWith("agent-")) await agentAction(b);
    else if (action === "speak-digest") await speech.toggle();
    else if (action === "decide") {
      const reply = state.drafts.get(`reply-${number}`);
      await decide(number, { choice: b.dataset.choice, ...(reply ? { reply } : {}), then: state.drafts.get(`then-${number}`) || "return" }, b);
    } else if (action === "move") await move(number, b.dataset.status);
    else if (action === "panel-move") {
      const status = root.querySelector("#card-status").value;
      if (status === "review") reviewForm(); else await move(state.card.number, status);
    } else if (action === "approve-proposal" || action === "reject-proposal") await mutate(`card-${state.card.number}`, () => api(`/api/cards/${state.card.number}/approve`, { method: "POST", body: { approve: action === "approve-proposal", version: state.card.version } }));
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
    else if (action === "retry") await refresh();
    else if (action === "older-messages") await olderMessages();
    else if (action === "space-tab") {
      voice.stop();
      const saved = remember(); state.spaceTab = b.dataset.tab; spaceView(); restore(saved);
    } else if (action === "coordinator") {
      voice.stop();
      const slug = decodeURIComponent(state.base.split("/")[2]), board = state.boards.find((b) => b.slug === slug);
      const panel = root.querySelector("#card-panel"); panel.hidden = false;
      panel.innerHTML = `<form class="form-tile" data-form="coordinator" data-board="${e(slug)}"><h2>Coordinator</h2><label>Agent<input name="coordinator" value="${e(board?.coordinator || "")}" data-draft="coordinator-${e(slug)}"></label><button class="blockbtn">Save</button><button type="button" class="opt" data-action="close-panel">Cancel</button></form>`;
    }
    else if (action === "close-panel") {
      if (state.route.startsWith("#/card/")) location.hash = state.base;
      else root.querySelector("#card-panel").hidden = true;
    } else if (action === "new-card") newCardForm();
    else if (action === "edit-card") newCardForm(state.card);
    else if (action === "handoff") handoffForm();
    else if (action === "new-board") {
      const panel = root.querySelector("#card-panel"); panel.hidden = false;
      panel.innerHTML = '<form class="form-tile" data-form="board"><h2>New space</h2><label>Name<input name="name" required maxlength="1000"></label><button class="blockbtn">Create</button><button type="button" class="opt" data-action="close-panel">Cancel</button></form>';
    } else if (action === "memory") await openMemory(b.dataset.id);
    else if (action === "map-node") { const n = state.mapDispose?.graph.nodes.find((n) => n.id === b.dataset.node); if (n) await openMemoryNode(n); }
    else if (action === "new-memory") {
      const panel = root.querySelector("#card-panel"); panel.hidden = false;
      panel.innerHTML = '<form class="form-tile" data-form="memory"><h2>Save memory</h2><label>Text<textarea name="text" required></textarea></label><label>Theme<input name="theme"></label><label>Topic<input name="topic"></label><label>Card<input name="task"></label><button class="blockbtn">Save</button><button type="button" class="opt" data-action="close-panel">Cancel</button></form>';
    } else if (action === "logout") {
      voice.stop();
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
    state.drafts.set(`reply-${cardNumber}`, reply);
    queueApply({ type: "reply", reply });
    return;
  }
  const payload = data(form), kind = form.dataset.form;
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
    else if (kind === "handoff") await mutate(`card-${state.card.number}`, () => api(`/api/cards/${state.card.number}/handoff`, { method: "POST", body: { ...payload, options: payload.options.split(",").map((s) => s.trim()).filter(Boolean) } }));
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
    else if (kind === "search") await search(form);
    else if (kind === "memory") await mutate("new-memory", async () => {
      const result = await api("/api/memory", { method: "POST", body: Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== "")) });
      root.querySelector("#card-panel").hidden = true; await openMemory(result.id);
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
document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.target instanceof Element && event.target.closest("input,textarea,select,[contenteditable]")) return;
  const panel = root.querySelector("#card-panel");
  const panelOpen = panel && !panel.hidden;
  if (event.key === "Escape") {
    if (panelOpen) { panel.querySelector('[data-action="close-panel"]')?.click(); return; }
    if (state.base === "#/decide") { location.hash = "#/"; return; }
    root.querySelector('[data-action="close-panel"]')?.click();
    return;
  }
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
  if (state.base !== "#/" || !state.me) return;
  // With a card open, its keys answer and open that card, not the row selected behind it.
  const target = /^#\/card\/\d+$/.test(state.route) ? Number(state.route.slice(7)) : state.selected;
  const cards = [...new Map(needsCards().map((c) => [c.number, c])).values()];
  const index = cards.findIndex((c) => c.number === state.selected);
  if (["j", "k"].includes(event.key) && cards.length) {
    event.preventDefault(); state.selected = cards[Math.max(0, Math.min(cards.length - 1, index + (event.key === "j" ? 1 : -1)))].number;
    const saved = remember(); needsView(); restore(saved);
    root.querySelector(`[data-row="${state.selected}"]`)?.focus({ preventScroll: true });
    root.querySelector(`[data-row="${state.selected}"]`)?.scrollIntoView({ block: "nearest" });
  } else if (/^[1-6]$/.test(event.key)) {
    // Answer through the open card's own buttons, so its "After decision" select applies.
    const option = `[data-action="decide"][data-option="${Number(event.key) - 1}"]:not(:disabled)`;
    const button = root.querySelector(`[data-ask="${target}"] ${option}`);
    if (button) { event.preventDefault(); button.click(); }
  } else if (event.key === "o" && target) location.hash = `#/card/${target}`;
  else if (event.key === "Enter" && target && !(event.target instanceof Element && event.target.closest("button,a"))) {
    event.preventDefault();
    const reply = root.querySelector(`[data-draft="reply-${target}"]`);
    if (reply) reply.focus(); else location.hash = `#/card/${target}`;
  }
});
let timer = null, polling = false, retryDelay = 2000;
function schedule(delay) { clearTimeout(timer); timer = setTimeout(poll, delay ?? (document.hidden ? 30000 : 2000)); }
async function poll() {
  if (polling || !state.me) { schedule(); return; }
  polling = true;
  try {
    const feed = await api(`/api/events?after=${state.seq}`);
    if (!state.me) return;
    if (feed.events.length && await refresh({ background: true, events: feed.events }) === false) throw new Error("Refresh failed");
    state.seq = feed.seq;
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
document.addEventListener("session-expired", () => { state.me = null; state.drafts.clear(); clearTimeout(timer); state.queue = null; state.error = ""; signin(); });
window.addEventListener("hashchange", route);
window.addEventListener("pagehide", () => { clearAgentSecret(root); queueFlush(true); speech.stop(); voice.stop(); });
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
  try { state.me = await api("/api/me"); setPeople(state.me.people); }
  catch (error) { state.error = error.status === 401 ? "" : error.message; await signin(signinCode); return; }
  if (!state.me) return;
  shell(); void voice.load();
  if (!state.me) return;
  await route();
  if (state.me) schedule(0);
}
boot();
