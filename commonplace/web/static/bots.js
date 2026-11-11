// Bots screen: a roster of named bots and one streamed chat thread per bot.
// Pure markup functions (…HTML) take the escape helper `e`; createBots owns state, polling and events.
// Controls carry data-bots-action / data-bots-form, never data-action, so the shell's own click
// handler leaves them alone. All server text goes through `e` before it reaches innerHTML.
import { elapsed, safeLink } from "./api.js";
import { sendMessage } from "./botstream.js";

export const BOTS_COPY = Object.freeze({
  title: "Bots",
  newBot: "New bot",
  empty: "No bots yet. Create one to get started.",
  pick: "Choose a bot, or create a new one.",
  hint: (name) => `Tell ${name} what you need. Ask a question, or ask for work and ${name} files it for you.`,
  loading: "Loading…",
  earlier: "Earlier messages",
  waiting: (n) => `Waiting on you (${n})`,
  gone: "That work is no longer available to you.",
  offline: "Could not reach the server. Retrying…",
  unavailable: "This bot is no longer available.",
  stopped: "Stopped.",
  orchestrator: "Orchestrator",
});

const STATUS = Object.freeze({ queued: "Queued", working: "Working", needs_you: "Needs you", ready: "Ready", blocked: "Blocked", done: "Done" });
const DOT = Object.freeze({ needs_you: "Needs you", working: "Working" });
const FAILS = Object.freeze({
  interrupted: "This reply was interrupted. Ask again if you still need it.",
  needs_configuration: "No chat model is set up.",
  limit: "The daily limit is reached.",
  rate: "Too many messages for now. Try again in a minute.",
  model_error: "The model could not answer.",
  timeout: "The bot took too long to answer.",
});
const SETUP = Object.freeze({
  needs_configuration: ["Bots need a chat model before they can answer.", "Choose one in Workspace settings.", "Ask the workspace owner to choose a chat model."],
  needs_turn_on: ["This project's chat model is not turned on yet.", "Turn it on in the project's settings, then send again.", "Ask the workspace owner to turn it on for this project."],
  key_missing: ["The chat model has no key yet.", "Add it in Workspace settings.", "Ask the workspace owner to add it."],
});
// Server-supplied keys only ever match the table's own entries ("constructor" is not a status).
const own = (table, key, fallback) => typeof key === "string" && Object.hasOwn(table, key) ? table[key] : fallback;
const MAX_TEXT = 8000;
const PAGE = 50;
const enc = encodeURIComponent;

export const markRedactions = (escaped) => escaped.replace(/\[(?:redacted|omitted):[a-z_]+\]/g, (marker) => `<mark class="redacted">${marker}</mark>`);
const marked = (value, e) => markRedactions(e(value));
const oneLine = (value) => String(value ?? "").replace(/\s+/g, " ").trim().slice(0, 160);
const card = (value) => Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : 0;
const initial = (name) => (Array.from(String(name ?? "").trim())[0] || "?").toUpperCase();
const link = (url) => {
  const href = safeLink(url);
  if (href) return href;
  return typeof url === "string" && /^#\/[\w\-./?=&%]*$/.test(url) ? url : null;
};
const chevron = '<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 6 3.5 3.5L11.5 6"/></svg>';
const back = '<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 3 5 8l5 5"/></svg>';

export function setupText(code, canConfigure) {
  const [what, owner, member] = own(SETUP, code, SETUP.needs_configuration);
  return `${what} ${canConfigure ? owner : member}`;
}

// ---------- markup ----------

export function rosterHTML({ bots, current = null, loaded = true }, e) {
  if (!bots.length) return `<li class="bots-note">${e(loaded ? BOTS_COPY.empty : BOTS_COPY.loading)}</li>`;
  return bots.map((bot) => {
    const line = bot.last?.text ? `${bot.last.role === "user" ? "You: " : ""}${oneLine(bot.last.text)}` : oneLine(bot.job);
    const unread = Number.isSafeInteger(bot.unread) && bot.unread > 0 ? `<span class="bot-unread">${bot.unread}<span class="sr"> unread</span></span>` : "";
    const dot = own(DOT, bot.state) ? `<span class="bot-dot ${e(bot.state)}" role="img" aria-label="${DOT[bot.state]}"></span>` : "";
    return `<li><a class="bot-row" href="#/bots/${enc(bot.id)}" data-bot="${e(bot.id)}"${bot.id === current ? ' aria-current="page"' : ""}>
      <span class="bot-av" aria-hidden="true">${e(initial(bot.name))}</span>
      <span class="bot-main"><span class="bot-top"><b class="bot-name">${e(bot.name)}</b><span class="deck-bot-job" hidden>${e(oneLine(bot.job))}</span>${bot.last?.at ? `<time class="bot-at" datetime="${e(bot.last.at)}">${e(elapsed(bot.last.at))}</time>` : ""}</span><span class="bot-line">${marked(line, e)}</span></span>
      <span class="bot-side">${unread}${dot}<span class="deck-bot-state" data-state="${e(bot.state)}" hidden>${own({ needs_you: "Waiting on you", working: "Working" }, bot.state, "Idle")}</span></span></a></li>`;
  }).join("");
}

export function modelNoteHTML({ model, canConfigure, error }, e) {
  if (error) return `<p class="bots-notice" role="alert">${e(error)} <button type="button" data-bots-action="retry">Retry</button></p>`;
  if (!model || model.ready !== false) return "";
  const text = setupText(model.code, canConfigure);
  return `<p class="bots-notice" role="status">${e(text)}${canConfigure ? ' <a href="#/settings">Open settings</a>' : ""}</p>`;
}

const gone = (e) => `<p class="bb-gone">${e(BOTS_COPY.gone)}</p>`;

