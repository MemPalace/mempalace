import { api as request, escape as e } from "./api.js";
import { detailsAuthor, detailsContext } from "./details.js";
import { cardId } from "./board.js";

const enc = encodeURIComponent;
const exact = (text) => e(text).replaceAll("\r", "&#13;");
const filters = { all: "All", approvals: "Approvals", decisions: "Decisions", reviews: "Reviews" };
const labels = { approval: "Approval", decision: "Decision", review: "Review" };
const buckets = ["backlog_from_agents", "approvals", "asks", "done_review", "ready_to_merge", "plans", "stale"];
const active = (hold) => hold && ["waiting", "settling", "released"].includes(hold.state);
const clock = (at) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const stamp = (at) => at ? `<time datetime="${e(at)}">${e(clock(at))}</time>` : "";
const boardFor = (boards, card) => boards.find((board) => board.slug === card.board) || { slug: card.board };
const latestHold = (holds, number) => holds.filter((hold) => hold.card === number).at(-1) || null;

// Classify by the server's queue and proposal state, never by words in a title.
export function needsItems(needs = {}, filter = "all") {
  const items = new Map();
  for (const bucket of buckets) for (const card of needs[bucket] || []) {
    if (!items.has(card.number)) items.set(card.number, { card, buckets: new Set() });
    items.get(card.number).buckets.add(bucket);
  }
  return [...items.values()].map((item) => ({ ...item, kind: item.card.proposal_status === "pending" ? "approval" :
    item.buckets.has("done_review") || item.buckets.has("ready_to_merge") || item.buckets.has("plans") || item.card.review_kind === "completion" ? "review" : "decision" }))
    .filter((item) => filter === "all" || `${item.kind}s` === filter);
}

// A loaded card gets the actions the server queued for this version; nothing else draws a button.
export function needsCard(needs, card) {
  const queued = needsItems(needs).find((item) => item.card.number === card.number)?.card;
  return { ...card, actions: queued?.version === card.version ? queued.actions || [] :
    card.can_approve && card.proposal_status === "pending" ? ["approve", "reject"] : [] };
}

export function holdState(hold, now = Date.now()) {
  if (!hold) return { state: "none", seconds: 0, canStop: false };
  const remaining = Date.parse(hold.release_at) - now;
  const seconds = Number.isFinite(remaining) ? Math.max(0, Math.ceil(remaining / 1000)) : 0;
  return { state: hold.state === "settling" || (hold.state === "waiting" && !seconds) ? "sending" : hold.state,
    seconds, canStop: hold.state === "waiting" && seconds > 0 && !hold.tooLate };
}
const countdown = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
function stopButton(hold, now) {
  const state = holdState(hold, now);
  return state.canStop ? `<button class="btn sm" data-needs-stop="${e(hold.id)}">Stop · <span data-hold-countdown="${e(hold.id)}" aria-hidden="true">${countdown(state.seconds)}</span></button>` : "";
}
function holdCopy(hold, now) {
  const state = holdState(hold, now).state;
  if (hold.tooLate && state !== "released") return `<b>Too late to stop</b><p>The 60 seconds have passed, so this can't be stopped here anymore. To follow up, ask the orchestrator.</p>`;
  if (state === "waiting") return `<b>Approved, not sent yet</b><p>Commonplace holds it until ${stamp(hold.release_at)}; nothing has been sent.</p>`;
  if (state === "sending") return "<b>Sending now…</b><p>Waiting for Commonplace to release your answer to the orchestrator.</p>";
  if (state === "released") return `<b>Released to the orchestrator at ${stamp(hold.updated_at || hold.release_at)}</b><p>The 60 seconds have passed, so this can't be stopped here anymore. This can't be recalled here. To follow up, ask the orchestrator.</p>`;
  if (state === "stopped") return "<b>Stopped. Nothing was sent.</b><p>Your answer did not reach the orchestrator.</p>";
  if (state === "failed") return `<b>Approval was not released.</b><p>${hold.code === "stale_version" ? "The card changed. Open it again to review the latest words." : "Commonplace could not release this answer. Open the card again before answering."}</p>`;
  return "";
}

