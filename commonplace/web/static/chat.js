import { actor, api as request } from "./api.js";
import { cardId } from "./board.js";
import { deriveChatState, seenStore, clearSeen } from "./chatstate.js";

export const CHAT_COPY = Object.freeze({
  title: "Chat", loading: "Loading chat…", unavailable: "Chat is unavailable. Try again.",
  noProjects: "Choose a project to chat with its orchestrator.",
  empty: "Write what you need. The orchestrator answers here and turns the work into cards marked “From chat” on the board.",
  placeholder: "Ask for something…", send: "Send", basic: "Basic replies (no model)",
  model: "Answers by {provider}/{model}", statusPrivate: "status private", decisions: "Decisions",
  runnerAnswer: "via your {cli} login", runnerMode: "Answers via your {cli} login",
  runnerUsage: "{used}/{cap} today", runnerError: "Last error: {code}",
  offline: "Not sent. This device is offline. Your words are kept here until it sends.",
  retry: "Not sent. Try again", sending: "Sending…", kept: "Your message is kept, and nothing changed on the board.", waiting: "Waiting to send your earlier message.", discard: "Discard",
});

const marked = (value, e) => e(value).replace(/\[(?:redacted|omitted):[a-z_]+\]/g, (marker) => `<mark class="redacted">${marker}</mark>`);
const validId = (number) => Number.isSafeInteger(number) && number > 0;
const merge = (rows = [], added = []) => [...new Map([...rows, ...added].map((row) => [row.id, row])).values()].sort((a, b) => a.id - b.id);
const writer = (ref, me, board) => ref === `user:${me?.user}` ? "You" : ref && ref === board.coordinator
  ? `${board.name || board.slug} orchestrator` : `${actor(ref)}${ref?.startsWith("conn:") ? " (agent)" : ""}`;
const localSetting = (front) => {
  if (front?.provider !== "openai_compatible") return false;
  try {
    const host = new URL(front.base_url).hostname;
    return host === "localhost" || host === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(host);
  } catch { return false; }
};
const quickKind = (model) => model?.startsWith("local:") ? "local model" : "hosted provider";
const quickLabel = (model) => `Quick reply · ${quickKind(model)}`;
const modelName = (model) => String(model || "").replace(/^local:/, "");
const pad = (n) => String(n).padStart(2, "0");
// Local wall-clock HH:MM, as the design draws it; the ISO stamp stays in the datetime attribute.
const clock = (at) => {
  const date = at ? new Date(at) : null;
  return date && !Number.isNaN(date.getTime()) ? `${pad(date.getHours())}:${pad(date.getMinutes())}` : "";
};
const words = (...parts) => parts.filter(Boolean).join(" ");
// The phrase a card row's Details strip reads after "Opened from {Project} chat:".
function messageSource(message, me, board) {
  if (message.actor && message.actor === board.coordinator) return words("the orchestrator's", clock(message.at), "answer");
  if (message.actor === `user:${me?.user}`) return words("your", clock(message.at), "message");
  return words(`${writer(message.actor, me, board).replace(/ \(agent\)$/, "")}'s`, clock(message.at), "message");
}

function cardRows(cards, heading, key, board, e, src = "", note = "select one to open its details") {
  if (!cards.length) return "";
  return `<div class="chat-cards" role="group" aria-labelledby="${e(key)}"><div class="chat-cards-heading"><span id="${e(key)}">${e(heading)}</span><span class="chat-cards-note">${e(note)}</span></div>${cards.map((card) => `<a id="chat-row-${e(key)}-${card.number}" href="#/card/${card.number}" data-chat-card="${card.number}" data-src="${e(src)}"><span class="cid">${e(cardId(board, card.number))}</span><span>${marked(card.title || "", e)}</span><span class="chat-card-status">${e(card.status || "")} <span class="chat-show">Show →</span></span></a>`).join("")}</div>`;
}

