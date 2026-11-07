"""Connect agent only mints a credential: the UI must not claim an agent is attached."""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECTS_JS = Path(__file__).resolve().parents[1] / "commonplace/web/static/projects.js"
NODE = shutil.which("node")

PRELUDE = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const ui = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
const e = v => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const visible = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
"""


def _run(script):
    if NODE is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PRELUDE + script, str(PROJECTS_JS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_main_row_says_connected_only_after_the_agent_used_the_token():
    _run(r"""
const project = {board: 'alpha', name: 'Alpha', can_configure: true,
  main: {name: 'orchestrator:alpha', connected: true, seen: false, status: 'unused', connectable: true}};
const unused = visible(ui.mainHTML(project, e));
assert.doesNotMatch(unused, /\bconnected\b/i, `unused token reads as connected: ${unused}`);
assert.match(unused, /no agent has used it yet/i, unused);
const seen = visible(ui.mainHTML({...project, main: {...project.main, seen: true, status: 'working'}}, e));
assert.match(seen, /\bconnected\b/i, seen);
assert.doesNotMatch(seen, /no agent has used it yet/i, seen);
const none = visible(ui.mainHTML({...project, main: {...project.main, connected: false}}, e));
assert.match(none, /not connected/i, none);
""")


def test_connect_result_is_a_checklist_with_named_client_setup_and_one_token():
    _run(r"""
const result = {token: 'synthetic-once-token', mcp_url: 'http://localhost/mcp', board: 'alpha',
  setup: {webhook_add: {events: 'space_message', board: 'alpha'}}};
const html = ui.connectResultHTML(result, e);
const step = html.match(/<li\b[^>]*data-connect-step="setup"[^>]*>[\s\S]*?<\/li>/);
assert.ok(step, 'the dialog has a setup step');
assert.match(step[0], /data-project-app[\s\S]*value="claude-code"/, 'the setup step names an agent app');
assert.match(html, /data-connect-step="start"/, 'the dialog says to start the agent');
assert.doesNotMatch(html, /<pre>\s*\{/, 'no raw webhook_add JSON blob');
assert.doesNotMatch(visible(html), /attaches an agent/i, 'Connect does not attach an agent');
assert.match(visible(html), /shown once/i);
assert.equal(html.split(result.token).length - 1, 1, 'the token is rendered once');
assert.match(html, /<input data-project-token value="synthetic-once-token" readonly/);
assert.match(ui.PROJECT_COPY.allowShared, /read and (write|change)/i, 'allow-shared names its scope');
assert.match(ui.sharedWingHTML({}, e), /data-wing-choice="shared">[^<]*read and (write|change)[^<]*all shared memory/i);
""")


def test_connect_result_shows_recommended_webhook_events():
    _run(r"""
const result = {token: 'synthetic-once-token', mcp_url: 'http://localhost/mcp', board: 'alpha',
  setup: {webhook_add: {events: ['space_message', 'card.moved'], board: 'alpha'}}};
const html = ui.connectResultHTML(result, e);
const step = html.match(/<li\b[^>]*data-connect-step="webhook"[^>]*>[\s\S]*?<\/li>/);
assert.ok(step, 'the dialog has a webhook step');
assert.ok(step[0].includes(e(JSON.stringify(result.setup.webhook_add.events))),
  'the hint preserves the recommended event array');
assert.match(step[0], /and board alpha/);
const enabledEvents = ['space_message', 'card.moved', 'card.created'];
const enabled = ui.connectResultHTML({...result,
  setup: {webhook_add: {events: enabledEvents, board: 'alpha'}}}, e);
assert.ok(enabled.includes(e(JSON.stringify(enabledEvents))),
  'orchestration retains both card-created and completion recommendations');
const unsafe = ui.connectResultHTML({...result,
  setup: {webhook_add: {events: ['<unsafe>'], board: '<board>'}}}, e);
assert.doesNotMatch(unsafe, /<unsafe>|<board>/);
assert.match(unsafe, /&lt;unsafe&gt;/);
""")
