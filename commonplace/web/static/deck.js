// Deck: the opt-in look and navigation ("decisions first"). One switch per browser, kept in
// localStorage; while it is off nothing in this file changes what the app draws.
// On: the shell draws a top bar instead of the rail (Today, Bots, Compute, Settings), the phone gets the same
// four tabs, and deck.css re-tokens every screen (true black, lightness for elevation, amber
// only for what waits on you). Screens that exist only in Deck register in SCREENS.
import { escape as e } from './api.js';

export const UI_KEY = 'cmmn.ui';
// Deck-only screens by route name: name -> loader of a module exporting TITLE and mount(context).
// mount returns { refresh?(context), stop?() }. A name without a loader is not offered in the bar.
export const SCREENS = {};
const SECTIONS = [['today', 'Today'], ['bots', 'Bots'], ['compute', 'Compute'], ['settings', 'Settings']];

const store = () => { try { return globalThis.localStorage || null; } catch { return null; } };
export function deckOn(storage = store()) {
  try { return storage?.getItem(UI_KEY) === 'deck'; } catch { return false; }
}
export function setDeck(on, storage = store()) {
  try { if (on) storage?.setItem(UI_KEY, 'deck'); else storage?.removeItem(UI_KEY); } catch { /* The look stays as it was for this visit. */ }
}
/** Read ?ui=deck or ?ui=classic once (a link can switch the look), then mark the document. */
export function applyDeck({ doc = globalThis.document, loc = globalThis.location, hist = globalThis.history, storage = store() } = {}) {
  if (loc?.search) {
    const params = new URLSearchParams(loc.search), asked = params.get('ui');
    if (asked === 'deck' || asked === 'classic') {
      setDeck(asked === 'deck', storage);
      params.delete('ui');
      const search = params.toString();
      hist?.replaceState?.(hist.state, '', `${loc.pathname}${search ? `?${search}` : ''}${loc.hash}`);
    }
  }
  const on = deckOn(storage);
  if (doc?.documentElement) { if (on) doc.documentElement.dataset.ui = 'deck'; else delete doc.documentElement.dataset.ui; }
  return on;
}

