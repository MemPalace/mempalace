/** Read-first workspace administration, with independently bounded section loads. */
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

export function createAdmin({ mount, api, e, current, timers = globalThis }) {
  let host = null, workspace = null, epoch = 0, live = false;
  const sections = new Map(), requests = new Map(), generations = new Map();
  const owner = () => current()?.can_admin === true;
  const eligible = () => live && owner() && current()?.workspace === workspace;
  const label = key => key === 'users' ? 'Users' : key === 'connections' ? 'Connections' : SECTIONS[key];
  const value = v => e(v == null || v === '' ? '—' : v);
  const rows = (headers, items) => `<table><thead><tr>${headers.map(h => `<th scope="col">${e(h)}</th>`).join('')}</tr></thead><tbody>${items.map(cells => `<tr>${cells.map((v, i) => `<td data-label="${e(headers[i])}">${value(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;

  function content(key, data) {
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

  function render() {
    if (!host || !live) return;
    host.innerHTML = `<div class="admin settings"><p class="admin-workspace">${e(current().workspace_name || '')}</p><a href="#/settings">Manage agents and workspace settings</a>${Object.entries(SECTIONS).map(([key, title]) => {
      const item = sections.get(key);
      if (!item) return `<section class="settings-section" data-admin-section="${key}"><h2>${e(title)}</h2><p>Available in a later slice</p></section>`;
      const busy = requests.has(key);
      const button = (action, text) => `<button type="button" class="btn" data-admin-section="${key}" data-admin-action="${action}" aria-label="${e(text)} ${e(label(key))}"${busy ? ' disabled' : ''}>${e(text)}</button>`;
      return `<section class="settings-section" data-admin-section="${key}" aria-busy="${busy}"><h2>${e(title)}</h2>${item.data ? content(key, item.data) : ''}${busy ? `<p role="status">${item.data ? 'Refreshing' : 'Loading'} ${e(label(key))}…</p>` : ''}${item.error ? `<p role="status">${e(item.error)}</p>${item.restricted ? '' : button('retry', 'Retry')}` : ''}${item.data ? `<p class="quiet">Recorded at ${value(item.data.recorded_at)}</p>${button('refresh', 'Refresh')}${item.data.has_more ? button('more', 'Load more') : ''}` : ''}</section>`;
    }).join('')}</div>`;
  }

  function stop() {
    live = false; epoch++;
    for (const request of requests.values()) request.abort();
    requests.clear(); sections.clear(); generations.clear();
    host?.removeEventListener('click', click);
    globalThis.document?.removeEventListener('session-expired', stop);
    globalThis.document?.removeEventListener('signout', stop);
    if (host) host.innerHTML = '';
    workspace = null;
  }

  function refreshSection(key, { more = false, cursor: requestedCursor } = {}) {
    if (!eligible()) { if (live) stop(); return; }
    if (!['users', 'connections'].includes(key) || requests.has(key)) return;
    const generation = (generations.get(key) || 0) + 1, pageEpoch = epoch;
    generations.set(key, generation);
    const controller = new AbortController(), item = sections.get(key);
    const cursor = more ? requestedCursor ?? item.data?.next_cursor : null;
    if (more && !cursor) return;
    requests.set(key, controller);
    item.error = ''; item.restricted = false;
    render();
    const valid = () => {
      if (live && !eligible()) stop();
      return eligible() && epoch === pageEpoch && generations.get(key) === generation;
    };
    const path = `/api/admin/workspaces/${encodeURIComponent(workspace)}/${key}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`;
    return boundedRequest(api, path, { timers, signal: controller.signal }).then(data => {
      if (!valid()) return;
      if (data.workspace_id !== workspace) throw new Error('Invalid workspace response');
      if (more) {
        const field = key === 'users' ? 'members' : 'connections';
        data = { ...data, [field]: [...item.data[field], ...data[field]] };
      }
      item.data = data; item.retry = null;
    }).catch(error => {
      if (!valid()) return;
      item.retry = { more, cursor };
      if (error.status === 401) { stop(); return; }
      if (error.status === 403) {
        item.data = null; item.restricted = true;
        item.error = 'a signed-in workspace owner is required';
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
    const { adminSection: key, adminAction: action } = button.dataset;
    void refreshSection(key, action === 'retry' ? sections.get(key)?.retry : { more: action === 'more' });
  }

  function show() {
    stop(); host = mount();
    if (!host) return;
    if (!owner()) { host.innerHTML = '<p role="status">a signed-in workspace owner is required</p>'; return; }
    workspace = current().workspace; live = true;
    for (const key of ['users', 'connections']) sections.set(key, { data: null, error: '', restricted: false });
    host.addEventListener('click', click);
    globalThis.document?.addEventListener('session-expired', stop);
    globalThis.document?.addEventListener('signout', stop);
    render();
    void refreshSection('users'); void refreshSection('connections');
  }
  return { show, refreshSection, stop };
}
