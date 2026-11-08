"""Settings deadlines and recovery use production JavaScript with a fake clock."""

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace.projects import routes

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = [
    "/api/connections",
    "/api/webhooks",
    "/api/plan",
    "/api/phone-push",
    "/api/chat-settings",
    "/api/settings/orchestration",
]
SHARED = ["/api/boards", "/api/needs-me", "/api/rules/count"]

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const [path, failure, mode] = process.argv.slice(1);
const read = name => fs.readFileSync('commonplace/web/static/' + name, 'utf8');
const app = read('app.js');
function slice(start, end) {
  const at = app.indexOf(start), stop = app.indexOf(end, at);
  assert.ok(at >= 0 && stop > at);
  return app.slice(at, stop);
}
let now = 0, serial = 0;
const timers = new Map();
const tick = async () => { await new Promise(resolve => setImmediate(resolve)); };
async function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers]) {
    if (timer.at <= now) { timers.delete(id); timer.fn(); }
  }
  await tick();
}
let html = '', screenError = '', orchestrationHTML = '', blocked = true, saved = true, webhookDraft = null;
const input = {};
const host = {
  isConnected: true,
  get innerHTML() { return orchestrationHTML; },
  set innerHTML(value) { orchestrationHTML = value; },
  querySelector: () => input,
};
const content = {
  children: [{}],
  get innerHTML() { return html; },
  set innerHTML(value) {
    html = value; orchestrationHTML = '';
    webhookDraft = value.includes('data-form="webhook"') ? { value: '' } : null;
  },
  querySelector(selector) {
    if (selector === 'input[name="url"]') return webhookDraft;
    if (selector === '.settings') return html.includes('class="settings"') ? {} : null;
    if (selector === '[data-workspace-orchestration]') return host;
    return null;
  },
};
const values = {
  '/api/boards': { boards: [] }, '/api/needs-me': {}, '/api/rules/count': {},
  '/api/connections': {}, '/api/webhooks': { webhooks: [] },
  '/api/plan': { plan: 'free' }, '/api/phone-push': {},
  '/api/chat-settings': { front: { enabled: false }, usage: { used_today: 0, user_used_today: 0 } },
};
const reads = [], stalled = [];
const context = {
  AbortController, Event,
  document: { dispatchEvent() {} },
  setTimeout(fn, ms) { const id = ++serial; timers.set(id, { fn, at: now + ms }); return id; },
  clearTimeout(id) { timers.delete(id); },
  fetch(url, options) {
    reads.push(url);
    if (blocked && url === path) {
      if (failure === 'network') return Promise.reject(new Error('Network unavailable'));
      if (failure === 'http') return Promise.resolve({ status: 503, ok: false, json: async () => ({ error: 'Service unavailable' }) });
      if (failure === 'json') return Promise.resolve({ status: 200, ok: true, json: async () => { throw new SyntaxError('Invalid JSON'); } });
      let resolve;
      const pending = new Promise(done => { resolve = done; });
      // Deliberately ignore abort: the deadline must settle even if body decoding does not.
      stalled.push({ resolve, signal: options.signal });
      if (failure === 'body') return Promise.resolve({ status: 200, ok: true, json: () => pending });
      return pending;
    }
    if (url === '/api/settings/orchestration' && options.method === 'PUT') saved = JSON.parse(options.body).enabled;
    const value = url === '/api/settings/orchestration' ? { enabled: saved } : values[url];
    assert.notEqual(value, undefined, `Unexpected request: ${url}`);
    return Promise.resolve({ status: 200, ok: true, json: async () => value });
  },
  state: { me: { actor: 'user:reader' }, base: '#/settings', route: '#/settings', screen: 1, refreshRequest: 0 },
  root: { querySelector: selector => selector === '#content' ? content : null },
  remember: () => ({}), restore() {}, capture: () => () => {}, navigation() {}, title() {},
  showError(value) { screenError = value; },
  voice: { settings: () => '' }, planView: () => '<h2>Free plan</h2>', agentsView: () => '<h2>Agents</h2>',
};
vm.createContext(context);
vm.runInContext(read('api.js').replace(/export /g, '') + '\nconst e = escape;', context);
vm.runInContext(slice('const message = ', '// The board lists child'), context);
const projects = read('projects.js');
vm.runInContext('function installOrchestrationStyles() {}\n' +
  projects.slice(projects.indexOf('export function mountWorkspaceOrchestration('), projects.indexOf('export function mountOrchestratorTrust(')).replace('export ', ''), context);
vm.runInContext(slice('function phonePushView(', 'async function memoryView(') +
  slice('async function refresh(', 'function decideStart('), context);