const count = (n) => Number.isSafeInteger(n) && n > 0 ? n : 0;
const current = (yes) => yes ? ' aria-current="page"' : '';
const paths = {
  today: '<path d="M3 8.5l3 3 7-7"/>',
  bots: '<rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M8 5V2.8M5.8 9h.1M10.2 9h.1"/>',
  compute: '<circle cx="3.5" cy="8" r="1.6"/><circle cx="12.5" cy="4" r="1.6"/><circle cx="12.5" cy="12" r="1.6"/><path d="M5 7.3l6-2.6M5 8.7l6 2.6"/>',
  settings: '<path d="M2 4.5h7M12.5 4.5H14M2 11.5h1.5M6.5 11.5H14"/><circle cx="10.8" cy="4.5" r="1.6"/><circle cx="5" cy="11.5" r="1.6"/>',
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
const brand = '<svg class="deck-mark" viewBox="0 0 22 22" aria-hidden="true"><rect x="2" y="2" width="18" height="18" rx="6" fill="currentColor"/><path class="deck-tick" d="M7 11.5l3 3 5.5-6.5" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const badge = (n, label) => count(n) ? `<span class="count need">${n}<span class="sr"> ${label}</span></span>` : '';

/** The bar section a route belongs to; '' for the workspace tools reached from the account menu. */
export function deckSection(hash = '') {
  const name = /^#\/([a-z]+)/.exec(hash)?.[1] || '';
  if (name === 'bots' || name === 'today' || name === 'compute') return name;
  return ['settings', 'rules', 'admin'].includes(name) ? 'settings' : '';
}
export const deckSections = (screens = SCREENS) => SECTIONS.filter(([id]) => id === 'bots' || id === 'settings' || screens[id]);
/** Where "#/" lands: Today once that screen is registered, the bots until then. */
export const deckHome = (screens = SCREENS) => screens.today ? '#/today' : '#/bots';
const here = () => { try { return globalThis.location?.hash || ''; } catch { return ''; } };

// The home section carries the count of what waits on the person (the bots' count when the shell has one).
// Same contract as renderRail: pure markup, the shell delegates data-action clicks. The opening
// tag stays `<nav class="rail"` because the shell gives that element its id.
export function renderDeckBar({ workspace = 'Commonplace', user = '', needs = 0, botsWaiting = 0, rulesWaiting = 0, can_admin = false, hash = here(), screens = SCREENS } = {}) {
  const waiting = count(botsWaiting) || needs, section = deckSection(hash), sections = deckSections(screens), home = sections[0][0];
  return `<nav class="rail" data-deck="bar" aria-label="Workspace">
    <a class="deck-brand" href="#/${home}" aria-label="Commonplace home">${brand}<b>Commonplace</b></a>
    <div class="deck-links">${sections.map(([id, label]) => `<a class="nav" href="#/${id}" data-nav="${id}"${current(section === id)}>${label}${id === home ? badge(waiting, 'waiting on you') : ''}</a>`).join('')}</div>
    <button class="iconbtn" id="cmdk" data-action="palette" aria-keyshortcuts="Control+K Meta+K" aria-label="Search or do">${icon('search')}</button>
    <button class="ws" data-action="switch-workspace" aria-label="Workspace: ${e(workspace)}. Switch workspace">${e(workspace)}</button>
    <details class="user-menu"><summary class="me" aria-label="${e(user || 'Account')}"><span class="mark" aria-hidden="true">${e(Array.from(user)[0] || '?')}</span>${badge(rulesWaiting, 'rules waiting')}</summary><div class="user-links"><a href="#/needs">Needs me${badge(needs, 'items')}</a><a href="#/boards">Projects</a><a href="#/memory">Memory</a><a href="#/console">Console</a><a href="#/rules">Rules${badge(rulesWaiting, 'waiting')}</a>${can_admin === true ? '<a href="#/admin">Admin</a>' : ''}<button data-action="shortcuts">Shortcuts</button><button data-action="logout">Sign out</button></div></details>
  </nav>`;
}
export function renderDeckTop({ hash = here() } = {}) {
  const section = deckSection(hash), label = SECTIONS.find(([id]) => id === section)?.[1] || 'Commonplace';
  return `<header class="m-top" data-deck="top"><span class="deck-brand">${brand}<b>${label}</b></span><span class="spacer"></span><button class="iconbtn" data-action="palette" aria-label="Search or do (command palette)">${icon('search')}</button></header>`;
}
export function renderDeckTabs({ needs = 0, botsWaiting = 0, hash = here(), screens = SCREENS } = {}) {
  const waiting = count(botsWaiting) || needs, section = deckSection(hash), sections = deckSections(screens), home = sections[0][0];
  return `<nav class="m-tabs" data-deck="tabs" data-count="${sections.length}" aria-label="Main">${sections.map(([id, label]) => `<a href="#/${id}" data-nav="${id}"${current(section === id)}>${icon(id)}${label}${id === home ? badge(waiting, 'waiting on you') : ''}</a>`).join('')}</nav>`;
}

/** The Look section appended to Settings: one switch, saved in this browser only. */
export function lookHTML(on) {
  return `<section class="settings-section" id="look"><h2>Look</h2><p>Deck puts what waits on you first and moves the workspace tools into the account menu. The choice is saved in this browser.</p><button class="opt" data-deck-action="toggle" aria-pressed="${on ? 'true' : 'false'}">${on ? 'Go back to the classic look' : 'Try the Deck look'}</button></section>`;
}

/** The shell's handle on Deck. Everything it needs comes in, so it runs without a browser.
 *  context() returns what a screen may read: { me, boards, needs, go(hash) }. */
export function createDeck({ root, api, context, title, screen = () => 0, reload = () => {}, screens = SCREENS, storage = store(), doc = globalThis.document } = {}) {
  let active = null;
  const name = (base) => /^#\/([a-z]+)$/.exec(base || '')?.[1] || '';
  const owns = (base) => deckOn(storage) && typeof screens[name(base)] === 'function';
  function leave() { active?.ui?.stop?.(); active = null; }
  async function show(base, { background = false, epoch = 0 } = {}) {
    const id = name(base);
    if (active?.id === id) { active.ui?.refresh?.(context()); return; }
    if (background) return;
    const module = await screens[id]();
    if (epoch !== screen()) return;
    leave();
    active = { id, ui: module.mount({ root: root.querySelector('#content'), api, e, ...context() }) };
    title(module.TITLE || 'Commonplace');
  }
  function settings(content) {
    const box = content?.querySelector?.('.settings');
    if (box && !box.querySelector('#look')) box.insertAdjacentHTML('beforeend', lookHTML(deckOn(storage)));
  }
  function toggle() {
    setDeck(!deckOn(storage), storage);
    applyDeck({ doc, loc: null, storage });
    leave();
    reload();
  }
  doc?.addEventListener?.('click', (event) => { if (event.target?.closest?.('[data-deck-action="toggle"]')) toggle(); });
  const drawn = (render) => (options = {}) => deckOn(storage) ? render({ ...options, screens }) : '';
  return { on: () => deckOn(storage), home: () => deckHome(screens), owns, show, leave, settings, toggle, bar: drawn(renderDeckBar), top: drawn(renderDeckTop), tabs: drawn(renderDeckTabs) };
}
