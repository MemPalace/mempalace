import { layout, render, device } from './deck-topology.js';

export const TITLE = 'Compute';
const HUB = 'commonplace-hub';
const TABS = ['Physical', 'Config', 'Agents', 'Sites'];
const STATES = { ready: 'Ready', waiting: 'Waiting for attestation', off: 'Off' };
let stylesheet;
function styles(doc) {
  if (!doc || !globalThis.CSSStyleSheet) return;
  if (!stylesheet) {
    stylesheet = new CSSStyleSheet();
    stylesheet.replaceSync(`
.deck-compute#content { gap: 0; overflow: auto; padding: 24px max(24px, calc((100% - 1200px) / 2)) 24px; color: var(--text); }
.deck-compute .dc-title { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 16px; margin-bottom: 20px; }
.deck-compute h1 { font-size: 24px; line-height: 32px; font-weight: 600; letter-spacing: -.02em; }
.deck-compute .dc-summary { margin: 0; color: var(--text-2); font-size: 14px; }
.deck-compute .dc-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
.deck-compute .dc-action { display: inline-flex; align-items: center; justify-content: center; min-height: 40px; padding: 0 18px; border: 0; border-radius: 999px; background: var(--accent-weak); color: var(--text); text-decoration: none; font-size: 14px; font-weight: 500; }
.deck-compute .dc-primary { background: var(--text); color: var(--bg); }
.deck-compute .dc-action:hover { background: var(--well); }
.deck-compute .dc-primary:hover { background: var(--text-2); }
/* At 1280: 1200px canvas = 860px field + 14px gap + 326px details. */
.deck-compute .dc-grid { flex: none; display: grid; grid-template-columns: minmax(0, 1fr) 326px; gap: 14px; align-items: start; }
.deck-compute .dc-map { min-width: 0; }
.deck-compute .dc-field { height: max(440px, calc(100dvh - 196px)); overflow: auto; border-radius: 20px; background: var(--surface); }
.deck-compute .dc-topology { display: block; max-width: none; }
.deck-compute .dc-link { stroke: var(--line-strong); stroke-width: 1.5; }
.deck-compute .dc-link.dc-waiting { stroke-dasharray: 5 4; }
.deck-compute .dc-link.dc-off { stroke-dasharray: 2 5; }
.deck-compute .dc-dot { stroke: var(--surface); stroke-width: 2; }
.deck-compute .dc-dot.dc-ready { fill: var(--ok); }
.deck-compute .dc-dot.dc-waiting { fill: var(--wait); }
.deck-compute .dc-dot.dc-off { fill: var(--block); }
.deck-compute .dc-node { color: var(--text); cursor: pointer; outline: none; }
.deck-compute .dc-pad { fill: var(--accent-weak); }
.deck-compute .dc-node:hover .dc-pad { fill: var(--line-strong); }
.deck-compute .dc-node.dc-selected .dc-pad { fill: var(--text); }
.deck-compute .dc-selected { color: var(--bg); }
.deck-compute .dc-node:focus-visible .dc-pad { stroke: var(--text); stroke-width: 2; stroke-dasharray: 3 3; }
.deck-compute .dc-device { fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.deck-compute .dc-name { font: 500 13px var(--font); fill: var(--text); }
.deck-compute .dc-role { font: 400 12px var(--font); fill: var(--text-3); }
.deck-compute .dc-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; padding: 8px 16px 0; font-size: 12px; color: var(--text-3); }
.deck-compute .dc-legend span { display: inline-flex; align-items: center; gap: 6px; }
.deck-compute .dc-swatch { width: 8px; height: 8px; }
.deck-compute .dc-detail { display: flex; flex-direction: column; gap: 12px; padding: 22px; border-radius: 20px; background: var(--surface); min-width: 0; }
.deck-compute .dc-detail header { display: flex; align-items: center; gap: 12px; }
.deck-compute .dc-detail h2 { font-size: 16px; line-height: 22px; font-weight: 600; overflow-wrap: anywhere; }
.deck-compute .dc-state { font-size: 13px; color: var(--text-2); }
.deck-compute .dc-tabs { display: flex; gap: 16px; border-bottom: 1px solid var(--line); }
.deck-compute .dc-tab { min-width: 44px; min-height: 44px; padding: 0; background: none; border: 0; border-bottom: 2px solid transparent; color: var(--text-3); font-size: 14px; }
.deck-compute .dc-tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--text); }
.deck-compute .dc-panel { font-size: 14px; line-height: 22px; color: var(--text-2); overflow-wrap: anywhere; }
.deck-compute .dc-panel p { margin: 0; }
.deck-compute .dc-config { display: grid; gap: 12px; margin: 0; }
.deck-compute .dc-config dt { font-size: 12px; color: var(--text-3); }
.deck-compute .dc-config dd { margin: 0; color: var(--text); }
.deck-compute .dc-notice:empty { display: none; }
.deck-compute .dc-notice { margin: 12px 0; font-size: 14px; color: var(--text-2); }
.deck-compute .dc-empty { margin: 0 0 16px; color: var(--text-2); }
.deck-compute .dc-action:focus-visible, .deck-compute .dc-tab:focus-visible, .deck-compute .dc-field:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; }
@media (max-width: 999px) { .deck-compute .dc-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 699px) {
.deck-compute#content { padding: 18px 16px 104px; }
.deck-compute .dc-title { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 10px 8px; margin-bottom: 18px; }
.deck-compute h1 { grid-area: 1 / 1; }
.deck-compute .dc-summary { grid-area: 2 / 1; }
.deck-compute .dc-actions { display: contents; }
.deck-compute .dc-actions .dc-action:first-child { grid-area: 1 / 2; }
.deck-compute .dc-actions .dc-action:nth-child(2) { grid-area: 1 / 3; }
.deck-compute .dc-action { min-height: 44px; padding: 0 14px; }
.deck-compute .dc-actions .dc-primary { grid-area: 2 / 2 / 3 / 4; }
.deck-compute .dc-field { height: 440px; }
.deck-compute .dc-grid { gap: 18px; }
.deck-compute .dc-detail { padding: 22px; }
.deck-compute .dc-legend { padding-left: 4px; padding-right: 4px; }
}
`);
  }
  if (!doc.adoptedStyleSheets.includes(stylesheet)) doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, stylesheet];
}

