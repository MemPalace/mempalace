// Deck home. Reads the person's bots; answers use the existing versioned decision route.
import { elapsed } from './api.js';

export const TITLE = 'Today';
const enc = encodeURIComponent;
const count = (n) => Number.isSafeInteger(n) && n > 0 ? n : 0;
const number = (n) => count(Number(n));
const initial = (name) => (Array.from(String(name ?? '').trim())[0] || '?').toUpperCase();
const avatar = (bot, e) => `<span class="today-avatar" aria-hidden="true">${e(initial(bot.name))}</span>`;
const thread = (bot) => `#/bots/${enc(bot.id)}`;
const key = (q) => `${q.card}:${q.version}`;
// Only clear affirmative actions get an inverted pill; choices such as currencies stay peers.
const affirmative = (text) => /^(?:yes\b|accept\b|approve\b|ship\b|start\b|send\b|publish\b|go ahead\b)/i.test(String(text));

/** General and unscoped custom bots can repeat the projects' questions. */
export function questionsFrom(pages) {
  const questions = new Map();
  for (const { bot, waiting } of pages) for (const block of waiting || []) {
    if (block?.type !== 'decision' || !block.open || block.gone || !number(block.card)) continue;
    const n = number(block.card), previous = questions.get(n);
    if (!previous || (bot.kind === 'project' && previous.bot.kind !== 'project')) questions.set(n, { ...block, card: n, bot });
  }
  return [...questions.values()];
}

export function summary(questions, bots, boards) {
  const waiting = questions.length;
  const projects = boards.filter((b) => Number.isSafeInteger(b.agents_working));
  const working = projects.length ? projects.reduce((n, b) => n + count(b.agents_working), 0) : bots.filter((b) => b.state === 'working').length;
  const noun = projects.length ? 'agent' : 'bot';
  return `${waiting} thing${waiting === 1 ? '' : 's'} ${waiting === 1 ? 'waits' : 'wait'} on you. ${working} ${noun}${working === 1 ? ' is' : 's are'} working.`;
}

export function questionHTML(q, { busy = new Set(), settled = new Map(), errors = new Map() } = {}, e) {
  const answer = settled.get(key(q)), disabled = busy.has(q.card) ? ' disabled' : '';
  const options = (Array.isArray(q.options) ? q.options : []).map((option, i) => `<button type="button" class="today-option${i === 0 && affirmative(option) ? ' inverted' : ''}" data-today-decide="${q.card}" data-choice="${e(option)}"${disabled}>${e(option)}</button>`).join('');
  return `<article class="today-question" data-question="${q.card}" data-state="${answer === undefined ? 'open' : 'settled'}">
    <header>${avatar(q.bot, e)}<b>${e(q.bot.name)}</b><span class="today-job">${e(q.bot.job)}</span>${q.bot.last?.at ? `<time datetime="${e(q.bot.last.at)}">${e(elapsed(q.bot.last.at))} ago</time>` : ''}</header>
    <h2>${e(q.question)}</h2>${q.context ? `<p class="today-context">${e(q.context)}</p>` : ''}
    ${answer !== undefined ? `<p class="today-settled" role="status">Answered: ${e(answer)}</p>` : `<div class="today-actions">${options}<a href="${e(thread(q.bot))}">Answer in your own words</a></div>`}
    ${errors.has(q.card) ? `<p class="today-error" role="alert">${e(errors.get(q.card))}</p>` : ''}
  </article>`;
}

export function workingHTML(bots, boards, e) {
  const working = bots.filter((b) => b.state === 'working');
  const projects = boards.filter((b) => count(b.agents_working));
  return `<h2>Working now <span>${working.length}</span></h2>${working.length || projects.length ? `<ul class="today-working">${working.map((bot) => `<li><a href="${e(thread(bot))}">${avatar(bot, e)}<span class="today-work-text"><b>${e(bot.name)}</b><span>${e(bot.last?.text || bot.job || '')}</span></span>${bot.last?.at ? `<time datetime="${e(bot.last.at)}">${e(elapsed(bot.last.at))}</time>` : ''}</a></li>`).join('')}</ul>${projects.length ? `<ul class="today-projects">${projects.map((b) => `<li><a href="#/board/${e(enc(b.slug))}"><b>${e(b.name || b.slug)}</b><span>${count(b.agents_working)} agent${b.agents_working === 1 ? '' : 's'} working</span></a></li>`).join('')}</ul>` : ''}` : '<p class="today-empty">No agents are working right now.</p>'}`;
}