export function workHTML(b, ctx, e) {
  const n = card(b.card);
  if (b.gone) return gone(e);
  const key = `work:${n}:${ctx.mid ?? ""}`;
  const open = ctx.open?.has(key) === true;
  const links = (Array.isArray(b.links) ? b.links : []).map((l) => ({ label: l?.label, href: link(l?.url) })).filter((l) => l.href);
  return `<div class="bb work" data-status="${e(b.status)}"><button type="button" class="bb-head" data-bots-action="toggle" data-key="${e(key)}" aria-expanded="${open}">${own(STATUS, b.status) ? `<span class="chip ${e(b.status)}">${own(STATUS, b.status)}</span>` : ""}<span class="bb-title">${marked(b.title ?? "", e)}</span>${chevron}</button>${open ? `<div class="bb-body">${b.note ? `<p class="bb-note">${marked(b.note, e)}</p>` : ""}${links.length ? `<ul class="bb-links">${links.map((l) => `<li><a href="${e(l.href)}" target="_blank" rel="noopener noreferrer">${e(l.label || l.href)}</a></li>`).join("")}</ul>` : ""}${n ? `<a class="bb-open" href="#/card/${n}">Open</a>` : ""}</div>` : ""}</div>`;
}

export function decisionHTML(b, ctx, e) {
  const n = card(b.card);
  if (b.gone) return gone(e);
  const head = `<p class="bb-q">${marked(b.question ?? "", e)}</p>${b.context ? `<p class="bb-ctx">${marked(b.context, e)}</p>` : ""}`;
  if (!b.open) return `<div class="bb decision" data-state="answered">${head}<p class="bb-answer">${b.answer ? `Answered: ${marked(b.answer, e)}` : "Closed"}</p></div>`;
  const off = ctx.busy?.has(n) ? " disabled" : "";
  const options = (Array.isArray(b.options) ? b.options : []).map((o) => `<button type="button" class="bb-opt" data-bots-action="decide" data-work="${n}" data-version="${e(b.version)}" data-choice="${e(o)}"${off}>${e(o)}</button>`).join("");
  return `<div class="bb decision" data-state="open">${head}${options ? `<div class="bb-btns">${options}</div>` : ""}<form class="bb-reply" data-bots-form="reply" data-work="${n}" data-version="${e(b.version)}"><input name="reply" aria-label="Your own answer" placeholder="Or answer in your own words" autocomplete="off" required${off}><button type="submit" class="bb-opt"${off}>Reply</button></form></div>`;
}

export function confirmHTML(b, ctx, e) {
  const label = marked(b.label ?? "", e);
  if (b.state === "open") {
    const off = ctx.busy?.has(`p:${b.pending}`) ? " disabled" : "";
    return `<div class="bb confirm" data-state="open"><p class="bb-label">${label}</p><div class="bb-btns"><button type="button" class="bb-opt go" data-bots-action="confirm" data-pending="${e(b.pending)}" data-accept="1"${off}>Confirm</button><button type="button" class="bb-opt" data-bots-action="confirm" data-pending="${e(b.pending)}" data-accept="0"${off}>Cancel</button></div></div>`;
  }
  const word = own({ done: "Done", cancelled: "Cancelled", expired: "Expired" }, b.state);
  return `<p class="bb-settled">${label}${word ? ` · ${word}` : ""}</p>`;
}

export function setupHTML(b, e) {
  const canConfigure = b.can_configure === true;
  return `<div class="bb setup"><p>${e(setupText(b.code, canConfigure))}</p>${canConfigure ? '<a class="bb-open" href="#/settings">Open settings</a>' : ""}</div>`;
}

function machinesHTML(b, e) {
  const list = Array.isArray(b.machines) ? b.machines : [];
  if (!list.length) return `<p class="bb-settled">No machines are available to you.</p>`;
  return `<ul class="bb machines">${list.map((m) => `<li><b>${e(m?.label)}</b><span>${m?.capabilities?.start_agent ? "can start work" : "cannot start work"}${m?.last_seen ? ` · seen ${e(elapsed(m.last_seen))} ago` : ""}</span></li>`).join("")}</ul>`;
}

/** Actions are small and share one line; every other block is a card of its own. */
export function actionsHTML(actions, e) {
  if (!actions.length) return "";
  return `<p class="acts">${actions.map((a) => `<span class="act${a.ok === false || a.state === "refused" ? " no" : ""}${a.state === "running" ? " run" : ""}">${e(a.label)}</span>`).join('<span class="sep" aria-hidden="true"> · </span>')}</p>`;
}

export function blocksHTML(blocks, ctx, e) {
  const out = [];
  let run = [];
  const flush = () => { if (run.length) out.push(actionsHTML(run, e)); run = []; };
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (!b || typeof b !== "object") continue;
    if (b.type === "action") { run.push(b); continue; }
    flush();
    if (b.type === "work") out.push(workHTML(b, ctx, e));
    else if (b.type === "decision") out.push(decisionHTML(b, ctx, e));
    else if (b.type === "confirm") out.push(confirmHTML(b, ctx, e));
    else if (b.type === "setup") out.push(setupHTML(b, e));
    else if (b.type === "machines") out.push(machinesHTML(b, e));
  }
  flush();
  return out.join("");
}

function noteHTML(m, e) {
  if (m.state === "stopped") return `<p class="bm-note">${e(BOTS_COPY.stopped)}</p>`;
  const code = /^failed:(.+)$/.exec(m.state || "")?.[1];
  if (!code || (code === "needs_configuration" && (m.blocks || []).some((b) => b?.type === "setup"))) return "";
  return `<p class="bm-note">${e(own(FAILS, code, "The reply failed."))}</p>`;
}

export function messageHTML(m, ctx, e) {
  const kind = m.role === "user" ? "me" : m.role === "event" ? "event" : "bot";
  const bubble = m.text ? `<div class="bm-bubble"><div class="bm-text">${marked(m.text, e)}</div></div>` : "";
  // An event carrying an orchestrator block is the orchestrator's own reply, forwarded word for word.
  const from = kind === "event" && Array.isArray(m.blocks) && m.blocks.some((b) => b?.type === "orchestrator") ? `<p class="bm-from">${BOTS_COPY.orchestrator}</p>` : "";
  return `<li class="bm ${kind}" data-id="${e(m.id)}"${m.at ? ` title="${e(m.at)}"` : ""}>${from}${bubble}${blocksHTML(m.blocks, { ...ctx, mid: m.id }, e)}${kind === "bot" ? noteHTML(m, e) : ""}</li>`;
}