export function chatHTML(page, me, e) {
  const board = page.project || { slug: page.board || "", coordinator: page.coordinator };
  const cards = [...new Map([...(page.cards || []), ...(page.linked || [])].filter((card) => validId(card.number) && (!card.board || card.board === board.slug)).map((card) => [card.number, card])).values()];
  const ownReplies = (page.replies || []).filter((reply) => reply.for_user == null || reply.for_user === me?.user);
  let divided = false, today = false;
  const messages = page.messages || [];
  // A request's cards sit under the orchestrator's answer to it (the first orchestrator
  // message before the next person's message), else under the request itself.
  const hosted = new Map();
  messages.forEach((message, index) => {
    if (!cards.some((card) => card.message_id === message.id)) return;
    let host = message;
    for (const next of messages.slice(index + 1)) {
      if (next.actor && next.actor === board.coordinator) { host = next; break; }
      if (next.actor?.startsWith("user:")) break;
    }
    hosted.set(host.id, [...(hosted.get(host.id) || []), message.id]);
  });
  return messages.map((message) => {
    let prefix = "";
    if (!divided && ((message.actor?.startsWith("conn:") && message.id > (page.seen?.message ?? Infinity)) || ownReplies.some((reply) => reply.message_id === message.id && ["answer", "handoff"].includes(reply.kind) && reply.id > (page.seen?.reply ?? Infinity)))) {
      divided = true; prefix += '<li class="chat-new" role="separator">New since you last looked</li>';
    }
    if (!today && page.todayIds?.includes(message.id)) { today = true; prefix += '<li class="chat-day">Today</li>'; }
    return `${prefix}<li class="chat-message${message.id === page.at ? " chat-hit" : ""}" id="chat-message-${message.id}">
      ${message.id === page.at ? '<p class="chat-hint">Found in memory</p>' : ""}
      <article><div class="chat-stamp"><b>${marked(writer(message.actor, me, board), e)}</b> <time datetime="${e(message.at)}">${e(clock(message.at) || message.at)}</time></div><p>${marked(message.text, e)}</p></article>
      ${ownReplies.filter((reply) => reply.message_id === message.id).sort((a, b) => a.id - b.id).map((reply) => {
        let stamp = reply.kind === "answer" ? quickLabel(reply.model) : "Delivery update";
        if (reply.model?.startsWith("runner:")) {
          const [cli, ...model] = reply.model.slice(7).split("/");
          stamp += ` · ${CHAT_COPY.runnerAnswer.replace("{cli}", () => cli)}${model.length ? ` · ${model.join("/")}` : ""}`;
        }
        const prefix = cardId(board, 1).split("-")[0];
        const refs = new Set([...String(reply.text).matchAll(/(?:^|[^\w-])(?:#([1-9]\d*)|([A-Za-z])-([1-9]\d*))(?![\w-])/g)].filter((m) => m[1] || m[2].toUpperCase() === prefix).map((m) => Number(m[1] || m[3])));
        const referenced = reply.kind === "answer" ? cards.filter((card) => refs.has(card.number)) : [];
        // Never the model id: the strip names the kind of quick reply only.
        const src = words("the", clock(reply.at), reply.kind === "answer" ? `quick reply (${quickKind(reply.model)})` : "delivery update");
        return `<article class="chat-reply"><div class="chat-stamp"><b>${e(stamp)}</b>${clock(reply.at) ? ` <time datetime="${e(reply.at)}">${e(clock(reply.at))}</time>` : ""}</div>${reply.kind === "answer" ? '<p class="chat-writer-note">Written by the model chosen in Settings, not by the orchestrator.</p>' : ""}<p>${marked(reply.text, e)}</p>${cardRows(referenced, "Cards in this answer", `answer-${reply.id}`, board, e, src)}${reply.kind !== "answer" && validId(reply.task) ? `<a href="#/card/${reply.task}" data-chat-card="${reply.task}" data-src="${e(src)}">Card ${e(cardId(board, reply.task))}</a>` : ""}</article>`;
      }).join("")}
      ${(hosted.get(message.id) || []).map((request) => cardRows(cards.filter((card) => card.message_id === request), "From this request · on the board now", `request-${request}`, board, e, messageSource(message, me, board), `${cards.filter((card) => card.message_id === request).length} new`)).join("")}
    </li>`;
  }).join("") || `<li class="chat-empty"><h3>Ask the orchestrator for something</h3><p>${e(CHAT_COPY.empty)}</p><p>Try asking</p><div class="chat-suggestions">${["What is blocked?", "Plan the next launch", "Split a card into smaller cards"].map((text) => `<button type="button" data-chat-suggest="${e(text)}">${e(text)}</button>`).join("")}</div></li>`;
}

export function statusHTML(status, e) {
  const agent = status.orchestrator, front = status.front;
  let mode = front?.mode === "model" ? `Answers by ${front.provider ? `${front.provider}/` : ""}${modelName(front.model)}` : front?.mode === "off" ? "Chat answers off" : CHAT_COPY.basic;
  if (front?.applies === false) mode = `${front.provider === "none" ? "Basic" : localSetting(front) ? "Local" : "Hosted"}${front.inherited ? " · set for the whole workspace" : ""} · Turn on in Settings for this board`;
  else if (front?.mode === "runner") {
    mode = CHAT_COPY.runnerMode.replace("{cli}", () => front.runner?.cli || "CLI");
    mode += ` · ${CHAT_COPY.runnerUsage.replace("{used}", front.runner?.used_today ?? 0).replace("{cap}", front.runner?.daily_cap ?? 0)}`;
  }
  if ((front?.mode === "runner" || front?.mode === "basic") && front.runner?.last_error) mode += ` · ${CHAT_COPY.runnerError.replace("{code}", () => front.runner.last_error)}`;
  return `${agent ? `<span>${marked(agent.name, e)} · ${marked(agent.visible ? agent.status : CHAT_COPY.statusPrivate, e)}</span>${agent.visible && agent.console === "#/console" ? ' <a href="#/console">Console</a>' : ""}` : ""}
    ${front ? `<span>${e(mode)}</span>` : ""}${status.needs_you > 0 ? `<a href="#/decide">${e(CHAT_COPY.decisions)} (${e(status.needs_you)})</a>` : ""}`;
}

