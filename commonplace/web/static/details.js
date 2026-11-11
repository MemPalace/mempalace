import { api as request, escape as e, actor } from "./api.js";
import { loadCardLinks, placeCardLinks } from "./cardlinks.js";
import { attachCardRuns, cardRunsSlot } from "./cardruns.js";
import { blockersSection } from "./blockers.js";
import { waitingSection } from "./waiting.js";

const statuses = ["backlog", "todo", "doing", "review", "done"];
const priorities = ["urgent", "high", "normal", "low"];
const enc = encodeURIComponent;
const exact = (value) => e(value).replaceAll("\r", "&#13;");
const label = (value) => value ? value[0].toUpperCase() + value.slice(1) : "—";

export function detailsAuthor(ref, board = {}) {
  if (!ref) return "";
  if (ref === board.coordinator) return `${board.name || board.slug} orchestrator`;
  return String(ref).startsWith("conn:") ? `${actor(ref)} (agent)` : actor(ref);
}

// Events arrive in append order. A later comment must never reset the ask clock.
export function detailsContext(card, me) {
  const events = card.events || [];
  const opening = events.filter((event) => Object.hasOwn(event, "options")).at(-1);
  const proposal = card.proposal_status === "pending";
  const event = proposal ? events.find((item) => item.kind === "add") : opening;
  const asking = card.status === "review" && Boolean(card.ask);
  const completion = card.review_kind === "completion";
  return {
    blocked: asking && !completion && card.ask_to !== me,
    needsMe: asking && !completion && card.ask_to === me,
    since: asking && !completion ? opening?.at : null,
    text: proposal ? card.body ?? "" : asking ? opening?.context ?? "" : null,
    author: event?.actor || null,
    proposal,
  };
}

export function originMessage(card, messages = []) {
  if (card.message_id == null) return null;
  return messages.find((message) => String(message.id) === String(card.message_id) &&
    (message.board == null || message.board === card.board)) || null;
}

export async function loadDetailsOrigin(card, api = request) {
  if (card.message_id == null) return null;
  const page = await api(`/api/chat/${enc(card.board)}?before=${Number(card.message_id) + 1}&last=1`);
  return page.board === card.board ? originMessage(card, page.messages) : null;
}

// Component styles stay here until the shell imports this unit; no legacy sheet changes.
const styles = `
.workbench-details { display:flex; flex-direction:column; min-height:0; height:100%; background:#FEFEFC; overflow:hidden; }
.workbench-details .ctx-head { display:flex; align-items:center; padding:8px 10px 0; border-bottom:1px solid var(--line); }
.workbench-details .ctx-tabs { display:flex; flex:1; gap:2px; }
.workbench-details [role=tab] { min-height:36px; padding:0 10px; border:0; border-bottom:2px solid transparent; background:none; color:var(--text-2); }
.workbench-details [aria-selected=true] { color:var(--text); border-bottom-color:var(--accent); font-weight:650; }
.workbench-details .ctx-panel-wrap { display:flex; flex-direction:column; flex:1; min-height:0; }
.workbench-details .ctx-body { flex:1; min-height:0; overflow:auto; padding:14px 16px 16px; display:flex; flex-direction:column; gap:14px; }
.workbench-details .title-row, .workbench-details .row, .workbench-details .acts, .workbench-details .reply { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.workbench-details h2 { font-size:17px; line-height:23px; flex:1; }
.workbench-details .fields { display:grid; grid-template-columns:100px minmax(0,1fr); gap:6px 8px; font-size:13px; }
.workbench-details dt { color:var(--text-3); } .workbench-details dd { margin:0; }
.workbench-details .callout { border-radius:var(--r); padding:10px 12px; display:flex; flex-direction:column; gap:8px; font-size:13px; }
.workbench-details .block { background:var(--block-bg); box-shadow:inset 3px 0 0 var(--block); }
.workbench-details .wait { background:var(--wait-bg); box-shadow:inset 3px 0 0 var(--wait-line); }
.workbench-details .info { background:var(--accent-weak); box-shadow:inset 3px 0 0 var(--accent); }
.workbench-details .quote, .workbench-details .body-text, .workbench-details .event-text { white-space:pre-wrap; overflow-wrap:anywhere; }
.workbench-details .quote { border-left:3px solid var(--line-strong); padding:2px 0 2px 12px; line-height:21px; }
.workbench-details .activity { list-style:none; padding:0; display:flex; flex-direction:column; gap:12px; }
.workbench-details .activity .act-at { font-size:12px; color:var(--text-3); }
.workbench-details .act-opts { list-style:none; padding:0; margin:4px 0; display:flex; flex-wrap:wrap; gap:4px; }
.workbench-details .act-opts li { font-size:12.5px; padding:1px 8px; border:1px solid var(--line-strong); border-radius:12px; }
.workbench-details .act-choice { margin:2px 0; }
.workbench-details .activity .tag { padding:0 6px; border-radius:4px; font-size:11.5px; font-weight:600; }
.workbench-details .sec-h { display:flex; align-items:center; gap:8px; }
.workbench-details .sec-h h3 { flex:1; font-size:12px; line-height:16px; font-weight:650; letter-spacing:.06em; text-transform:uppercase; color:var(--text-2); }
.workbench-details .ctx-foot { border-top:1px solid var(--line); padding:10px 12px 12px; }
.workbench-details textarea { flex:1; min-width:0; } .workbench-details .chat-about { padding:10px 12px; background:var(--accent-weak); }
@media(max-width:699px) { .workbench-details button, .workbench-details select, .workbench-details .btn { min-height:44px; } }
`;
// The app's CSP is style-src 'self', which refuses inline style elements: adopt a constructed sheet once.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