export function liveHTML(live, ctx, e) {
  const actions = (live?.actions || []).filter(Boolean);
  return `<li class="bm bot live"><div class="bm-bubble"><div class="bm-text" data-stream>${marked(live?.text ?? "", e)}</div></div>${actionsHTML(actions, e)}${blocksHTML(live?.blocks, { ...ctx, mid: "live" }, e)}</li>`;
}

export function waitingHTML(waiting, open, ctx, e) {
  if (!waiting.length) return "";
  return `<div class="wait"><button type="button" class="wait-toggle" data-bots-action="waiting" aria-expanded="${open}">${e(BOTS_COPY.waiting(waiting.length))}${chevron}</button>${open ? `<div class="wait-list">${waiting.map((b) => decisionHTML(b, ctx, e)).join("")}</div>` : ""}</div>`;
}

export function headHTML(bot, e) {
  return `<a class="bots-back" href="#/bots">${back}<span>${e(BOTS_COPY.title)}</span></a><button type="button" class="bots-name" data-bots-action="sheet" aria-haspopup="dialog"><span class="bot-av" aria-hidden="true">${e(initial(bot.name))}</span><span class="bots-title"><b>${e(bot.name)}</b>${bot.job ? `<small>${e(oneLine(bot.job))}</small>` : ""}</span></button>`;
}

export function stripHTML(bots, bot, e) {
  // Keep the selected bot available even if the roster request has not finished yet.
  const list = bots.some((b) => b.id === bot.id) ? bots : [bot, ...bots];
  return `<nav class="deck-bots-strip" aria-label="Bots"><a class="bots-back" href="#/bots" aria-label="Back to bots">${back}</a><div class="deck-bot-chips">${list.map((b) => `<a class="deck-bot-chip" href="#/bots/${enc(b.id)}"${b.id === bot.id ? ' aria-current="page"' : ""}><span class="bot-av" aria-hidden="true">${e(initial(b.name))}</span><span>${e(b.name)}</span>${b.state === "needs_you" ? '<span class="deck-bot-wait" role="img" aria-label="Waiting on you"></span>' : ""}</a>`).join("")}</div><button type="button" class="deck-bot-settings" data-bots-action="sheet" aria-label="${e(bot.name)} settings and memory" title="Bot settings and memory"><span class="deck-bot-settings-name">${e(bot.name)}</span>${chevron}</button>${bot.board ? `<a class="deck-project-link" href="#/bots/${enc(bot.id)}" data-bots-action="project"><span class="deck-project-long">What ${e(bot.name)} is doing</span><span class="deck-project-short">Project</span>${chevron}</a>` : ""}</nav>`;
}

/** Mirror bots.blocks.work_status over the person's existing project read projection. */
export function projectWork(item) {
  let status, note = "";
  if (item.status === "done") status = "done";
  else if (item.status === "review") {
    if (item.review_kind !== "decision") status = "ready";
    else if (item.need === "you") status = "needs_you";
    else { status = "blocked"; note = "Waiting for an answer"; }
  } else if (item.waiting) {
    status = "blocked";
    note = item.waiting.text || "";
    if (item.waiting.code === "claim_expired") note = note.replace("hand the card off", "hand the task off");
  }
  else status = item.status === "doing" ? "working" : "queued";
  return { ...item, status, note: oneLine(note || item.status_line) };
}

export function projectHTML(sheet, bot, e, now = new Date()) {
  const week = new Date(now);
  week.setHours(0, 0, 0, 0);
  week.setDate(week.getDate() - (week.getDay() + 6) % 7);
  const groups = new Map([["Needs you", []], ["Working", []], ["Blocked", []], ["Queued", []], ["Done this week", []]]);
  for (const raw of sheet.items || []) {
    const item = projectWork(raw);
    if (item.status === "done") {
      const at = Date.parse(item.done_at);
      if (at >= week.getTime() && at <= now.getTime()) groups.get("Done this week").push(item);
    } else groups.get(own({ needs_you: "Needs you", ready: "Needs you", working: "Working", blocked: "Blocked" }, item.status, "Queued")).push(item);
  }
  const rows = [...groups].filter(([, items]) => items.length).map(([label, items]) => `<section class="deck-project-group"><h3>${label}</h3><ul>${items.map((item) => `<li><span class="chip ${item.status}">${STATUS[item.status]}</span><div><b>${marked(item.title ?? "", e)}</b>${item.note ? `<p>${marked(item.note, e)}</p>` : ""}</div></li>`).join("")}</ul></section>`).join("");
  return `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-h"><div class="sheet-body deck-project"><header><div><h2 id="sheet-h">What ${e(bot.name)} is doing</h2>${bot.job ? `<p>${e(bot.job)}</p>` : ""}</div><button type="button" class="bb-opt" data-bots-action="close" data-focus>Close</button></header>${sheet.loading ? `<p class="bots-note">${e(BOTS_COPY.loading)}</p>` : sheet.error ? '<p class="bots-note" role="alert">This project’s work is unavailable. Close and try again.</p>' : rows || '<p class="bots-note">No open work or work finished this week.</p>'}</div></div>`;
}

export function composerHTML(bot, e) {
  return `<div class="bots-box"><textarea name="text" rows="1" maxlength="${MAX_TEXT}" aria-label="Message to ${e(bot.name)}" placeholder="Message ${e(bot.name)}" enterkeyhint="send"></textarea><span data-region="send"></span></div>`;
}

export function sendButtonHTML({ busy, canStop }) {
  return busy
    ? `<button type="button" class="bots-send stop" data-bots-action="stop"${canStop ? "" : " disabled"}>Stop</button>`
    : '<button type="submit" class="bots-send">Send</button>';
}