const SLOW_MS = 15000;
const part = (value) => encodeURIComponent(value).replace(/\./g, "%2E");
const unsentKey = (user, slug) => `cmmn.unsent.${part(user)}.${part(slug)}`;
function storage() { try { return globalThis.sessionStorage; } catch { return null; } }
function forgetStored(name, text) {
  try { if (storage()?.getItem(name) === text) storage()?.removeItem(name); } catch { /* No persisted draft. */ }
}

// Sends outlive the pane: one item per user and board, kept from the offline queue through the
// request until the server acknowledges it, the outcome stays ambiguous (explicit retry), or the
// person signs out. A pane that opens meanwhile shows the item as queued or as sending.
const OFFLINE = new Map(), LIVE = new Set();
let held = null;
function deliverAll() {
  for (const item of [...OFFLINE.values()]) {
    if (item.sending) continue;
    // A pane showing the item sends it with its own bubble; otherwise it is sent from here.
    if ([...LIVE].some((pane) => pane.claim(item))) continue;
    item.sending = true;
    void deliver(item);
  }
}
// One request and, on failure, one read for the person's own echo. `api` is the sending pane's
// while that pane is current; afterwards the plain request continues on its own.
async function deliver(item, api = request, current = () => false) {
  let sent = false, page = null;
  try {
    await (current() ? api : request)(`/api/boards/${encodeURIComponent(item.slug)}/chat`, { method: "POST", body: { text: item.text } });
    sent = true;
  } catch {
    // Signed out or expired: no follow-up request, nothing kept.
    if (item.cancelled) return { sent, page, cancelled: true };
    try {
      page = await (current() ? api : request)(`/api/chat/${encodeURIComponent(item.slug)}?after=${item.cursor}`);
      sent = (page.messages || []).some((m) => m.actor === `user:${item.user}` && m.text === item.text);
    } catch { /* Ambiguous: kept for an explicit retry. */ }
  }
  if (item.cancelled) return { sent, page, cancelled: true };
  if (sent) forgetStored(item.name, item.text);
  if (OFFLINE.get(item.name) === item) OFFLINE.delete(item.name);
  for (const pane of [...LIVE]) pane.settled(item, sent);
  release();
  return { sent, page };
}
function dropOffline(prefix = "") {
  for (const item of [...OFFLINE.values()]) {
    if (!item.name.startsWith(prefix)) continue;
    item.cancelled = true;
    OFFLINE.delete(item.name);
    forgetStored(item.name, item.text);
  }
  release();
}
const heldOnline = () => deliverAll();
function heldSignOut() { for (const pane of [...LIVE]) pane.signOut(); dropOffline(); }
function heldLogout(event) { if (event.target.closest?.('[data-action="logout"]')) heldSignOut(); }
function track(item) {
  // Never replace another message for the same user and board.
  const existing = OFFLINE.get(item.name);
  if (existing && existing !== item && (existing.sending || existing.text !== item.text)) return existing;
  OFFLINE.set(item.name, item);
  if (held) return item;
  held = { win: globalThis, doc: globalThis.document };
  held.win.addEventListener?.("online", heldOnline);
  held.doc?.addEventListener?.("session-expired", heldSignOut);
  held.doc?.addEventListener?.("signout", heldSignOut);
  held.doc?.addEventListener?.("click", heldLogout, true);
  return item;
}
function release() {
  if (!held || OFFLINE.size) return;
  held.win.removeEventListener?.("online", heldOnline);
  held.doc?.removeEventListener?.("session-expired", heldSignOut);
  held.doc?.removeEventListener?.("signout", heldSignOut);
  held.doc?.removeEventListener?.("click", heldLogout, true);
  held = null;
}