// Styles are scoped here so the shell can wire the module without a shared-sheet edit.
const styles = `
.needs-me,.needs-detail { display:flex; flex-direction:column; min-width:0; min-height:0; height:100%; background:#FEFEFC; color:var(--text); }
.needs-me .bar { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:12px 16px; border-bottom:1px solid var(--line); }
.needs-me h1 { font-size:18px; line-height:24px; font-weight:650; }
.needs-me .seg { display:flex; flex-wrap:wrap; margin-left:auto; gap:2px; }
.needs-me .seg button { min-height:32px; padding:0 10px; border:1px solid transparent; background:transparent; border-radius:var(--r); color:var(--text-2); }
.needs-me .seg [aria-pressed=true] { border-color:var(--line-strong); background:var(--surface); color:var(--text); }
.needs-me .list { flex:1; min-height:0; overflow:auto; padding:8px 16px 16px; }
.needs-me .list-note { font-size:13px; color:var(--text-2); padding:4px 4px 0; }
.needs-me .group-h { padding:12px 4px 6px; font-size:12px; font-weight:650; color:var(--text-3); letter-spacing:.03em; text-transform:uppercase; }
.needs-me .row-item { display:grid; grid-template-columns:92px minmax(0,1fr) auto; gap:12px; align-items:center; padding:10px 12px; min-height:56px; border:1px solid var(--line); border-radius:var(--r); background:var(--surface); margin-bottom:6px; }
.needs-me .row-item h3 { font-size:14px; line-height:19px; font-weight:600; overflow-wrap:anywhere; }
.needs-me .row-item h3 a { color:inherit; text-decoration:none; }
.needs-me .row-item .ago { font-size:12px; color:var(--text-3); white-space:nowrap; }
.needs-me .sub { font-size:12.5px; color:var(--text-3); line-height:17px; }
.needs-me .is-selected { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
.needs-me .is-pending { background:var(--wait-bg); border-color:var(--wait-line); }
.needs-me .is-done { background:var(--ok-bg); }
.needs-me .keys { display:flex; flex-wrap:wrap; gap:12px; padding:8px 16px; border-top:1px solid var(--line); color:var(--text-3); font-size:12px; }
.needs-me .typ,.needs-detail .typ { font-size:12px; font-weight:650; padding:2px 8px; border-radius:4px; text-align:center; background:var(--accent-weak); color:var(--accent); }
.needs-me .typ.approval,.needs-me .typ.pending,.needs-detail .typ.approval { background:var(--wait-bg); color:var(--wait); }
.needs-me .typ.decision,.needs-detail .typ.decision { background:var(--agent-bg); color:var(--agent); }
.needs-detail .ctx-head { display:flex; justify-content:space-between; align-items:center; padding:8px 12px; border-bottom:1px solid var(--line); }
.needs-detail .ctx-body { flex:1; min-height:0; overflow:auto; padding:14px 16px 16px; display:flex; flex-direction:column; gap:14px; }
.needs-detail .detail-h { display:flex; flex-direction:column; gap:6px; }
.needs-detail h2 { font-size:17px; line-height:23px; font-weight:650; overflow-wrap:anywhere; }
.needs-detail h3 { font-size:13px; line-height:20px; font-weight:650; }
.needs-detail .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.needs-detail .quote { white-space:pre-wrap; overflow-wrap:anywhere; border-left:3px solid var(--line-strong); padding:2px 0 2px 12px; line-height:21px; }
.needs-detail .context-words { display:flex; flex-direction:column; gap:6px; }
.needs-detail .linked { display:flex; flex-direction:column; border:1px solid var(--line); border-radius:var(--r); }
.needs-detail .linked a { display:grid; grid-template-columns:1fr auto; gap:8px; align-items:center; padding:6px 10px; min-height:34px; color:var(--text); text-decoration:none; font-size:13px; border-top:1px solid var(--line); overflow-wrap:anywhere; }
.needs-detail .linked a:first-child { border-top:0; }
.needs-detail .facts,.needs-detail .hold-note { border:1px solid var(--line); border-radius:var(--r); padding:10px 12px; display:flex; flex-direction:column; gap:6px; font-size:13px; background:var(--pane); }
.needs-detail .hold-note { background:var(--wait-bg); border-color:var(--wait-line); }
.needs-detail .hold-note.stopped,.needs-detail .hold-note.released { background:var(--ok-bg); }
.needs-detail .lbl { display:flex; flex-direction:column; gap:6px; font-size:13px; }
.needs-detail .help { color:var(--text-3); }
.needs-detail textarea { width:100%; min-height:40px; resize:vertical; }
.needs-detail .ctx-foot { border-top:1px solid var(--line); padding:10px 12px 12px; display:flex; flex-direction:column; gap:8px; }
.needs-detail .decide-acts,.needs-detail .acts { display:flex; gap:8px; flex-wrap:wrap; }
.needs-detail .decide-acts > *,.needs-detail .acts > * { flex:1; justify-content:center; }
.needs-detail .confirm { display:flex; flex-direction:column; gap:10px; padding:12px; border-radius:8px; background:var(--wait-bg); box-shadow:inset 0 0 0 1px var(--wait-line); }
.needs-detail dialog.confirm { color:var(--text); border:0; width:calc(100% - 24px); max-width:500px; margin:auto auto 12px; }
.needs-detail dialog.confirm::backdrop { background:rgb(23 24 28 / .25); }
.needs-detail .error { color:var(--block); }
.needs-detail .phone-back { display:none; }
@media(max-width:1099px) { .needs-detail { width:100%; } }
@media(max-width:699px) {
.needs-me .bar { padding:10px 14px; } .needs-me .seg { margin-left:0; width:100%; }
.needs-me .seg button { flex:1; min-height:44px; padding:0 6px; }
.needs-me .row-item { grid-template-columns:minmax(0,1fr) auto; padding:14px; gap:6px; }
.needs-me .row-item .typ { justify-self:start; grid-column:1 / -1; }
.needs-me .row-item h3 { font-size:16px; line-height:21px; } .needs-me .row-item a { display:block; min-height:44px; }
.needs-me .keys { display:none; } .needs-detail .phone-back { display:inline-flex; }
.needs-detail button,.needs-detail a,.needs-detail textarea { min-height:44px; } .needs-detail .ctx-body { padding:16px; }
.needs-detail .ctx-foot { padding-bottom:max(12px,env(safe-area-inset-bottom)); }
.needs-detail .confirm { position:relative; } .needs-detail .confirm .acts { flex-wrap:nowrap; }
}
@media(max-width:359px) { .needs-detail .decide-acts { flex-direction:column; } }
`;
// The app's CSP is style-src 'self', which refuses inline style elements: adopt a constructed sheet once.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function renderNeeds({ needs = {}, boards = [], holds = [], cards = [], filter = "all", selected = null, now = Date.now() } = {}) {
  const items = needsItems(needs, filter);
  const known = new Map([...cards, ...needsItems(needs).map((item) => item.card)].map((card) => [card.number, card]));
  const approvals = holds.filter((hold) => hold.kind === "approve");
  const held = [...new Set(approvals.map((hold) => hold.card))].map((number) => latestHold(approvals, number))
    .filter((hold) => known.has(hold.card) && (filter === "all" || filter === (hold.kind === "approve" ? "approvals" : "decisions")));
  const row = (card, kind, hold = null) => {
    const board = boardFor(boards, card), state = holdState(hold, now);
    const opening = detailsContext(card).author || card.return_to || card.created_by;
    const status = hold ? ["waiting", "sending"].includes(state.state) ? "Waiting" : state.state === "released" ? "Released" : state.state === "stopped" ? "Stopped" : "Failed" : labels[kind];
    return `<article class="row-item${card.number === selected ? " is-selected" : ""}${hold ? active(hold) && state.state !== "released" ? " is-pending" : " is-done" : ""}" data-row="${e(card.number)}"><span class="typ ${hold ? "pending" : kind}">${status}</span><div><h3><a href="#/card/${e(card.number)}" data-needs-select="${e(card.number)}"${card.number === selected ? ' aria-current="true"' : ""}>${kind === "review" ? `<span class="cid">${e(cardId(board, card.number))}</span> ` : ""}${e(card.title)}</a></h3><div class="sub">${e(board.name || board.slug)}${hold ? ` · ${holdCopy(hold, now)}` : opening ? ` · ${e(detailsAuthor(opening, board))}` : ""}</div></div>${hold ? stopButton(hold, now) : ago(card.updated_at, now)}</article>`;
  };
  const groups = new Map();
  for (const item of items) {
    if (held.some((hold) => hold.card === item.card.number)) continue;
    const name = boardFor(boards, item.card).name || item.card.board;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  const heldGroup = (name, predicate) => {
    const rows = held.filter(predicate);
    return rows.length ? `<h2 class="group-h">${name}</h2>${rows.map((hold) => row(known.get(hold.card), hold.kind === "approve" ? "approval" : "decision", hold)).join("")}` : "";
  };
  const visibleCards = [...new Set([...items.map((item) => item.card.number), ...held.map((hold) => hold.card)])];
  const projects = new Set(visibleCards.map((number) => known.get(number)?.board).filter(Boolean)).size;
  return `<section class="needs-me" aria-label="Needs me"><header class="bar"><h1>Needs me</h1><span data-needs-chat></span><span class="sub">${visibleCards.length} ${visibleCards.length === 1 ? "item" : "items"}, ${projects} ${projects === 1 ? "project" : "projects"}</span><div class="seg" role="group" aria-label="Filter">${Object.entries(filters).map(([key, text]) => `<button data-needs-filter="${key}" aria-pressed="${filter === key}">${text}</button>`).join("")}</div></header><div class="list"><p class="list-note">Only what an agent can't do without you.</p>${heldGroup("Approved, not sent yet", (hold) => ["waiting", "settling"].includes(hold.state))}${heldGroup("Recent answers", (hold) => !["waiting", "settling"].includes(hold.state))}${[...groups].map(([name, group]) => `<h2 class="group-h">${e(name)}</h2>${group.map(({ card, kind }) => row(card, kind)).join("")}`).join("")}${!items.length && !held.length ? '<p class="empty">Nothing needs you right now.</p>' : ""}</div><div class="keys" aria-label="Keyboard shortcuts"><span><kbd>J</kbd><kbd>K</kbd> move</span><span><kbd>↵</kbd> open linked card</span><span><kbd>R</kbd> ask a question</span><span>Approve and Decline have no single-key shortcut, on purpose: use the buttons.</span></div></section>`;
}

