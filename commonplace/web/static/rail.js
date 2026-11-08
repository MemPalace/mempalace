import { escape as e } from './api.js';

const enc = encodeURIComponent;
const count = (n) => Number.isSafeInteger(n) && n > 0 ? n : 0;
const current = (yes) => yes ? ' aria-current="page"' : '';
const paths = {
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>',
  needs: '<path d="M2 9h3.5l1 2h3l1-2H14M3.8 3h8.4L14 9v4H2V9z"/>',
  memory: '<path d="M3.5 2.5h6.5L12.5 5v8.5h-9zM5.8 7h4.4M5.8 9.5h4.4M5.8 12h2.4"/>',
  board: '<rect x="2" y="2.5" width="3.4" height="11" rx=".6"/><rect x="6.3" y="2.5" width="3.4" height="7.5" rx=".6"/><rect x="10.6" y="2.5" width="3.4" height="9" rx=".6"/>',
  chat: '<path d="M2.5 3.5h11v7H7.2L4.3 13v-2.5H2.5z"/>',
  settings: '<path d="M2 4.5h7M12.5 4.5H14M2 11.5h1.5M6.5 11.5H14"/><circle cx="10.8" cy="4.5" r="1.6"/><circle cx="5" cy="11.5" r="1.6"/>',
  keys: '<rect x="1.5" y="4" width="13" height="8" rx="1.5"/><path d="M4 7h.1M6.5 7h.1M9 7h.1M11.5 7h.1M5 9.5h6"/>',
  chevron: '<path d="m4.5 6 3.5 3.5L11.5 6"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
const badge = (n, label) => count(n) ? `<span class="count need">${n}<span class="sr"> ${label}</span></span>` : '';
const boardHref = (board) => board?.slug ? `#/board/${enc(board.slug)}` : '#/start';

export function projectSubline(board) {
  const agents = count(board.agents_working);
  const doing = count(board.counts?.doing);
  return agents ? `${agents} agent${agents === 1 ? '' : 's'} working` : doing ? `${doing} doing` : 'Idle';
}

// Pure markup: the shell delegates data-action clicks and owns navigation/state.
// needsByBoard contains attention counts, never the whole Review column count.
// Rail styles live in workbench.css, so re-rendering the rail never adds a stylesheet.
export function renderRail({ workspace = 'Commonplace', user = '', boards = [], board = null, view = 'board', needs = 0, needsByBoard = {}, rulesWaiting = 0, can_admin = false } = {}) {
  return `<nav class="rail" aria-label="Workspace">
    <button class="ws" data-action="switch-workspace" aria-label="Workspace: ${e(workspace)}. Switch workspace"><span class="mark" aria-hidden="true">${e(Array.from(workspace)[0] || 'C')}</span>${e(workspace)}${icon('chevron')}</button>
    <button class="cmdk" id="cmdk" data-action="palette" aria-keyshortcuts="Control+K Meta+K">${icon('search')}<span>Search or do…</span><kbd>⌘/Ctrl K</kbd></button>
    <a class="nav" href="#/needs" data-nav="needs"${current(view === 'needs')}>${icon('needs')}Needs me ${badge(needs, 'items')}</a>
    <a class="nav" href="#/memory" data-nav="memory"${current(view === 'memory')}>${icon('memory')}Memory</a>
    <div class="rail-h"><span id="ph">Projects</span><button class="iconbtn" id="new-project" data-action="new-board" aria-label="New project">+</button></div>
    <div role="list" aria-labelledby="ph">${boards.map((b) => `<div role="listitem"><a class="proj" href="${boardHref(b)}"${current(['board', 'chat', 'settings', 'space'].includes(view) && b.slug === board?.slug)}><span class="sw" aria-hidden="true"></span><span class="pn"><b>${e(b.name || b.slug)}</b><small>${projectSubline(b)}</small></span>${badge(needsByBoard[b.slug], 'need you')}</a></div>`).join('')}</div>
    <div class="rail-foot"><button class="nav" id="shortcuts-link" data-action="shortcuts">${icon('keys')}Shortcuts <kbd>?</kbd></button>
    <details class="user-menu"><summary class="me"><span class="mark" aria-hidden="true">${e(Array.from(user)[0] || '?')}</span>${e(user || 'Account')}${badge(rulesWaiting, 'rules waiting')}</summary><div class="user-links"><a href="#/rules">Rules${badge(rulesWaiting, 'waiting')}</a><a href="#/console">Console</a><a href="#/settings">Workspace settings</a>${can_admin === true ? '<a href="#/admin">Admin</a>' : ''}<button data-action="logout">Sign out</button></div></details></div>
  </nav>`;
}

export function renderPhoneBar({ board = null } = {}) {
  const name = board?.name || board?.slug || 'Projects';
  return `<header class="m-top"><button class="projbtn" data-action="switch-project" aria-label="Project: ${e(name)}. Switch project"><span class="sw" aria-hidden="true"></span>${e(name)}${icon('chevron')}</button><span class="spacer"></span><button class="iconbtn" data-action="palette" aria-label="Search or do (command palette)">${icon('search')}</button>${board?.slug ? `<button class="btn m-settings" id="m-settings" data-action="coordinator" data-board="${e(board.slug)}" aria-label="Settings">${icon('settings')}Settings</button>` : ''}</header>`;
}

export function renderPhoneTabs({ board = null, view = 'board', needs = 0, unread = 0 } = {}) {
  const n = count(unread);
  const name = board?.name || board?.slug || 'Project';
  const chatLabel = view === 'needs' ? `${name} chat` : 'Chat';
  const newLabel = n ? `${n} new repl${n === 1 ? 'y' : 'ies'}` : '';
  return `<nav class="m-tabs" aria-label="Main"><a href="${boardHref(board)}" data-nav="boards"${current(view === 'board')}>${icon('board')}Board</a><a href="${board?.slug ? `${boardHref(board)}/chat` : '#/start'}" id="m-chat-tab" data-nav="chat" aria-haspopup="dialog" aria-label="${e(chatLabel)}${n ? ` ${newLabel}` : ''}"${current(view === 'chat')}>${icon('chat')}${e(chatLabel)}${n ? `<span class="badge newb">${n} new<span class="sr"> repl${n === 1 ? 'y' : 'ies'}</span></span>` : ''}</a><a href="#/needs" data-nav="needs"${current(view === 'needs')}>${icon('needs')}Needs me${badge(needs, 'items')}</a><span class="sr" role="status">${n ? `${newLabel} in ${e(name)} chat` : ''}</span></nav>`;
}