// Readiness is an enrollment state, not a claim of liveness or independent verification.
export function machineState(machine) {
  return !machine.enabled ? 'off' : machine.binding_state === 'owner_attested' ? 'ready' : 'waiting';
}

export function mount(context) {
  let ctx = context, rows = [], general = null, selected = null, tab = 'Physical';
  let stopped = false, epoch = 0, request = null, observer = null, message = '';
  const root = context.root, doc = root.ownerDocument || globalThis.document;
  styles(doc);
  root.classList.add('deck-compute');
  // The screen owns its title row; restore the shared header on departure.
  const shellHeaders = ['.head', '#phone-bar'].map(selector => root.parentElement?.querySelector(selector)).filter(Boolean).map(node => ({ node, hidden: node.hidden }));
  for (const { node } of shellHeaders) node.hidden = true;
  // Keep the shell's phone destinations reachable while the long map/details page scrolls.
  const phoneTabs = root.parentElement?.querySelector('#phone-tabs');
  const navStyle = phoneTabs?.style;
  const previousNav = navStyle && ['position', 'bottom', 'left', 'right', 'zIndex'].map(key => [key, navStyle[key]]);
  if (navStyle) Object.assign(navStyle, { position: 'fixed', bottom: '0', left: '0', right: '0', zIndex: '10' });
  const region = name => root.querySelector(`[data-compute-region="${name}"]`);
  const getNodes = () => [{ id: HUB, name: 'Commonplace', role: 'Hub', type: 'hub', state: 'hub' }, ...rows.map(m => ({ id: m.id, name: m.label, role: STATES[machineState(m)], state: machineState(m), type: 'machine' }))];
  function map() {
    const field = region('field');
    if (!field) return;
    const focused = doc?.activeElement?.dataset?.computeNode;
    const phone = globalThis.matchMedia?.('(max-width:699px)').matches === true;
    field.innerHTML = render(layout({ nodes: getNodes(), links: rows.map(m => ({ from: HUB, to: m.id, state: machineState(m) })), width: field.clientWidth || (phone ? 358 : 860), height: field.clientHeight || (phone ? 440 : 620), phone }), { selected });
    if (focused) [...field.querySelectorAll('[data-compute-node]')].find(n => n.dataset.computeNode === focused)?.focus({ preventScroll: true });
  }
  function detail(focusTab = false) {
    const node = getNodes().find(n => n.id === selected), machine = rows.find(m => m.id === selected);
    const host = region('detail');
    if (!host || !node) return;
    const { e } = ctx;
    const list = value => Array.isArray(value) ? value.length ? value.join(', ') : 'None granted' : 'Not recorded';
    let body;
    if (tab === 'Physical') body = '<p>No physical details are recorded yet.</p>';
    else if (tab === 'Sites') body = '<p>No sites are recorded yet.</p>';
    else if (tab === 'Agents') body = '<p>Running work is not reported for this machine.</p>';
    else if (!machine) body = '<p>No machine configuration is recorded for the hub.</p>';
    else {
      const facts = [['Projects', list(machine.projects)], ['Topics', list(machine.topics)], ['Wings', list(machine.wings)], ['Longest runtime mode', machine.max_runtime_mode || 'Not recorded'], ['Trust', machine.binding_state === 'owner_attested' ? 'Attested by the owner, not verified' : 'Not attested, not verified']];
      body = `<dl class="dc-config">${facts.map(([key, value]) => `<div><dt>${e(key)}</dt><dd>${e(value)}</dd></div>`).join('')}</dl>`;
    }
    host.innerHTML = `<header>${device(node.type, 30)}<div><h2>${e(node.name)}</h2><span class="dc-state">${e(node.role)}</span></div></header><div class="dc-tabs" role="tablist" aria-label="Machine details">${TABS.map(t => `<button class="dc-tab" type="button" role="tab" id="dc-tab-${t}" data-compute-tab="${t}" aria-controls="dc-panel" aria-selected="${t === tab}" tabindex="${t === tab ? 0 : -1}">${t}</button>`).join('')}</div><div class="dc-panel" role="tabpanel" tabindex="0" id="dc-panel" aria-labelledby="dc-tab-${tab}">${body}</div>`;
    if (focusTab) host.querySelector(`[data-compute-tab="${tab}"]`)?.focus();
  }
  function actions() {
    const host = region('actions');
    if (host) host.innerHTML = '<a class="dc-action" href="#/admin">Add machine</a>' + (general ? '<button class="dc-action" type="button" data-compute-action="site">Add site</button><button class="dc-action dc-primary" type="button" data-compute-action="map">Let a bot map it</button>' : '');
  }
  function paint() {
    const focusedTab = doc?.activeElement?.dataset?.computeTab;
    observer?.disconnect();
    root.innerHTML = `<header class="dc-title"><h1>Compute</h1><p class="dc-summary">${rows.filter(m => machineState(m) === 'ready').length} of ${rows.length} machines ready.</p><div class="dc-actions" data-compute-region="actions"></div></header>${rows.length ? '' : '<p class="dc-empty">No machines yet. Add a machine to begin.</p>'}<p class="dc-notice" data-compute-region="notice" role="status"></p><div class="dc-grid"><div class="dc-map"><div class="dc-field" data-compute-region="field" tabindex="0" aria-label="Machine map; scroll to see more machines"></div><div class="dc-legend" aria-label="Machine states">${Object.entries(STATES).map(([key, label]) => `<span><svg class="dc-swatch" viewBox="0 0 8 8" aria-hidden="true"><circle class="dc-dot dc-${key}" cx="4" cy="4" r="4"/></svg>${label}</span>`).join('')}</div></div><aside class="dc-detail" aria-label="Selected machine" data-compute-region="detail"></aside></div>`;
    actions(); map(); detail(Boolean(focusedTab));
    region('notice').textContent = message;
    observer?.observe(region('field'));
  }
  async function refresh(next = {}) {
    ctx = { ...ctx, ...next }; const mine = ++epoch;
    request?.abort(); request = new AbortController();
    const options = { signal: request.signal, timeoutMs: 15000 };
    try {
      const result = await ctx.api('/api/runners', options);
      if (stopped || mine !== epoch) return;
      rows = (Array.isArray(result?.machines) ? result.machines : []).filter(m => !m.revoked_at).map(m => ({ ...m, id: String(m.id ?? m.machine_id) }));
      if (!getNodes().some(n => n.id === selected)) selected = rows[0]?.id || HUB;
      message = ''; paint();
      try {
        const bots = await ctx.api('/api/bots', options);
        if (stopped || mine !== epoch) return;
        general = (Array.isArray(bots?.bots) ? bots.bots : []).find(b => b.kind === 'general') || null;
      } catch { if (stopped || mine !== epoch) return; general = null; }
      actions();
    } catch (error) {
      if (stopped || mine !== epoch) return;
      if (error.status === 403) {
        observer?.disconnect(); rows = []; general = null;
        root.innerHTML = '<h1>Compute</h1><p class="dc-notice">Only workspace owners can see the machines.</p>';
      } else {
        message = 'Machines could not be loaded. Try again.';
        if (region('notice')) region('notice').textContent = message;
        else root.innerHTML = '<h1>Compute</h1><p class="dc-notice" role="status">Machines could not be loaded. Try again.</p>';
      }
    }
  }
  function click(event) {
    const node = event.target.closest?.('[data-compute-node]');
    if (node) { selected = node.dataset.computeNode; map(); detail(); return; }
    const target = event.target.closest?.('[data-compute-tab]');
    if (target) { tab = target.dataset.computeTab; detail(true); return; }
    const action = event.target.closest?.('[data-compute-action]')?.dataset.computeAction;
    if (!action || !general) return;
    try {
      globalThis.sessionStorage.setItem('cmmn.deck.draft', JSON.stringify({ bot: general.id, text: action === 'site' ? 'Add a site to my map.' : 'Map my machines and sites.' }));
      ctx.go(`#/bots/${encodeURIComponent(general.id)}`);
    } catch { const note = region('notice'); if (note) note.textContent = 'The message could not be prepared. Try again.'; }
  }
  function keydown(event) {
    if (event.target.closest?.('[data-compute-node]') && ['Enter', ' '].includes(event.key)) { event.preventDefault(); click(event); return; }
    const target = event.target.closest?.('[data-compute-tab]');
    if (!target || !['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = TABS.indexOf(target.dataset.computeTab);
    tab = TABS[event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length];
    detail(true);
  }
  root.addEventListener('click', click); root.addEventListener('keydown', keydown);
  if (globalThis.ResizeObserver) observer = new ResizeObserver(() => { if (!stopped) map(); });
  root.innerHTML = '<h1>Compute</h1><p class="dc-notice" role="status">Loading machines…</p>';
  void refresh();
  return { refresh, stop() {
    if (stopped) return;
    stopped = true; epoch++; request?.abort(); observer?.disconnect();
    root.removeEventListener('click', click); root.removeEventListener('keydown', keydown);
    root.classList.remove('deck-compute'); root.innerHTML = '';
    for (const { node, hidden } of shellHeaders) node.hidden = hidden;
    if (navStyle) for (const [key, value] of previousNav) navStyle[key] = value;
  } };
}
