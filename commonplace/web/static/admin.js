/** Read-first workspace administration, with independently bounded section loads. The one write is the owner's machine requester list. */
const SECTIONS = {
  users: 'Users and workspaces', connections: 'Agents and connections',
  runtime: 'Orchestrators and runners/machines', limits: 'Agent limits',
  stuck: 'Needs-me and stuck cards', deploy: 'Deployment and health', backups: 'Backups',
};

// api includes response.json(): the deadline covers both transport and body decoding.
export async function boundedRequest(api, path, { timers = globalThis, signal } = {}) {
  const controller = new AbortController();
  let timer, abort;
  const deadline = new Promise((_, reject) => {
    abort = () => { controller.abort(); reject(Object.assign(new Error('Request cancelled'), { name: 'AbortError' })); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    timer = timers.setTimeout(() => {
      controller.abort();
      reject(Object.assign(new Error('Request timed out'), { name: 'TimeoutError' }));
    }, 5000);
  });
  try {
    if (controller.signal.aborted) return await deadline;
    return await Promise.race([deadline, api(path, { signal: controller.signal })]);
  } finally {
    timers.clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export function createAdmin({ mount, api, e, current, runners = null, timers = globalThis }) {
  let host = null, body = null, workspace = null, epoch = 0, live = false, instanceBlocked = false;
  const sections = new Map(), requests = new Map(), generations = new Map(), writing = new Set(), notes = new Map();
  const owner = () => current()?.can_admin === true;
  const eligible = () => live && owner() && current()?.workspace === workspace;
  const operational = key => ['deploy','health','backups','backup-online','backup-offline'].includes(key);
  const instanceOwner = () => current()?.can_admin_instance === true && !instanceBlocked;
  const label = key => ({health:'Health','backup-online':'Online backup','backup-offline':'Offline backup',users:'Users',connections:'Connections',needs:'Needs-me',stuck:'Stuck',runtime:'Runtime',limits:'Agent limits'})[key] || SECTIONS[key];
  const value = v => e(v == null || v === '' ? '—' : v);
  const rows = (headers, items) => `<table><thead><tr>${headers.map(h => `<th scope="col">${e(h)}</th>`).join('')}</tr></thead><tbody>${items.map(cells => `<tr>${cells.map((v, i) => `<td data-label="${e(headers[i])}">${value(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;

  const cardLink = card => Number.isSafeInteger(card.number) && card.number > 0
    ? `<a href="#/card/${card.number}">${e('#' + card.number)} ${e(card.title || '')}</a>` : '';
  const contact = activity => activity === 'working' ? 'recent contact' : activity === 'unused' || activity == null ? 'never contacted' : 'no recent contact';
  const moreButton = (key, kind, data) => data?.has_more ? `<button type="button" class="btn" data-admin-section="${key}" data-admin-action="more" data-admin-list="${kind}" aria-label="Load more ${e(kind)}"${requests.has(key) ? ' disabled' : ''}>Load more ${e(kind)}</button>` : '';
  const sourceError = (key, title, data) => `<p role="status">${e(data?.error || title + ' unavailable; try again')}</p><button type="button" class="btn" data-admin-section="${key}" data-admin-action="retry" aria-label="Retry ${e(title)}"${requests.has(key) ? ' disabled' : ''}>Retry ${e(title)}</button>`;

  function runtimeContent(data) {
    const flag = data.orchestration, agents = data.orchestrators, machines = data.machines;
    let html = flag.state === 'ready' ? `<p>Orchestration ${flag.enabled ? 'enabled' : 'disabled'}</p>` : sourceError('runtime', 'Orchestration', flag);
    html += '<h3>Orchestrators</h3><p>Activity records token use, not a running process.</p>';
    html += agents.state === 'ready' ? `<p>${e(agents.count)} projects</p>` + (agents.items.length ? rows(['Project', 'Coordinator', 'Configuration', 'Connection', 'Activity', 'Last used', 'Wakeable'], agents.items.map(r => [r.name, r.coordinator || r.seat_name, ({coordinator:'Coordinator',seat:'Stored seat',not_configured:'Not configured'})[r.source], r.connected ? 'Connected' : 'Not connected', r.activity, r.last_used_at, r.wakeable ? 'Eligible for a wake' : 'No eligible subscription'])) : '<p>No projects configured.</p>') + moreButton('runtime', 'orchestrators', agents) : sourceError('runtime', 'Orchestrators', agents);
    if (agents.state === 'ready' && agents.error) html += sourceError('runtime', 'Orchestrators', agents);
    html += '<h3>Registered machines</h3><p>Contact records activity, not runner liveness. Owner attestation is not verified.</p>';
    html += machines.state === 'ready' ? `<p>${e(machines.count)} machines</p>` + (machines.items.length ? rows(['Machine', 'State', 'Binding', 'Machine contact', 'Worker contact', 'Limits'], machines.items.map(r => [r.label, r.revoked_at ? 'revoked' : r.enabled ? 'enabled' : 'disabled', `${r.binding_state}; not verified`, `${contact(r.activity)} · ${r.machine_last_used_at || '—'}`, `${contact(r.worker_activity)} · ${r.worker_last_used_at || '—'}`, `${r.max_in_flight} in flight; ${r.max_starts_per_hour} starts/hour`])) : '<p>No registered machines.</p>') + moreButton('runtime', 'machines', machines) : sourceError('runtime', 'Machines', machines);
    if (machines.state === 'ready' && machines.error) html += sourceError('runtime', 'Machines', machines);
    if (machines.state === 'ready') html += requesterContent(machines);
    return html;
  }

  // Owner-only: who may ask a machine to start work. Each change names one agent; the server re-checks the owner and vets an addition.
  function requesterContent(machines) {
    const unrevoked = (machines.items || []).filter(r => !r.revoked_at);
    if (!unrevoked.length) return '';
    const offered = machines.requester_candidates || [];
    const button = (action, machine, ref, text, name) => `<button type="button" class="btn" data-admin-section="runtime" data-admin-action="${action}" data-admin-machine="${e(machine)}" data-admin-requester="${e(ref)}" aria-label="${e(text)} ${e(name)}"${writing.has(machine) ? ' disabled' : ''}>${e(text)}</button>`;
    return '<h3>Machine requesters</h3><p>Requesters are the trusted agents allowed to ask a machine to start work. Only a workspace owner can change them. Each Add or Remove changes that one agent and leaves the rest of the list as stored.</p>' + unrevoked.map(r => {
      const chosen = r.requesters || [], addable = offered.filter(c => !chosen.some(n => n.id === c.id));
      const name = n => n.name || n.id;
      return `<div data-admin-machine="${e(r.id)}"><h4>${value(r.label)}</h4>` +
        (chosen.length ? `<ul>${chosen.map(n => `<li>${value(name(n))}${n.state === 'active' ? '' : ' (inactive)'} ${button('requester-remove', r.id, n.id, 'Remove', name(n))}</li>`).join('')}</ul>` : '<p>No requesters.</p>') +
        (r.requesters_total > chosen.length ? `<p>Showing ${e(chosen.length)} of ${e(r.requesters_total)} requesters; the rest stay as stored.</p>` : '') +
        (addable.length ? `<ul>${addable.map(c => `<li>${value(c.name)} ${button('requester-add', r.id, c.id, 'Add', c.name)}</li>`).join('')}</ul>` : '<p>No other trusted agents to add.</p>') +
        (machines.requester_candidates_more ? `<p>Showing the first ${e(offered.length)} trusted agents.</p>` : '') +
        (notes.has(r.id) ? `<p role="status">${e(notes.get(r.id))}</p>` : '') + '</div>';
    }).join('');
  }

  async function changeRequesters({ adminAction: action, adminMachine: machine, adminRequester: ref }) {
    const row = sections.get('runtime')?.data?.machines?.items?.find(r => r.id === machine);
    if (!eligible() || !row || row.revoked_at || !ref || writing.has(machine)) return;
    const pageEpoch = epoch;
    writing.add(machine); notes.delete(machine); render();
    try {
      // One agent at a time: the server applies it to the stored list, never to the list this page shows.
      // If-Match carries the grant version this page read; a newer one means another edit came first.
      const base = `/api/runners/${encodeURIComponent(machine)}/requesters`, headers = { 'If-Match': String(row.grant_version) };
      const result = await (action === 'requester-add'
        ? api(base, { method: 'POST', body: { requester: ref }, headers, timeoutMs: 10000 })
        : api(`${base}/${encodeURIComponent(ref)}`, { method: 'DELETE', headers, timeoutMs: 10000 }));
      // The answer carries the machine's new version, so a second edit before the reload is not stale.
      // A read that landed meanwhile replaced the rows, so look the machine up again and keep the larger version.
      const stored = sections.get('runtime')?.data?.machines?.items?.find(r => r.id === machine);
      if (stored && Number.isInteger(result?.grant_version)) stored.grant_version = Math.max(stored.grant_version, result.grant_version);
      notes.set(machine, result?.changed === false ? (action === 'requester-add' ? 'Already a requester.' : 'Already removed.') : 'Requesters saved.');
    } catch (error) {
      notes.set(machine, error.status === 403 ? 'a signed-in workspace owner is required'
        : error.status === 409 ? 'This machine changed since the page loaded. The list was reloaded; try again.'
        : 'Could not save requesters. Try again.');
    } finally { writing.delete(machine); }
    if (epoch !== pageEpoch || !live) return;
    render();
    // A read already in flight predates this edit; drop it so the reload below shows the new list.
    requests.get('runtime')?.abort(); requests.delete('runtime');
    void refreshSection('runtime');
  }

  function limitsContent(data) {
    return '<p>Workspace concurrency cap: not configured. Project and machine limits apply separately.</p>' +
      (data.projects.length ? rows(['Project', 'Configured', 'Accepted config', 'Effective', 'Used', 'Counting mode'], data.projects.map(r => [r.name, r.configured_max_agents ?? 'Unlimited', r.accepted_config_max_agents, r.effective_max_agents ?? 'Unlimited', r.used, ({orchestration_slots:'Orchestration slots',distinct_doing_agents:'Distinct doing agents'})[r.counting_mode]])) : '<p>No project limits configured.</p>') + moreButton('limits', 'projects', data.pagination?.projects) +
      '<h3>Machine grants</h3>' + (data.machines.length ? rows(['Machine', 'In flight', 'Starts/hour'], data.machines.map(r => [r.id, r.max_in_flight, r.max_starts_per_hour])) : '<p>No machine grants configured.</p>') + moreButton('limits', 'machines', data.pagination?.machines);
  }

  function queueContent(data) {
    const titles = {asks:'Questions',done_review:'Completed work to review',backlog_from_agents:'Agent backlog',ready_to_merge:'Ready to merge',stale:'Stale owner queue',plans:'Plans',approvals:'Approvals'};
    let html = `<p>${e(data.count || 0)} card${data.count === 1 ? '' : 's'} waiting</p>`;
    if (!data.count) html += '<p>No cards waiting.</p>';
    for (const [key, title] of Object.entries(titles)) {
      if (data[key]?.length) html += `<h4>${e(title)}</h4><ul>${data[key].map(c => `<li>${cardLink(c)}</li>`).join('')}</ul>`;
    }
    if (data.approvals_truncated) html += '<p>More approvals may be waiting. Open the owner queue for all details.</p>';
    return html;
  }

  function stuckContent(data) {
    const names = {expired_lease:'Expired lease',blocked:'Blocked',waiting_on_person:'Waiting on a person',stale_agent:'Stale agent'};
    return `<p>${e(data.count)} cards with recorded facts · Blockers ${data.blockers_enforced ? 'enforced' : 'not enforced'}</p><p>${Object.entries(data.reason_counts || {}).map(([key, n]) => `${e(names[key] || key)}: ${e(n)}`).join(' · ')}</p>` +
      (data.cards.length ? `<table><thead><tr>${['Card','State','Facts','Recorded timestamps'].map(h => `<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${data.cards.map(c => `<tr><td data-label="Card">${cardLink(c)}<small>${e(c.board)}</small></td><td data-label="State">${value(c.status)}<small>${value(c.assignee)}</small></td><td data-label="Facts">${c.reasons.map(r => e(names[r] || r)).join(', ')}${c.active_blockers.map(b => `<small>Blocked by ${cardLink(b)}</small>`).join('')}${c.expired_jobs.map(j => `<small>${value(j.job_id)} · ${value(j.state)} · ${value(j.lease_until)}</small>`).join('')}</td><td data-label="Recorded timestamps"><small>Claim until ${value(c.claim_until)}</small><small>Heartbeat ${value(c.heartbeat_at)}</small><small>Updated ${value(c.updated_at)}</small></td></tr>`).join('')}</tbody></table>` : '<p>No stuck facts recorded.</p>');
  }

  function content(key, data) {
    if (key === 'deploy') return rows(['Package', 'Version'], [['Commonplace', data.commonplace_version], ['MemPalace', data.mempalace_version]]) + `<p>${data.commit_source === 'build' && /^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$/.test(data.commit) ? 'Running commit ' + e(data.commit) : 'Running commit unknown'}</p>`;
    if (key === 'health') return '<p>Gateway reachable</p><p class="quiet">The gateway answered. This does not verify memory, runners, databases or backups.</p>';
    if (key === 'backups') return '<p>Last completed recorded attempt. Snapshot status does not verify remote upload or retention. Interrupted runs may leave the previous receipt.</p>' + '<h3>Online</h3>' + panel('backup-online') + '<h3>Offline</h3>' + panel('backup-offline');
    if (key.startsWith('backup-')) {
      if (data.state === 'unknown') return `<p>${data.reason === 'not_configured' ? 'Backup status not configured' : 'No recorded attempt'}</p>`;
      return rows(['Mode', 'Result', 'Started', 'Finished'], [[data.mode, data.result, data.started_at, data.finished_at]]);
    }
    if (key === 'runtime') return runtimeContent(data);
    if (key === 'limits') return limitsContent(data);
    if (key === 'needs') return queueContent(data);
    if (key === 'stuck') return stuckContent(data);
    if (key === 'users') {
      return `<p>${e(data.workspace.name)} · ${e(data.count)} member${data.count === 1 ? '' : 's'}</p>` +
        (data.members.length ? rows(['Name', 'Email', 'Role', 'Created', 'Joined', 'Disabled'], data.members.map(m => [m.name, m.email, m.role, m.created_at, m.joined_at, m.disabled_at])) : '<p>No members recorded.</p>');
    }
    const line = (name, text) => `<small>${e(name)} ${value(text)}</small>`;
    const cells = (name, html) => `<td data-label="${e(name)}"><div>${html}</div></td>`;
    return `<p>${e(data.count)} connection${data.count === 1 ? '' : 's'} · Activity records token use, not a running process.</p>` +
      (data.connections.length ? `<table><thead><tr>${['Agent', 'Permissions', 'State', 'Activity'].map(h => `<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${data.connections.map(c => `<tr>${
        cells('Agent', `<b>${value(c.name)}</b>${line('Client', c.client)}${line('Auth', c.auth)}${line('User', c.user_id)}`) +
        cells('Permissions', `<span>${value(c.trust)}</span>${line('Scopes', c.scopes.join(', '))}${line('Read wings', c.read_wings.join(', '))}`) +
        cells('State', `<span>${value(c.state)}</span>${line('Created', c.created_at)}${line('Expires', c.expires_at)}${line('Revoked', c.revoked_at)}`) +
        cells('Activity', `<span>${value(c.activity)}</span>${line('Last used', c.last_used_at)}`)
      }</tr>`).join('')}</tbody></table>` : '<p>No connections recorded.</p>');
  }

  function panel(key) {
    const item = sections.get(key), busy = requests.has(key);
    const button = (action, text) => `<button type="button" class="btn" data-admin-section="${key}" data-admin-action="${action}" aria-label="${e(text)} ${e(label(key))}"${busy ? ' disabled' : ''}>${e(text)}</button>`;
    return `<div data-admin-panel="${key}" aria-busy="${busy}">${item.data ? content(key, item.data) : ''}${busy ? `<p role="status">${item.data ? 'Refreshing' : 'Loading'} ${e(label(key))}…</p>` : ''}${item.error ? `<p role="status">${e(item.error)}</p>${item.restricted ? '' : button('retry', 'Retry')}` : ''}${item.data ? `${item.data.recorded_at ? `<p class="quiet">Recorded at ${value(item.data.recorded_at)}</p>` : ''}${button('refresh', 'Refresh')}${item.data.has_more ? button('more', 'Load more') : ''}` : ''}</div>`;
  }

  function render() {
    if (!host || !live) return;
    (body || host).innerHTML = `<div class="admin settings"><p class="admin-workspace">${e(current().workspace_name || '')}</p><a href="#/settings">Manage agents and workspace settings</a>${Object.entries(SECTIONS).map(([key, title]) => {
      if (!sections.has(key)) return '';
      if (key === 'deploy') return `<section class="settings-section" data-admin-section="deploy"><h2>${e(title)}</h2>${panel('deploy')}${panel('health')}</section>`;
      return `<section class="settings-section" data-admin-section="${key}" aria-busy="${requests.has(key)}"><h2>${e(title)}</h2>${key === 'stuck' ? '<h3>Needs-me</h3>' + panel('needs') + '<h3>Stuck facts</h3>' : ''}${panel(key)}</section>`;
    }).join('')}</div>`;
  }

  function stop() {
    live = false; epoch++;
    for (const request of requests.values()) request.abort();
    requests.clear(); sections.clear(); generations.clear(); writing.clear(); notes.clear();
    host?.removeEventListener('click', click);
    globalThis.document?.removeEventListener('session-expired', stop);
    globalThis.document?.removeEventListener('signout', stop);
    runners?.stop();
    if (host) host.innerHTML = '';
    body = null; workspace = null;
  }

  function refreshSection(key, { more = false, list = null, cursor: requestedCursor } = {}) {
    if (!eligible()) { if (live) stop(); return; }
    if (!sections.has(key) || requests.has(key) || (operational(key) && !instanceOwner())) return;
    const generation = (generations.get(key) || 0) + 1, pageEpoch = epoch;
    generations.set(key, generation);
    const controller = new AbortController(), item = sections.get(key);
    const pagination = list ? (key === 'runtime' ? item.data?.[list] : item.data?.pagination?.[list]) : item.data;
    const cursor = more ? requestedCursor ?? pagination?.next_cursor : null;
    if (more && !cursor) return;
    requests.set(key, controller);
    item.error = ''; item.restricted = false;
    render();
    const valid = () => {
      if (live && !eligible()) stop();
      return eligible() && epoch === pageEpoch && generations.get(key) === generation;
    };
    const path = key === 'health' ? '/healthz' : operational(key) ? `/api/admin/instance/${key.startsWith('backup-') ? 'backups' : key}` : key === 'needs' ? '/api/needs-me' : `/api/admin/workspaces/${encodeURIComponent(workspace)}/${key}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`;
    return boundedRequest(api, path, { timers, signal: controller.signal }).then(data => {
      if (!valid()) return;
      if (key !== 'needs' && !operational(key) && data.workspace_id !== workspace) throw new Error('Invalid workspace response');
      if (more) {
        const unique = (old, next, id) => [...new Map([...old, ...next].map(row => [row[id], row])).values()];
        if (key === 'runtime') {
          if (data[list].state !== 'ready') {
            item.data = {...item.data, [list]: {...item.data[list], error:data[list].error}};
            item.retry = {more, cursor, list};
            return;
          }
          const merged = {...item.data, [list]: {...data[list], items:unique(item.data[list].items, data[list].items, 'id')}};
          data = merged;
        } else if (key === 'limits') {
          data = {...item.data, [list]:unique(item.data[list], data[list], 'id'), pagination:{...item.data.pagination, [list]:data.pagination[list]}};
        } else {
          const field = {users:'members',connections:'connections',stuck:'cards'}[key];
          data = {...data, [field]:unique(item.data[field], data[field], key === 'stuck' ? 'number' : 'id')};
        }
      }
      if (key === 'health' && data?.ok !== true) throw new Error('Invalid health response');
      if (key === 'backups' || key.startsWith('backup-')) {
        const modes = key === 'backups' ? ['online','offline'] : [key.slice(7)];
        for (const mode of modes) {
          const state = sections.get('backup-' + mode), receipt = data[mode];
          if (!['ready','unknown','error'].includes(receipt?.state)) throw new Error('Invalid receipt response');
          if (key === 'backups') {
            const child = 'backup-' + mode;
            generations.set(child, (generations.get(child) || 0) + 1);
            requests.get(child)?.abort(); requests.delete(child);
          }
          state.data = receipt.state === 'error' ? null : receipt;
          state.error = receipt.state === 'error' ? 'Backup status unavailable. Retry.' : '';
          state.restricted = false;
        }
        if (key.startsWith('backup-')) return;
      }
      item.data = data; item.retry = null;
    }).catch(error => {
      if (!valid()) return;
      item.retry = { more, cursor, list };
      if (error.status === 401) { stop(); return; }
      if (error.status === 403) {
        if (operational(key)) {
          instanceBlocked = true;
          for (const [name, state] of sections) if (operational(name)) {
            state.data = null; state.restricted = true; state.error = 'Instance administrator access required';
            requests.get(name)?.abort(); requests.delete(name);
            generations.set(name, (generations.get(name) || 0) + 1);
          }
          render();
        } else {
          item.data = null; item.restricted = true;
          item.error = 'a signed-in workspace owner is required';
        }
      } else if (error.name !== 'AbortError') {
        item.error = `${label(key)} ${error.name === 'TimeoutError' ? 'timed out' : 'unavailable'}. Retry.`;
      }
    }).finally(() => {
      if (!valid()) return;
      requests.delete(key);
      // Rendering/schema failures settle as a section error, too.
      try { render(); } catch {
        item.data = null; item.error = `${label(key)} unavailable. Retry.`; render();
      }
    });
  }

  function click(event) {
    const button = event.target.closest('[data-admin-action]');
    if (!button) return;
    const { adminSection: key, adminAction: action, adminList: list } = button.dataset;
    if (action === 'requester-add' || action === 'requester-remove') { void changeRequesters(button.dataset); return; }
    void refreshSection(key, action === 'retry' ? sections.get(key)?.retry ?? {} : { more: action === 'more', list });
  }

  function show() {
    stop(); host = mount();
    if (!host) return;
    if (!owner()) { host.innerHTML = '<p role="status">a signed-in workspace owner is required</p>'; return; }
    workspace = current().workspace; live = true; instanceBlocked = false;
    // The runner block sits beside the refreshing sections so its form and one-time tokens survive their redraws.
    if (runners) {
      host.innerHTML = '<div data-admin-body></div><div data-admin-runners></div>';
      body = host.querySelector('[data-admin-body]');
      runners.show(host.querySelector('[data-admin-runners]'));
    }
    for (const key of ['users', 'connections', 'runtime', 'limits', 'stuck', 'needs']) sections.set(key, { data: null, error: '', restricted: false });
    for (const key of ['deploy','health','backups','backup-online','backup-offline']) sections.set(key, { data:null, error:instanceOwner() ? '' : 'Instance administrator access required', restricted:!instanceOwner() });
    host.addEventListener('click', click);
    globalThis.document?.addEventListener('session-expired', stop);
    globalThis.document?.addEventListener('signout', stop);
    render();
    for (const key of sections.keys()) if (!key.startsWith('backup-')) void refreshSection(key);
  }
  return { show, refreshSection, stop };
}
