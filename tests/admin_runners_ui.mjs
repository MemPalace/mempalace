// Runner block checks against a minimal fake DOM: no browser, no network.
import assert from 'node:assert/strict';
let input = ''; for await (const part of process.stdin) input += part;
const sources = JSON.parse(input);
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const { createRunnerBlock, parseList, chosenWings } = await load(sources.adminrunners);
const { createAdmin } = await load(sources.admin);
const e = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const TOKENS = { machine: 'cmmn_MACHINE_CANARY_0001', worker: 'cmmn_WORKER_CANARY_0002' };

const leaks = [];
const ledger = [];
for (const name of ['log', 'info', 'warn', 'error', 'debug']) console[name] = (...args) => ledger.push(args.join(' '));
for (const name of ['localStorage', 'sessionStorage']) {
  globalThis[name] = { setItem: (k, v) => leaks.push(name + k + v), getItem: () => null, removeItem() {} };
}

function region() {
  return {
    innerHTML: '', textContent: '', dataset: {},
    replaceChildren() { this.innerHTML = ''; },
    querySelector(selector) {
      const name = /data-runner-secret="(\w+)"/.exec(selector)?.[1];
      const found = name && new RegExp(`data-runner-secret="${name}" value="([^"]*)"`).exec(this.innerHTML);
      return found ? { value: found[1] } : null;
    },
  };
}
function fixture({ machines, wings = ['shared', 'team'], fail = {}, wingsGate = null } = {}) {
  const regions = {}, listeners = {}, calls = [];
  const host = {
    innerHTML: '',
    addEventListener(kind, fn) { listeners[kind] = fn; },
    removeEventListener(kind) { delete listeners[kind]; },
    querySelector(selector) { return regions[/data-runner-([\w-]+)/.exec(selector)[1]] ??= region(); },
  };
  const state = { machines: machines ?? [machine()], copied: [] };
  const api = async (path, options = {}) => {
    calls.push({ path, method: options.method || 'GET', body: options.body});
    (calls.timeouts ??= {})[`${options.method || 'GET'} ${path}`] = options.timeoutMs;
    const key = `${options.method || 'GET'} ${path}`;
    if (fail[key]) throw Object.assign(new Error(fail[key]), { status: 409 });
    if (path === '/api/runners' && !options.method) return { machines: state.machines };
    if (path === '/api/runners/wings' || path === '/api/connections') { await wingsGate; return { wings, connections: [] }; }
    if (path === '/api/runners' && options.method === 'POST') return { machine_id: 'mach_0123456789abcdef', machine_token: TOKENS.machine, worker_token: TOKENS.worker, binding_state: 'unbound' };
    return { ok: true };
  };
  const block = createRunnerBlock({ api, e, copy: async text => { state.copied.push(text); } });
  const form = (kind, id, fields) => ({ dataset: { runnerForm: kind, runnerId: id }, elements: fields });
  const submit = async (kind, id, fields) => {
    const target = form(kind, id, fields);
    listeners.submit({ target: { closest: () => target }, preventDefault() { calls.preventDefault = (calls.preventDefault || 0) + 1; } });
    await flush();
  };
  const click = async (dataset) => { await listeners.click({ target: { closest: () => ({ dataset }) } }); await flush(); };
  return { block, host, regions, calls, state, submit, click, listeners };
}
function machine(extra = {}) {
  return {
    machine_id: 'mach_aaaaaaaaaaaaaaaa', label: 'bench-1', projects: ['demo'], topics: ['worker'], wings: ['shared'],
    max_runtime_mode: 'approval-required', max_in_flight: 1, enabled: true, revoked_at: null, binding_state: 'unbound',
    binding_provider_instance_id: null, worker_connection_id: 'conn_workerid', ...extra,
  };
}
const field = (name, value, type = 'text', checked = false) => ({ name, value, type, checked });

// 1. Listing and the wing picker come from the owner routes; values are escaped.
{
  const f = fixture({ machines: [machine({ label: 'x<img>', wings: ['shared', 'legacy'] })], wings: ['shared', 'team'] });
  f.block.show(f.host); await flush();
  assert.deepEqual(f.calls.map(c => c.path).sort(), ['/api/runners', '/api/runners/wings'], 'wings come from the runner route, not the slow connections listing');
  assert.ok(f.calls.every(c => c.method === 'GET'));
  const list = f.regions.list.innerHTML, enroll = f.regions['enroll-slot'].innerHTML;
  assert.doesNotMatch(list, /<img>/); assert.match(list, /x&lt;img&gt;/);
  assert.match(enroll, /name="wings" value="team"/); assert.doesNotMatch(enroll, /legacy/);
  assert.match(list, /name="wings" value="legacy" checked/, 'a granted wing outside the picker stays visible and checked');
  assert.match(list, /value="conn_workerid"/, 'worker connection id is prefilled');
  assert.match(list, /value="worker"/, 'topics are prefilled');
  assert.match(list, /resets this machine to unbound/);
  f.block.stop();
}