// Activity reads as a sentence ("Maya created the card"), never as the raw event kind.
const EVENT_WORDS = { add: "created the card", claim: "started working on it", release: "stopped working on it", move: "moved the card", status: "changed the status", comment: "commented", ask: "asked", decide: "answered", handoff: "handed it off", approve: "approved it", reject: "declined it", edit: "edited the card", done: "marked it done", pr: "linked a pull request", link: "linked a memory", block: "marked this blocked" };
// Activity keeps the decision history for every viewer: what was asked (target and the choices
// offered, the open one marked), which choice answered it, replies and reasons, all verbatim.
function activityHTML(card, author, stamp) {
  const events = card.events || [];
  const lastAsk = events.findLastIndex((ev) => ev.options);
  const choices = (options, open) => options?.length ? `<ul class="act-opts" aria-label="${open ? "Choices, waiting for an answer" : "Choices offered"}">${options.map((option) => `<li>${e(option)}</li>`).join("")}</ul>` : "";
  const items = events.map((event, i) => {
    const open = card.ask != null && i === lastAsk;
    const target = event.target && (event.options || event.kind === "handoff") ? ` → ${e(author(event.target))}` : "";
    return `<li${open ? ' class="is-open"' : ""}><p><b>${e(author(event.actor))}</b> ${e(EVENT_WORDS[event.kind] || event.kind)}${target}${open ? ' <span class="tag wait">Open</span>' : ""}</p>${choices(event.options, open)}${event.choice ? `<p class="act-choice">Chose <b>${e(event.choice)}</b></p>` : ""}${event.text ? `<p class="event-text">${exact(event.text)}</p>` : ""}${event.reason != null ? `<p class="event-text">Reason: ${exact(event.reason)}</p>` : ""}<p class="act-at">${stamp(event.at)}</p></li>`;
  });
  // An open ask that no event recorded yet still shows its question and choices.
  if (card.ask != null && lastAsk < 0) items.push(`<li class="is-open"><p><b>${e(author(card.return_to))}</b> asked${card.ask_to ? ` → ${e(author(card.ask_to))}` : ""} <span class="tag wait">Open</span></p><p class="event-text">${exact(card.ask)}</p>${choices(card.ask_options, true)}<p class="act-at">${stamp(card.updated_at)}</p></li>`);
  return items.join("");
}
export function renderDetails(card, { board = { slug: card.board }, me, messages = [], origin = originMessage(card, messages), strip = null, tab = "details" } = {}) {
  const context = detailsContext(card, me);
  const name = board.name || board.slug || card.board;
  const prefix = [board.name, board.slug, card.board].map((part) => String(part || "").normalize("NFKD").toUpperCase().match(/[A-Z]/)?.[0]).find(Boolean) || "C";
  const id = `${prefix}-${card.number}`;
  const author = (ref) => detailsAuthor(ref, board);
  const stamp = (at) => {
    if (!at) return "";
    const date = new Date(at);
    const time = Number.isNaN(date.getTime()) ? at : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return `<time datetime="${e(at)}" title="${e(at)}">${e(time)}</time>`;
  };
  const quote = (text, attr) => `<p class="quote" ${attr}>${exact(text)}</p>`;
  const contextBox = context.text == null ? "" : `<section class="callout ${context.blocked ? "block" : "wait"}" role="note">${context.proposal ? `<h3>${e(card.title)}</h3>` : context.since ? `<h3>${context.blocked ? "Blocked" : "Needs me"} since ${stamp(context.since)}</h3>` : ""}<h3>Context (exact words)</h3>${quote(context.text, "data-context-text")}${context.author ? `<p>— ${e(author(context.author))}</p>` : ""}${card.ask ? `<p class="event-text">${e(card.ask)}</p><a class="btn sm" href="#/memory?q=${enc(card.ask)}">Search memory for the answer</a>` : ""}</section>`;
  const originBox = origin ? `<section class="callout info" role="note"><h3>${e(author(origin.actor))}'s request, quoted exactly</h3>${quote(origin.text, "data-origin-text")}<p>${e(name)} chat · ${stamp(origin.at)}</p></section>` : "";
  const stripBox = strip ? `<div class="chat-about from-chat" data-row="${e(strip.row || "")}"><span class="fc-text">${strip.kind === "board" ? `You picked ${e(id)} on the board. The chat is one click away.` : `Opened from ${e(name)} chat: ${e(strip.source || "")}.`}</span><button class="btn sm" data-back-chat data-action="back-to-chat">Back to chat</button></div>` : "";
  const tag = context.blocked ? "Blocked" : context.needsMe ? "Needs me" : card.review_kind === "completion" ? `Reviewer: ${author(card.ask_to)}` : "";
  const linked = (card.linked || []).map((link) => `<li><a href="#/memory?id=${enc(link.drawer_id)}"${link.invalidated ? ' class="gone"' : ""}>${e(link.title || "Memory")}</a></li>`).join("");
  return `<section class="workbench-details" aria-label="Card details" data-card="${e(card.number)}" data-version="${e(card.version)}"><div class="ctx-head"><div class="ctx-tabs" role="tablist" aria-label="Context">${["details", "memory"].map((key) => `<button role="tab" id="tab-${key}" aria-selected="${tab === key}" aria-controls="ctx-panel" tabindex="${tab === key ? 0 : -1}" data-details-tab="${key}">${label(key)}</button>`).join("")}</div><button class="btn ghost sm hide" data-action="close-panel" aria-keyshortcuts="]">Hide</button></div>${stripBox}<div class="ctx-panel-wrap" id="ctx-panel" role="tabpanel" aria-labelledby="tab-${tab}">${tab === "memory" ? `<div class="ctx-body"><h2>Memory for ${e(id)}</h2><ul>${linked || "<li>No linked memories</li>"}</ul><a class="btn" href="#/memory?q=${enc(card.ask || card.title)}">Search memory</a></div>` : `<div class="ctx-body"><header class="detail-h"><div class="row"><span class="cid">${e(id)}</span>${tag ? `<span class="tag ${context.blocked ? "block" : "wait"}">${e(tag)}</span>` : ""}</div><div class="title-row"><h2 tabindex="-1" data-details-title>${e(card.title)}</h2><button class="btn ghost sm" data-action="edit-card">Edit <kbd>E</kbd></button><button class="btn ghost sm" data-action="handoff">Hand off</button></div></header><dl class="fields"><dt>Status</dt><dd><select id="card-status" aria-label="Card status"${context.proposal ? " disabled" : ""}>${statuses.map((status) => `<option value="${status}"${status === card.status ? " selected" : ""}>${label(status)}</option>`).join("")}</select><button class="btn sm" data-action="panel-move"${context.proposal ? " disabled" : ""}>Move</button></dd><dt>Priority</dt><dd>${card.can_prioritize ? `<select id="card-priority" aria-label="Card priority">${priorities.map((item) => `<option value="${item}"${item === (card.priority || "normal") ? " selected" : ""}>${label(item)}</option>`).join("")}</select><button class="btn sm" data-action="panel-priority">Set</button>` : e(label(card.priority || "normal"))}</dd><dt>${card.need === "working" ? "Working on it" : "Who"}</dt><dd>${e(card.assignee ? author(card.assignee) : card.status === "todo" ? "Available for an agent" : "—")}</dd>${card.ask_to ? `<dt>${card.review_kind === "completion" ? "Reviewer" : "Waiting on"}</dt><dd>${e(author(card.ask_to))}</dd>` : ""}${card.return_to?.startsWith("conn:") && card.status === "review" ? `<dt>Built by</dt><dd>${e(author(card.return_to))}</dd>` : ""}${card.labels?.length ? `<dt>Labels</dt><dd>${card.labels.map((item) => e(item)).join(" · ")}</dd>` : ""}</dl>${waitingSection(card)}${blockersSection(card)}${cardRunsSlot(card)}${contextBox}${originBox}<section data-details-description><div class="sec-h"><h3>Description</h3><button class="btn ghost sm" data-action="edit-card" aria-label="Edit ${e(id)}">Edit</button></div><p class="body-text">${exact(card.body)}</p></section><section><div class="sec-h"><h3>Activity</h3></div><ol class="activity">${activityHTML(card, author, stamp)}</ol></section></div><div class="ctx-foot"><form class="reply" data-form="comment"><label class="sr" for="details-comment">Comment on ${e(id)}</label><textarea id="details-comment" name="text" rows="1" data-draft="comment-${e(card.number)}" placeholder="Comment or @mention…" required></textarea><button class="btn">Send</button></form></div>`}</div></section>`;
}

