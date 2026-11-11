import { escape as e } from './api.js';

const count = (n) => Number.isSafeInteger(n) && n > 0 ? n : 0;
const current = (yes) => yes ? ' aria-current="page"' : '';
const paths = {
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>',
  memory: '<path d="M3.5 2.5h6.5L12.5 5v8.5h-9zM5.8 7h4.4M5.8 9.5h4.4M5.8 12h2.4"/>',
  bots: '<rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M8 5V2.8M5.8 9h.1M10.2 9h.1"/>',
  settings: '<path d="M2 4.5h7M12.5 4.5H14M2 11.5h1.5M6.5 11.5H14"/><circle cx="10.8" cy="4.5" r="1.6"/><circle cx="5" cy="11.5" r="1.6"/>',
  keys: '<rect x="1.5" y="4" width="13" height="8" rx="1.5"/><path d="M4 7h.1M6.5 7h.1M9 7h.1M11.5 7h.1M5 9.5h6"/>',
  chevron: '<path d="m4.5 6 3.5 3.5L11.5 6"/>',
  menu: '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
const badge = (n, label) => count(n) ? `<span class="count need">${n}<span class="sr"> ${label}</span></span>` : '';

export function projectSubline(board) {
  const agents = count(board.agents_working);
  const doing = count(board.counts?.doing);
  return agents ? `${agents} agent${agents === 1 ? '' : 's'} working` : doing ? `${doing} doing` : 'Idle';
}

// The Workspace group of the account menu: the screens people only visit to set things up.
// Shared by the desktop rail and the phone top bar so both list the same entries.
function menuLinks({ needs = 0, rulesWaiting = 0, can_admin = false } = {}) {
  return `<div class="menu-group" role="group" aria-label="Workspace"><span class="menu-h" aria-hidden="true">Workspace</span><a href="#/boards">Projects</a><a href="#/needs">Needs me${badge(needs, 'items')}</a><a href="#/console">Console</a></div><a href="#/rules">Rules${badge(rulesWaiting, 'waiting')}</a><a href="#/settings">Workspace settings</a>${can_admin === true ? '<a href="#/admin">Admin</a>' : ''}<button data-action="logout">Sign out</button>`;
}

// Pure markup: the shell delegates data-action clicks and owns navigation/state.
// The rail carries Bots and Memory only; botsWaiting is the one count (questions waiting on the person).
// Rail styles live in workbench.css, so re-rendering the rail never adds a stylesheet.
export function renderRail({ workspace = 'Commonplace', user = '', view = 'board', needs = 0, rulesWaiting = 0, botsWaiting = 0, can_admin = false } = {}) {
  return `<nav class="rail" aria-label="Workspace">
    <button class="ws" data-action="switch-workspace" aria-label="Workspace: ${e(workspace)}. Switch workspace"><span class="mark" aria-hidden="true">${e(Array.from(workspace)[0] || 'C')}</span>${e(workspace)}${icon('chevron')}</button>
    <button class="cmdk" id="cmdk" data-action="palette" aria-keyshortcuts="Control+K Meta+K">${icon('search')}<span>Search or do…</span><kbd>⌘/Ctrl K</kbd></button>
    <a class="nav" href="#/bots" data-nav="bots"${current(view === 'bots')}>${icon('bots')}Bots ${badge(botsWaiting, 'waiting on you')}</a>
    <a class="nav" href="#/memory" data-nav="memory"${current(view === 'memory')}>${icon('memory')}Memory</a>
    <div class="rail-foot"><button class="nav" id="shortcuts-link" data-action="shortcuts">${icon('keys')}Shortcuts <kbd>?</kbd></button>
    <details class="user-menu"><summary class="me"><span class="mark" aria-hidden="true">${e(Array.from(user)[0] || '?')}</span>${e(user || 'Account')}${badge(rulesWaiting, 'rules waiting')}</summary><div class="user-links">${menuLinks({ needs, rulesWaiting, can_admin })}</div></details></div>
  </nav>`;
}

// board is passed only on project screens, where the bar names the project and offers its settings.
export function renderPhoneBar({ board = null, needs = 0, rulesWaiting = 0, can_admin = false } = {}) {
  const name = board?.name || board?.slug || 'Projects';
  return `<header class="m-top"><details class="m-menu"><summary class="iconbtn" aria-label="Menu">${icon('menu')}</summary><div class="user-links">${menuLinks({ needs, rulesWaiting, can_admin })}</div></details>${board?.slug ? `<button class="projbtn" data-action="switch-project" aria-label="Project: ${e(name)}. Switch project"><span class="sw" aria-hidden="true"></span>${e(name)}${icon('chevron')}</button>` : ''}<span class="spacer"></span><button class="iconbtn" data-action="palette" aria-label="Search or do (command palette)">${icon('search')}</button>${board?.slug ? `<button class="btn m-settings" id="m-settings" data-action="coordinator" data-board="${e(board.slug)}" aria-label="Settings">${icon('settings')}Settings</button>` : ''}</header>`;
}

export function renderPhoneTabs({ view = 'bots', botsWaiting = 0 } = {}) {
  return `<nav class="m-tabs" aria-label="Main"><a href="#/bots" data-nav="bots"${current(view === 'bots')}>${icon('bots')}Bots ${badge(botsWaiting, 'waiting on you')}</a><a href="#/memory" data-nav="memory"${current(view === 'memory')}>${icon('memory')}Memory</a></nav>`;
}
