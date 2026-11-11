/** Owner screen for runner machines: enroll, attest and edit wings. Tokens live only in the reveal panel. */
const MODES = ['approval-required', 'auto-accept-edits', 'auto', 'full-access'];
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
  let host = null, epoch = 0, machines = [], wings = [], wingsError = false, busy = false;
  const region = name => host?.querySelector(`[data-runner-${name}]`);
  const status = (text, bad = false) => {
    const node = region('status');
    if (node) { node.textContent = text; node.dataset.state = bad ? 'error' : 'ok'; }
  };
  const wingBoxes = (selected, name = 'wings') => {
    const all = [...new Set([...wings, ...selected])].sort();
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
      <p>${e(RESET_NOTE)}</p><button type="submit" class="btn">Save wings</button></form>` : '';
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
    const field = (name, label, value) => `<label>${e(label)}<input data-runner-secret="${e(name)}" value="${e(value)}" readonly autocomplete="off" spellcheck="false"></label><button type="button" class="btn" data-runner-action="copy" data-runner-copy="${e(name)}">Copy ${e(label.toLowerCase())}</button>`;
    region('reveal').innerHTML = `<div class="agent-secret" role="group" aria-label="New runner credentials"><p><strong>Copy both tokens now. They are not shown again.</strong></p>${field('machine', 'Machine token', result.machine_token)}${field('worker', 'Worker token', result.worker_token)}<p>Next: attest this machine below once its worker is set up.</p><button type="button" class="btn" data-runner-action="dismiss">Done</button></div>`;
  };

  async function load(resetForm = false) {
    const mine = epoch;
    const [list, conns] = await Promise.allSettled([api('/api/runners'), api('/api/connections')]);
    if (mine !== epoch) return;
    if (list.status === 'rejected') { status(list.reason?.message || 'Runner machines unavailable. Reload to try again.', true); return; }
    machines = list.value.machines || [];
    wingsError = conns.status === 'rejected';
    wings = wingsError ? [] : conns.value.wings || [];
    renderList(resetForm);
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
      } else {
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

  async function onClick(event) {
    const button = event.target.closest?.('[data-runner-action]');
    if (!button) return;
    if (button.dataset.runnerAction === 'dismiss') { region('reveal').replaceChildren(); status(''); return; }
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
    epoch++; busy = false; machines = []; wings = [];
    if (host) {
      host.removeEventListener('submit', onSubmit);
      host.removeEventListener('click', onClick);
      host.innerHTML = '';
    }
    host = null;
  }

  return { show, stop };
}