export function githubPrUrl(value) {
  if (typeof value !== "string" || /[\x00-\x20\x7f]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.hostname !== "github.com" || url.port || url.username || url.password || url.search || url.hash || !/^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}
function githubControls(card, github, busy) {
  if (card.need !== "merge") return "";
  const state = github?.state || "loading", disabled = busy || github?.pending || state === "loading" ? " disabled" : "";
  const access = '<a href="https://github.com/settings/applications" target="_blank" rel="noopener noreferrer">Review GitHub app access</a>';
  if (state === "error") return `<p role="alert">GitHub connection unavailable. ${access}</p>`;
  if (state === "other_workspace") return `<p>This GitHub account is connected in another workspace. Disconnect it before connecting here.</p><button class="btn" data-needs-github="disconnect"${disabled}>Disconnect</button>${access}`;
  if (state === "confirm" && github.connect_enabled === false) return `<p>New GitHub connections are disabled.</p><button class="btn" data-needs-github="disconnect"${disabled}>Disconnect</button>${access}`;
  if (state === "unconfigured") return '<p>GitHub connection setup is required.</p>';
  if (state === "loading") return '<button class="btn" disabled>Loading GitHub…</button>';
  if (state === "confirm") return `<p>Connect GitHub account ${e(github.login)} (${e(github.subject)})?${github.inherited_scopes?.length ? " This includes inherited sign-in permissions: " + e(github.inherited_scopes.join(", ")) : ""}</p><button class="btn" data-needs-github="confirm"${disabled}>Connect this account</button><button class="btn" data-needs-github="decline"${disabled}>Cancel</button>`;
  if (state === "connected") {
    const url = githubPrUrl(card.pr_url);
    return `${url ? `<a class="btn" href="${e(url)}" target="_blank" rel="noopener noreferrer">Open PR</a>` : ""}<button class="btn" data-needs-github="disconnect"${disabled}>Disconnect</button><p>PR reports are unverified. A connection proves no merge permissions. Disconnect also revokes GitHub login authorization for this app; your current session survives.</p>`;
  }
  return `<button class="btn" data-needs-github="connect"${disabled}>Connect GitHub</button>${state === "expired" ? `<p>Expired locally. ${access}</p>` : ""}${github.acknowledge_required ? `<label><input type="checkbox" data-needs-github-ack> I reviewed GitHub app access; revocation was unconfirmed.</label>${access}` : ""}`;
}
export function renderNeedDetail(card, { board = { slug: card.board }, hold = null, now = Date.now(), confirm = false, note = "", error = "", busy = false, chatOpen = false, phone = false, kind, github = null } = {}) {
  const proposal = card.proposal_status === "pending";
  kind ||= proposal ? "approval" : card.review_kind === "completion" || card.need === "merge" ? "review" : "decision";
  const context = detailsContext(card), canAnswer = !active(hold) && !hold?.tooLate;
  const allowed = (action) => (card.actions || []).includes(action);
  const quote = !context.text ? "" : `<section class="context-words"><h3>Context (exact words)</h3><p class="quote" data-context-text>${exact(context.text)}</p>${context.author ? `<p>— ${e(detailsAuthor(context.author, board))}</p>` : ""}</section>`;
  const linked = card.child_cards?.length ? `<section><h3>Linked cards</h3><div class="linked">${card.child_cards.map((child) => `<a href="#/card/${e(child.number)}"><span><span class="cid">${e(cardId(board, child.number))}</span> ${e(child.title)}</span><span class="tag${child.status === "done" ? " ok" : ""}">${e(child.status)}</span></a>`).join("")}</div></section>` : "";
  const ask = `<button class="btn" data-needs-ask aria-keyshortcuts="R" aria-controls="chat" aria-expanded="${chatOpen}">${hold?.tooLate || hold?.state === "released" ? "Ask the orchestrator" : "Ask a question"} <kbd>R</kbd></button>`;
  const disabled = busy ? " disabled" : "";
  const confirmTag = phone ? "dialog" : "div";
  const noteBox = canAnswer && (proposal || allowed("answer")) ? `<label class="lbl">Note to the orchestrator <span class="help">Optional. Sent with your answer.</span><textarea data-needs-note rows="1" placeholder="e.g. Send at 09:00 tomorrow"${proposal ? ' maxlength="500"' : ""}>${exact(note)}</textarea></label>` : "";
  const decisions = !proposal && kind === "decision" && allowed("answer") ? (card.ask_options || []).map((choice, index) => `<button class="btn primary" data-needs-choice="${index}"${disabled}>${e(choice)}</button>`).join("") : "";
  // The shell's existing handlers carry these: Accept, and recovery for a card whose claim ran out.
  const shellButton = (action, label, tone = "") => allowed(action) ? `<button class="btn${tone}" data-action="need-${action}" data-card="${e(card.number)}" data-version="${e(card.version)}"${disabled}>${label}</button>` : "";
  // A plan is approved directly; only a proposal's approval waits as a hold.
  const plan = !proposal && allowed("approve") ? `<button class="btn primary" data-needs-approve="plan"${disabled}>Approve</button>${allowed("reject") ? `<button class="btn danger" data-action="need-reject" data-card="${e(card.number)}" data-version="${e(card.version)}"${disabled}>Reject</button>` : ""}` : "";
  const recovery = ["requeue", "reassign", "park"].map((action) => shellButton(action, { requeue: "Requeue", reassign: "Reassign", park: "Park" }[action])).join("");
  const direct = proposal ? "" : plan + shellButton("accept", "Accept", " primary") + recovery;
  const open = recovery ? `<a class="btn" href="#/card/${e(card.number)}">Answer</a>` : `<a class="btn" href="${kind === "decision" ? `#/decide/${e(card.number)}` : `#/card/${e(card.number)}`}">${kind === "review" ? "Open review" : "Open card"}</a>`;
  const actions = canAnswer ? proposal && allowed("approve") ? `${confirm ? `<${confirmTag} class="confirm" role="${phone ? "alertdialog" : "group"}"${phone ? ' aria-modal="true"' : ""} aria-labelledby="needs-confirm-title" aria-describedby="needs-confirm-description"><h3 id="needs-confirm-title">${e(card.title)}</h3><p id="needs-confirm-description">Your approval reaches the orchestrator 60 seconds after you confirm. Until then you can stop it from Needs me.</p><div class="acts"><button class="btn" data-needs-cancel${disabled}>Cancel</button><button class="btn primary" data-needs-confirm${disabled}>Yes, approve</button></div></${confirmTag}>` : `<button class="btn primary" data-needs-approve${disabled}>Approve and send…</button><div class="decide-acts">${allowed("reject") ? `<button class="btn danger" data-action="need-reject" data-card="${e(card.number)}" data-version="${e(card.version)}"${disabled}>Decline</button>` : ""}${ask}</div>`}` : `${direct ? `<div class="acts">${direct}</div>` : ""}<div class="decide-acts">${decisions}${open}${ask}</div>` : ask;
  return `<section class="needs-detail" data-card="${e(card.number)}" data-version="${e(card.version)}" aria-label="Needs me details"><header class="ctx-head"><b>Details</b><button class="btn ghost sm" data-needs-close>Hide</button></header><div class="ctx-body"><button class="btn phone-back" data-needs-close>Back to Needs me</button><header class="detail-h"><div class="row"><span class="typ ${kind}">${labels[kind]}</span><span>${e(board.name || board.slug)}${asked(card) ? ` · asked ${asked(card)}` : ""}</span></div><h2 tabindex="-1" data-needs-title>${e(card.title)}</h2></header>${hold ? `<section class="hold-note ${e(hold.state)}" role="status">${holdCopy(hold, now)}${stopButton(hold, now)}</section>` : ""}${quote}${card.ask && !proposal ? `<p class="quote">${exact(card.ask)}</p>` : ""}${linked}${proposal && canAnswer ? '<div class="facts" role="note">Commonplace holds your approval for <b>60 seconds</b> before the orchestrator receives it, so it can’t send earlier. Until then, Stop cancels it and nothing is sent. After that it can’t be undone here.</div>' : ""}${noteBox}${error ? `<p class="error" role="alert">${e(error)}</p>` : ""}</div><footer class="ctx-foot">${githubControls(card, github, busy)}${actions}</footer></section>`;
}

// The shell owns routing, sheet modality and chat. All requests use the shared JSON api.
// Row age as the design writes it ("12 min", "3 h"); the detail names the local time of the ask.
function ago(at, now) {
  const minutes = Math.floor((now - Date.parse(at)) / 60000);
  if (!Number.isFinite(minutes) || minutes < 0) return "";
  const text = minutes < 1 ? "now" : minutes < 60 ? `${minutes} min` : minutes < 1440 ? `${Math.floor(minutes / 60)} h` : `${Math.floor(minutes / 1440)} d`;
  return `<time class="ago" datetime="${e(at)}">${text}</time>`;
}
function asked(card) {
  const at = [...(card.events || [])].reverse().find((ev) => ev.options || ev.kind === "ask" || ev.kind === "add")?.at || (card.ask ? card.updated_at : null);
  const date = new Date(at);
  return at && Number.isFinite(date.getTime()) ? `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}` : "";
}
export function createNeeds({ mount, detailMount, api = request, onAction, onAsk = () => {}, onSelect = () => {}, onClose = () => {}, onChange = () => {}, onError = () => {}, onRender = () => {}, singleKeys = () => true,
  isPhone = () => globalThis.matchMedia?.("(max-width:699px)").matches || false, now = Date.now, document: doc = globalThis.document, setInterval: schedule = globalThis.setInterval, clearInterval: unschedule = globalThis.clearInterval } = {}) {
  let data = { needs: {}, boards: [] }, holds = [], selected = null, filter = "all", current = null;
  let holdsLoaded = false, github = null;
  let stopped = false, paused = false, revision = 0, busy = false, confirming = false, chatOpen = false, error = "", lastPoll = 0, holdRead = null;
  const cards = new Map(), notes = new Map(), tooLate = new Set();
  const mounts = [...new Set([mount, detailMount].filter(Boolean))];
  const rememberNote = () => { const value = detailMount?.querySelector("[data-needs-note]")?.value; if (current && value != null) notes.set(current.number, value); };
  const currentHold = () => latestHold(holds, current?.number);
  const focus = (selector) => detailMount?.querySelector(selector)?.focus();
  function savedFocus(root) {
    const el = doc?.activeElement;
    if (!el || !root?.contains(el)) return () => {};
    const attr = [...(el.attributes || [])].find((item) => item.name.startsWith("data-needs-"));
    if (!attr) return () => {};
    const start = el.selectionStart, end = el.selectionEnd;
    return () => {
      // Values here are controller-owned numbers or filter names; notes have an empty attribute.
      const next = [...root.querySelectorAll(`[${attr.name}]`)].find((item) => item.getAttribute(attr.name) === attr.value);
      next?.focus({ preventScroll: true });
      if (typeof start === "number") next?.setSelectionRange?.(start, end);
    };
  }
  const withActions = (card) => needsCard(data.needs, card);
  function drawDetail() {
    // While the shell shows something else in the shared panel (project settings), the detail waits.
    if (!detailMount || stopped || paused) return;
    const restoreFocus = savedFocus(detailMount);
    detailMount.innerHTML = current ? renderNeedDetail(current, { board: boardFor(data.boards, current), hold: currentHold(), now: now(), confirm: confirming, note: notes.get(current.number) || "", error, busy: busy || !holdsLoaded, chatOpen, phone: isPhone(),
      github, kind: needsItems(data.needs).find((item) => item.card.number === current.number)?.kind }) : '<p class="empty">Choose an item to see its exact words.</p>';
    const dialog = detailMount.querySelector('dialog.confirm');
    if (dialog && !dialog.open) dialog.showModal();
    restoreFocus();
  }
  function draw() {
    if (stopped) return;
    const restoreFocus = savedFocus(mount);
    const list = mount.querySelector(".list"), scroll = list?.scrollTop;
    mount.innerHTML = renderNeeds({ ...data, holds, cards: [...cards.values()], selected, filter, now: now() });
    if (scroll != null) { const next = mount.querySelector(".list"); if (next) next.scrollTop = scroll; }
    drawDetail(); restoreFocus();
    signature = holds.map((hold) => `${hold.id}:${holdState(hold, now()).state}`).join("|");
    onRender({ header: mount.querySelector(".bar"), card: current });
  }
  function items() {
    const visible = needsItems(data.needs, filter).map((item) => item.card);
    for (const hold of holds) {
      const card = cards.get(hold.card);
      if (card && !visible.some((item) => item.number === card.number) && (filter === "all" || filter === (hold.kind === "approve" ? "approvals" : "decisions"))) visible.push(card);
    }
    return visible;
  }
  async function select(number) {
    if (stopped) return;
    rememberNote();
    const token = ++revision;
    selected = Number(number); current = null; github = null; confirming = false; error = ""; chatOpen = false;
    draw();
    try {
      const card = await api(`/api/cards/${selected}`);
      if (stopped || token !== revision) return;
      current = withActions(card); cards.set(card.number, current); draw();
      if (card.need === "merge") {
        try {
          const result = await api("/api/github");
          if (stopped || token !== revision) return;
          github = result;
        } catch (failure) {
          if (stopped || token !== revision) return;
          github = { state: "error" }; onError(failure);
        }
        draw();
      }
      focus("[data-needs-title]"); onSelect(card);
    } catch (failure) {
      if (stopped || token !== revision) return;
      if (detailMount && !paused) detailMount.innerHTML = '<p role="alert">Could not load this card. Open it again to retry.</p>';
      onError(failure);
    }
  }
  async function refreshHolds() {
    if (stopped) return;
    if (holdRead) return holdRead;
    holdRead = (async () => {
      const page = await api("/api/holds");
      if (stopped) return;
      const next = page.holds.filter((hold) => hold.kind === "approve").map((hold) => ({ ...hold, ...(tooLate.has(hold.id) ? { tooLate: true } : {}) }));
      const changed = !holdsLoaded || JSON.stringify(next) !== JSON.stringify(holds);
      holds = next; holdsLoaded = true; lastPoll = now();
      for (const hold of holds) if (!cards.has(hold.card)) {
        try {
          const card = await api(`/api/cards/${hold.card}`);
          if (stopped) return;
          cards.set(card.number, card);
        } catch { /* An inaccessible card must never acquire an invented title. */ }
      }
      if (changed) { rememberNote(); draw(); }
    })();
    try { await holdRead; } finally { holdRead = null; }
  }
  function showError(message, failure) {
    if (stopped) return;
    error = message; drawDetail(); onError(failure);
  }
  // A request's failure belongs to the card it was sent for: drawn while that card is selected, otherwise only reported.
  const failedFor = (number, message, failure) => number === selected ? showError(message, failure) : onError(failure);
  function approve() {
    if (stopped || !holdsLoaded || busy || !current || current.proposal_status !== "pending" || !(current.actions || []).includes("approve") || active(currentHold())) return;
    rememberNote(); confirming = true; drawDetail(); focus("[data-needs-cancel]");
  }
  async function sendHold(body) {
    if (stopped || busy) return;
    const number = body.card;
    busy = true; error = ""; drawDetail();
    try {
      const result = await api("/api/holds", { method: "POST", body });
      if (stopped) return;
      const hold = { ...result, card: body.card, kind: body.kind, state: "waiting" };
      holds = [...holds.filter((item) => item.id !== hold.id), hold]; confirming = false;
      try { await refreshHolds(); } catch (failure) { onError(failure); }
      if (!stopped) onChange();
    } catch (failure) {
      // A response can be lost after the hold was persisted. Read once, never resend.
      try { await refreshHolds(); } catch { /* Keep the ambiguous outcome visible. */ }
      failedFor(number, "Could not confirm approval. Check Needs me before trying again.", failure);
    } finally { busy = false; if (!stopped) draw(); }
  }
  async function confirm() {
    if (!holdsLoaded || !confirming || !current || active(currentHold())) return;
    rememberNote();
    const reason = notes.get(current.number) || "";
    return sendHold({ card: current.number, kind: "approve", version: current.version, ...(reason ? { reason } : {}) });
  }
  function dropFromQueue(card, token) {
    const needs = {};
    for (const [bucket, value] of Object.entries(data.needs)) needs[bucket] = Array.isArray(value) ? value.filter((item) => item.number !== card.number) : value;
    data = { ...data, needs };
    if (token === revision) { confirming = false; current = null; selected = null; revision++; }
  }
  // A plan's Approve uses the card's approve route at once; no hold, unlike a proposal.
  async function approvePlan() {
    if (stopped || busy || !current || current.proposal_status === "pending" || !(current.actions || []).includes("approve")) return;
    const card = current, token = revision;
    busy = true; error = ""; drawDetail();
    try {
      await api(`/api/cards/${card.number}/approve`, { method: "POST", body: { approve: true, version: card.version } });
      if (stopped) return;
      dropFromQueue(card, token);
      onChange();
    } catch (failure) { failedFor(card.number, "Could not approve. Open the card again before trying again.", failure); }
    finally { busy = false; draw(); }
  }
  // Only Approve waits 60 seconds; a decision answer goes straight to the card's decide route.
  async function answer(choice, reply) {
    if (stopped || busy || !current || !(current.actions || []).includes("answer") || !(current.ask_options || []).includes(choice)) return;
    rememberNote(); const card = current, token = revision;
    const text = reply ?? notes.get(card.number) ?? "";
    busy = true; error = ""; drawDetail();
    try {
      await api(`/api/cards/${card.number}/decide`, { method: "POST", body: { choice, version: card.version, ...(text ? { reply: text } : {}) } });
      if (stopped) return;
      notes.delete(card.number); dropFromQueue(card, token);
      onChange();
    } catch (failure) { failedFor(card.number, "Could not send your answer. Open the card again before trying again.", failure); }
    finally { busy = false; draw(); }
  }
  async function decline() {
    if (stopped || !holdsLoaded || busy || !current || active(currentHold()) || !(current.actions || []).includes("reject")) return;
    rememberNote(); const card = current, token = revision;
    busy = true; drawDetail();
    try {
      const reason = notes.get(card.number) || "";
      if (onAction) await onAction("need-reject", card, reason ? { reason } : {});
      else await api(`/api/cards/${card.number}/approve`, { method: "POST", body: { approve: false, version: card.version, ...(reason ? { reason } : {}) } });
      if (stopped) return;
      dropFromQueue(card, token);
      onChange();
    } catch (failure) { failedFor(card.number, "Could not decline. Open the card again before trying again.", failure); }
    finally { busy = false; draw(); }
  }
  async function stopHold(id) {
    if (stopped || busy) return;
    const hold = holds.find((item) => item.id === id);
    if (!holdState(hold, now()).canStop) return;
    busy = true; rememberNote();
    try {
      await api(`/api/holds/${enc(id)}/stop`, { method: "POST", body: {} });
      if (stopped) return;
      holds = holds.map((item) => item.id === id ? { ...item, state: "stopped" } : item);
      await refreshHolds(); if (!stopped) onChange();
    } catch (failure) {
      if (failure.status === 409 && failure.payload?.error === "too_late") {
        tooLate.add(id); holds = holds.map((item) => item.id === id ? { ...item, tooLate: true } : item);
        try { await refreshHolds(); } catch { /* The server's too_late answer is authoritative. */ }
      } else showError("Could not stop this approval. Check Needs me for its current state.", failure);
    } finally { busy = false; draw(); }
  }
  function ask() {
    if (stopped || !current) return;
    rememberNote(); chatOpen = true; drawDetail();
    const context = detailsContext(current), board = boardFor(data.boards, current);
    onAsk(current, { board: current.board, wing: board.wing, author: detailsAuthor(context.author, board), text: context.text ?? current.ask ?? current.body ?? "", title: current.title, hold: currentHold() });
  }
  function cancel() { confirming = false; drawDetail(); focus("[data-needs-approve]"); }
  async function githubAction(action) {
    if (busy || !current || !github || stopped) return;
    const card = current, token = revision;
    const acknowledge = detailMount?.querySelector("[data-needs-github-ack]")?.checked === true;
    busy = true;
    try {
      const method = action === "disconnect" ? "DELETE" : "POST";
      const path = action === "disconnect" ? "/api/github" : action === "connect" ? "/api/github/connect" : "/api/github/confirm";
      const result = await api(path, { method, body: { csrf: github.csrf, card: card.number, accept: action === "confirm", acknowledge } });
      if (stopped || token !== revision) return;
      github = null;
      if (result.authorize_url) { globalThis.location.assign(result.authorize_url); return; }
      github = await api("/api/github");
      if (stopped || token !== revision) { github = null; return; }
      error = result.message || ""; drawDetail();
    } catch (failure) { failedFor(card.number, "Could not update GitHub connection. Check GitHub app access before trying again.", failure); }
    finally { busy = false; if (!stopped && token === revision) drawDetail(); }
  }
  const click = async (event) => {
    if (paused && detailMount?.contains(event.target)) return;
    const button = event.target.closest("[data-needs-github],[data-needs-select],[data-needs-filter],[data-needs-approve],[data-needs-confirm],[data-needs-cancel],[data-needs-stop],[data-needs-ask],[data-needs-choice],[data-needs-close],[data-action=need-reject]");
    if (!button) return;
    event.preventDefault(); event.stopPropagation();
    if (button.disabled) return;
    // Picking an item takes the shared panel back from whatever paused the detail.
    if (button.hasAttribute("data-needs-github")) await githubAction(button.dataset.needsGithub);
    else if (button.hasAttribute("data-needs-select")) { paused = false; await select(button.dataset.needsSelect); }
    else if (button.hasAttribute("data-needs-filter")) await setFilter(button.dataset.needsFilter);
    else if (button.dataset?.needsApprove === "plan") await approvePlan();
    else if (button.hasAttribute("data-needs-approve")) approve();
    else if (button.hasAttribute("data-needs-confirm")) await confirm();
    else if (button.hasAttribute("data-needs-cancel")) cancel();
    else if (button.hasAttribute("data-needs-stop")) await stopHold(button.dataset.needsStop);
    else if (button.hasAttribute("data-needs-ask")) ask();
    else if (button.hasAttribute("data-needs-choice")) await answer(current.ask_options[Number(button.dataset.needsChoice)]);
    else if (button.hasAttribute("data-needs-close")) onClose(current);
    else await decline();
  };
  const keydown = (event) => {
    if (paused && detailMount?.contains(event.target)) return;
    if (event.key === "Escape" && confirming && !busy) { event.preventDefault(); event.stopPropagation(); cancel(); return; }
    if (!singleKeys() || event.altKey || event.ctrlKey || event.metaKey || event.target.closest("input,textarea,select,[contenteditable=true]")) return;
    if (event.key.toLowerCase() === "r") { event.preventDefault(); event.stopPropagation?.(); ask(); }
    else if (event.key.toLowerCase() === "z" && holdState(currentHold(), now()).canStop) { event.preventDefault(); event.stopPropagation?.(); void stopHold(currentHold().id); }
    else if (["j", "k"].includes(event.key.toLowerCase())) {
      const rows = items(), index = rows.findIndex((card) => card.number === selected);
      const next = rows[Math.max(0, Math.min(rows.length - 1, index + (event.key.toLowerCase() === "j" ? 1 : -1)))];
      if (next) { event.preventDefault(); event.stopPropagation?.(); paused = false; void select(next.number); }
    }
  };
  async function setFilter(value) {
    if (!Object.hasOwn(filters, value) || stopped) return;
    rememberNote(); filter = value;
    const rows = items();
    if (!rows.some((card) => card.number === selected)) {
      if (rows.length) return select(rows[0].number);
      revision++; selected = null; current = null; confirming = false;
    }
    draw(); mount.querySelector(`[data-needs-filter="${filter}"]`)?.focus();
  }
  let signature = "";
  async function tick() {
    if (stopped || doc?.hidden) return;
    const next = holds.map((hold) => `${hold.id}:${holdState(hold, now()).state}`).join("|");
    if (next !== signature) { signature = next; rememberNote(); draw(); }
    for (const root of mounts) for (const el of root.querySelectorAll("[data-hold-countdown]")) {
      const hold = holds.find((item) => item.id === el.dataset.holdCountdown);
      el.textContent = countdown(holdState(hold, now()).seconds);
    }
    if (now() - lastPoll >= 5000) {
      try { await refreshHolds(); } catch (failure) { onError(failure); }
    }
  }
  const visibility = () => { if (!doc.hidden) void refreshHolds().catch(onError); };
  for (const root of mounts) { root.addEventListener("click", click); root.addEventListener("keydown", keydown); }
  doc?.addEventListener("visibilitychange", visibility);
  const timer = schedule(tick, 1000);
  return {
    async show(settings = {}) {
      if (stopped) return;
      rememberNote(); data = { needs: {}, boards: [], ...settings };
      for (const { card } of needsItems(data.needs)) cards.set(card.number, card);
      const number = settings.selected ?? selected ?? items()[0]?.number;
      // Start selection before reading holds; neither response may choose an older card.
      const loading = number != null ? select(number) : Promise.resolve();
      try { await refreshHolds(); } catch (failure) { showError("Could not load approvals. Check again before answering.", failure); }
      await loading;
      if (!current && selected == null && items().length) await select(items()[0].number);
      draw();
    },
    select, setFilter, approve, approvePlan, confirm, decline, answer, stopHold, ask, refreshHolds,
    setChatOpen(open) { rememberNote(); chatOpen = Boolean(open); drawDetail(); },
    pause(on) { if (paused === Boolean(on)) return; paused = Boolean(on); if (!paused) drawDetail(); },
    paused: () => paused,
    stop() {
      stopped = true; github = null; revision++; unschedule(timer);
      detailMount?.querySelector("dialog.confirm")?.close(); if (detailMount && current?.need === "merge") detailMount.innerHTML = ""; cards.clear(); notes.clear(); holds = []; current = null;
      for (const root of mounts) { root.removeEventListener("click", click); root.removeEventListener("keydown", keydown); }
      doc?.removeEventListener("visibilitychange", visibility);
    },
  };
}
