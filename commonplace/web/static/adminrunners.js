/** Owner screen for runner machines: enroll, attest, edit wings and revoke. Tokens live only in the reveal panel. */
const MODES = ['approval-required', 'auto-accept-edits', 'auto', 'full-access'];
const SETUP_NOTE = 'Setup: put the machine token in the environment variable the runner config names as token_env, and the worker token in the Commonplace MCP entry of the worker\'s provider instance. Set hub_url to this site\'s address, then run "python -m commonplace runner check --config <file>" before starting the runner. See docs/runner.md.';
// The wings read goes through the memory backend and can be slow; the machine list must never wait on it.
const LIST_TIMEOUT_MS = 15000, WINGS_TIMEOUT_MS = 30000;
const RESET_NOTE = 'Changing wings or topics resets this machine to unbound: queued and leased starts are cancelled and the machine must be attested again. Saving the same wings only refreshes the worker\'s access.';

export const parseList = text => [...new Set(String(text || '').split(',').map(part => part.trim()).filter(Boolean))];

// Reads named controls through form.elements; checkbox groups become arrays.
export function fieldValues(form) {
  const values = {};
  for (const field of form.elements) {
    if (!field.name) continue;
    if (field.type === 'checkbox') {
      values[field.name] ??= [];
      if (field.checked) values[field.name].push(field.value);
    } else values[field.name] = field.value;
  }
  return values;
}

export function enrollBody(values) {
  return {
    label: String(values.label || '').trim(),
    projects: parseList(values.projects),
    topics: parseList(values.topics),
    wings: values.wings || [],
    max_runtime_mode: values.max_runtime_mode,
    max_in_flight: Number(values.max_in_flight),
  };
}

export function attestBody(values) {
  return {
    topics: parseList(values.topics),
    provider_instance_id: String(values.provider_instance_id || '').trim(),
    worker_connection_id: String(values.worker_connection_id || '').trim(),
  };
}

