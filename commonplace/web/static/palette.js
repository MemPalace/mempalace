import { escape as e } from './api.js';
import { cardId } from './board.js';

const keyPreference = 'cmmn.keys.single';
const encode = encodeURIComponent;

// Command descriptors are dispatched by the shell; this module makes no API calls.
export function paletteItems({ board = null, boards = [], cards = [], selected = null, query = '' } = {}) {
  const commands = [];
  const add = (id, label, group, details = {}) => commands.push({ id, label, group, ...details });
  const currentCards = board ? cards.filter(card => !card.board || card.board === board.slug) : [];
  const selectedCard = typeof selected === 'object' ? selected : currentCards.find(card => card.number === selected);
  if (selectedCard && board && (!selectedCard.board || selectedCard.board === board.slug)) {
    const details = { number: selectedCard.number, board: board.slug };
    add('move-card', 'Move to…', 'Selected card', { ...details, action: 'move-card', key: 'M' });
    add('edit-card', 'Edit title', 'Selected card', { ...details, action: 'edit-card', key: 'E' });
    if (selectedCard.status === 'review' && selectedCard.ask && selectedCard.review_kind !== 'completion') {
      add('search-blocker', 'Search memory for this blocker', 'Selected card', { ...details, action: 'search-memory', query: selectedCard.ask, href: `#/memory?q=${encode(selectedCard.ask)}` });
    }
    add('copy-card-link', 'Copy link', 'Selected card', { ...details, action: 'copy-card-link', href: `#/card/${selectedCard.number}` });
  }
  if (board) {
    add('new-card', `New card in ${board.name || board.slug}`, 'Actions', { action: 'new-card', board: board.slug, key: 'C' });
    add('new-request', 'New request to the orchestrator', 'Actions', { action: 'open-chat', board: board.slug, key: '/' });
  }
  add('new-project', 'New project', 'Actions', { action: 'new-board' });
  for (const project of boards) add(`board-${project.slug}`, project.name || project.slug, 'Projects', { href: `#/board/${encode(project.slug)}`, board: project.slug });
  add('needs', 'Needs me', 'Go to', { href: '#/needs', key: 'G N' });
  add('memory', 'Memory', 'Go to', { href: '#/memory', key: 'G M' });
  if (board) {
    add('board', 'Go to board', 'Go to', { href: `#/board/${encode(board.slug)}`, key: 'G B' });
    add('space', 'Space', 'Go to', { href: `#/space/${encode(board.slug)}` });
    add('settings', 'Settings', 'Go to', { action: 'coordinator', board: board.slug });
  }
  add('rules', 'Rules', 'Workspace', { href: '#/rules' });
  add('console', 'Console', 'Workspace', { href: '#/console' });
  add('workspace-settings', 'Workspace settings', 'Workspace', { href: '#/settings', keywords: 'voice webhooks phone notifications plan' });
  add('decide', 'Decision queue', 'Workspace', { href: '#/decide' });
  add('shortcuts', 'Keyboard shortcuts', 'Workspace', { action: 'shortcuts', key: '?' });
  add('sign-out', 'Sign out', 'Workspace', { action: 'logout' });

  const term = String(query).trim().toLowerCase();
  const words = term.split(/\s+/).filter(Boolean);
  const matches = item => words.every(word => `${item.label} ${item.keywords || ''}`.toLowerCase().includes(word));
  const results = commands.filter(matches);
  const cardResults = [];
  for (const card of currentCards) {
    const id = cardId(board, card.number);
    const lookup = /^(?:([a-z])-?|#)?(\d+)$/i.exec(term);
    const idMatch = lookup && Number(lookup[2]) === card.number && (!lookup[1] || lookup[1].toUpperCase() === id[0]);
    const item = { id: `card-${card.number}`, label: `${id} ${card.title}`, group: 'Cards', href: `#/card/${card.number}`, number: card.number, board: board.slug, detail: card.status };
    if (!term || idMatch || (!lookup && matches(item))) cardResults.push(item);
  }
  const navigation = results.findIndex(item => ['Projects', 'Go to', 'Workspace'].includes(item.group));
  results.splice(navigation < 0 ? results.length : navigation, 0, ...cardResults);
  if (term) results.push({ id: 'search-memory', label: `Search memory for “${String(query).trim()}”`, group: 'Go to', action: 'search-memory', query: String(query).trim(), href: `#/memory?q=${encode(String(query).trim())}`, key: 'G M' });
  return results;
}

// Scoped styles travel with the component so no shell stylesheet needs changing.
const styles = `
  dialog.workbench-palette, #shell-dialog.workbench-palette {
    padding:0; width:640px; max-width:calc(100vw - 32px); max-height:calc(100dvh - 112px); margin:96px auto 0;
    border:0; border-radius:12px; background:var(--surface); color:var(--text);
    box-shadow:0 24px 60px rgba(0,0,0,.28); overflow:auto;
  }
  dialog.workbench-palette::backdrop { background:rgba(23,24,28,.42); }
  .workbench-palette .pin { display:flex; align-items:center; gap:10px; padding:0 16px; min-height:52px; border-bottom:1px solid var(--line); }
  .workbench-palette .pin:focus-within { box-shadow:inset 0 0 0 3px var(--accent); border-radius:12px 12px 0 0; }
  .workbench-palette .pin input { flex:1; width:100%; min-width:0; min-height:44px; padding:0; border:0; box-shadow:none; outline:none; background:transparent; font-size:15px; }
  .workbench-palette .scope { font-size:12px; padding:2px 8px; border-radius:10px; background:var(--well); color:var(--text-2); }
  .workbench-palette .pl { list-style:none; margin:0; padding:6px; max-height:min(470px,60dvh); overflow:auto; }
  .workbench-palette .gh { font-size:11.5px; font-weight:650; color:var(--text-3); letter-spacing:.04em; text-transform:uppercase; padding:10px 10px 4px; }
  .workbench-palette .opt { display:flex; align-items:center; gap:10px; width:100%; min-height:38px; padding:6px 10px; border:0; border-radius:6px; box-shadow:none; background:transparent; color:var(--text); font-weight:400; font-size:14px; text-align:left; cursor:pointer; }
  .workbench-palette .opt:hover { box-shadow:none; background:var(--pane); }
  .workbench-palette .i { width:16px; height:16px; flex:none; color:var(--text-2); }
  .workbench-palette .opt[aria-selected="true"] .i { color:var(--accent); }
  .workbench-palette .opt .label { min-width:0; overflow-wrap:anywhere; }
  .workbench-palette .opt .r { margin-left:auto; display:flex; gap:4px; align-items:center; color:var(--text-3); font-size:12.5px; }
  .workbench-palette .opt[aria-selected="true"] { background:var(--accent-weak); box-shadow:inset 3px 0 0 var(--accent); }
  .workbench-palette .empty { padding:12px; color:var(--text-2); }
  .workbench-palette .pfoot { display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:8px 14px; border-top:1px solid var(--line); background:var(--pane); font-size:12px; color:var(--text-3); }
  .workbench-palette .pfoot .note { flex:1; }
  .workbench-palette .shortcut-body { display:flex; flex-direction:column; max-height:calc(100dvh - 112px); padding:0; }
  .workbench-palette .shortcut-head { flex:none; padding:24px 24px 0; }
  .workbench-palette .shortcut-scroll { flex:1; min-height:0; overflow:auto; padding:0 24px; }
  .workbench-palette .shortcut-body .acts { flex:none; margin:0; padding:12px 24px 16px; border-top:1px solid var(--line); background:var(--surface); }
  .workbench-palette h2 { font-size:20px; margin:0 0 16px; }
  .workbench-palette .key-setting { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px; border-radius:8px; background:var(--pane); border:1px solid var(--line); }
  .workbench-palette .key-help, .workbench-palette .key-note { font-size:13px; color:var(--text-2); }
  .workbench-palette .key-note { margin:12px 0; }
  .workbench-palette .switch { display:inline-flex; align-items:center; gap:10px; min-height:32px; border:0; background:none; padding:0; font-weight:600; }
  .workbench-palette .track { width:40px; height:24px; border-radius:12px; background:var(--accent); position:relative; flex:none; }
  .workbench-palette .track::after { content:""; position:absolute; top:3px; left:19px; width:18px; height:18px; border-radius:50%; background:#FEFEFC; }
  .workbench-palette [aria-checked="false"] .track { background:var(--text-3); }
  .workbench-palette [aria-checked="false"] .track::after { left:3px; }
  .workbench-palette .state { font-weight:600; color:var(--text-2); }
  .workbench-palette .kt { width:100%; border-collapse:collapse; font-size:13px; }
  .workbench-palette .kt th { text-align:left; font-size:12px; color:var(--text-3); font-weight:600; padding:4px 6px; border-bottom:1px solid var(--line); }
  .workbench-palette .kt td { padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:middle; }
  .workbench-palette .acts { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
  .workbench-palette .kt .always { text-align:right; color:var(--text-3); white-space:nowrap; }
  .workbench-palette .kt td:first-child { white-space:nowrap; }
  @media (max-width:699px) {
    dialog.workbench-palette, #shell-dialog.workbench-palette { margin:16px auto 0; max-height:calc(100dvh - 32px); }
    .workbench-palette .opt, .workbench-palette .switch, .workbench-palette .btn { min-height:44px; }
    .workbench-palette .shortcut-body { max-height:calc(100dvh - 32px); }
    .workbench-palette .shortcut-head { padding:16px 16px 0; }
    .workbench-palette .shortcut-scroll { padding:0 16px; }
    .workbench-palette .shortcut-body .acts { padding:12px 16px; }
    .workbench-palette .scope { display:none; }
    .workbench-palette .kt td { padding:8px 4px; }
  }
`;
// The app's CSP is style-src 'self', which refuses inline style elements: adopt a constructed sheet once.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

const icons = {
  card: '<rect x="2.5" y="3" width="11" height="10" rx="1.5"/><path d="M5 6.5h6M5 9.5h3.5"/>',
  add: '<path d="M8 3v10M3 8h10"/>',
  chat: '<path d="M2.5 3.5h11v7H7.2L4.3 13v-2.5H2.5z"/>',
  folder: '<path d="M2.5 4.5h4l1.2 1.5h5.8v6.5h-11z"/>',
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>',
  move: '<path d="M3 8h10M9 4l4 4-4 4"/>',
  edit: '<path d="M3 13h2.5L13 5.5 10.5 3 3 10.5zM9 4.5l2.5 2.5"/>',
  link: '<path d="M7 9a2.5 2.5 0 0 0 3.5 0l2-2A2.5 2.5 0 0 0 9 3.5l-.5.5M9 7a2.5 2.5 0 0 0-3.5 0l-2 2A2.5 2.5 0 0 0 7 12.5l.5-.5"/>',
};
function icon(name) {
  return `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.move}</svg>`;
}
function itemIcon(item) {
  if (item.group === 'Cards') return icon('card');
  const names = { 'new-card': 'add', 'new-project': 'folder', 'new-request': 'chat', 'move-card': 'move', 'edit-card': 'edit', 'copy-card-link': 'link', memory: 'search', 'search-blocker': 'search', 'search-memory': 'search' };
  return icon(names[item.id] || (item.group === 'Projects' ? 'folder' : 'move'));
}

const shortcutRows = [
  ['⌘K / Ctrl K', 'Search or do (palette)', true],
  ['C', 'New card'], ['/', 'Open the chat and put the cursor in it'],
  ['G then B / N / M', 'Go to board / Needs me / memory'],
  ['J / K', 'Next / previous item'], ['Enter', 'Open selected card'],
  ['E', 'Edit the selected field'], ['M', 'Move card to…'],
  ['R', 'Ask a question (Needs me)'], ['Y / L', 'Copy exact words / link to card (memory notes)'],
  ['Z', 'Stop a pending send (while shown)'], [']', 'Close the chat, or hide the side panel'],
  ['?', 'Keyboard shortcuts'], ['Esc', 'Close a dialog, the palette, or the chat', true],
];

export function createPalette({ dialog, onCommand, onSingleKeysChange = () => {}, storage } = {}) {
  let context = {}, results = [], active = 0, mode = '', opener = null, single = null;
  const previousLabel = dialog.getAttribute('aria-label');
  function singleKeys() {
    if (single !== null) return single;
    try { return (storage || globalThis.localStorage)?.getItem(keyPreference) !== 'false'; }
    catch { return true; }
  }
  function setSingleKeys(enabled) {
    single = enabled;
    try { (storage || globalThis.localStorage)?.setItem(keyPreference, String(enabled)); }
    catch { /* The in-memory switch still works for this visit. */ }
    const toggle = dialog.querySelector('[role="switch"]');
    toggle.setAttribute('aria-checked', String(enabled));
    dialog.querySelector('.state').textContent = enabled ? 'On' : 'Off';
    onSingleKeysChange(enabled);
  }
  // The close event can arrive after a command moved focus (new card, composer): only restore focus nobody claimed.
  function restoreFocus() {
    const active = document.activeElement;
    if (opener?.isConnected && (!active || active === document.body || dialog.contains(active))) opener.focus();
  }
  function close() { if (dialog.open) dialog.close(); }
  function show(label) {
    if (!dialog.open) opener = document.activeElement;
    dialog.classList.add('workbench-palette');
    dialog.setAttribute('aria-label', label);
    dialog.setAttribute('aria-modal', 'true');
    if (!dialog.open) dialog.showModal();
  }
  function select(index) {
    active = results.length ? (index + results.length) % results.length : 0;
    const input = dialog.querySelector('[role="combobox"]');
    if (!results.length) { input.removeAttribute('aria-activedescendant'); return; }
    input.setAttribute('aria-activedescendant', `palette-option-${active}`);
    dialog.querySelectorAll('[data-palette-index]').forEach(node => node.setAttribute('aria-selected', String(Number(node.dataset.paletteIndex) === active)));
    dialog.querySelector(`[data-palette-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }
  function renderResults(query) {
    results = paletteItems({ ...context, query });
    let group = '';
    dialog.querySelector('[role="listbox"]').innerHTML = results.map((item, index) => {
      const heading = item.group !== group ? `<li class="gh" role="presentation">${e(item.group)}</li>` : '';
      group = item.group;
      return `${heading}<li class="opt" role="option" id="palette-option-${index}" data-palette-index="${index}" aria-selected="${index === 0}">${itemIcon(item)}<span class="label">${e(item.label)}</span><span class="r">${e(item.detail || '')}${item.key ? `<kbd>${e(item.key)}</kbd>` : ''}</span></li>`;
    }).join('') || '<li class="empty" role="presentation">No results</li>';
    select(0);
  }
  function open(options = {}) {
    context = options; mode = 'commands';
    show('Command palette');
    dialog.innerHTML = `<div class="pin">${icon('search')}<input type="text" role="combobox" aria-label="Search or run an action" aria-expanded="true" aria-controls="palette-results" aria-autocomplete="list" autocomplete="off" spellcheck="false"><span class="scope">${context.board ? `in ${e(context.board.name || context.board.slug)}` : 'Workspace'}</span></div><ul class="pl" id="palette-results" role="listbox" aria-label="Results"></ul><div class="pfoot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> run</span><span class="note">Every action except Approve and Decline, which stay in Needs me</span><button class="btn ghost sm" data-palette-action="close">Close <kbd>Esc</kbd></button></div>`;
    const input = dialog.querySelector('[role="combobox"]');
    input.value = options.query || '';
    renderResults(input.value); input.focus();
  }
  function shortcuts() {
    mode = 'shortcuts'; show('Keyboard shortcuts');
    const enabled = singleKeys();
    dialog.innerHTML = `<section class="shortcut-body"><div class="shortcut-head"><h2>Keyboard shortcuts</h2></div><div class="shortcut-scroll"><div class="key-setting"><span><b id="palette-single-label">Single-key shortcuts</b><br><span class="key-help">Turn off if you use voice control or a screen reader that sends letters. ⌘K / Ctrl K and Esc keep working.</span></span><button class="switch" data-palette-action="single-keys" role="switch" aria-checked="${enabled}" aria-labelledby="palette-single-label"><span class="track" aria-hidden="true"></span><span class="state">${enabled ? 'On' : 'Off'}</span></button></div><p class="key-note">Single keys never fire while you type in a field. <b>Approve and Decline have no shortcut</b>; they always need the button.</p><table class="kt"><caption class="sr">Shortcuts</caption><thead><tr><th scope="col">Key</th><th scope="col">Action</th><th scope="col"><span class="sr">Setting</span></th></tr></thead><tbody>${shortcutRows.map(([key, action, always]) => `<tr><td><kbd>${e(key)}</kbd></td><td>${e(action)}</td><td class="always">${always ? 'Always on' : ''}</td></tr>`).join('')}</tbody></table></div><div class="acts"><button class="btn" data-palette-action="reset">Reset to defaults</button><button class="btn primary" data-palette-action="done">Done <kbd>Esc</kbd></button></div></section>`;
    dialog.querySelector('[role="switch"]').focus();
  }
  function run(index) {
    const item = results[index];
    if (!item) return;
    if (item.action === 'shortcuts') { shortcuts(); return; }
    close(); onCommand(item);
  }
  function click(event) {
    const option = event.target.closest('[data-palette-index]');
    const action = event.target.closest('[data-palette-action]')?.dataset.paletteAction;
    if (option && mode === 'commands') run(Number(option.dataset.paletteIndex));
    else if (action === 'single-keys') setSingleKeys(!singleKeys());
    else if (action === 'reset') setSingleKeys(true);
    else if (action === 'close' || action === 'done') close();
  }
  function input(event) { if (mode === 'commands' && event.target === dialog.querySelector('[role="combobox"]')) renderResults(event.target.value); }
  function keydown(event) {
    if (event.isComposing) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
    if (event.key === 'Tab') {
      const nodes = [...dialog.querySelectorAll('input,button,a[href],[tabindex="0"]')].filter(node => !node.disabled && !node.hidden);
      const index = nodes.indexOf(document.activeElement);
      if (nodes.length && ((event.shiftKey && index <= 0) || (!event.shiftKey && index === nodes.length - 1))) {
        event.preventDefault(); nodes[event.shiftKey ? nodes.length - 1 : 0].focus();
      }
      return;
    }
    if (mode !== 'commands' || event.target !== dialog.querySelector('[role="combobox"]')) return;
    if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'Enter') run(active);
      else select(event.key === 'Home' ? 0 : event.key === 'End' ? results.length - 1 : active + (event.key === 'ArrowDown' ? 1 : -1));
    }
  }
  function cancel(event) { event.preventDefault(); close(); }
  const listeners = { click, input, keydown, cancel, close: restoreFocus };
  for (const [type, handler] of Object.entries(listeners)) dialog.addEventListener(type, handler);
  return { open, shortcuts, close, stop() {
    close();
    for (const [type, handler] of Object.entries(listeners)) dialog.removeEventListener(type, handler);
    dialog.classList.remove('workbench-palette');
    if (previousLabel === null) dialog.removeAttribute('aria-label'); else dialog.setAttribute('aria-label', previousLabel);
    dialog.removeAttribute('aria-modal');
  } };
}