const styles = `
.deck-today { min-height:0; min-width:0; background:var(--bg); color:var(--text); font:400 14px/20px var(--font); }
.deck-today > .head, .deck-today > #phone-bar { display:none; }
.deck-today #content, .deck-today:is(#content) { padding:0; gap:0; overflow:hidden; }
.deck-today .today-view { flex:1; min-height:0; display:flex; flex-direction:column; }
.deck-today .today-scroll { flex:1; min-height:0; overflow:auto; overscroll-behavior:contain; }
.deck-today .today-column { width:min(1200px, calc(100% - 48px)); margin-inline:auto; }
.deck-today .today-page { display:flex; flex-direction:column; gap:20px; padding:24px 0 28px; }
.deck-today .today-title { display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
.deck-today h1 { font-size:24px; line-height:30px; font-weight:600; letter-spacing:-.01em; }
.deck-today .today-title p { color:var(--text-2); }
.deck-today .today-grid { display:grid; grid-template-columns:minmax(0,7fr) minmax(0,5fr); gap:14px; align-items:start; }
.deck-today .today-stack { display:flex; flex-direction:column; gap:14px; min-width:0; }
.deck-today .today-stack > h2, .deck-today .today-now > h2 { font-size:14px; line-height:20px; font-weight:500; color:var(--text-2); }
.deck-today .today-stack > h2 { padding:0 2px; color:var(--wait); }
.deck-today h2 > span { margin-left:6px; color:var(--text-3); }
.deck-today .today-stack > h2 > span { color:var(--wait); }
.deck-today .today-question { padding:22px; border-radius:20px; background:var(--surface); display:flex; flex-direction:column; gap:10px; overflow-wrap:anywhere; }
.deck-today .today-question header { display:flex; align-items:center; gap:10px; color:var(--text-3); font-size:13px; min-width:0; }
.deck-today .today-question header b { font-size:14px; font-weight:500; color:var(--text); }
.deck-today .today-job { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.deck-today time { flex:none; color:var(--text-3); font-size:13px; white-space:nowrap; }
.deck-today .today-question time { margin-left:auto; }
.deck-today .today-avatar { display:grid; place-items:center; flex:none; width:28px; height:28px; border-radius:9px; background:var(--well); color:var(--text); font-size:12px; font-weight:600; }
.deck-today .today-question h2 { font-size:20px; line-height:26px; font-weight:600; letter-spacing:-.01em; margin-top:4px; }
.deck-today .today-context { color:var(--text-2); margin-top:-6px; white-space:pre-wrap; }
.deck-today .today-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin-top:8px; }
.deck-today .today-option { min-height:40px; max-width:100%; padding:8px 18px; border:0; border-radius:999px; background:var(--accent-weak); color:var(--text); font-weight:500; overflow-wrap:anywhere; }
.deck-today .today-option.inverted { background:var(--text); color:var(--bg); min-width:104px; }
.deck-today .today-option:disabled { opacity:.5; cursor:wait; }
.deck-today .today-actions a { margin-left:auto; color:var(--text-2); font-size:13px; text-decoration:none; }
.deck-today .today-option:hover:not(:disabled), .deck-today .today-send:hover:not(:disabled) { opacity:.85; }
.deck-today .today-actions a:hover { text-decoration:underline; }
.deck-today .today-settled { color:var(--ok); padding:8px 0; }
.deck-today .today-error { color:var(--text-2); overflow-wrap:anywhere; }
.deck-today .today-now { background:var(--surface); border-radius:20px; padding:20px 22px; min-width:0; }
.deck-today .today-working, .deck-today .today-projects { list-style:none; margin:6px 0 0; padding:0; }
.deck-today .today-working a { display:flex; align-items:center; gap:12px; min-height:56px; color:inherit; text-decoration:none; }
.deck-today .today-working li + li { border-top:1px solid var(--line); }
.deck-today .today-work-text { flex:1; min-width:0; display:flex; flex-direction:column; }
.deck-today .today-work-text b, .deck-today .today-projects b { font-weight:500; }
.deck-today .today-work-text > span { color:var(--text-3); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.deck-today .today-projects a { display:flex; flex-wrap:wrap; justify-content:space-between; gap:4px 12px; min-height:44px; align-items:center; text-decoration:none; color:var(--text-2); overflow-wrap:anywhere; }
.deck-today .today-projects span { color:var(--text-3); font-size:13px; }
.deck-today .today-empty { padding:12px 0; color:var(--text-2); }
.deck-today .today-dock { flex:none; padding:10px 0 20px; background:var(--bg); }
.deck-today .today-compose { display:flex; align-items:center; gap:12px; min-height:56px; padding:6px 10px 6px 18px; border-radius:28px; background:var(--well); }
.deck-today .today-to { display:flex; align-items:center; gap:4px; flex:none; max-width:36%; min-height:32px; padding:0 8px 0 12px; border-radius:999px; background:var(--accent-weak); font-weight:500; }
.deck-today .today-to select { min-width:0; max-width:100%; min-height:32px; border:0; box-shadow:none; appearance:none; padding:0 2px; background:var(--accent-weak); color:var(--text); text-overflow:ellipsis; }
.deck-today .today-to svg { width:12px; height:12px; flex:none; }
.deck-today .today-to select:focus-visible { outline:2px solid var(--text); outline-offset:2px; }
.deck-today .today-compose input { flex:1; min-width:0; min-height:40px; padding:0; border:0; box-shadow:none; outline:none; background:none; font-size:16px; color:var(--text); }
.deck-today .today-compose input::placeholder { color:var(--text-3); }
.deck-today .today-compose:focus-within { outline:2px solid var(--line-strong); outline-offset:2px; }
.deck-today .today-send { display:grid; place-items:center; flex:none; width:38px; height:38px; padding:0; border:0; border-radius:50%; background:var(--text); color:var(--bg); }
.deck-today .today-send:disabled { opacity:.5; }
.deck-today .today-send svg { width:18px; height:18px; }
.deck-today .today-dock .today-error { padding:6px 18px 0; }
@media (max-width:699px) {
  .deck-today { height:100dvh; overflow:hidden; }
  .deck-today .today-column { width:calc(100% - 32px); }
  .deck-today .today-page { padding:18px 0 20px; gap:16px; }
  .deck-today .today-title { gap:4px 12px; }
  .deck-today .today-title p { flex-basis:100%; }
  .deck-today .today-grid { grid-template-columns:minmax(0,1fr); }
  .deck-today .today-question { padding:18px; }
  .deck-today .today-question h2 { font-size:19px; }
  .deck-today .today-option { flex:1 1 30%; min-height:46px; padding-inline:14px; }
  .deck-today .today-actions a { display:flex; align-items:center; justify-content:center; flex-basis:100%; min-height:44px; margin:0; text-align:center; }
  .deck-today .today-dock { padding:8px 0 10px; }
  .deck-today .today-compose { gap:8px; padding-left:12px; }
  .deck-today .today-to { padding-left:10px; min-height:44px; max-width:38%; }
  .deck-today .today-to select { min-height:44px; font-size:14px; }
  .deck-today .today-send { width:44px; height:44px; }
  .deck-today .today-compose input { min-height:44px; }
}
`;
let sheet;
function addStyles() {
  if (sheet || !globalThis.document || !globalThis.CSSStyleSheet) return;
  sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function mount(context) {
  addStyles();
  const { root, api, e } = context;
  // Scope the shell's header and content adjustments to this screen's lifetime too.
  const host = root.closest?.('.center') || root;
  host.classList.add('deck-today');
  root.innerHTML = `<div class="today-view"><div class="today-scroll"><div class="today-column today-page"><header class="today-title"><h1>Today</h1><p data-today="summary">Loading…</p></header><div class="today-grid"><section class="today-stack" aria-label="Waiting on you" data-today="waiting"></section><section class="today-now" aria-label="Working now" data-today="working"></section></div></div></div><div class="today-dock"><div class="today-column"><form class="today-compose"><label class="today-to">To <select name="bot" aria-label="Bot to tell" disabled></select><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 6 5 5 5-5"/></svg></label><input name="text" aria-label="What do you need?" placeholder="What do you need?" maxlength="8000" autocomplete="off" required><button type="submit" class="today-send" aria-label="Send" disabled><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13V3M4 7l4-4 4 4"/></svg></button></form><p class="today-error" data-today="draft-error" role="alert" hidden></p></div></div></div>`;
  const region = (name) => root.querySelector(`[data-today="${name}"]`);
  const form = root.querySelector('form'), picker = form.elements.bot;
  const s = { bots: [], questions: [], busy: new Set(), settled: new Map(), errors: new Map(), error: '', loaded: false };
  let ctx = context, active = true, generation = 0, readController, rosterSignature = '';
  const writes = new Set();
  function paint() {
    if (!active) return;
    const open = s.questions.filter((q) => !s.settled.has(key(q)));
    region('summary').textContent = s.loaded ? summary(open, s.bots, ctx.boards || []) : 'Loading…';
    region('waiting').innerHTML = `<h2>Waiting on you <span>${open.length}</span></h2>${s.error ? `<p class="today-error" role="alert">${e(s.error)} <button type="button" class="today-option" data-today-retry>Retry</button></p>` : ''}${s.questions.length ? s.questions.map((q) => questionHTML(q, s, e)).join('') : `<p class="today-empty">${s.error ? 'Questions are unavailable.' : s.loaded ? 'Nothing waits on you.' : 'Loading questions…'}</p>`}`;
    region('working').innerHTML = workingHTML(s.bots, ctx.boards || [], e);
    // Keep both controls alive: text, selection and focus never pass through a redraw.
    const signature = JSON.stringify(s.bots.map((b) => [b.id, b.name]));
    if (signature !== rosterSignature) {
      const previous = picker.value;
      picker.innerHTML = s.bots.map((b) => `<option value="${e(b.id)}">${e(b.name)}</option>`).join('');
      picker.value = s.bots.some((b) => b.id === previous) ? previous : (s.bots.find((b) => b.kind === 'project') || s.bots.find((b) => b.kind === 'general') || s.bots[0])?.id || '';
      rosterSignature = signature;
    }
    picker.disabled = !s.bots.length;
    form.querySelector('button').disabled = !s.bots.length;
  }
  async function refresh(next = {}) {
    if (!active) return;
    ctx = { ...ctx, ...next };
    readController?.abort();
    const controller = readController = new AbortController(), token = ++generation;
    const alive = () => active && generation === token;
    try {
      const roster = await api('/api/bots', { signal: controller.signal });
      if (!alive()) return;
      const bots = roster.bots || [];
      const waitingBots = bots.filter((b) => count(b.waiting));
      const pages = await Promise.allSettled(waitingBots.map(async (bot) => ({ bot, ...(await api(`/api/bots/${enc(bot.id)}/messages?limit=1`, { signal: controller.signal })) })));
      if (!alive()) return;
      const loaded = pages.filter((p) => p.status === 'fulfilled').map((p) => p.value);
      // A failed project read must not erase an already-visible question.
      const failed = waitingBots.filter((_, i) => pages[i].status === 'rejected');
      loaded.push(...failed.map((bot) => ({ bot, waiting: s.questions.filter((q) => q.bot.id === bot.id) })));
      s.bots = bots;
      s.questions = questionsFrom(loaded).filter((q) => !s.settled.has(key(q)));
      s.error = failed.length ? 'Could not load every question. Try again.' : '';
      s.loaded = true;
    } catch (error) {
      if (!alive()) return;
      s.error = error.message || 'Could not load Today. Try again.';
    }
    paint();
  }
  async function decide(n, choice) {
    const q = s.questions.find((q) => q.card === n);
    if (!active || !q || s.busy.has(n) || s.settled.has(key(q)) || !q.options?.includes(choice)) return;
    const controller = new AbortController();
    writes.add(controller); s.busy.add(n); s.errors.delete(n); paint();
    try {
      await api(`/api/cards/${n}/decide`, { method: 'POST', body: { version: Number(q.version), choice }, signal: controller.signal });
      if (active) s.settled.set(key(q), choice);
    } catch (error) {
      if (!active) return;
      s.errors.set(n, error.status === 409 ? 'That question changed. Showing the latest.' : error.message || 'Could not send your answer. Try again.');
      if (error.status === 409) await refresh();
    } finally {
      writes.delete(controller); s.busy.delete(n); paint();
    }
  }
  function onClick(event) {
    const button = event.target.closest?.('[data-today-decide]');
    if (button) { event.preventDefault(); void decide(Number(button.dataset.todayDecide), button.dataset.choice); }
    if (event.target.closest?.('[data-today-retry]')) void refresh();
  }
  function onSubmit(event) {
    if (event.target !== form) return;
    event.preventDefault();
    const text = form.elements.text.value, bot = picker.value;
    if (!text.trim() || text.length > 8000 || !s.bots.some((b) => b.id === bot)) return;
    try { globalThis.sessionStorage.setItem('cmmn.deck.draft', JSON.stringify({ bot, text })); }
    catch { region('draft-error').hidden = false; region('draft-error').textContent = 'Could not keep your message. Try again.'; return; }
    ctx.go(`#/bots/${enc(bot)}`);
  }
  root.addEventListener('click', onClick);
  root.addEventListener('submit', onSubmit);
  paint();
  void refresh();
  return {
    refresh,
    stop() {
      active = false; generation++; readController?.abort();
      for (const controller of writes) controller.abort();
      writes.clear();
      root.removeEventListener('click', onClick);
      root.removeEventListener('submit', onSubmit);
      host.classList.remove('deck-today');
    },
  };
}