// The shell keeps ownership of card mutations, drafts, sheet modality and chat focus return.
export function createDetails({ mount, api = request, onMemory = () => {} }) {
  let current, options, revision = 0, links = null;
  const draw = () => {
    mount.innerHTML = renderDetails(current, options);
    if (options.tab === "details") placeCardLinks(mount, links);
    void attachCardRuns(mount, current, { api });
  };
  function selectTab(tab, focus = false) {
    if (!current || !["details", "memory"].includes(tab)) return;
    // Keep an unfinished comment when switching away and back.
    const draft = mount.querySelector("[name=text]")?.value;
    if (draft != null) options.draft = draft;
    options.tab = tab;
    draw();
    const input = mount.querySelector("[name=text]");
    if (input) input.value = options.draft || "";
    if (focus) mount.querySelector(`[data-details-tab="${tab}"]`)?.focus();
    if (tab === "memory") onMemory(current);
  }
  const click = (event) => {
    const tab = event.target.closest("[data-details-tab]");
    if (tab) selectTab(tab.dataset.detailsTab, true);
  };
  const keydown = (event) => {
    if (!event.target.closest("[data-details-tab]")) return;
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      selectTab(event.key === "Home" ? "details" : event.key === "End" ? "memory" : options.tab === "memory" ? "details" : "memory", true);
    }
  };
  mount.addEventListener("click", click);
  mount.addEventListener("keydown", keydown);
  return {
    async show(card, settings = {}) {
      const token = ++revision;
      // A refresh of the same card keeps the user's focus; only a new card moves it to the heading.
      const changed = current?.number !== card.number || current?.board !== card.board;
      if (changed) links = null;
      current = card;
      options = { ...settings, tab: "details" };
      draw();
      loadCardLinks(card, api).then((found) => {
        if (token !== revision) return;
        links = found;
        if (options.tab === "details") placeCardLinks(mount, links);
      }).catch(() => {});
      if (changed) mount.querySelector("[data-details-title]")?.focus();
      if (card.message_id != null && !originMessage(card, settings.messages)) {
        try {
          const origin = await loadDetailsOrigin(card, api);
          if (token !== revision) return;
          // Only replace the origin block; never overwrite a live comment or tab.
          if (origin) {
            options.messages = [origin];
            const body = mount.querySelector(".ctx-body");
            if (options.tab === "details") {
              const html = renderDetails(card, { ...options, tab: "details" });
              const template = document.createElement("template");
              template.innerHTML = html;
              const block = template.content.querySelector("[data-origin-text]")?.closest("section");
              if (block && body) body.insertBefore(block, body.querySelector("[data-details-description]"));
            }
          }
        } catch {
          // Missing access or a deleted source must not turn into an invented quote.
        }
      }
    },
    selectTab,
    stop() {
      revision++;
      mount.removeEventListener("click", click);
      mount.removeEventListener("keydown", keydown);
    },
  };
}