export function createRunnerBlock({ api, e, copy = text => globalThis.navigator.clipboard.writeText(text) }) {
  let host = null, epoch = 0, machines = [], wings = [], wingsError = false, wingsPending = true, busy = false, confirming = null, revealed = null;
  const region = name => host?.querySelector(`[data-runner-${name}]`);
  const status = (text, bad = false) => {
    const node = region('status');
    if (node) { node.textContent = text; node.dataset.state = bad ? 'error' : 'ok'; }
  };
  const wingBoxes = (selected, name = 'wings') => {
    const all = [...new Set([...wings, ...selected])].sort();
    if (wingsPending) return '<p>Loading wings…</p>';
    return all.length ? `<fieldset><legend>Wings</legend>${all.map(w => `<label><input type="checkbox" name="${name}" value="${e(w)}"${selected.includes(w) ? ' checked' : ''}>${e(w)}</label>`).join('')}</fieldset>` : `<p>${wingsError ? 'Wings could not be loaded. Reload to try again.' : 'No readable wings yet.'}</p>`;
  };
  const enrollForm = () => `<form class="agent-form" data-runner-form="enroll"><h3>Add runner</h3>
    <label>Label<input name="label" required maxlength="40" pattern="[a-z0-9][a-z0-9._-]*" autocomplete="off"></label>
    <label>Projects (comma separated)<input name="projects" required autocomplete="off"></label>
    <label>Topics (comma separated)<input name="topics" required autocomplete="off"></label>
    ${wingBoxes([])}
    <label>Max runtime mode<select name="max_runtime_mode">${MODES.map(m => `<option value="${e(m)}">${e(m)}</option>`).join('')}</select></label>
    <label>Max in flight<input name="max_in_flight" type="number" min="1" max="4" value="1" required></label>
    <button type="submit" class="btn">Add runner</button></form>`;
  // Revoke is one click to ask, one to confirm; the confirm names the machine and what stops working.
  const revokeControl = m => {
    const id = e(m.machine_id);
    if (confirming !== m.machine_id) return `<p><button type="button" class="btn" data-runner-action="revoke" data-runner-id="${id}" aria-label="Revoke ${e(m.label)}">Revoke</button></p>`;
    return `<div role="group" aria-label="Confirm revoking ${e(m.label)}"><p>Revoke ${e(m.label)}? Its machine and worker tokens stop working and its queued and leased starts are cancelled. This cannot be undone; enroll a new machine to replace it.</p>
      <button type="button" class="btn" data-runner-action="revoke-confirm" data-runner-id="${id}" aria-label="Confirm revoking ${e(m.label)}">Revoke machine</button>
      <button type="button" class="btn" data-runner-action="revoke-cancel" data-runner-id="${id}">Keep machine</button></div>`;
  };
  const machineView = m => {
    const id = e(m.machine_id), live = !m.revoked_at;
    const state = m.revoked_at ? 'revoked' : m.enabled ? 'enabled' : 'disabled';
    const forms = live ? `<form class="agent-form" data-runner-form="attest" data-runner-id="${id}"><h4>Attest</h4>
      <p>Records your statement that this worker is set up for these topics. It is not verified.</p>
      <label>Topics (comma separated)<input name="topics" required value="${e(m.topics.join(', '))}" autocomplete="off"></label>
      <label>Provider instance id<input name="provider_instance_id" required value="${e(m.binding_provider_instance_id || '')}" autocomplete="off"></label>
      <label>Worker connection id<input name="worker_connection_id" required value="${e(m.worker_connection_id)}" autocomplete="off"></label>
      <button type="submit" class="btn">Attest</button></form>
      <form class="agent-form" data-runner-form="wings" data-runner-id="${id}"><h4>Edit wings</h4>${wingBoxes(m.wings)}
      <p>${e(RESET_NOTE)}</p><button type="submit" class="btn">Save wings</button></form>${revokeControl(m)}` : '';
    return `<article class="runner-machine" aria-label="${e(m.label)}"><h3>${e(m.label)}</h3>
      <p>${e(state)} · ${e(m.binding_state)}, not verified · ${e(m.max_runtime_mode)} · ${e(m.max_in_flight)} in flight</p>
      <p>Projects: ${e(m.projects.join(', '))} · Topics: ${e(m.topics.join(', '))} · Wings: ${e(m.wings.join(', '))}</p>${forms}</article>`;
  };
  const renderList = resetForm => {
    const node = region('list');
    if (node) node.innerHTML = machines.length ? machines.map(machineView).join('') : '<p>No runner machines yet.</p>';
    const form = region('enroll-slot');
    if (form && resetForm) form.innerHTML = enrollForm();
  };
  const reveal = result => {
    revealed = result.machine_id || null;
    const field = (name, label, value) => `<label>${e(label)}<input data-runner-secret="${e(name)}" value="${e(value)}" readonly autocomplete="off" spellcheck="false"></label><button type="button" class="btn" data-runner-action="copy" data-runner-copy="${e(name)}">Copy ${e(label.toLowerCase())}</button>`;
    region('reveal').innerHTML = `<div class="agent-secret" role="group" aria-label="New runner credentials"><p><strong>Copy both tokens now. They are not shown again.</strong></p>${field('machine', 'Machine token', result.machine_token)}${field('worker', 'Worker token', result.worker_token)}<p>${e(SETUP_NOTE)}</p><p>Next: attest this machine below once its worker is set up.</p><button type="button" class="btn" data-runner-action="dismiss">Done</button></div>`;
  };

  async function load(resetForm = false) {
    const mine = epoch;
    const wingsRead = api('/api/connections', { timeoutMs: WINGS_TIMEOUT_MS }).then(result => ({ wings: result.wings || [], failed: false }), () => ({ wings: [], failed: true }));
    try {
      const list = await api('/api/runners', { timeoutMs: LIST_TIMEOUT_MS });
      if (mine !== epoch) return;
      machines = list.machines || [];
      renderList(resetForm);
      const read = await wingsRead;
      if (mine !== epoch) return;
      const changed = wingsPending || wingsError !== read.failed || wings.join('\n') !== read.wings.join('\n');
      wings = read.wings; wingsError = read.failed; wingsPending = false;
      if (changed) renderList(true);
    } catch (error) {
      if (mine !== epoch) return;
      const node = region('list');
      if (node) node.innerHTML = '<p>Runner machines could not be loaded. Reload to try again.</p>';
      status(error?.message || 'Runner machines unavailable. Reload to try again.', true);
    }
  }

  async function submit(kind, id, values) {
    if (busy) return;
    busy = true; status('Saving…');
    const mine = epoch;
    try {
      if (kind === 'enroll') {
        const result = await api('/api/runners', { method: 'POST', body: enrollBody(values) });
        if (mine !== epoch) return;
        reveal(result);
        status('Runner added. Copy the tokens below; they are not shown again.');
      } else if (kind === 'attest') {
        await api(`/api/runners/${encodeURIComponent(id)}/attest`, { method: 'POST', body: attestBody(values) });
        if (mine !== epoch) return;
        status('Attested. This is your statement; Commonplace does not verify it.');
      } else if (kind === 'wings') {
        if (!(values.wings || []).length) throw new Error('Choose at least one wing.');
        await api(`/api/runners/${encodeURIComponent(id)}`, { method: 'PATCH', body: { wings: values.wings } });
        if (mine !== epoch) return;
        status('Wings saved. Attest the machine again if it was reset.');
      }
      await load(kind === 'enroll');
    } catch (error) {
      if (mine === epoch) status(error.message || 'Request failed', true);
    } finally {
      busy = false;
    }
  }

  function onSubmit(event) {
    const form = event.target.closest?.('[data-runner-form]');
    if (!form) return;
    event.preventDefault();
    void submit(form.dataset.runnerForm, form.dataset.runnerId, fieldValues(form));
  }

  async function revoke(id) {
    if (busy) return;
    busy = true; status('Revoking…');
    const mine = epoch;
    try {
      await api(`/api/runners/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (mine !== epoch) return;
      confirming = null;
      if (revealed === id) { region('reveal').replaceChildren(); revealed = null; }
      status('Machine revoked. Its tokens no longer work.');
      await load();
    } catch (error) {
      if (mine === epoch) { confirming = null; renderList(false); status(error.message || 'Request failed', true); }
    } finally {
      busy = false;
    }
  }

  async function onClick(event) {
    const button = event.target.closest?.('[data-runner-action]');
    if (!button) return;
    const action = button.dataset.runnerAction, id = button.dataset.runnerId;
    if (action === 'dismiss') { region('reveal').replaceChildren(); revealed = null; status(''); return; }
    if (action === 'revoke' || action === 'revoke-cancel') { confirming = action === 'revoke' ? id : null; renderList(false); return; }
    if (action === 'revoke-confirm') { await revoke(id); return; }
    const input = region('reveal').querySelector(`[data-runner-secret="${button.dataset.runnerCopy}"]`);
    try { await copy(input.value); status('Copied.'); } catch { status('Copy failed. Select the value and copy it manually.', true); }
  }

  function show(container) {
    stop();
    host = container;
    host.innerHTML = `<section class="settings-section runner-block" aria-labelledby="runner-heading"><h2 id="runner-heading">Runner machines</h2>
      <p>Enroll a machine so one worker can start and work cards on every wing you grant it. The worker can write only to those wings. Attestation is your own statement and is not verified.</p>
      <div data-runner-reveal></div><p role="status" aria-live="polite" data-runner-status></p>
      <div data-runner-enroll-slot></div><div data-runner-list><p>Loading runner machines…</p></div></section>`;
    host.addEventListener('submit', onSubmit);
    host.addEventListener('click', onClick);
    void load(true);
  }

  function stop() {
    epoch++; busy = false; machines = []; wings = []; wingsError = false; wingsPending = true; confirming = null; revealed = null;
    if (host) {
      host.removeEventListener('submit', onSubmit);
      host.removeEventListener('click', onClick);
      host.innerHTML = '';
    }
    host = null;
  }

  return { show, stop };
}