export function createChat({ mount, api, e, me, board, coordinator, onCard = () => {}, onState = () => {} }) {
  // open: the dock shows the pane. Only an open, visible pane polls every 2 s and marks chat seen.
  let active = false, open = true, generation = 0, timer = null, delay = 2000, state;
  let lastProject = null;
  const written = new WeakMap(), bound = new WeakSet();
  // The board card a focused or hovered chat row points at carries is-linked (the dashed outline).
  const linked = { focus: null, hover: null };
  const drafts = new Map(), pending = new Map(), seen = new Map(), users = new Set(me?.user ? [me.user] : []);
  const root = () => typeof mount === "function" ? mount() : mount;
  const current = (token) => active && token === generation;
  const path = () => `/api/chat/${encodeURIComponent(state.slug)}`;
  const field = () => root()?.querySelector(".chat-compose")?.elements.text;
  function cancel() { if (timer !== null) clearTimeout(timer); timer = null; }
  function key() { return state.me?.user ? unsentKey(state.me.user, state.slug) : null; }
  function seenFor() {
    const name = key();
    if (!name) return null;
    if (!seen.has(name)) seen.set(name, seenStore(state.me.user, state.slug));
    return seen.get(name);
  }
  function remember(text) {
    const name = key();
    if (!name) return;
    pending.set(name, text);
    try { storage()?.setItem(name, text); } catch { /* Keep the in-memory bubble. */ }
  }
  function restore() {
    const name = key();
    if (!name) return;
    // The shared item wins: it outlives this pane even when storage is denied.
    const shared = OFFLINE.get(name);
    let text = shared?.text ?? pending.get(name);
    try { text ??= storage()?.getItem(name); } catch { /* Storage may be denied. */ }
    if (text != null) {
      // A send still queued or on its way from before this pane existed keeps going by itself.
      const queued = shared?.text === text ? shared : null;
      pending.set(name, text);
      state.unsent = queued?.sending ? { text, draft: queued.draft, origin: queued.origin, pending: true }
        : queued ? { text, draft: queued.draft, origin: queued.origin, offline: true, auto: true } : { text, draft: text, offline: false, auto: false };
      if (field()) field().value = text;
    }
  }
  function forget(name, text) {
    if (!name) return;
    if (pending.get(name) === text) pending.delete(name);
    forgetStored(name, text);
  }
  function clearUnsent() {
    for (const user of users) {
      const prefix = `cmmn.unsent.${part(user)}.`;
      try {
        const store = storage(), keys = Array.from({ length: store?.length || 0 }, (_, i) => store.key(i));
        for (const name of keys) if (name?.startsWith(prefix)) store.removeItem(name);
      } catch { /* Clear memory even if storage is denied. */ }
      clearSeen(user);
    }
    for (const store of seen.values()) store.clearSeen();
    seen.clear(); pending.clear(); drafts.clear();
    for (const user of users) dropOffline(`cmmn.unsent.${part(user)}.`);
    if (state) { state.unsent = null; state.quote = null; }
    if (field()) field().value = "";
  }
  function detach() {
    document.removeEventListener("session-expired", signOut);
    document.removeEventListener("signout", signOut);
    document.removeEventListener("click", logout, true);
    globalThis.removeEventListener?.("online", online);
    LIVE.delete(pane);
  }
  function signOut() { clearUnsent(); controller.stop(); detach(); }
  function logout(event) { if (event.target.closest?.('[data-action="logout"]')) signOut(); }
  document.addEventListener("session-expired", signOut);
  document.addEventListener("signout", signOut);
  document.addEventListener("click", logout, true);
  globalThis.addEventListener?.("online", online);
  function project() {
    const found = { ...state.projects.find((p) => p.slug === state.slug), ...(typeof board === "object" && board.slug === state.slug ? board : {}) };
    return { ...found, name: found.name || state.name, slug: state.slug, coordinator: coordinator || board?.coordinator || state.page.orchestrator?.actor || (state.page.orchestrator?.name ? `conn:${state.page.orchestrator.name}` : null) };
  }
  let lit = false;
  function outline() {
    const want = new Set([linked.focus, linked.hover].filter((n) => validId(n)).map(String));
    if (!want.size && !lit) return;
    lit = want.size > 0;
    const cards = (selector) => document.querySelectorAll?.(selector) || [];
    for (const card of cards(".workbench-board .card.is-linked")) if (!want.has(card.dataset.card)) card.classList.remove("is-linked");
    for (const number of want) for (const card of cards(`.workbench-board .card[data-card="${number}"]`)) card.classList.add("is-linked");
  }
  function unlink() { linked.focus = linked.hover = null; outline(); }
  const rowOf = (el) => { const row = el?.closest?.("[data-chat-card]"); return row && root()?.contains(row) ? Number(row.dataset.chatCard) : null; };
  function bindOutline(host) {
    if (bound.has(host) || typeof host.addEventListener !== "function") return;
    bound.add(host);
    host.addEventListener("focusin", (event) => { linked.focus = rowOf(event.target); outline(); });
    host.addEventListener("focusout", (event) => { linked.focus = rowOf(event.relatedTarget); outline(); });
    host.addEventListener("mouseover", (event) => { linked.hover = rowOf(event.target); outline(); });
    host.addEventListener("mouseout", (event) => { linked.hover = rowOf(event.relatedTarget); outline(); });
  }
  // The one-line status the dock shows under its heading (M5); the pane itself has no header row.
  let headerSink = null, headerShown = null;
  function headerLine() {
    let text = "";
    if (state?.slug && state.loaded) {
      const request = state.page.messages.find((m) => m.id === state.derived?.message_id);
      const hasOrchestrator = !!(state.status?.orchestrator || project().coordinator);
      if (state.derived?.state === "no_answer") text = `Connected, not answering since ${clock(request?.at) || "earlier"}`;
      else if (state.derived?.state === "waiting" || !hasOrchestrator) text = "No orchestrator yet";
      else text = "Orchestrator connected";
    }
    if (text !== headerShown) { headerShown = text; headerSink?.(text); }
  }
  function render() {
    const host = root();
    if (!host) return;
    const old = host.querySelector(".chat-stream"), top = old?.scrollTop || 0;
    const bottom = !old || old.scrollHeight - top - old.clientHeight < 60;
    const page = { ...state.page, project: project(), cards: state.cards, at: state.at, seen: state.baseline, todayIds: state.todayIds };
    if (!host.querySelector(".chat-view")) {
      host.innerHTML = `<aside class="chat-view chatpane" aria-labelledby="chat-h"><header class="chat-header"><h2 id="chat-h">${e(project().name || state.slug || "Project")} chat</h2><button class="btn ghost sm" type="button" data-close-chat>Close chat</button><label>Project<select data-chat-project></select></label><a data-chat-board>Board</a><div class="chat-status" data-chat-status></div></header><div data-chat-source></div><p data-chat-error role="alert" hidden></p><div class="chat-history"><button type="button" data-chat-earlier>Show earlier</button></div><ol class="chat-stream" role="log" aria-label="Messages" tabindex="0">${chatHTML(page, state.me, e)}</ol><div data-chat-recovery></div><div data-chat-unsent></div><form class="chat-compose"><div data-chat-quote></div><label class="sr" for="chat-text">Message to ${e(project().name || state.slug || "project")} chat</label><div class="chat-box"><textarea id="chat-text" name="text" rows="2" placeholder="${e(CHAT_COPY.placeholder)}" required></textarea><button class="btn primary sm" type="submit" aria-label="Send" aria-describedby="chat-wait"><svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 8 13.5 3l-3 10.5-2.3-4.3z"/><path d="M8.2 9.2 13.5 3"/></svg></button></div><p class="chat-wait" id="chat-wait" data-chat-wait role="status" hidden>${e(CHAT_COPY.waiting)}</p><p class="chat-hint chat-keys"><span>Enter to send · Shift+Enter new line</span><span><kbd>Esc</kbd> closes the chat</span></p></form></aside>`;
      const form = host.querySelector(".chat-compose");
      if (form) {
        form.onsubmit = send;
        form.elements.text.value = drafts.get(state.slug) || "";
        form.elements.text.oninput = () => drafts.set(state.slug, form.elements.text.value);
        form.elements.text.onkeydown = (event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send({ preventDefault() {}, currentTarget: form }); }
        };
      }
      host.onclick = click;
      bindOutline(host);
    }
    // Rewrite a region only when its markup changed, so an unchanged poll keeps focus and selection.
    const write = (el, html) => {
      if (!el || written.get(el) === html) return false;
      el.innerHTML = html; written.set(el, html); return true;
    };
    const put = (selector, html) => write(host.querySelector(selector), html);
    const view = host.querySelector(".chat-view");
    if (view) view.dataset.board = state.slug || "";
    // The project name can arrive after the first render (the list or the dock's board).
    const name = project().name || state.slug;
    const heading = host.querySelector(".chat-header h2"), label = host.querySelector('.chat-compose label[for="chat-text"]');
    if (heading) heading.textContent = `${name || "Project"} chat`;
    if (label) label.textContent = `Message to ${name || "project"} chat`;
    host.querySelector(".chat-stream")?.setAttribute?.("aria-label", name ? `Messages in ${name} chat` : "Messages");
    const picker = host.querySelector("[data-chat-project]");
    if (picker) {
      write(picker, '<option value="">Choose a project</option>' + state.projects.map((p) => `<option value="${e(p.slug)}" ${p.slug === state.slug ? "selected" : ""}>${marked(p.name || p.slug, e)}</option>`).join(""));
      picker.onchange = () => { location.hash = `#/board/${encodeURIComponent(picker.value)}/chat`; };
    }
    const link = host.querySelector("[data-chat-board]");
    if (link) { link.hidden = !state.slug; link.href = `#/board/${encodeURIComponent(state.slug)}`; }
    put("[data-chat-status]", state.status ? statusHTML(state.status, e) : "");
    const error = host.querySelector("[data-chat-error]");
    if (error) { error.hidden = !state.error; error.textContent = state.error; }
    const stream = host.querySelector(".chat-stream");
    if (write(stream, state.slug ? chatHTML(page, state.me, e) : `<li>${e(CHAT_COPY.noProjects)}</li>`)) stream.scrollTop = bottom ? stream.scrollHeight : top;
    const earlier = host.querySelector("[data-chat-earlier]");
    if (earlier) earlier.hidden = !state.page.has_more;
    put("[data-chat-source]", state.at ? '<p class="chat-source">Opened from memory. Your question takes the quote along, word for word. <button type="button" data-close-chat>Back to the source</button></p>' : "");
    put("[data-chat-quote]", state.quote ? `<div class="chat-quote">Quoting ${e(state.quote.author)}, ${e(state.quote.date ?? state.quote.at)}, exact words <button type="button" data-chat-remove-quote aria-label="Remove the quote">×</button></div>` : "");
    const unsent = state.unsent, inFlight = unsent?.pending;
    put("[data-chat-unsent]", unsent ? `<div class="chat-unsent" role="${inFlight ? "status" : "alert"}"><p>${marked(unsent.text, e)}</p><p>${e(inFlight ? CHAT_COPY.sending : unsent.offline ? CHAT_COPY.offline : CHAT_COPY.retry)}</p>${inFlight ? "" : `<div><button type="button" data-chat-retry ${state.sending ? "disabled" : ""}>Try again</button> <button type="button" data-chat-discard ${state.sending ? "disabled" : ""}>${e(CHAT_COPY.discard)}</button></div>`}</div>` : "");
    const request = state.page.messages.find((m) => m.id === state.derived?.message_id);
    const recovery = state.derived?.state;
    const lastActive = clock(state.derived?.last_seen) || state.derived?.last_seen;
    const recoveryText = recovery === "no_answer"
      ? `It has not answered your message from ${clock(request?.at) || "earlier"}.${lastActive ? ` Its agent was last active at ${lastActive}.` : ""} ${CHAT_COPY.kept}`
      : lastActive ? `Agent last active at ${lastActive}.` : "";
    const acts = recovery === "no_answer"
      ? `<a class="btn sm primary" href="#/board/${encodeURIComponent(state.slug)}/settings">Show how to restart the agent</a><button class="btn sm" type="button" data-chat-ask-again>Ask again</button>`
      : '<p>Connect an agent in Settings. Your message is delivered when it connects.</p>';
    put("[data-chat-recovery]", request && ["waiting", "working", "no_answer"].includes(recovery) ? `<div class="chat-recovery" role="${recovery === "no_answer" ? "alert" : "status"}"><b>${recovery === "waiting" ? "Waiting for an agent" : recovery === "working" ? "Working on it…" : "No answer yet."}</b>${recoveryText ? `<p>${e(recoveryText)}</p>` : ""}${recovery !== "working" ? `<div class="chat-acts">${acts}<button class="btn sm" type="button" data-chat-make-card ${state.making ? "disabled" : ""}>Make it a card now (adds it to Todo)</button></div>` : ""}</div>` : "");
    headerLine();
    const form = host.querySelector(".chat-compose");
    // While an earlier message is unsent, the composer keeps its text but does not send.
    const waiting = !!unsent && !state.sending;
    if (form) { form.hidden = !state.slug; form.querySelector("button[type=submit]").disabled = !!state.sending || !!unsent; }
    const wait = host.querySelector("[data-chat-wait]");
    if (wait) wait.hidden = !waiting;
  }
  function update(page, { history = false } = {}) {
    const previous = state.page;
    state.page = { ...previous, ...page, cursor: Math.max(previous.cursor || 0, page.cursor || 0), reply_cursor: Math.max(previous.reply_cursor || 0, page.reply_cursor || 0), messages: merge(previous.messages, page.messages), replies: merge(previous.replies, page.replies), linked: [...new Map([...(previous.linked || []), ...(page.linked || [])].map((c) => [c.number, c])).values()] };
    if (!history && previous.messages.length) { state.page.oldest = previous.oldest; state.page.has_more = previous.has_more; }
    state.me = page.me || state.me;
    if (state.me?.user) {
      users.add(state.me.user);
      const store = seenFor();
      // The divider keeps the boundary from when the pane opened; unread follows the store.
      state.baseline ??= store.read(state.page);
      state.derived = deriveChatState(state.page, state.status, project(), Date.now(), store.read(state.page));
      onState(state.derived);
      // A closed or hidden pane has not been read: a reply arriving meanwhile stays new.
      if (open && !document.hidden) store.markSeen(state.page);
    }
  }
  function schedule(token) {
    if (!current(token) || document.hidden || !open) return;
    cancel(); timer = setTimeout(() => { timer = null; void tick(token); }, delay);
  }
  // Reads everything after a cursor: a full page (100 rows) may have more behind it.
  async function readAfter(from, token) {
    const read = (at) => api(`${path()}?after=${at.cursor || 0}&replies_after=${at.reply_cursor || 0}`);
    let page = await read(from), last = page;
    while (current(token) && ((last.messages || []).length >= 100 || (last.replies || []).length >= 100)) {
      last = await read(page);
      if ((last.cursor || 0) <= (page.cursor || 0) && (last.reply_cursor || 0) <= (page.reply_cursor || 0)) break;
      page = { ...last, oldest: page.oldest, has_more: page.has_more, messages: merge(page.messages, last.messages), replies: merge(page.replies, last.replies) };
    }
    return page;
  }
  // A stored seen entry reads everything after it, page by page; only a first sight uses the last 20.
  async function firstRead(token) {
    const stored = seenFor()?.peek();
    if (!stored) return api(`${path()}?last=20`);
    const page = await readAfter({ cursor: stored.message, reply_cursor: stored.reply }, token);
    if (!current(token) || (page.messages || []).length >= 20) return page;
    // Few new rows: the last 20 give them context.
    const recent = await api(`${path()}?last=20`);
    return { ...page, ...recent, messages: merge(recent.messages, page.messages), replies: merge(recent.replies, page.replies),
      cursor: Math.max(page.cursor || 0, recent.cursor || 0), reply_cursor: Math.max(page.reply_cursor || 0, recent.reply_cursor || 0) };
  }
  async function tick(token) {
    if (!current(token) || document.hidden) return;
    try {
      if (!state.loaded) {
        const list = await api("/api/chat");
        if (!current(token)) return;
        state.projects = list.projects || [];
        state.slug ||= state.projects.find((p) => p.slug === lastProject)?.slug || list.default || (state.projects.length === 1 ? state.projects[0].slug : null);
        if (state.slug && !state.projects.some((p) => p.slug === state.slug)) throw new Error("Project unavailable");
      }
      if (state.slug) {
        const read = state.loaded ? readAfter({ cursor: state.page.cursor, reply_cursor: state.page.reply_cursor }, token) : firstRead(token);
        // New messages every tick; status, cards and settings change slowly.
        const slow = !state.loaded || Date.now() - (state.slowAt || 0) >= SLOW_MS;
        const [page, status, cards, settings] = await Promise.all([read, ...(slow ? [api(`${path()}/status`), api(`/api/boards/${encodeURIComponent(state.slug)}/cards`), api(`/api/chat-settings?board=${encodeURIComponent(state.slug)}`)] : [])]);
        if (!current(token)) return;
        if (slow) { state.status = { ...status, front: { ...status.front, ...settings.front } }; state.cards = [...(cards.cards || []), ...(cards.children || [])]; state.slowAt = Date.now(); }
        update(page);
        if (!state.loaded) restore();
        lastProject = state.slug;
      }
      state.loaded = true; state.error = ""; delay = 2000;
    } catch { if (!current(token)) return; state.error = CHAT_COPY.unavailable; delay = Math.min(delay * 2, 30000); }
    render(); schedule(token);
  }
  // origin: "composer" (the form), "ask" (Ask again) or "retry" (keeps the first send's origin).
  // Only composer sends may clear the composer or the quote.
  async function post(text, origin = "composer") {
    if (!active || state.sending || !state.slug || !text.trim()) return;
    const retry = origin === "retry", source = retry ? state.unsent?.origin || "composer" : origin;
    // One unsent message per board: a new send waits until the earlier one is sent or discarded,
    // and nothing re-sends a message that is already on its way.
    const shared = OFFLINE.get(key());
    if ((!retry && (state.unsent || shared)) || (retry && shared?.sending)) { render(); return; }
    const token = ++generation, sendingState = state, name = key(), sentDraft = field()?.value;
    const originalDraft = retry ? state.unsent?.draft : source === "composer" ? sentDraft : null;
    const bubble = { text, draft: originalDraft, origin: source, offline: false, auto: false };
    cancel(); remember(text);
    state.sending = true;
    if (globalThis.navigator?.onLine === false) {
      state.unsent = { ...bubble, offline: true, auto: true };
      if (name) track({ name, slug: state.slug, user: state.me?.user, cursor: state.page.cursor || 0, text, draft: originalDraft, origin: source, sending: false });
      state.sending = false; render(); return;
    }
    const item = { name, slug: state.slug, user: state.me?.user, cursor: state.page.cursor || 0, text, draft: originalDraft, origin: source, sending: true, owner: pane };
    // Defensive: the guard above already refuses a send while another one is tracked.
    if (name && track(item) !== item) { state.sending = false; render(); return; }
    state.unsent = { ...bubble, pending: true }; render();
    const { sent, page, cancelled } = await deliver(item, api, () => current(token));
    if (cancelled) return;
    if (sent) forget(name, text);
    if (!current(token)) return;
    if (page) update(page);
    sendingState.sending = false;
    if (sent) {
      state.unsent = null; state.slowAt = 0;
      if (source === "composer") {
        const untouched = field()?.value === sentDraft && (!retry || sentDraft === originalDraft);
        if (!retry || untouched) state.quote = null;
        if (untouched) { field().value = ""; drafts.delete(state.slug); }
      }
      await tick(token);
    } else { state.unsent = bubble; render(); schedule(token); }
  }
  async function send(event) {
    event.preventDefault();
    const question = event.currentTarget.elements.text.value;
    const quote = state.quote;
    const text = quote ? `${question}\n\n${quote.author}, ${quote.sourceLabel}, ${quote.date ?? quote.at}, exact words:\n“${quote.text}”` : question;
    if (question.trim()) await post(text);
  }
  async function history(at = null) {
    if (!active || !state.loaded || state.sending || state.historyLoading) return;
    const token = generation;
    state.historyLoading = true;
    try {
      if (at !== null) {
        const before = await api(`${path()}?before=${at + 1}&last=3`);
        if (!current(token)) return;
        const after = await api(`${path()}?after=${at}`);
        if (!current(token)) return;
        const today = await api(`${path()}?last=20`);
        if (!current(token)) return;
        state.at = at; state.todayIds = (today.messages || []).map((m) => m.id);
        update(before, { history: true }); update({ ...after, messages: (after.messages || []).slice(0, 2) }); update(today);
      } else {
        const page = await api(`${path()}?before=${state.page.oldest || state.page.messages[0]?.id || 0}&last=20`);
        if (!current(token)) return;
        update(page, { history: true });
      }
      state.error = ""; render();
      if (at !== null) root()?.querySelector(`#chat-message-${at}`)?.scrollIntoView?.({ block: "center" });
    } catch { if (current(token)) { state.error = CHAT_COPY.unavailable; render(); } }
    finally { if (current(token)) state.historyLoading = false; }
  }
  async function click(event) {
    const button = event.target.closest?.("[data-chat-card], [data-chat-suggest], [data-chat-remove-quote], [data-chat-retry], [data-chat-discard], [data-chat-make-card], [data-chat-ask-again], [data-chat-earlier]");
    if (!button) return;
    if (button.dataset.chatCard) { event.preventDefault(); onCard(Number(button.dataset.chatCard), button); }
    else if (button.dataset.chatSuggest) { if (field()) field().value = button.dataset.chatSuggest; controller.focusComposer(); }
    else if (button.hasAttribute("data-chat-remove-quote")) { controller.setQuote(null); controller.focusComposer(); }
    else if (button.hasAttribute("data-chat-retry")) await post(state.unsent?.text || "", "retry");
    else if (button.hasAttribute("data-chat-discard")) {
      // Discarding is explicit: the earlier words leave the queue and this device.
      const name = key();
      if (state.sending || !state.unsent || state.unsent.pending || OFFLINE.get(name)?.sending) return;
      if (OFFLINE.get(name)?.text === state.unsent.text) OFFLINE.delete(name);
      forget(name, state.unsent.text); state.unsent = null; release(); render(); controller.focusComposer();
    }
    else if (button.hasAttribute("data-chat-earlier")) await history();
    else {
      const request = state.page.messages.find((m) => m.id === state.derived?.message_id);
      if (!request) return;
      if (button.hasAttribute("data-chat-ask-again")) await post(request.text, "ask");
      else if (!state.making) {
        const token = generation; state.making = true; render();
        try {
          const card = await api(`/api/boards/${encodeURIComponent(state.slug)}/cards`, { method: "POST", body: { title: request.text, message_id: request.id } });
          if (!current(token)) return;
          update({ linked: [{ ...card, message_id: request.id }] });
        } catch { if (current(token)) state.error = "Could not make a card. Try again."; }
        finally { if (current(token)) { state.making = false; render(); } }
      }
    }
  }
  function visibility() { cancel(); if (active && open && !state.sending && !document.hidden) void tick(++generation); }
  // This pane's view of the shared offline queue.
  const pane = {
    claim(item) {
      if (!active || key() !== item.name || !state.unsent?.auto) return false;
      state.unsent.auto = false; void post(item.text, "retry"); return true;
    },
    settled(item, sent) {
      // The sending pane finishes its own bubble.
      if (item.owner === pane) return;
      if (sent && pending.get(item.name) === item.text) pending.delete(item.name);
      if (!active || key() !== item.name || state.unsent?.text !== item.text) return;
      state.unsent = sent ? null : { text: item.text, draft: item.draft, origin: item.origin, offline: false, auto: false };
      if (sent && item.origin !== "ask") {
        state.quote = null;
        if (field()?.value === item.draft || field()?.value === item.text) { field().value = ""; drafts.delete(item.slug); }
      }
      render();
    },
    signOut,
  };
  LIVE.add(pane);
  function online() { deliverAll(); }
  const controller = {
    async show(slug, { at, quote, name } = {}) {
      if (active && state.requested === slug) {
        if (name) { state.name = name; render(); }
        if (quote !== undefined) this.setQuote(quote);
        if (validId(at)) await history(at);
        this.focusComposer(); return;
      }
      this.stop(); active = true;
      state = { requested: slug, slug, name: name || "", projects: [], cards: [], me, loaded: false, page: { messages: [], replies: [], cursor: 0, reply_cursor: 0 }, status: null, quote: quote || null, error: CHAT_COPY.loading };
      if (root()) root().innerHTML = "";
      document.addEventListener("visibilitychange", visibility);
      render(); await tick(++generation);
      if (active && state.requested === slug) { if (validId(at)) await history(at); this.focusComposer(); }
    },
    stop() { active = false; generation++; cancel(); unlink(); document.removeEventListener("visibilitychange", visibility); headerShown = ""; headerSink?.(""); },
    /** The dock's status line under its heading; called with the text whenever it changes. */
    watchHeader(fn) { headerSink = typeof fn === "function" ? fn : null; headerSink?.(headerShown || ""); },
    focusComposer() { field()?.focus(); },
    /** The dock opened or closed the pane. Closed: no 2 s poll and no seen marks; reopened: read now. */
    setOpen(value) {
      const was = open;
      open = !!value;
      if (!open) { cancel(); unlink(); }
      else if (!was && active && !state.sending && !document.hidden) { cancel(); void tick(++generation); }
    },
    /** The dock reopened the pane on the same board: move the divider to what was read before. */
    reopened() {
      const store = state?.me?.user && seen.get(key());
      // Before the first page there is nothing to baseline: first sight sets it from that page.
      if (!active || !state.loaded || !store) return;
      state.baseline = store.read(state.page); render();
    },
    /** Stop and remove the document and window listeners this controller added. */
    destroy() { controller.stop(); detach(); },
    setQuote(quote) { if (state) { state.quote = quote ? { ...quote } : null; render(); } },
    clearUnsent,
  };
  return controller;
}