export function sheetHTML(sheet, bot, e) {
  if (sheet.kind === "project") return projectHTML(sheet, bot, e);
  if (sheet.kind === "new") {
    return `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-h"><form class="sheet-body" data-bots-form="create"><h2 id="sheet-h">New bot</h2>
      <label>Name<input name="name" maxlength="60" required autocomplete="off" data-focus></label>
      <label>Job<input name="job" maxlength="200" autocomplete="off" placeholder="What it helps with"></label>
      <label>How to work<textarea name="instructions" maxlength="4000" rows="4" placeholder="Optional: tone, habits, things to avoid"></textarea></label>
      ${sheet.error ? `<p class="bots-notice" role="alert">${e(sheet.error)}</p>` : ""}
      <div class="sheet-btns"><button type="submit" class="bb-opt go"${sheet.busy ? " disabled" : ""}>Create</button><button type="button" class="bb-opt" data-bots-action="close">Cancel</button></div></form></div>`;
  }
  const memories = sheet.memories;
  return `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-h"><div class="sheet-body"><h2 id="sheet-h">${e(bot.name)}</h2>
    <form data-bots-form="save">
      <label>Name<input name="name" maxlength="60" required autocomplete="off" value="${e(bot.name)}" data-focus></label>
      <label>Job<input name="job" maxlength="200" autocomplete="off" value="${e(bot.job ?? "")}"></label>
      <label>How to work<textarea name="instructions" maxlength="4000" rows="3">${e(bot.instructions ?? "")}</textarea></label>
      <div class="sheet-btns"><button type="submit" class="bb-opt go"${sheet.busy ? " disabled" : ""}>Save</button></div>
    </form>
    <h3>Memory</h3>
    ${memories === null ? `<p class="bots-note">${e(BOTS_COPY.loading)}</p>` : memories.length ? `<ul class="memory">${memories.map((m) => `<li><span>${marked(m.text, e)}</span><button type="button" class="bb-opt" data-bots-action="forget" data-id="${e(m.id)}" aria-label="Remove this note">Remove</button></li>`).join("")}</ul>` : `<p class="bots-note">Nothing remembered yet. Say “remember that…” in the chat, or add a note.</p>`}
    <form class="memory-add" data-bots-form="memory"><input name="text" maxlength="2000" aria-label="New note" placeholder="Add a note" autocomplete="off" required><button type="submit" class="bb-opt">Add</button></form>
    ${sheet.error ? `<p class="bots-notice" role="alert">${e(sheet.error)}</p>` : ""}
    <div class="sheet-btns"><button type="button" class="bb-opt" data-bots-action="archive">${sheet.confirmArchive ? "Archive for good?" : "Archive bot"}</button><button type="button" class="bb-opt" data-bots-action="close">Close</button></div>
  </div></div>`;
}

const shellHTML = `<div class="bots" data-pane="roster">
  <section class="bots-roster" aria-labelledby="bots-h">
    <header class="bots-bar"><h1 id="bots-h">${BOTS_COPY.title}</h1><p class="deck-bots-intro" hidden>One thread per bot. Tap one to talk.</p><button type="button" class="bots-new" data-bots-action="new">${BOTS_COPY.newBot}</button></header>
    <div data-region="model"></div>
    <ul class="bots-list" data-region="roster"></ul>
  </section>
  <section class="bots-thread" aria-label="Conversation">
    <header class="bots-head" data-region="head"></header>
    <ol class="bots-stream" data-region="messages" tabindex="-1"></ol>
    <div data-region="waiting"></div>
    <div data-region="notice"></div>
    <form class="bots-compose" data-region="composer" data-bots-form="send"></form>
  </section>
  <div data-region="sheet"></div>
</div>`;

