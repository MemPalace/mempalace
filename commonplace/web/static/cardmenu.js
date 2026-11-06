import { api as request, escape as e } from './api.js';
import { cardId } from './board.js';

const columns = ['todo', 'doing', 'review', 'done'];
const label = (value) => value[0].toUpperCase() + value.slice(1);
const icon = (path) => `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
const arrow = icon('<path d="M3 8h10M9 4l4 4-4 4"/>');
const check = icon('<path d="m3 8.5 3 3 7-7"/>');
const styles = `
.card-menu { --menu-paper:#FEFEFC; position:fixed; inset:auto 0 0; margin:0; width:100%; max-width:none; max-height:calc(100dvh - 16px); overflow:auto; border:0; border-radius:16px 16px 0 0; padding:10px 16px max(24px,env(safe-area-inset-bottom)); background:var(--surface,var(--menu-paper)); color:var(--text); box-shadow:0 -12px 32px rgba(0,0,0,.2); }
.card-menu::backdrop { background:rgba(23,24,28,.42); }
.card-menu .menu-body { display:flex; flex-direction:column; gap:12px; }
.card-menu .grab { width:40px; height:4px; border-radius:2px; background:var(--line-strong); align-self:center; }
.card-menu h2 { font-size:18px; line-height:24px; font-weight:700; overflow-wrap:anywhere; }
.card-menu .move-label { font-size:13px; font-weight:650; color:var(--text-3); margin-bottom:-6px; }
.card-menu .opt { display:flex; align-items:center; gap:12px; min-height:48px; padding:0 4px; border:0; border-bottom:1px solid var(--line); background:none; font-size:16px; width:100%; text-align:left; }
.card-menu .opt .i { width:20px; height:20px; color:var(--text-2); }
.card-menu .opt[aria-current] { font-weight:650; }
.card-menu .sub { font-size:13px; color:var(--text-3); margin-left:auto; }
.card-menu .opt:hover { background:var(--well); }
.card-menu button:disabled { cursor:default; opacity:.6; }
.card-menu button:focus-visible,.card-menu input:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
.card-menu .menu-close { min-height:48px; justify-content:center; font-size:16px; }
.card-menu form,.card-menu label { display:grid; gap:12px; }
.card-menu input { width:100%; min-height:48px; font-size:16px; }
.card-menu [data-menu-error]:empty { display:none; }
`;
// The app's CSP is style-src 'self', which refuses inline style elements: adopt a constructed sheet once.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function renderCardMenu(card, { board = {}, editing = false } = {}) {
  const id = cardId(board, card.number), proposal = card.proposal_status === 'pending';
  const actions = `<p class="move-label">MOVE TO</p><div role="group" aria-label="Move to column">${columns.map((status) => `<button type="button" class="opt" data-menu-move="${status}"${status === card.status ? ' aria-current="true"' : ''}${proposal ? ' disabled' : ''}>${status === card.status ? check : arrow}${label(status)}${status === card.status ? '<span class="sub">now here</span>' : ''}</button>`).join('')}</div><div role="group" aria-label="Other actions"><button type="button" class="opt" data-menu-action="edit">${icon('<path d="M3 13h2.5L13 5.5 10.5 3 3 10.5z"/><path d="m9 4.5 2.5 2.5"/>')}Edit title</button><button type="button" class="opt" data-menu-action="copy">${icon('<path d="M7 9a2.5 2.5 0 0 0 3.5 0l2-2A2.5 2.5 0 0 0 9 3.5l-.5.5"/><path d="M9 7a2.5 2.5 0 0 0-3.5 0l-2 2A2.5 2.5 0 0 0 7 12.5l.5-.5"/>')}Copy link</button></div>`;
  return `<dialog class="sheet menu-sheet card-menu" aria-modal="true" aria-labelledby="cm-h" data-return-focus="more-${e(id)}"><div class="menu-body"><span class="grab" aria-hidden="true"></span><h2 id="cm-h">${e(id)} ${e(card.title)}</h2>${editing ? `<form data-menu-title><label>Title<input name="title" value="${e(card.title)}" maxlength="5000" required></label><button class="btn primary" type="submit">Save title</button><button class="btn" type="button" data-menu-action="cancel-edit">Cancel</button></form>` : actions}<p role="alert" data-menu-error></p><button type="button" class="btn menu-close" data-menu-action="close">Close</button></div></dialog>`;
}

/** Mount outside the shell's modal regions. onChange refreshes the board before focus returns.
 * onReview opens the shell's existing question/options form; it owns the eventual review PATCH.
 */
export function createCardMenu({ mount, api = request, onChange = () => {}, onReview,
  clipboard = globalThis.navigator?.clipboard, href = () => globalThis.location.href }) {
  const doc = mount.ownerDocument;
  let card, settings, dialog, opener, busy = false, generation = 0;
  const controls = () => [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled)')];
  const error = (text) => { dialog.querySelector('[data-menu-error]').textContent = text; };
  function close() {
    if (!dialog) return;
    generation++;
    const target = doc.getElementById(`more-${cardId(settings.board || {}, card.number)}`) || opener;
    dialog.close();
    dialog = null;
    mount.innerHTML = '';
    busy = false;
    target?.focus();
  }
  function paint(editing = false) {
    dialog?.close();
    mount.innerHTML = renderCardMenu(card, { ...settings, editing });
    dialog = mount.querySelector('dialog');
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); event.stopPropagation(); close(); });
    dialog.showModal();
    (editing ? dialog.querySelector('input') : controls()[0])?.focus();
  }
  function show(value, options = {}) {
    if (dialog) close();
    generation++;
    card = { ...value };
    settings = options;
    opener = options.from || doc.activeElement;
    paint();
  }
  async function run(action, failure) {
    if (busy || !dialog) return;
    busy = true;
    const current = generation;
    const disabled = [...dialog.querySelectorAll('button, input')].map((el) => [el, el.disabled]);
    disabled.forEach(([el]) => { el.disabled = true; });
    // Keep one focusable escape route while the request is in flight.
    dialog.querySelector('[data-menu-action="close"]').disabled = false;
    error('');
    try {
      const result = await action();
      if (current !== generation) return;
      if (result !== undefined) await onChange(result);
      if (current === generation) close();
    } catch (err) {
      if (current === generation) error(err.status === 409 ? 'This card changed. Close and reopen its actions before trying again.' : failure);
    } finally {
      if (current === generation) {
        busy = false;
        disabled.forEach(([el, was]) => { el.disabled = was; });
      }
    }
  }
  async function click(event) {
    const button = event.target.closest('[data-menu-move], [data-menu-action]');
    if (!button || !dialog || button.disabled) return;
    event.stopPropagation();
    const action = button.dataset.menuAction, status = button.dataset.menuMove;
    if (action === 'close') { close(); return; }
    if (busy) return;
    if (status) {
      if (!columns.includes(status) || card.proposal_status === 'pending') return;
      if (status === card.status) { close(); return; }
      if (status === 'review') {
        if (!onReview) { error('Open card details to request a review.'); return; }
        const selected = card;
        close();
        onReview(selected);
        return;
      }
      await run(() => api(`/api/cards/${card.number}`, { method:'PATCH', body:{ status, version:card.version } }), 'Could not move this card. Try again.');
    } else if (action === 'edit') paint(true);
    else if (action === 'cancel-edit') { paint(); dialog.querySelector('[data-menu-action="edit"]').focus(); }
    else if (action === 'copy') await run(async () => {
      const url = new URL(href());
      url.search = '';
      url.hash = `#/card/${card.number}`;
      await clipboard.writeText(url.href);
    }, 'Could not copy the link. Try again.');
  }
  async function submit(event) {
    if (!event.target.matches('[data-menu-title]') || !dialog) return;
    event.preventDefault();
    event.stopPropagation();
    const input = dialog.querySelector('input');
    if (!input.value.trim()) { error('Enter a title.'); input.focus(); return; }
    const title = input.value;
    await run(() => api(`/api/cards/${card.number}`, { method:'PATCH', body:{ title, version:card.version } }), 'Could not save the title. Your words are kept here. Try again.');
  }
  function keydown(event) {
    if (!dialog) return;
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    else if (event.key === 'Tab') {
      const list = controls(), index = list.indexOf(doc.activeElement);
      if (event.shiftKey ? index <= 0 : index < 0 || index === list.length - 1) {
        event.preventDefault();
        list[event.shiftKey ? list.length - 1 : 0]?.focus();
      }
    }
  }
  mount.addEventListener('click', click);
  mount.addEventListener('submit', submit);
  mount.addEventListener('keydown', keydown);
  return { show, close, stop() {
    close();
    mount.removeEventListener('click', click);
    mount.removeEventListener('submit', submit);
    mount.removeEventListener('keydown', keydown);
  } };
}