// Exercise the actual delegated Retry branch rather than a test-only reload path.
const retry = app.match(/else if \(action === "retry"\) ([^\n]+)/);
assert.ok(retry);
vm.runInContext('async function clickRetry() { ' + retry[1] + ' }', context);
(async () => {
  if (mode === 'background') {
    blocked = false;
    await context.refresh();
    const rendered = html, savedState = orchestrationHTML;
    const draft = content.querySelector('input[name="url"]');
    assert.ok(draft, 'Initial Settings load includes the webhook form');
    draft.value = 'https://example.test/unsaved-webhook';
    blocked = true;
    const background = context.refresh({ background: true, events: [{ kind: 'board.created' }] });
    await tick();
    await advance(10000);
    assert.equal(await background, false);
    assert.equal(html, rendered, 'A failed background read must preserve the rendered Settings sections');
    assert.equal(orchestrationHTML, savedState, 'Saved orchestration state remains visible');
    assert.equal(content.querySelector('input[name="url"]'), draft, 'The draft input must remain mounted');
    assert.equal(draft.value, 'https://example.test/unsaved-webhook');
    assert.ok(screenError.includes(path), 'The separate screen error must identify the failed prerequisite');
    assert.equal(timers.size, 0);
    blocked = false;
    await context.refresh({ background: true });
    assert.equal(screenError, '');
    assert.equal(html, rendered);
    assert.equal(content.querySelector('input[name="url"]'), draft);
    assert.equal(draft.value, 'https://example.test/unsaved-webhook');
    return;
  }
  if (failure === 'success') {
    blocked = false; saved = path === 'on';
    await context.refresh();
    assert.match(html, /Agent orchestration/);
    assert.equal(/data-orchestration-enabled checked/.test(orchestrationHTML), saved);
    assert.equal(timers.size, 0, 'Successful reads clear every deadline');
    // Real save handler, followed by a fresh settings render, retains server state.
    await input.onchange({ target: { checked: !saved } });
    assert.match(orchestrationHTML, /Saved/);
    await context.refresh();
    assert.equal(/data-orchestration-enabled checked/.test(orchestrationHTML), saved);
    return;
  }
  let settled = false;
  const load = context.refresh().finally(() => { settled = true; });
  await tick();
  if (failure === 'navigate') {
    context.state.screen++; context.state.base = context.state.route = '#/boards'; html = 'Another page';
    await advance(10000);
    assert.equal(html, 'Another page'); assert.equal(screenError, '');
    assert.equal(settled, true, 'An abandoned Settings load still releases its wait');
    return;
  }
  await advance(10000);
  const visible = html + screenError;
  const label = {
    '/api/connections': 'Agents', '/api/webhooks': 'Webhooks', '/api/plan': 'Plan',
    '/api/phone-push': 'Phone notifications', '/api/chat-settings': 'Chat',
    '/api/settings/orchestration': 'Agent orchestration',
  }[path] || path;
  assert.match(visible, /Could not load|timed out|unavailable|Invalid JSON/, 'A failed read must surface an error');
  assert.ok(visible.includes(label), `Error must identify ${label}: ${visible}`);
  assert.match(html, /data-action="retry"[^>]*>Retry/);
  assert.doesNotMatch(html, /Loading settings/);
  if (['fetch', 'body'].includes(failure)) {
    assert.ok(stalled[0].signal?.aborted, 'Deadline aborts the request');
    assert.equal(timers.size, 0);
  }
  blocked = false;
  await context.clickRetry();
  assert.match(html, /Agent orchestration/);
  assert.match(orchestrationHTML, /data-orchestration-enabled checked/, 'Retry renders the saved ON state');
  assert.doesNotMatch(html, /Could not load|timed out|unavailable|Invalid JSON/);
  assert.equal(screenError, '');
  assert.equal(timers.size, 0, 'Retry clears every timer');
  // An old request completing after Retry must never overwrite the recovered page.
  const recovered = html + orchestrationHTML;
  stalled.forEach(({ resolve }) => resolve(failure === 'body' ? { enabled: false } : { status: 200, ok: true, json: async () => ({ enabled: false }) }));
  await tick();
  assert.equal(html + orchestrationHTML, recovered);
  await load;
  assert.ok(reads.filter(url => url === path).length >= 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("path", SETTINGS + SHARED)
@pytest.mark.parametrize("failure", ["fetch", "body", "http", "network", "json"])
def test_settings_failure_and_retry(path, failure):
    result = subprocess.run(
        ["node", "-e", HARNESS, path, failure],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path", SHARED)
@pytest.mark.parametrize("failure", ["fetch", "body", "http", "network", "json"])
def test_background_prerequisite_failure_preserves_settings(path, failure):
    result = subprocess.run(
        ["node", "-e", HARNESS, path, failure, "background"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("saved", ["on", "off"])
def test_settings_saved_state_after_reload(saved):
    result = subprocess.run(
        ["node", "-e", HARNESS, saved, "success"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_settings_deadline_after_navigation():
    result = subprocess.run(
        ["node", "-e", HARNESS, SETTINGS[-1], "navigate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("enabled", [False, True])
def test_orchestration_settings_lock_contention_is_retryable(monkeypatch, enabled):
    """A fake contended lock avoids sleeps and deadlocking the pinned code."""

    class ContendedLock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def acquire(self, *, timeout):
            assert 0 < timeout <= 1
            return False

        def release(self):
            pytest.fail("A lock not acquired must not be released")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orchestration_settings(workspace_id TEXT, enabled INT)")
    conn.execute("INSERT INTO orchestration_settings VALUES ('workspace', ?)", (enabled,))
    gateway = SimpleNamespace(db=SimpleNamespace(_lock=ContendedLock(), _conn=conn))
    monkeypatch.setattr(routes, "_person", lambda *_: SimpleNamespace(workspace_id="workspace"))
    replies = []
    monkeypatch.setattr(
        routes, "send_json", lambda _, status, payload: replies.append((status, payload))
    )
    try:
        routes.orchestration_route(gateway, SimpleNamespace(command="GET"))
    finally:
        conn.close()
    assert replies == [(503, {"error": "settings_unavailable"})]