const newRequestId = () => {
  const id = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  return id || `r${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
};
/** Questions waiting on the person across all their bots. The General bot's count already covers every
 * readable project, so project bots are not added on top of it (each question would count twice). */
export function waitingTotal(bots) {
  const n = (bot) => Number.isSafeInteger(bot?.waiting) && bot.waiting > 0 ? bot.waiting : 0;
  const list = Array.isArray(bots) ? bots : [];
  const general = list.find((bot) => bot?.kind === "general");
  return general ? n(general) : list.reduce((sum, bot) => sum + n(bot), 0);
}
const lastId = (messages) => messages.reduce((top, m) => Math.max(top, Number(m.id) || 0), 0);
const merge = (rows, added) => [...new Map([...rows, ...added].map((m) => [m.id, m])).values()].sort((a, b) => a.id - b.id);
const waitingIds = (list) => list.map((b) => b.card).sort().join(",");

// ---------- controller ----------

export function createBots({
  mount, api, e,
  setTimer = setTimeout, clearTimer = clearTimeout,
  hidden = () => globalThis.document?.hidden === true,
  phone = () => globalThis.matchMedia?.("(max-width:699px)")?.matches === true,
  go = (hash) => { globalThis.location.hash = hash; },
  send = sendMessage, fetchImpl,
  viewport = globalThis.visualViewport,
  makeId = newRequestId,
  onRoster = () => {},
}) {
  const s = {
    bots: [], model: null, canConfigure: false, loaded: false, error: "",
    botId: null, bot: null, messages: [], waiting: [], running: false, more: false, ready: false,
    open: new Set(), busy: new Set(), waitingOpen: false, notice: null,
    turn: null, unsent: null, sheet: null,
    drafts: new Map(), readSent: new Map(),
  };
  let active = false, generation = 0, ticks = 0, loadingEarlier = false;
  const timers = { thread: null, roster: null };
  let rootEl = null, visibilityBound = false;

  const alive = (token) => active && token === generation;
  const host = () => mount();
  const region = (name) => host()?.querySelector(`[data-region="${name}"]`) ?? null;
  const atBottom = (node) => !node || node.scrollHeight - node.scrollTop - node.clientHeight <= 40;
  const busy = () => Boolean(s.turn) || s.running;
  const ctx = () => ({ open: s.open, busy: s.busy });
  const runningRequestId = () => s.turn?.requestId || (s.running ? [...s.messages].reverse().find((m) => m.role === "user")?.request_id : "") || "";
  const deck = () => globalThis.document?.documentElement?.dataset?.ui === "deck";

  // --- painting ---
  function paintRoster() {
    const node = region("roster");
    if (node) node.innerHTML = rosterHTML({ bots: s.bots, current: s.botId, loaded: s.loaded }, e);
    const model = region("model");
    if (model) model.innerHTML = modelNoteHTML({ model: s.model, canConfigure: s.canConfigure, error: s.error }, e);
    if (deck()) paintHead();
  }
  function paintMessages({ force = false } = {}) {
    const node = region("messages");
    if (!node) return;
    const stick = force || atBottom(node), top = node.scrollTop;
    const parts = [];
    if (!s.botId) parts.push(`<li class="bots-empty">${e(BOTS_COPY.pick)}</li>`);
    else if (!s.ready) parts.push(`<li class="bots-empty">${e(s.notice?.gone ? BOTS_COPY.unavailable : BOTS_COPY.loading)}</li>`);
    else {
      if (s.more) parts.push(`<li class="bots-more"><button type="button" data-bots-action="earlier">${e(BOTS_COPY.earlier)}</button></li>`);
      for (const m of s.messages) parts.push(messageHTML(m, ctx(), e));
      if (s.turn && !s.turn.acked) parts.push(`<li class="bm me echo"><div class="bm-bubble"><div class="bm-text">${marked(s.turn.text, e)}</div></div></li>`);
      if (s.turn || s.running) parts.push(liveHTML(s.turn?.live, ctx(), e));
      if (!s.messages.length && !s.turn && !s.running) parts.push(`<li class="bots-empty">${e(BOTS_COPY.hint(s.bot?.name || "the bot"))}</li>`);
    }
    node.innerHTML = parts.join("");
    node.scrollTop = stick ? node.scrollHeight : top;
  }
  function paintWaiting() {
    const node = region("waiting");
    if (node) node.innerHTML = s.botId ? waitingHTML(s.waiting, s.waitingOpen, ctx(), e) : "";
  }
  function paintNotice() {
    const node = region("notice");
    if (!node) return;
    node.innerHTML = s.notice && !s.notice.gone ? `<p class="bots-notice" role="${s.notice.tone === "info" ? "status" : "alert"}">${e(s.notice.text)}</p>` : "";
  }
  function paintSend() {
    const node = region("send");
    if (node) node.innerHTML = sendButtonHTML({ busy: busy(), canStop: Boolean(runningRequestId()) });
  }
  function paintSheet() {
    const node = region("sheet");
    if (!node) return;
    node.innerHTML = s.sheet ? sheetHTML(s.sheet, s.bot || {}, e) : "";
    if (s.sheet && !s.sheet.painted) { s.sheet.painted = true; node.querySelector?.("[data-focus]")?.focus?.(); }
  }
  function paintThread() {
    host()?.querySelector(".bots")?.setAttribute?.("data-pane", s.botId ? "thread" : "roster");
    const composer = region("composer");
    paintHead();
    if (composer) {
      composer.innerHTML = s.bot ? composerHTML(s.bot, e) : "";
      const box = composer.querySelector?.('textarea[name="text"]');
      if (box && s.drafts.get(s.botId)) { box.value = s.drafts.get(s.botId); fit(box); }
      takeDraft();
    }
    paintMessages({ force: true });
    paintWaiting();
    paintNotice();
    paintSend();
  }
  function paintHead() {
    const head = region("head");
    if (head) head.innerHTML = s.bot ? deck() ? stripHTML(s.bots, s.bot, e) : headHTML(s.bot, e) : "";
  }
  function takeDraft() {
    const box = composerBox();
    if (!s.bot || !s.ready || !box || s.notice?.gone) return;
    try {
      const storage = globalThis.sessionStorage, raw = storage?.getItem("cmmn.deck.draft");
      if (raw === null || raw === undefined) return;
      let draft;
      try { draft = JSON.parse(raw); } catch { storage.removeItem("cmmn.deck.draft"); return; }
      if (!draft || typeof draft.bot !== "string" || typeof draft.text !== "string") { storage.removeItem("cmmn.deck.draft"); return; }
      if (draft.bot !== s.botId) return;
      setComposer(draft.text);
      box.focus?.();
      storage.removeItem("cmmn.deck.draft");
    } catch { /* Storage can be disabled; the composer still works. */ }
  }
  function fit(box) {
    if (!box?.style) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight || 0, 160)}px`;
  }
  const composerBox = () => region("composer")?.querySelector?.('textarea[name="text"]') ?? null;
  function setComposer(text) {
    const box = composerBox();
    if (box) { box.value = text; fit(box); }
    if (text) s.drafts.set(s.botId, text); else s.drafts.delete(s.botId);
  }
  function updateStream() {
    const node = region("messages");
    const live = node?.querySelector?.("[data-stream]");
    if (!live) { paintMessages(); return; }
    const stick = atBottom(node);
    live.innerHTML = marked(s.turn?.live?.text ?? "", e);
    if (stick) node.scrollTop = node.scrollHeight;
  }
  function repaintAll() { paintMessages(); paintWaiting(); paintSend(); }

  // --- reading ---
  function takePage(page, { replace = false } = {}) {
    if (page.bot) s.bot = { ...s.bot, ...page.bot };
    if (Array.isArray(page.messages)) s.messages = replace ? merge([], page.messages) : merge(s.messages, page.messages);
    if (Array.isArray(page.waiting)) s.waiting = page.waiting;
    s.running = page.running === true;
  }
  function markRead() {
    const top = lastId(s.messages);
    if (!s.botId || !s.ready || hidden() || !top || (s.readSent.get(s.botId) || 0) >= top) return;
    const id = s.botId;
    s.readSent.set(id, top);
    const row = s.bots.find((b) => b.id === id);
    if (row && row.unread) { row.unread = 0; paintRoster(); }
    api(`/api/bots/${enc(id)}/read`, { method: "POST", body: { upto: top } }).catch(() => { if ((s.readSent.get(id) || 0) === top) s.readSent.delete(id); });
  }
  async function loadRoster(token) {
    try {
      const result = await api("/api/bots");
      if (!alive(token)) return;
      s.bots = result.bots || []; s.model = result.model || null; s.canConfigure = result.can_configure === true;
      s.loaded = true; s.error = "";
      if (s.botId && !s.bot) s.bot = s.bots.find((b) => b.id === s.botId) || null;
      onRoster(s.bots);
    } catch (error) {
      if (!alive(token)) return;
      s.loaded = true; s.error = error.message || BOTS_COPY.offline;
    }
    paintRoster();
  }
  async function loadThread(token, id) {
    try {
      const page = await api(`/api/bots/${enc(id)}/messages?limit=${PAGE}`);
      if (!alive(token) || s.botId !== id) return;
      takePage(page, { replace: true });
      s.more = (page.messages || []).length >= PAGE;
      s.ready = true; s.notice = null;
    } catch (error) {
      if (!alive(token) || s.botId !== id) return;
      if (error.status === 404) { s.notice = { gone: true, text: BOTS_COPY.unavailable }; s.bot = null; }
      else s.notice = { text: error.message || BOTS_COPY.offline, tone: "error" };
    }
    paintThread();
    markRead();
    scheduleThread(token);
  }
  async function reloadTail(token = generation) {
    if (!s.botId) return;
    const id = s.botId;
    try {
      const page = await api(`/api/bots/${enc(id)}/messages?limit=${PAGE}`);
      if (!alive(token) || s.botId !== id) return;
      takePage(page);
      repaintAll();
      markRead();
    } catch { /* the next poll tries again */ }
  }
  async function pollThread(token) {
    const id = s.botId;
    if (!id || !s.ready || hidden()) return;
    ticks++;
    try {
      // The newest messages by id; every sixth round re-reads the tail so older blocks show their current state.
      const refresh = ticks % 6 === 0;
      const page = await api(`/api/bots/${enc(id)}/messages?${refresh ? "" : `after=${lastId(s.messages)}&`}limit=${PAGE}`);
      if (!alive(token) || s.botId !== id) return;
      const before = waitingIds(s.waiting), wasRunning = s.running;
      takePage(page);
      if (s.turn?.state === "polling" && !s.running) { s.turn = null; s.notice = null; }
      if (before !== waitingIds(s.waiting) && !refresh) { await reloadTail(token); }
      else repaintAll();
      if (wasRunning && !s.running) void loadRoster(token);
      if (s.notice?.offline) { s.notice = null; paintNotice(); }
      markRead();
    } catch (error) {
      if (!alive(token) || s.botId !== id) return;
      if (error.status === 404) { s.notice = { gone: true, text: BOTS_COPY.unavailable }; s.ready = false; paintThread(); return; }
      s.notice = { offline: true, text: BOTS_COPY.offline, tone: "info" };
      paintNotice();
    }
  }
  function scheduleThread(token) {
    clearTimer(timers.thread);
    timers.thread = null;
    if (!alive(token) || !s.botId) return;
    timers.thread = setTimer(async () => {
      timers.thread = null;
      if (!alive(token)) return;
      await pollThread(token);
      scheduleThread(token);
    }, busy() ? 1500 : 5000);
  }
  function scheduleRoster(token) {
    clearTimer(timers.roster);
    timers.roster = null;
    if (!alive(token)) return;
    timers.roster = setTimer(async () => {
      timers.roster = null;
      if (!alive(token)) return;
      if (!hidden() && (!s.botId || !phone())) await loadRoster(token);
      scheduleRoster(token);
    }, 15000);
  }
  async function earlier() {
    const first = s.messages[0]?.id, id = s.botId;
    if (!first || loadingEarlier) return;
    loadingEarlier = true;
    try {
      const page = await api(`/api/bots/${enc(id)}/messages?before=${first}&limit=${PAGE}`);
      if (s.botId !== id) return;
      s.messages = merge(s.messages, page.messages || []);
      s.more = (page.messages || []).length >= PAGE;
      const node = region("messages"), height = node?.scrollHeight || 0, top = node?.scrollTop || 0;
      paintMessages();
      if (node) node.scrollTop = top + (node.scrollHeight - height);
    } catch (error) { s.notice = { text: error.message, tone: "error" }; paintNotice(); }
    finally { loadingEarlier = false; }
  }

  // --- sending ---
  function onStream(turn, name, data) {
    if (s.turn !== turn) return;
    if (name === "ack") {
      turn.acked = data?.message?.id ?? true; turn.state = "streaming";
      if (data?.message) s.messages = merge(s.messages, [data.message]);
      paintMessages({ force: true });
    } else if (name === "delta") {
      turn.live.text += String(data?.text ?? "");
      updateStream();
    } else if (name === "action") {
      turn.live.actions[Number.isFinite(data?.step) ? data.step : turn.live.actions.length] = { label: data?.label, state: data?.state, ok: data?.state === "refused" ? false : true };
      paintMessages();
    } else if (name === "block") {
      if (data && data.type !== "action") { turn.live.blocks.push(data); paintMessages(); }
    } else if (name === "done") {
      if (data?.message) s.messages = merge(s.messages, [data.message]);
      turn.finished = true;
    } else if (name === "error") {
      if (data?.message) s.messages = merge(s.messages, [data.message]);
      turn.finished = true;
      if (!data?.message && data?.text) s.notice = { text: data.text, tone: "error" };
    } else if (name === "running") turn.state = "polling";
  }
  async function sendText(raw) {
    const text = String(raw ?? "").trim();
    if (!text || text.length > MAX_TEXT || busy() || !s.botId || !s.ready) return;
    const botId = s.botId;
    const retry = s.unsent?.botId === botId && s.unsent.text === text;
    const requestId = retry ? s.unsent.requestId : makeId();
    s.unsent = null; s.notice = null;
    const turn = s.turn = { botId, requestId, text, acked: null, live: { text: "", actions: [], blocks: [] }, state: "sending", finished: false, controller: new AbortController() };
    setComposer("");
    paintNotice(); paintMessages({ force: true }); paintSend();
    scheduleThread(generation);
    try {
      const result = await send({ botId, text, requestId, signal: turn.controller.signal, fetchImpl, onEvent: (name, data) => onStream(turn, name, data) });
      if (s.turn !== turn) return;
      if (result.state === "dropped" || result.state === "running") {
        // The turn goes on without us: poll until the server says it is no longer running.
        turn.state = "polling";
        s.running = true;
        paintSend();
        void pollThread(generation);
        return;
      }
      s.turn = null;
      repaintAll();
      scheduleThread(generation);
      void pollThread(generation);
      void loadRoster(generation);
    } catch (error) {
      if (s.turn !== turn) return;
      s.turn = null;
      s.unsent = { botId, requestId, text };
      if (error.status === 404) { s.notice = { gone: true, text: BOTS_COPY.unavailable }; s.unsent = null; void loadRoster(generation); }
      else { setComposer(text); s.notice = { text: `${error.message} Send again to retry.`, tone: "error" }; }
      paintThread();
      if (error.status === 404) paintMessages();
      scheduleThread(generation);
    }
  }
  async function stop() {
    const requestId = runningRequestId();
    if (!requestId || !s.botId) return;
    try { await api(`/api/bots/${enc(s.botId)}/stop`, { method: "POST", body: { request_id: requestId } }); }
    catch (error) { s.notice = { text: error.message, tone: "error" }; paintNotice(); }
  }

  // --- answering inside the thread ---
  async function decide(n, version, payload) {
    if (!n || s.busy.has(n)) return;
    s.busy.add(n); repaintAll();
    try {
      await api(`/api/cards/${n}/decide`, { method: "POST", body: { version: Number(version), ...payload } });
      s.notice = null; paintNotice();
    } catch (error) {
      s.notice = error.status === 409 ? { text: "That question changed. Showing the latest.", tone: "info" } : { text: error.message, tone: "error" };
      paintNotice();
    } finally { s.busy.delete(n); }
    await reloadTail();
    void loadRoster(generation);
  }
  async function confirm(pending, accept) {
    const key = `p:${pending}`;
    if (!pending || s.busy.has(key) || !s.botId) return;
    s.busy.add(key); repaintAll();
    try {
      const result = await api(`/api/bots/${enc(s.botId)}/confirm`, { method: "POST", body: { pending, accept } });
      if (result?.message) s.messages = merge(s.messages, [result.message]);
      s.notice = null; paintNotice();
    } catch (error) { s.notice = { text: error.message, tone: "error" }; paintNotice(); }
    finally { s.busy.delete(key); }
    await reloadTail();
    void loadRoster(generation);
  }

  // --- sheets ---
  async function openSheet(kind) {
    if (kind === "project") {
      if (!deck() || !s.bot?.board) return;
      const sheet = s.sheet = { kind, loading: true, items: [], error: "", painted: false };
      const token = generation, id = s.botId;
      paintSheet();
      try {
        const result = await api(`/api/boards/${enc(s.bot.board)}/cards`);
        if (alive(token) && s.botId === id && s.sheet === sheet) sheet.items = [...(result.cards || []), ...(result.children || [])];
      } catch { if (alive(token) && s.sheet === sheet) sheet.error = "unavailable"; }
      if (alive(token) && s.sheet === sheet) { sheet.loading = false; sheet.painted = false; paintSheet(); }
      return;
    }
    s.sheet = { kind, busy: false, error: "", confirmArchive: false, memories: kind === "bot" ? null : undefined, painted: false };
    paintSheet();
    if (kind !== "bot") return;
    const sheet = s.sheet, id = s.botId;
    try {
      const result = await api(`/api/bots/${enc(id)}/memory`);
      if (s.sheet === sheet) { sheet.memories = result.memories || []; paintSheet(); }
    } catch (error) { if (s.sheet === sheet) { sheet.memories = []; sheet.error = error.message; paintSheet(); } }
  }
  function closeSheet() { const project = s.sheet?.kind === "project"; s.sheet = null; paintSheet(); region("head")?.querySelector?.(deck() ? project ? ".deck-project-link" : ".deck-bot-settings" : ".bots-name")?.focus?.(); }
  async function sheetCall(sheet, work) {
    sheet.busy = true; sheet.error = ""; paintSheet();
    try { return await work(); }
    catch (error) { sheet.error = error.message; return undefined; }
    finally { sheet.busy = false; if (s.sheet === sheet) paintSheet(); }
  }
  async function create(form) {
    const sheet = s.sheet;
    if (!sheet || sheet.busy) return;
    const body = { name: form.elements.name.value.trim() };
    for (const key of ["job", "instructions"]) { const value = form.elements[key].value.trim(); if (value) body[key] = value; }
    if (!body.name) return;
    const result = await sheetCall(sheet, () => api("/api/bots", { method: "POST", body }));
    if (!result?.bot || s.sheet !== sheet) return;
    s.sheet = null; paintSheet();
    await loadRoster(generation);
    go(`#/bots/${enc(result.bot.id)}`);
  }
  async function save(form) {
    const sheet = s.sheet, id = s.botId;
    if (!sheet || sheet.busy || !s.bot) return;
    const body = {};
    for (const key of ["name", "job", "instructions"]) {
      const value = form.elements[key].value.trim();
      if (value !== String(s.bot[key] ?? "")) body[key] = value;
    }
    if (!Object.keys(body).length) { closeSheet(); return; }
    if (body.name === "") return;
    const result = await sheetCall(sheet, () => api(`/api/bots/${enc(id)}`, { method: "PATCH", body }));
    if (!result?.bot || s.botId !== id) return;
    s.bot = { ...s.bot, ...result.bot };
    s.bots = s.bots.map((b) => b.id === id ? { ...b, ...result.bot } : b);
    s.sheet = null; paintSheet(); paintRoster(); paintThread();
  }
  async function addMemory(form) {
    const sheet = s.sheet, id = s.botId, text = form.elements.text.value.trim();
    if (!sheet || sheet.busy || !text) return;
    const result = await sheetCall(sheet, () => api(`/api/bots/${enc(id)}/memory`, { method: "POST", body: { text } }));
    if (result?.memory && s.sheet === sheet) { sheet.memories = [...(sheet.memories || []), result.memory]; paintSheet(); }
  }
  async function forget(memoryId) {
    const sheet = s.sheet, id = s.botId;
    if (!sheet || sheet.busy) return;
    const result = await sheetCall(sheet, () => api(`/api/bots/${enc(id)}/memory/${enc(memoryId)}`, { method: "DELETE" }));
    if (result && s.sheet === sheet) { sheet.memories = (sheet.memories || []).filter((m) => String(m.id) !== String(memoryId)); paintSheet(); }
  }
  async function archive() {
    const sheet = s.sheet, id = s.botId;
    if (!sheet || sheet.busy) return;
    if (!sheet.confirmArchive) { sheet.confirmArchive = true; paintSheet(); return; }
    const result = await sheetCall(sheet, () => api(`/api/bots/${enc(id)}`, { method: "PATCH", body: { archived: true } }));
    if (!result?.bot) return;
    s.bots = s.bots.filter((b) => b.id !== id);
    s.sheet = null;
    go("#/bots");
  }

  // --- events ---
  function click(el) {
    const d = el?.dataset || {};
    switch (d.botsAction) {
      case "new": void openSheet("new"); break;
      case "sheet": void openSheet("bot"); break;
      case "project": void openSheet("project"); break;
      case "close": closeSheet(); break;
      case "retry": { const token = generation; s.error = ""; void loadRoster(token); if (s.botId) void loadThread(token, s.botId); break; }
      case "toggle": if (s.open.has(d.key)) s.open.delete(d.key); else s.open.add(d.key); paintMessages(); break;
      case "waiting": s.waitingOpen = !s.waitingOpen; paintWaiting(); break;
      case "earlier": void earlier(); break;
      case "decide": void decide(Number(d.work), d.version, { choice: d.choice }); break;
      case "confirm": void confirm(d.pending, d.accept === "1"); break;
      case "stop": void stop(); break;
      case "forget": void forget(d.id); break;
      case "archive": void archive(); break;
      default: break;
    }
  }
  function submit(form) {
    const d = form?.dataset || {};
    switch (d.botsForm) {
      case "send": return sendText(form.elements.text.value);
      case "reply": { const reply = form.elements.reply.value.trim(); return reply ? decide(Number(d.work), d.version, { reply }) : undefined; }
      case "create": return create(form);
      case "save": return save(form);
      case "memory": return addMemory(form).then(() => { if (form.elements.text) form.elements.text.value = ""; });
      default: return undefined;
    }
  }
  function onClick(event) {
    const el = event.target?.closest?.("[data-bots-action]");
    if (!el) return;
    event.preventDefault?.();
    click(el);
  }
  function onSubmit(event) {
    const form = event.target?.closest?.("[data-bots-form]");
    if (!form) return;
    event.preventDefault?.();
    void submit(form);
  }
  function onInput(event) {
    const box = event.target;
    if (box?.name !== "text" || !box.closest?.('[data-region="composer"]')) return;
    fit(box);
    if (box.value) s.drafts.set(s.botId, box.value); else s.drafts.delete(s.botId);
  }
  function onKeydown(event) {
    if (event.key === "Escape" && s.sheet) { event.preventDefault?.(); closeSheet(); return; }
    const box = event.target;
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && box?.name === "text" && box.closest?.('[data-region="composer"]')) {
      event.preventDefault?.();
      void sendText(box.value);
    }
  }
  function onVisible() {
    if (!active || hidden()) return;
    const token = generation;
    if (s.botId) { void pollThread(token); scheduleThread(token); }
    if (!s.botId || !phone()) void loadRoster(token);
  }
  // The on-screen keyboard shrinks the visual viewport; the screen follows it so the composer stays in view.
  function followViewport() {
    if (!viewport) return;
    const node = region("messages"), stick = atBottom(node);
    globalThis.document?.documentElement?.style?.setProperty("--bots-vh", `${Math.round(viewport.height)}px`);
    if (stick && node) node.scrollTop = node.scrollHeight;
  }

  function unbind() {
    clearTimer(timers.thread); clearTimer(timers.roster);
    timers.thread = timers.roster = null;
    if (visibilityBound) { globalThis.document?.removeEventListener?.("visibilitychange", onVisible); visibilityBound = false; }
    if (viewport) {
      viewport.removeEventListener?.("resize", followViewport);
      viewport.removeEventListener?.("scroll", followViewport);
      globalThis.document?.documentElement?.style?.removeProperty("--bots-vh");
    }
  }

  return {
    show(id = null) {
      const wasActive = active, previous = s.botId;
      const el = host();
      if (!el) return undefined;
      // The shell asks again after unrelated route changes (a work item opened beside the thread): keep what is on screen.
      if (wasActive && id === previous && el.querySelector(".bots")?.isConnected === true) { takeDraft(); return undefined; }
      const token = ++generation;
      active = true;
      if (id !== previous) {
        // Another thread: a local stream for the old one stops; the server finishes the turn and stores it.
        s.turn?.controller.abort();
        s.turn = null; s.unsent = null; s.sheet = null; s.notice = null;
        s.botId = id; s.bot = id ? s.bots.find((b) => b.id === id) || null : null;
        s.messages = []; s.waiting = []; s.running = false; s.more = false; s.ready = false; s.waitingOpen = false;
        ticks = 0;
      }
      el.innerHTML = shellHTML;
      rootEl = el.querySelector(".bots");
      for (const [type, handler] of [["click", onClick], ["submit", onSubmit], ["input", onInput], ["keydown", onKeydown]]) rootEl?.addEventListener?.(type, handler);
      if (!visibilityBound) { globalThis.document?.addEventListener?.("visibilitychange", onVisible); visibilityBound = true; }
      if (!wasActive && viewport) {
        viewport.addEventListener?.("resize", followViewport);
        viewport.addEventListener?.("scroll", followViewport);
        followViewport();
      }
      paintRoster();
      paintThread();
      paintSheet();
      scheduleRoster(token);
      const loads = [loadRoster(token)];
      if (id) loads.push(loadThread(token, id));
      return Promise.all(loads).then(() => undefined);
    },
    stop() {
      active = false;
      generation++;
      s.turn?.controller.abort();
      s.turn = null;
      unbind();
    },
    // Test and shell hooks: the same entry points the DOM listeners use.
    click, submit, keydown: onKeydown, state: s,
  };
}
