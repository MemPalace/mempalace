import { escape as e, actor, elapsed } from './api.js';
import { whyStrip, waitingText } from './waiting.js';

const columns = ['todo', 'doing', 'review', 'done'];
const ITEM = '.card[data-card]';
const heading = (s) => s[0].toUpperCase() + s.slice(1);
export function cardId(board, number) {
  const letter = (value) => String(value || '').normalize('NFKD').toUpperCase().match(/[A-Z]/)?.[0];
  return `${letter(board.name) || letter(board.slug) || 'C'}-${number}`;
}
export function isBlocked(card, me) {
  return card.status === 'review' && Boolean(card.ask) && card.review_kind !== 'completion' && card.ask_to !== me;
}
function author(ref, board) {
  if (ref && ref === board.coordinator) return `${board.name} orchestrator`;
  return `${actor(ref)}${String(ref).startsWith('conn:') ? ' (agent)' : ''}`;
}
export function cardTags(card, { me, board = {}, messages = [] } = {}) {
  const tags = [];
  if (card.waiting) tags.push(waitingText(card));
  if (isBlocked(card, me)) tags.push('Blocked');
  else if (card.need === 'you' || (card.status === 'review' && card.ask && card.ask_to === me && card.review_kind !== 'completion')) tags.push('Needs me');
  if (card.message_id) tags.push(chatTag(card, messages));
  if (card.need === 'working') tags.push(`Working on it: ${author(card.assignee, board)}`, `working ${elapsed(card.heartbeat_at || card.updated_at)}`);
  else if (card.status === 'todo' && !card.assignee && !card.waiting) tags.push('Available for an agent');
  if (card.status === 'review' && String(card.return_to).startsWith('conn:')) tags.push(`Built by ${author(card.return_to, board)}`);
  if (card.status === 'review' && card.review_kind === 'completion' && card.ask_to) tags.push(`Reviewer: ${author(card.ask_to, board)}`);
  return tags;
}
// The time is the source message's time; without that message the tag carries no time at all.
export function chatTag(card, messages = []) {
  const date = new Date(messages.find((m) => String(m.id) === String(card.message_id))?.at);
  return `From chat${Number.isFinite(date.getTime()) ? ` · ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}` : ''}`;
}
// One bounded read per unknown source message; a missing or unreadable message stays unknown.
export async function loadChatTimes({ api, board, cards = [] }) {
  const pages = await Promise.all(cards.filter((c) => c.message_id != null).map((c) => api(`/api/chat/${encodeURIComponent(board.slug)}?before=${Number(c.message_id) + 1}&last=1`)
    .then((page) => page.board === board.slug ? (page.messages || []).find((m) => String(m.id) === String(c.message_id)) : null).catch(() => null)));
  return pages.filter(Boolean);
}
export function filterCards(cards, filter, me) {
  return cards.filter((c) => filter === 'mine' ? c.assignee === me : filter === 'agents' ? String(c.assignee).startsWith('conn:') : filter === 'blocked' ? isBlocked(c, me) : true);
}
// Status tags are chips; the rest (who, source, open for agents) reads as quiet meta text.
const PRIORITIES = ['urgent', 'high', 'normal', 'low'];
// A card shows its priority only when it is not the default; the list order already carries the rest.
const prioTag = (card) => card.priority && card.priority !== 'normal' && PRIORITIES.includes(card.priority) ? `<span class="tag prio ${card.priority === 'urgent' ? 'block' : 'meta'}" data-priority="${card.priority}">${heading(card.priority)}</span>` : '';
const prioRank = (card) => { const i = PRIORITIES.indexOf(card.priority); return i < 0 ? PRIORITIES.indexOf('normal') : i; };
const tagKind = (t) => t === 'Blocked' ? 'block' : t === 'Needs me' ? 'you' : t.startsWith('From chat') ? 'new' : t === 'Available for an agent' ? 'meta open' : 'meta';
const icon = (d) => `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;
const PLUS = 'M8 3v10M3 8h10', CHECK = 'm3.5 8.5 3 3 6-7';
const hints = { todo: 'Work waiting to be picked up.', doing: 'An agent or a person is on it.', review: 'Done, waiting for a check.', done: 'Finished and checked.' };
// Done is a compact check list; the first few show, the rest open with Show N more.
const DONE_SHOWN = 4;
function doneView(card, options, stop) {
  const id = cardId(options.board, card.number), selected = options.selected === card.number;
  return `<article class="card is-done${selected ? ' is-selected' : ''}" data-card="${card.number}" data-key="${card.number}" draggable="true" tabindex="${stop ? 0 : -1}" aria-label="${e(id + ' ' + card.title)}, done"${selected ? ' aria-current="true"' : ''}><div class="card-top">${icon(CHECK)}<span class="cid">${e(id)}</span></div><h3><a href="#/card/${card.number}" tabindex="-1">${e(card.title)}</a></h3></article>`;
}
function cardView(card, options, stop) {
  const id = cardId(options.board, card.number), tags = cardTags(card, options);
  const blocked = isBlocked(card, options.me), selected = options.selected === card.number;
  return `<article class="card${blocked ? ' is-blocked' : ''}${tags.includes('Needs me') ? ' is-you' : ''}${selected ? ' is-selected' : ''}" data-card="${card.number}" data-key="${card.number}" draggable="true" tabindex="${stop ? 0 : -1}" aria-label="${e(id + ' ' + card.title)}"${selected ? ' aria-current="true"' : ''}><div class="card-top"><span class="cid">${e(id)}</span>${(card.labels || []).map((l) => `<span class="tag lbl">${e(l)}</span>`).join('')}</div><h3><a href="#/card/${card.number}" tabindex="-1">${e(card.title)}</a></h3><div class="card-meta">${prioTag(card)}${tags.map((t) => `<span class="tag ${tagKind(t)}">${e(t)}</span>`).join('')}${card.assignee && card.need !== 'working' ? `<span class="who">${e(author(card.assignee, options.board))}</span>` : ''}</div>${blocked ? `<p class="card-note">Waiting on ${e(author(card.ask_to, options.board))}: ${e(card.ask)}</p>` : ''}</article>`;
}
// Component styles (design 03, 04, 05, r2-04-board-1024) stay with this renderer.
const styles = `
.workbench-board { display: flex; flex-direction: column; min-height: 100%; }
.workbench-board .board-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 12px 14px 0; }
.workbench-board .seg { display: inline-flex; border: 1px solid var(--line-strong); border-radius: var(--r); overflow: hidden; background: var(--surface); }
.workbench-board .seg button { border: 0; background: none; min-height: 30px; padding: 0 10px; color: var(--text-2); font-size: 13px; font-weight: 500; }
.workbench-board .seg button + button { border-left: 1px solid var(--line); }
.workbench-board .seg button[aria-pressed="true"] { background: var(--text); color: var(--surface); }
.workbench-board .why-strip { margin: 12px 14px 0; padding: 8px 12px; border-radius: var(--r); background: var(--wait-bg); color: var(--wait); font-size: 12.5px; line-height: 17px; }
.workbench-board .why-strip h2 { margin: 0 0 2px; font-size: 12.5px; font-weight: 650; }
.workbench-board .why-strip ul { margin: 0; padding-left: 18px; }
.workbench-board .col-jump { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 14px 0; }
.workbench-board .col-jump button { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 12px; border-radius: 20px; border: 1px solid var(--line-strong); background: var(--surface); color: var(--text-2); font-size: 13px; font-weight: 600; }
.workbench-board .col-jump button .n { font-weight: 600; }
.workbench-board .col-jump button[aria-pressed="true"] { background: var(--text); border-color: var(--text); color: var(--surface); }
.workbench-board .board { flex: 1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(0, .82fr); gap: 10px; padding: 12px 14px 14px; align-items: stretch; }
.workbench-board .col { display: flex; flex-direction: column; min-width: 0; background: var(--well); border-radius: 8px; padding-bottom: 8px; }
.workbench-board .col-h { display: flex; align-items: center; gap: 8px; padding: 10px 10px 6px; }
.workbench-board .col-h h2 { font-size: 13px; line-height: 18px; font-weight: 650; margin: 0; }
.workbench-board .col-h .n { font-size: 12px; color: var(--text-3); font-weight: 600; }
.workbench-board .col-h .iconbtn { margin-left: auto; width: 28px; height: 28px; }
.workbench-board .col-hint { padding: 0 10px 8px; margin: 0; font-size: 12.5px; line-height: 17px; color: var(--text-3); }
.workbench-board .cards { display: flex; flex-direction: column; gap: 6px; padding: 2px 8px 0; }
.workbench-board .cards > * { flex-shrink: 0; }
.workbench-board .empty-col { margin: 0; padding: 14px 10px; border: 1px dashed var(--line-strong); border-radius: var(--r); font-size: 12.5px; color: var(--text-3); text-align: center; }
.workbench-board .add-ghost { display: flex; align-items: center; gap: 6px; width: 100%; min-height: 32px; padding: 0 8px; border: 1px dashed var(--line-strong); border-radius: var(--r); background: transparent; color: var(--text-2); font-weight: 500; }
.workbench-board .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 8px 10px 9px; display: flex; flex-direction: column; gap: 5px; cursor: pointer; box-shadow: 0 1px 0 rgba(0,0,0,.03); }
.workbench-board .card-top { display: flex; align-items: center; gap: 6px; min-height: 18px; flex-wrap: wrap; }
.workbench-board .card-top .tag.lbl { margin-left: auto; }
.workbench-board .card-top .tag.lbl + .tag.lbl { margin-left: 0; }
.workbench-board .card-top .card-more { width: 44px; height: 44px; margin: -10px -10px -10px auto; }
.workbench-board .card-top .tag.lbl ~ .card-more { margin-left: 0; }
.workbench-board .cid { font: 12px var(--mono); color: var(--text-3); }
.workbench-board .card h3 { font-size: 13.5px; line-height: 18px; font-weight: 550; margin: 0; overflow-wrap: anywhere; }
.workbench-board .card h3 a { color: inherit; text-decoration: none; }
.workbench-board .card-meta { display: flex; align-items: center; gap: 4px 8px; flex-wrap: wrap; font-size: 12.5px; line-height: 16px; color: var(--text-3); min-width: 0; }
.workbench-board .card-note { margin: 0; font-size: 12.5px; line-height: 17px; color: var(--block); }
.workbench-board .tag { height: auto; border: 0; box-shadow: none; display: inline-flex; align-items: center; gap: 4px; max-width: 100%; font-size: 11.5px; line-height: 16px; font-weight: 600; padding: 1px 6px; border-radius: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.workbench-board .tag.block { background: var(--block-bg); color: var(--block); }
.workbench-board .tag.you { background: var(--wait-bg); color: var(--wait); }
.workbench-board .tag.new { background: var(--accent-weak); color: var(--accent); }
.workbench-board .tag.lbl { background: transparent; color: var(--text-2); box-shadow: inset 0 0 0 1px var(--line); font-weight: 500; }
/* Who works on it, who built it, open for an agent: quiet meta text, wrapping inside the card (m3). */
.workbench-board .tag.meta { display: inline; padding: 0; background: none; color: var(--text-3); font-size: 12.5px; font-weight: 500; white-space: normal; overflow: visible; }
.workbench-board .tag.open { display: inline-flex; gap: 5px; }
.workbench-board .tag.open::before { content: ""; width: 14px; height: 14px; border-radius: 4px; border: 1.5px dashed var(--line-strong); flex: none; }
.workbench-board .who { color: var(--text-2); }
.workbench-board .card.is-selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.workbench-board .card.is-blocked { border-left: 3px solid var(--block); padding-left: 8px; }
.workbench-board .card.is-you { border-left: 3px solid var(--wait-line); padding-left: 8px; }
/* Done: a compact check list; the first four show (04). */
.workbench-board .card.is-done { flex-direction: row; align-items: baseline; gap: 6px; padding: 6px 4px; background: transparent; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; box-shadow: none; }
.workbench-board .card.is-done .card-top { flex: none; flex-wrap: nowrap; align-self: center; }
.workbench-board .card.is-done .cid { white-space: nowrap; }
.workbench-board .card.is-done .i { color: var(--ok); }
.workbench-board .card.is-done h3 { font-size: 13px; line-height: 17px; font-weight: 400; color: var(--text-2); }
.workbench-board .card.is-done.is-selected { box-shadow: inset 0 0 0 2px var(--accent); border-radius: 4px; }
.workbench-board .col:not([data-all]) .card.is-done:nth-child(n+5) { display: none; }
.workbench-board .show-more { align-self: flex-start; margin: 8px 12px 0; padding: 0; border: 0; background: none; color: var(--accent); text-decoration: underline; min-height: 32px; font-size: 13px; }
.workbench-board .backlog { margin: 0 14px 14px; } .workbench-board .backlog summary { cursor: pointer; min-height: 32px; display: flex; align-items: center; }
.workbench-board .backlog .cards { padding: 6px 0 0; max-width: 360px; }
/* Empty board: three numbered steps with their buttons (03). */
.workbench-board .empty-strip { padding: 12px 14px 0; }
.workbench-board .steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.workbench-board .steps li { display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 10px; align-items: start; padding: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.workbench-board .steps li.is-next { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.workbench-board .steps .num { width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; font-size: 13px; background: var(--well); color: var(--text); }
.workbench-board .steps .is-next .num { background: var(--accent); color: var(--surface); }
.workbench-board .steps h2 { font-size: 14px; line-height: 20px; font-weight: 650; margin: 0; }
.workbench-board .steps p { margin: 2px 0 10px; font-size: 13px; line-height: 18px; color: var(--text-2); }
/* Add card: the composer at the top of Todo (05). */
.workbench-board .composer { height: auto; display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 8px; background: var(--surface); border: 1px solid var(--accent); border-radius: var(--r); box-shadow: 0 0 0 1px var(--accent), 0 6px 18px rgba(23,24,28,.08); }
.workbench-board .composer label { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.workbench-board .composer label.vis { font-size: 12px; font-weight: 600; color: var(--text-2); }
.workbench-board .composer label.vis small { font-weight: 400; color: var(--text-3); }
.workbench-board .composer .ttl { font-size: 13.5px; font-weight: 550; }
.workbench-board .composer input, .workbench-board .composer textarea, .workbench-board .composer select { flex: none; width: 100%; min-width: 0; border: 1px solid var(--line-strong); border-radius: 4px; padding: 6px 8px; font: inherit; font-size: 13px; background: var(--surface); color: var(--text); }
.workbench-board .composer textarea { height: 56px; resize: vertical; }
.workbench-board .composer-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 8px; }
.workbench-board .composer-foot { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.workbench-board .composer-foot .btn { padding: 0 10px; }
.workbench-board .composer-hint, .workbench-board .composer [data-add-status] { margin: 0; font-size: 12px; color: var(--text-3); }
.workbench-board .composer [data-add-status]:empty { display: none; }

@media (min-width: 1100px) { .workbench-board .col-jump { display: none; } }
@media (min-width: 1024px) and (max-width: 1099px) {
  /* Closed: four columns fit (r2-04-board-1024). Pushed by the chat: 190 px columns scroll sideways, with the jump bar. */
  .shell:not(.chat-open):not(.ctx-pushed) .workbench-board .col-jump { display: none; }
  .workbench-board .board { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .shell.chat-open .workbench-board .board, .shell.ctx-pushed .workbench-board .board { grid-template-columns: repeat(4, 190px); overflow-x: auto; }
  .workbench-board .steps { grid-template-columns: minmax(0, 1fr); }
}
@media (min-width: 700px) and (max-width: 1023px) {
  /* Two whole columns at a time; the jump bar and sideways scroll reach the others (never a column cut mid-card). */
  .workbench-board .board { grid-template-columns: repeat(4, calc((100% - 10px) / 2)); overflow-x: auto; scroll-snap-type: x mandatory; }
  .workbench-board .col { scroll-snap-align: start; }
  .workbench-board .steps { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 699px) {
  .workbench-board .board-tools, .workbench-board .col-jump { padding: 10px 12px 0; }
  .workbench-board .col-jump button, .workbench-board .seg button, .workbench-board button, .workbench-board summary { min-height: 44px; }
  .workbench-board .col-jump button { padding: 0 12px; font-size: 14px; }
  .workbench-board .board { display: block; padding: 10px 12px 14px; }
  .workbench-board .col { display: none; }
  .workbench-board .col[data-active="true"] { display: flex; }
  .workbench-board .steps { grid-template-columns: minmax(0, 1fr); }
  .workbench-board .composer-row { grid-template-columns: minmax(0, 1fr); }
}
`;
// The app's CSP is style-src 'self', which refuses inline style elements: adopt a constructed sheet once.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}
// One tab stop per column (the selected card, else the first); arrows move inside and across columns.
const stopIn = (list, selected) => (list.find((c) => c.number === selected) || list[0])?.number;
// A hidden Done card (past the first four) opens the Done list before it takes the tab stop or focus.
function reveal(card) {
  const col = card.closest?.('.col');
  if (!col || col.hasAttribute('data-all') || [...col.querySelectorAll(ITEM)].indexOf(card) < DONE_SHOWN) return;
  col.setAttribute('data-all', '');
  col.querySelector('[data-board-more]')?.remove();
}
export function roveTo(card) {
  reveal(card);
  card.closest('.cards')?.querySelectorAll(ITEM).forEach((item) => { item.tabIndex = item === card ? 0 : -1; });
}
export function boardView({ board, cards = [], me, filter = 'all', selected, messages = [], adding = false, people = [], canPrioritize = false, column = 'todo', sheet = true, doneAll = false }) {
  const options = { board, me, selected, messages }, visible = filterCards(cards, filter, me).map((c, i) => [c, i]).sort(([a, i], [b, j]) => prioRank(a) - prioRank(b) || i - j).map(([c]) => c);
  const backlog = visible.filter((c) => c.status === 'backlog'), count = (s) => visible.filter((c) => c.status === s).length;
  const form = adding ? addCardForm({ people, sheet, canPrioritize }) : '';
  const steps = !cards.length ? `<section class="empty-strip" aria-label="Get started"><ol class="steps"><li class="is-next"><span class="num" aria-hidden="true">1</span><div><h2>Add your first card</h2><p>A card is one piece of work. Write it like a to-do.</p><button class="btn primary" data-action="new-card">${icon(PLUS)}Add card</button></div></li><li><span class="num" aria-hidden="true">2</span><div><h2>Connect an agent</h2><p>An agent is an AI helper that works on cards. The first one becomes the orchestrator.</p><button class="btn" data-action="connect-agent">Connect an agent</button></div></li><li><span class="num" aria-hidden="true">3</span><div><h2>Ask for something</h2><p>Write in chat. The orchestrator turns requests into cards.</p><button class="btn" data-action="open-chat">Write in chat <kbd>/</kbd></button></div></li></ol></section>` : '';
  return `<div class="workbench-board">${steps}${whyStrip(board)}<div class="board-tools"><div class="seg" role="group" aria-label="Filter cards">${['all','mine','agents','blocked'].map((f) => `<button data-board-filter="${f}" aria-pressed="${filter === f}">${heading(f)}${f === 'blocked' ? ` ${cards.filter((c) => isBlocked(c, me)).length}` : ''}</button>`).join('')}</div></div><nav class="col-jump" aria-label="Jump to a column">${columns.map((s) => `<button type="button" data-board-column="${s}" aria-controls="col-${s}" aria-pressed="${column === s}">${heading(s)} <span class="n">${count(s)}</span></button>`).join('')}</nav><div class="board" role="region" aria-label="Board">${columns.map((s) => {
    // The selected card is never one of the hidden Done cards: its column opens instead.
    const list = visible.filter((c) => c.status === s), done = s === 'done', all = done && (doneAll || list.findIndex((c) => c.number === selected) >= DONE_SHOWN), more = done && !all ? list.length - DONE_SHOWN : 0;
    const body = list.length ? list.map((c) => (done ? doneView : cardView)(c, options, c.number === stopIn(list, selected))).join('') : s === 'todo' ? `<button type="button" class="add-ghost" data-action="new-card">${icon(PLUS)}Add card</button>` : '<p class="empty-col">Nothing yet</p>';
    return `<section class="col" id="col-${s}" data-status="${s}" data-active="${column === s}"${all ? ' data-all' : ''} aria-label="${heading(s)}, ${list.length} cards"><div class="col-h"><h2>${heading(s)}</h2><span class="n">${list.length}</span>${s === 'todo' ? `<button class="iconbtn" data-action="new-card" aria-label="Add card to Todo">${icon(PLUS)}</button>` : ''}</div>${list.length ? '' : `<p class="col-hint">${hints[s]}</p>`}<div class="cards">${s === 'todo' && !sheet ? form : ''}${body}</div>${more > 0 ? `<button type="button" class="link show-more" data-board-more>Show ${more} more</button>` : ''}</section>`;
  }).join('')}</div>${backlog.length ? `<details class="backlog"><summary>Backlog (${backlog.length})</summary><div class="cards">${backlog.map((c) => cardView(c, options, c.number === stopIn(backlog, selected))).join('')}</div></details>` : ''}${sheet ? form : ''}</div>`;
}
// The composer opens at the top of Todo (05); on a phone it is a full-screen modal sheet (m4).
export function addCardForm({ people = [], sheet = true, canPrioritize = false } = {}) {
  return `<form data-board-add class="composer"${sheet ? ' role="dialog" aria-modal="true" aria-labelledby="add-card-h"' : ' aria-labelledby="add-card-h"'}><h2 id="add-card-h"${sheet ? '' : ' class="sr"'}>Add card</h2><label><span class="sr">Title</span><input class="ttl" name="title" required maxlength="500" placeholder="Card title"></label><label class="vis">Details <small>(optional)</small><textarea name="body"></textarea></label><div class="composer-row"><label class="vis">Who<select name="assignee"><option value="">Any agent</option>${people.map((p) => `<option value="${e(p.ref)}">${e(p.name)}</option>`).join('')}</select></label><label class="vis">Label<input name="label"></label>${canPrioritize ? `<label class="vis">Priority<select name="priority">${PRIORITIES.map((p) => `<option value="${p}"${p === 'normal' ? ' selected' : ''}>${heading(p)}</option>`).join('')}</select></label>` : ''}</div><div class="composer-foot"><button class="btn primary" type="submit">Add card</button><button type="button" class="btn ghost" data-action="cancel-card">Cancel</button></div><p class="composer-hint">Enter adds · Esc cancels</p><p role="status" data-add-status></p></form>`;
}
export async function submitCard({ api, board, values }) {
  return api(`/api/boards/${encodeURIComponent(board.slug)}/cards`, { method:'POST', body:{ title:values.title, body:values.body || '', assignee:values.assignee || null, labels:values.label ? [values.label] : [], ...(values.priority ? { priority:values.priority } : {}) } });
}
export function bindBoard(root, { api, board, onAdded = () => {}, onFilter = () => {}, onSelect = () => {}, onOpen = () => {}, onCancel = () => {} }) {
  const click = (event) => { const more = event.target.closest('[data-board-more]'); if (more) { const col = more.closest('.col'); col?.setAttribute('data-all', ''); more.remove(); const next = col?.querySelectorAll(ITEM)[DONE_SHOWN]; if (next) { roveTo(next); next.focus(); } return; } const button = event.target.closest('[data-board-filter]'); if (button) onFilter(button.dataset.boardFilter); const jump = event.target.closest('[data-board-column]'); if (jump) { root.querySelectorAll('[data-status]').forEach((el) => { el.dataset.active = String(el.dataset.status === jump.dataset.boardColumn); }); root.querySelectorAll('[data-board-column]').forEach((el) => el.setAttribute('aria-pressed', String(el === jump))); root.querySelector(`#col-${jump.dataset.boardColumn}`)?.scrollIntoView({ block:'nearest', inline:'nearest' }); } };
  const submit = async (event) => {
    if (!event.target.matches('[data-board-add]')) return;
    event.preventDefault();
    const form = event.target, button = form.querySelector('[type="submit"]'), status = form.querySelector('[data-add-status]');
    if (button.disabled) return;
    button.disabled = true;
    try { const card = await submitCard({ api, board, values:Object.fromEntries(new FormData(form)) }); status.textContent = `Card ${cardId(board, card.number)} added to Todo`; onAdded(card); }
    catch { status.textContent = 'Could not add card. Your words are kept here. Try again.'; }
    finally { button.disabled = false; }
  };
  // Enter, Space and arrows on a focused card work whatever the single-key switch says.
  const keydown = (event) => {
    // Esc cancels the composer; the phone sheet also keeps Tab inside.
    const composer = event.target.closest?.('[data-board-add]'), sheet = event.target.closest?.('[data-board-add][aria-modal="true"]');
    if (composer && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onCancel(); return; }
    if (sheet) {
      if (event.key !== 'Tab') return;
      const items = [...sheet.querySelectorAll('input,textarea,select,button:not(:disabled)')].filter((el) => el.getClientRects().length);
      const i = items.indexOf(event.target);
      if (event.shiftKey && i <= 0) { event.preventDefault(); items.at(-1)?.focus(); }
      else if (!event.shiftKey && i === items.length - 1) { event.preventDefault(); items[0]?.focus(); }
      return;
    }
    const card = event.target.closest?.(ITEM);
    if (!card || event.target !== card || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(Number(card.dataset.card)); return; }
    const groups = [...root.querySelectorAll('.workbench-board .cards')].map((group) => [...group.querySelectorAll(ITEM)].filter((c) => c.getClientRects().length)).filter((group) => group.length);
    const at = groups.findIndex((group) => group.includes(card));
    if (at < 0) return;
    const list = groups[at], i = list.indexOf(card), side = groups[at + (event.key === 'ArrowRight' ? 1 : -1)];
    const next = event.key === 'ArrowDown' ? list[i + 1] : event.key === 'ArrowUp' ? list[i - 1] : event.key === 'Home' ? list[0] : event.key === 'End' ? list.at(-1)
      : ['ArrowLeft', 'ArrowRight'].includes(event.key) && side ? side.find((c) => c.tabIndex === 0) || side[0] : null;
    if (!next) return;
    event.preventDefault(); roveTo(next); next.focus(); onSelect(Number(next.dataset.card));
  };
  root.addEventListener('click', click); root.addEventListener('submit', submit); root.addEventListener('keydown', keydown);
  return () => { root.removeEventListener('click', click); root.removeEventListener('submit', submit); root.removeEventListener('keydown', keydown); };
}