// 2. Enroll posts the form, shows tokens once with copy buttons, and never keeps them.
{
  const f = fixture();
  f.block.show(f.host); await flush();
  await f.submit('enroll', undefined, [
    field('label', ' bench-2 '), field('projects', 'demo, demo, other'), field('topics', 'worker'),
    field('wings', 'shared', 'checkbox', true), field('wings', 'team', 'checkbox', true), field('wings', 'extra', 'checkbox', false),
    field('max_runtime_mode', 'auto'), field('max_in_flight', '2', 'number'),
  ]);
  const post = f.calls.find(c => c.method === 'POST');
  assert.equal(post.path, '/api/runners');
  assert.deepEqual(post.body, { label: 'bench-2', projects: ['demo', 'other'], topics: ['worker'], wings: ['shared', 'team'], max_runtime_mode: 'auto', max_in_flight: 2 });
  assert.equal(f.calls.preventDefault, 1);
  const shown = f.regions.reveal.innerHTML;
  assert.match(shown, new RegExp(TOKENS.machine)); assert.match(shown, new RegExp(TOKENS.worker));
  assert.match(shown, /not shown again/);
  assert.equal((shown.match(/data-runner-copy=/g) || []).length, 2);
  assert.match(shown, /readonly/);
  assert.equal(f.calls.filter(c => c.path === '/api/runners' && c.method === 'GET').length, 2, 'the list reloads after enrolling');
  for (const name of ['list', 'enroll-slot', 'status']) {
    assert.doesNotMatch(f.regions[name].innerHTML + f.regions[name].textContent, /CANARY/, `${name} must not carry tokens`);
  }
  await f.click({ runnerAction: 'copy', runnerCopy: 'worker' });
  assert.deepEqual(f.state.copied, [TOKENS.worker]);
  await f.click({ runnerAction: 'copy', runnerCopy: 'machine' });
  assert.deepEqual(f.state.copied, [TOKENS.worker, TOKENS.machine]);
  await f.click({ runnerAction: 'dismiss' });
  assert.equal(f.regions.reveal.innerHTML, '', 'dismissal empties the reveal panel');
  // A later action re-renders the list but never brings the tokens back.
  await f.submit('wings', 'mach_aaaaaaaaaaaaaaaa', [field('wings', 'shared', 'checkbox', true)]);
  assert.equal(f.regions.reveal.innerHTML, '');
  assert.doesNotMatch(JSON.stringify(f.regions), /CANARY/);
  f.block.stop();
}

// 3. Attest and wings edit call the right routes with the right bodies.
{
  const f = fixture();
  f.block.show(f.host); await flush();
  await f.submit('attest', 'mach_aaaaaaaaaaaaaaaa', [field('topics', 'worker, review'), field('provider_instance_id', ' prov-1 '), field('worker_connection_id', 'conn_workerid')]);
  assert.deepEqual(f.calls.find(c => c.path.endsWith('/attest')), { path: '/api/runners/mach_aaaaaaaaaaaaaaaa/attest', method: 'POST', body: { topics: ['worker', 'review'], provider_instance_id: 'prov-1', worker_connection_id: 'conn_workerid' } });
  assert.match(f.regions.status.textContent, /not verify/);
  await f.submit('wings', 'mach_aaaaaaaaaaaaaaaa', [field('wings', 'shared', 'checkbox', true), field('wings', 'team', 'checkbox', true)]);
  assert.deepEqual(f.calls.find(c => c.method === 'PATCH'), { path: '/api/runners/mach_aaaaaaaaaaaaaaaa', method: 'PATCH', body: { wings: ['shared', 'team'] } });
  const before = f.calls.length;
  await f.submit('wings', 'mach_aaaaaaaaaaaaaaaa', [field('wings', 'shared', 'checkbox', false)]);
  assert.equal(f.calls.length, before, 'an empty wing choice sends nothing');
  assert.match(f.regions.status.textContent, /at least one wing/);
  f.block.stop();
}

// 4. Server errors surface as text; revoked machines offer no forms; no token ever persisted or logged.
{
  const f = fixture({ machines: [machine({ revoked_at: '2026-11-01T00:00:00Z', enabled: false })], fail: { 'POST /api/runners/mach_aaaaaaaaaaaaaaaa/attest': 'provider_change_requires_reset' } });
  f.block.show(f.host); await flush();
  assert.doesNotMatch(f.regions.list.innerHTML, /<form/); assert.match(f.regions.list.innerHTML, /revoked/);
  await f.submit('attest', 'mach_aaaaaaaaaaaaaaaa', [field('topics', 'worker'), field('provider_instance_id', 'p'), field('worker_connection_id', 'c')]);
  assert.equal(f.regions.status.textContent, 'provider_change_requires_reset');
  assert.equal(f.regions.status.dataset.state, 'error');
  f.block.stop();
  assert.equal(f.host.innerHTML, '');
}
// 4b. Revoke asks first, names the machine, then calls DELETE once; the list reloads and the machine offers nothing more.
{
  const f = fixture({ machines: [machine()] });
  f.block.show(f.host); await flush();
  assert.match(f.regions.list.innerHTML, /data-runner-action="revoke" data-runner-id="mach_aaaaaaaaaaaaaaaa"/);
  await f.click({ runnerAction: 'revoke', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  assert.ok(!f.calls.some(c => c.method === 'DELETE'), 'the first click only asks');
  assert.match(f.regions.list.innerHTML, /Revoke bench-1\?/);
  await f.click({ runnerAction: 'revoke-cancel', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  assert.ok(!f.calls.some(c => c.method === 'DELETE')); assert.doesNotMatch(f.regions.list.innerHTML, /revoke-confirm/);
  await f.click({ runnerAction: 'revoke', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  f.state.machines = [machine({ revoked_at: '2026-11-11T00:00:00Z', enabled: false })];
  await f.click({ runnerAction: 'revoke-confirm', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  assert.deepEqual(f.calls.filter(c => c.method === 'DELETE').map(c => c.path), ['/api/runners/mach_aaaaaaaaaaaaaaaa']);
  assert.match(f.regions.status.textContent, /revoked/); assert.equal(f.regions.status.dataset.state, 'ok');
  assert.doesNotMatch(f.regions.list.innerHTML, /data-runner-action="revoke/);
  f.block.stop();
}
// 4c. A failed revoke surfaces the server's words and leaves the machine as it was.
{
  const f = fixture({ fail: { 'DELETE /api/runners/mach_aaaaaaaaaaaaaaaa': 'a signed-in workspace owner is required' } });
  f.block.show(f.host); await flush();
  await f.click({ runnerAction: 'revoke', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  await f.click({ runnerAction: 'revoke-confirm', runnerId: 'mach_aaaaaaaaaaaaaaaa' });
  assert.equal(f.regions.status.textContent, 'a signed-in workspace owner is required');
  assert.equal(f.regions.status.dataset.state, 'error');
  assert.match(f.regions.list.innerHTML, /data-runner-action="revoke" /, 'confirmation closed; machine still offers Revoke');
  f.block.stop();
}
// 4d. Revoking the machine whose tokens are still on screen removes the panel; the setup text is there and carries no token.
{
  const f = fixture({ machines: [] });
  f.block.show(f.host); await flush();
  await f.submit('enroll', undefined, [field('label', 'bench-2'), field('projects', 'demo'), field('topics', 'worker'), field('wings', 'shared', 'checkbox', true), field('max_runtime_mode', 'approval-required'), field('max_in_flight', '1')]);
  const panel = f.regions.reveal.innerHTML;
  assert.match(panel, /token_env/); assert.match(panel, /runner check/);
  assert.equal(panel.split(TOKENS.machine).length - 1, 1, 'the machine token appears once, in its own field'); assert.equal(panel.split(TOKENS.worker).length - 1, 1);
  f.state.machines = [machine({ machine_id: 'mach_0123456789abcdef', label: 'bench-2' })];
  await f.click({ runnerAction: 'dismiss' }); // panel closed: tokens gone
  assert.doesNotMatch(f.regions.reveal.innerHTML, /CANARY/);
  await f.submit('enroll', undefined, [field('label', 'bench-2'), field('projects', 'demo'), field('topics', 'worker'), field('wings', 'shared', 'checkbox', true), field('max_runtime_mode', 'approval-required'), field('max_in_flight', '1')]);
  assert.match(f.regions.reveal.innerHTML, /CANARY/);
  await f.click({ runnerAction: 'revoke', runnerId: 'mach_0123456789abcdef' });
  await f.click({ runnerAction: 'revoke-confirm', runnerId: 'mach_0123456789abcdef' });
  assert.equal(f.regions.reveal.innerHTML, '', 'revoking the machine clears its tokens from the page');
  f.block.stop();
}

// 4e. A stuck wings read never holds the machine list: it renders at once, wing pickers wait, then fill in.
{
  let release; const wingsGate = new Promise(resolve => { release = resolve; });
  const f = fixture({ wingsGate });
  f.block.show(f.host); await flush();
  assert.doesNotMatch(f.regions.list.innerHTML, /Loading runner machines/);
  assert.ok(f.calls.timeouts['GET /api/runners'] > 0 && f.calls.timeouts['GET /api/runners/wings'] > 0, 'both reads are bounded by a timeout');
  assert.match(f.regions.list.innerHTML, /bench-1/);
  assert.match(f.regions.list.innerHTML, /Loading wings/); assert.doesNotMatch(f.regions.list.innerHTML, /type="checkbox"/);
  release(); await flush();
  assert.match(f.regions.list.innerHTML, /name="wings" value="shared" checked/);
  assert.match(f.regions['enroll-slot'].innerHTML, /name="wings" value="team"/);
  f.block.stop();
}
// 4g. A failed wing read still leaves a usable editor: granted wings stay ticked and new ones can be typed.
{
  const f = fixture({ fail: { 'GET /api/runners/wings': 'timed out', 'GET /api/connections': 'timed out' } });
  f.block.show(f.host); await flush();
  const list = f.regions.list.innerHTML, enroll = f.regions['enroll-slot'].innerHTML;
  assert.match(list, /name="wings" value="shared" checked/, 'the granted wing stays editable');
  assert.match(list, /name="typed_wings"/); assert.match(enroll, /name="typed_wings"/);
  assert.match(list, /could not be loaded\. Type a wing name/);
  await f.submit('wings', 'mach_aaaaaaaaaaaaaaaa', [field('wings', 'shared', 'checkbox', true), field('typed_wings', ' project_compute, shared ')]);
  assert.deepEqual(f.calls.find(c => c.method === 'PATCH').body, { wings: ['shared', 'project_compute'] });
  assert.match(f.regions.status.textContent, /Wings saved/);
  await f.submit('enroll', undefined, [field('label', 'bench-2'), field('projects', 'demo'), field('topics', 'worker'), field('typed_wings', 'team'), field('max_runtime_mode', 'auto'), field('max_in_flight', '1')]);
  assert.deepEqual(f.calls.find(c => c.method === 'POST').body.wings, ['team']);
  f.block.stop();
}
// 4f. A failed machine read replaces the loading text instead of leaving it up.
{
  const f = fixture({ fail: { 'GET /api/runners': 'boom' } });
  f.block.show(f.host); await flush();
  assert.doesNotMatch(f.regions.list.innerHTML, /Loading runner machines/);
  assert.match(f.regions.list.innerHTML, /could not be loaded/);
  assert.equal(f.regions.status.textContent, 'boom'); assert.equal(f.regions.status.dataset.state, 'error');
  f.block.stop();
}
assert.deepEqual(leaks, [], 'nothing is written to local or session storage');
assert.doesNotMatch(ledger.join('\n'), /CANARY/, 'tokens are never logged');
assert.deepEqual(parseList(' a, b ,,a'), ['a', 'b']);
assert.deepEqual(chosenWings({ wings: ['a'], typed_wings: 'b, a' }), ['a', 'b']);

// 5. Admin keeps the runner block outside its refreshing body and tears it down.
{
  const body = { innerHTML: '' }, slot = { innerHTML: '' }, events = [];
  const mount = { innerHTML: '', addEventListener() {}, removeEventListener() {}, querySelector: s => (s.includes('body') ? body : slot) };
  const runners = { show(node) { events.push(['show', node === slot]); }, stop() { events.push(['stop']); } };
  const me = { can_admin: true, can_admin_instance: false, workspace: 'w', workspace_name: 'W' };
  const ui = createAdmin({ mount: () => mount, current: () => me, e, runners, api: () => new Promise(() => {}) });
  ui.show();
  assert.match(mount.innerHTML, /data-admin-runners/);
  assert.match(body.innerHTML, /Users and workspaces/);
  assert.deepEqual(events[events.length - 1], ['show', true]);
  ui.stop();
  assert.deepEqual(events[events.length - 1], ['stop']);
}
