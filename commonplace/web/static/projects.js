export const PROJECT_COPY = Object.freeze({
  connected: "connected", disconnected: "not connected", unused: "Token created · no agent has used it yet",
  statusPrivate: "status private", connect: "Connect agent", settings: "Project settings",
  sharedExplain: "This project keeps its memory in the shared section that every project uses. An agent connected here could read and change all of it.",
  ownWing: "Give this project its own memory section",
  allowShared: "Allow this agent to read and write all shared memory",
  ownWingEffect: "Agents that can only read shared memory will stop seeing this project. Its coordinator may be cleared. This can't be undone here.",
  ownWingDone: "This project now has its own memory section.",
  wingTaken: "Another project already uses this memory section name. Nothing was changed.",
  wingChanged: "Project memory changed while saving. Reload and try again.",
  ownWingError: "Could not change project memory. Please try again.",
  wingInUse: "Some agent connections would gain access to this project. Nothing was changed.",
  loadError: "Could not load project settings.", connectError: "Could not connect the agent.",
  saveError: "Could not save project models.", saved: "Project models saved.",
  copyError: "Copy failed. Select the token and copy it manually.",
});

export function sharedWingHTML(payload, e, { allowShared = true } = {}) {
  return `<section class="form-tile"><h2 id="project-wing-title">Project memory</h2><p>${e(payload.explanation || PROJECT_COPY.sharedExplain)}</p><p>${e(payload.fix?.effect || PROJECT_COPY.ownWingEffect)}</p><button type="button" class="blockbtn" data-wing-choice="own">${e(PROJECT_COPY.ownWing)}</button>${allowShared ? `<button type="button" class="opt" data-wing-choice="shared">${e(PROJECT_COPY.allowShared)}</button>` : ""}<button type="button" class="opt" data-wing-choice="cancel">Cancel</button></section>`;
}

// These are plain text: callers assign them with textContent.
export function ownWingError(payload = {}) {
  if (payload.error === "wing_taken") return PROJECT_COPY.wingTaken;
  if (payload.error === "wing_changed") return PROJECT_COPY.wingChanged;
  if (payload.error === "wing_in_use") {
    const names = (payload.connections || []).join(", ");
    return PROJECT_COPY.wingInUse + (names ? ` Connections: ${names}.` : "");
  }
  return PROJECT_COPY.ownWingError;
}

export function ownWingStatus(result) {
  const lines = result.changed ? [PROJECT_COPY.ownWingDone] : [];
  if (result.runner_linked?.length) lines.push(`Runner-managed connections remain active; manage them in the runner registry: ${result.runner_linked.join(", ")}.`);
  if (result.affected?.length) lines.push(`Connections losing project access: ${result.affected.map(c => `${c.name} (${c.lost}${c.runner ? "; runner-managed" : ""})`).join(", ")}.`);
  return lines.join(" ");
}

export function memoryHTML(project, e) {
  const memory = project.memory;
  if (!memory) return "";
  const label = memory.shared ? "shared section" : memory.wing.startsWith("project_") && !/[*?\[\]]/.test(memory.wing) ? "own section" : memory.wing;
  return `<div class="setting-row"><span>Memory: ${e(label)}</span>${memory.shared && project.can_configure ? `<button type="button" class="opt" data-project-own-wing>${e(PROJECT_COPY.ownWing)}</button>` : ""}</div>`;
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
const openDialogs = [];

function focusIfShown(node) {
  if (!node?.isConnected || node.disabled || !node.getClientRects().length) return false;
  node.focus();
  return document.activeElement === node;
}

// A native modal dialog still lets Tab reach the browser, so the top dialog wraps it.
// Closing returns focus to the opener, or to `fallback()` when the opener was re-rendered.
function projectDialog({ labelledby, opener, fallback = () => null, role = "", onClose = () => {} } = {}) {
  const dialog = document.createElement("dialog");
  dialog.className = "project-dialog";
  dialog.setAttribute("aria-labelledby", labelledby);
  dialog.setAttribute("aria-modal", "true");
  if (role) dialog.setAttribute("role", role);
  let closed = false;
  const hooks = [onClose];
  const self = { dialog, closed: () => closed, dismiss, show, focus, onDismiss: (fn) => hooks.push(fn) };
  function keys(event) {
    if (openDialogs.at(-1) !== self) return;
    // The page's own Escape handler would close the panel under the dialog, and its
    // preventDefault stops the native cancel, so the top dialog takes Escape first.
    if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); dismiss(); return; }
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll(FOCUSABLE)].filter(n => n.getClientRects().length);
    const at = document.activeElement, first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (!dialog.contains(at) || at === dialog) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
    else if (event.shiftKey && at === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && at === last) { event.preventDefault(); first.focus(); }
  }
  const leave = () => dismiss({ restore: false });
  function dismiss({ restore = true } = {}) {
    if (closed) return;
    closed = true;
    if (openDialogs.includes(self)) openDialogs.splice(openDialogs.indexOf(self), 1);
    document.removeEventListener("keydown", keys, true);
    window.removeEventListener("hashchange", leave);
    window.removeEventListener("pagehide", leave);
    document.removeEventListener("session-expired", leave);
    dialog.replaceChildren();
    dialog.remove();
    hooks.forEach(fn => fn());
    if (restore && !focusIfShown(opener)) focusIfShown(fallback());
  }
  function focus(selector) { dialog.querySelector(selector)?.focus(); }
  function show(selector) {
    installProjectStyles();
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
    dialog.addEventListener("close", () => dismiss());
    window.addEventListener("hashchange", leave);
    window.addEventListener("pagehide", leave);
    document.addEventListener("session-expired", leave);
    document.addEventListener("keydown", keys, true);
    document.body.append(dialog);
    dialog.showModal();
    openDialogs.push(self);
    if (selector) focus(selector);
  }
  return self;
}

export function confirmHTML({ title, body, action, danger = false }, e) {
  return `<h2 id="project-confirm-title">${e(title)}</h2>${body}<div class="acts"><button type="button" class="btn" data-confirm="cancel">Cancel</button><button type="button" class="btn${danger ? " danger" : ""}" data-confirm="ok">${e(action)}</button></div>`;
}

// The drawn replacement for window.confirm: Cancel has focus, Esc cancels.
function askConfirm(copy, e, { opener, fallback } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const modal = projectDialog({ labelledby: "project-confirm-title", role: "alertdialog", opener, fallback, onClose: () => resolve(answer) });
    modal.dialog.innerHTML = confirmHTML(copy, e);
    modal.dialog.querySelectorAll("[data-confirm]").forEach(button => {
      button.onclick = () => { answer = button.dataset.confirm === "ok"; modal.dismiss({ restore: !answer }); };
    });
    modal.show('[data-confirm="cancel"]');
  });
}

// Disconnect revokes the connection itself (agent-revoke), so the copy names the whole workspace.
// `others`: other projects with a known assignment (orchestrator or card); null when unreadable.
export function disconnectCopy(project, connection, { main = false, only = false, others = null } = {}, e) {
  const name = String(connection.name || "").replace(/^conn:/, "");
  const who = main ? `${project.name} orchestrator` : name;
  // Only known assignments are named: wing access elsewhere is not visible here, so no
  // exhaustive "no other project" claim is made, and the workspace-wide line always shows.
  const known = others === null ? "Its assignments in other projects could not be checked."
    : others.length ? `It is also assigned in ${others.map(other => `<b>${e(other)}</b>`).join(", ")}.`
    : "No known assignments in other projects.";
  const scope = `<p class="scope" data-disconnect-scope><b>This revokes the connection “${e(name)}” for the whole workspace</b>, not only in ${e(project.name)}. ${known} Revoking it stops all of its access in every project of this workspace, including memory it can read there.</p>`;
  const lines = [
    ...(main ? [`${e(project.name)} has <b>no orchestrator</b> until an agent connects. The next one you connect becomes it.`] : []),
    ...(main && only ? ["Chat still works: new requests wait, as before your first agent."] : []),
    "Cards, chat and memory stay exactly as they are.",
    `${e(who)}'s setup stops working. To use it again, connect it as a new agent.`,
  ];
  const alone = only ? `<div class="alert" role="note"><h3>It is the only agent in ${e(project.name)}</h3><p>No other agent can take over.</p></div>` : "";
  return { title: `Disconnect ${who}?`, action: "Disconnect everywhere", danger: true, body: `${alone}${scope}<p><b>What happens:</b></p><ul>${lines.map(line => `<li>${line}</li>`).join("")}</ul>` };
}

/** Names of the other projects a connection works in: their orchestrator, or a card assigned to it.
 * Returns null when the boards or their cards cannot be read. */
export async function connectionProjects(api, connection, exceptBoard) {
  const ref = `conn:${String(connection.name || "").replace(/^conn:/, "")}`;
  try {
    const { boards = [] } = await api("/api/boards");
    const found = await Promise.all(boards.filter(board => board.slug !== exceptBoard).map(async (board) => {
      if (board.coordinator === ref) return board;
      const page = await api(`/api/boards/${encodeURIComponent(board.slug)}/cards`);
      return [...page.cards || [], ...page.linked || [], ...page.children || []].some(card => card.assignee === ref) ? board : null;
    }));
    return found.filter(Boolean).map(board => board.name || board.slug);
  } catch { return null; }
}

function chooseSharedWing(payload, e, options) {
  return new Promise((resolve) => {
    let choice = "cancel";
    const modal = projectDialog({ labelledby: "project-wing-title", onClose: () => resolve(choice) });
    modal.dialog.innerHTML = sharedWingHTML(payload, e, options);
    modal.dialog.querySelectorAll("[data-wing-choice]").forEach(button => {
      button.onclick = () => { choice = button.dataset.wingChoice; modal.dismiss(); };
    });
    modal.show();
  });
}

export function mainHTML(project, e, { notAnswering = "" } = {}) {
  const main = project.main;
  // Connect only creates a token; "connected" waits until an agent has used it.
  const live = main.connected && main.seen;
  const state = live ? `${PROJECT_COPY.connected} · ${main.status || PROJECT_COPY.statusPrivate} · reads chat, makes cards` : main.connected ? PROJECT_COPY.unused : PROJECT_COPY.disconnected;
  const initial = String(project.name || "?").trim().charAt(0).toUpperCase() || "?";
  return `<div class="setting-row orch-row" data-project-main data-project-seat="${e(main.name)}"><span class="av" aria-hidden="true">${e(initial)}</span><span class="orch-text"><b>${e(project.name)} orchestrator</b>${live ? ' <span class="tag ok">Connected</span>' : ""}<small>${e(state)}</small></span>${project.can_configure && main.connectable ? `<button type="button" class="btn sm desktop-agent" data-project-connect>${e(main.connected ? "Reconnect…" : PROJECT_COPY.connect)}</button>` : ""}</div>${notAnswering ? `<p class="set-note" data-project-not-answering>${e(notAnswering)}</p>` : ""}`;
}

const clockTime = (value) => {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toTimeString().slice(0, 5) : "";
};

// `chat` is deriveChatState(page, status, …); "since" is the unanswered work message.
export function notAnsweringText(project, chat, page = {}) {
  if (chat?.state !== "no_answer") return "";
  const since = clockTime((page.messages || []).find(m => m.id === chat.message_id)?.at);
  const seen = clockTime(chat.last_seen);
  return `Not answering${since ? ` since ${since}` : ""} · ${project.main.connected ? PROJECT_COPY.connected : PROJECT_COPY.disconnected}${seen ? ` · last active ${seen}` : ""}`;
}

// The same agent apps and setup formats as workspace Settings > Agents (agents.js clientConfig).
export const AGENT_APPS = Object.freeze([["claude-code", "Claude Code"], ["codex", "Codex"], ["cursor", "Cursor"], ["generic", "Generic MCP"]]);
const appLabel = (app) => (AGENT_APPS.find(([id]) => id === app) || AGENT_APPS[0])[1];
const appSelect = (app) => `<label>Agent app<select data-project-app>${AGENT_APPS.map(([id, label]) => `<option value="${id}" ${id === app ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;

/** The MCP setup an agent app reads: address plus the key as a Bearer header. */
export async function connectSetup(app, result) {
  const { clientConfig } = await import("./agents.js");
  return clientConfig(AGENT_APPS.some(([id]) => id === app) ? app : "generic", result.mcp_url, result.token);
}

export function connectResultHTML(result, e, { app = "claude-code" } = {}) {
  const board = result.setup?.webhook_add?.board || result.board;
  const events = result.setup?.webhook_add?.events || ["space_message", "card.moved"];
  return `<section class="form-tile"><h2 id="project-connect-title">Agent connection</h2><p>A token for this project's orchestrator seat is ready. No agent is attached until one uses it. The token is shown once: copy it now.</p><ol class="dsteps" data-connect-steps><li data-connect-step="setup"><b>Copy the setup for your agent app</b><span class="d">It holds the MCP address and this token as a Bearer header, in the format the app reads.</span>${appSelect(app)}<button type="button" class="btn primary" data-project-copy-setup>Copy setup</button><details><summary>Show the setup to select it</summary><pre data-project-config></pre></details></li><li data-connect-step="paste"><b>Paste it where the app lists its MCP servers</b><span class="d">Some apps call them Tools or Connectors. Save the change.</span></li><li data-connect-step="start"><b>Start or restart the agent app</b><span class="d">The project shows the agent as connected once it uses the token.</span></li><li data-connect-step="webhook"><b>Optional: wake on messages and card moves</b><span class="d">Ask the agent to call webhook_add with events ${e(JSON.stringify(events))}${board ? ` and board ${e(board)}` : ""}.</span></li></ol><label>Token<input data-project-token value="${e(result.token)}" readonly autocomplete="off" spellcheck="false"></label><button type="button" class="opt" data-project-copy>Copy token</button><label>MCP URL<input value="${e(result.mcp_url)}" readonly></label>${result.warning ? `<p role="status">${e(result.warning)}</p>` : ""}<p role="status" data-project-copy-status></p><button type="button" class="blockbtn" data-project-dismiss>Close</button></section>`;
}

export function modelsHTML(project, e) {
  return `<form data-project-models><p>Leave both fields empty to use the runner's default for that role.</p>${["main", "worker"].map((role) => {
    const choice = project.models?.[role];
    return `<fieldset ${project.can_configure ? "" : "disabled"}><legend>${role === "main" ? "Main orchestrator" : "Worker"}</legend><label>Provider<input name="${role}_provider" value="${e(choice?.provider)}" maxlength="128" autocomplete="off"></label><label>Model<input name="${role}_model" value="${e(choice?.model)}" maxlength="120" autocomplete="off"></label></fieldset>`;
  }).join("")}${project.can_configure ? '<button class="blockbtn" type="submit">Save models</button>' : ""}<p role="status" data-project-model-status></p></form>`;
}

export function showConnectResult(result, e, { api, path, project, current = () => true, onUpdate = () => {}, opener, fallback, into, app = "claude-code", copySetup = false } = {}) {
  let timer;
  const modal = into || projectDialog({ labelledby: "project-connect-title", opener, fallback });
  const { dialog, dismiss } = modal;
  const leave = () => dismiss({ restore: false });
  dialog.innerHTML = connectResultHTML(result, e, { app });
  modal.onDismiss(() => clearTimeout(timer));
  const copyStatus = (text) => { if (!modal.closed()) dialog.querySelector("[data-project-copy-status]").textContent = text; };
  const chosen = () => dialog.querySelector("[data-project-app]")?.value || app;
  // The setup text is built only in memory and on screen; it is never logged.
  const showSetup = async () => {
    const config = dialog.querySelector("[data-project-config]");
    if (!config) return "";
    const text = await connectSetup(chosen(), result);
    if (!modal.closed() && config.isConnected) config.textContent = text;
    return text;
  };
  const writeSetup = async () => {
    try {
      await navigator.clipboard.writeText(await showSetup());
      copyStatus(`Setup copied for ${appLabel(chosen())}. Paste it into the app's MCP servers, then restart the app.`);
    } catch { copyStatus("Copy failed. Open “Show the setup to select it” and copy it from there."); }
  };
  dialog.querySelector("[data-project-app]").onchange = () => { showSetup().catch(() => {}); copyStatus(""); };
  dialog.querySelector("[data-project-copy-setup]").onclick = writeSetup;
  showSetup().catch(() => {});
  dialog.querySelector("[data-project-dismiss]").onclick = () => dismiss();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(dialog.querySelector("[data-project-token]").value);
      copyStatus("Token copied");
    } catch { copyStatus(PROJECT_COPY.copyError); }
  };
  dialog.querySelector("[data-project-copy]").onclick = copy;
  if (into) modal.focus("[data-project-token]");
  else modal.show("[data-project-token]");
  if (copySetup) writeSetup();
  if (!api) return dismiss;
  let started = Date.now();
  const progress = document.createElement("div");
  dialog.append(progress);
  function phase(value) {
    progress.innerHTML = connectPhaseHTML(value, project, e);
    // One element names the dialog: the phase title replaces the setup heading's id.
    const head = dialog.querySelector(".form-tile > h2");
    if (head) {
      if (progress.querySelector("#project-connect-title")) head.removeAttribute("id");
      else head.id = "project-connect-title";
    }
    const close = progress.querySelector("[data-project-dismiss]");
    if (close) close.onclick = () => dismiss();
    const retry = progress.querySelector("[data-project-retry]");
    if (retry) retry.onclick = () => { started = Date.now(); phase("waiting"); modal.focus("[data-project-token]"); poll(); };
    if (value !== "waiting") progress.querySelector(".btn.primary")?.focus();
  }
  async function poll() {
    if (modal.closed() || !current()) { leave(); return; }
    try {
      const latest = await api(path);
      if (modal.closed() || !current()) { leave(); return; }
      onUpdate(latest);
      if (latest.main.seen) {
        // The one-time credential is removed as soon as it is no longer needed.
        dialog.querySelector("[data-project-token]").value = "";
        dialog.replaceChildren(progress); phase("done"); return;
      }
    } catch { /* Connection checks may recover without issuing another token. */ }
    if (modal.closed() || !current()) { leave(); return; }
    if (Date.now() - started >= 120000) { phase("failed"); return; }
    timer = setTimeout(poll, 2000);
  }
  phase("waiting"); poll();
  return dismiss;
}

const reconnectCopy = (project, e) => project.main.source === "coordinator"
  ? { title: "Replace the orchestrator?", body: `<p>Connect replaces ${e(project.main.name)} as this project's orchestrator.</p>`, action: "Connect" }
  : { title: "Reconnect this agent?", body: "<p>Its current token will stop working.</p>", action: "Reconnect" };

// Shared by the settings Connect button and the empty-board dialog.
// `onWing(path)` makes the project's own memory section; `ask(copy)` resolves to true or false.
async function requestConnect({ api, path, project, e, active, ask, onWing }) {
  const body = {};
  if (project.main.connected) {
    if (!await ask(reconnectCopy(project, e))) return null;
    if (project.main.source === "coordinator") body.replace = true;
  }
  let wingChanged = false;
  for (;;) {
    try { return await api(`${path}/orchestrator/connect`, { method: "POST", body }); }
    catch (error) {
      if (!active()) return null;
      if (error.status === 409 && error.payload?.error === "shared_wing") {
        const choice = await chooseSharedWing(error.payload, e);
        if (choice === "cancel" || !active()) return null;
        if (choice === "shared") body.allow_shared = true;
        else {
          const owned = await onWing(error.payload.fix.path);
          if (!active()) return null;
          wingChanged = owned.changed;
        }
      } else if (error.status === 409 && error.payload?.error === "replaces current orchestrator" && !body.replace) {
        const copy = { title: "Replace the orchestrator?", body: `<p>${wingChanged ? PROJECT_COPY.ownWingDone + " " : ""}Connect replaces the current orchestrator.</p>`, action: "Connect" };
        if (!await ask(copy)) return null;
        body.replace = true;
      } else throw error;
    }
  }
}

export function connectIntroHTML(name, e) {
  return `<section class="form-tile"><h2 id="project-connect-title">Connect an agent${name ? ` to ${e(name)}` : ""}</h2><p class="lead">An agent is an AI helper that runs on your computer or a server, for example a coding assistant. It connects to Commonplace through MCP, an open standard many agent apps support.</p><p class="facts"><b>You need:</b> an agent app that supports MCP, and access to its settings. About 2 minutes.</p><ol class="dsteps"><li><b>Copy the setup and paste it into your agent app</b><span class="d">The setup is the MCP address and a new key, in the format your agent app reads. The key is shown once.</span>${appSelect("claude-code")}<div class="acts"><button type="button" class="btn primary" data-project-create>Create and copy setup</button></div><ol class="howto" aria-label="Where to paste it"><li>In your agent app, open its settings and find <b>MCP servers</b> (some apps call it <b>Tools</b> or <b>Connectors</b>).</li><li>Choose <b>Add server</b>, then <b>Paste configuration</b> (or open its config file and paste at the end of the server list).</li><li>Save, then restart the app.</li></ol></li><li><b>Start or restart your agent app</b><span class="d">This dialog shows when it connects.</span></li></ol><p role="status" data-project-connect-status></p><div class="acts"><button type="button" class="btn" data-project-dismiss>Cancel</button></div></section>`;
}

// The empty board's "Connect an agent": 03c, then the setup (03c2) in the same dialog.
export function openConnectDialog({ slug, api, e, opener, current = () => true, onUpdate = () => {} }) {
  const path = `/api/projects/${encodeURIComponent(slug)}`;
  const fallback = () => document.querySelector('.empty-strip [data-action="connect-agent"]') || document.querySelector('[data-action="connect-agent"]');
  const modal = projectDialog({ labelledby: "project-connect-title", opener, fallback });
  const { dialog, dismiss } = modal;
  const active = () => !modal.closed() && current();
  let project = null, busy = false;
  dialog.innerHTML = connectIntroHTML("", e);
  dialog.querySelector("[data-project-dismiss]").onclick = () => dismiss();
  const create = dialog.querySelector("[data-project-create]");
  const loading = api(path).then((loaded) => {
    project = loaded;
    if (active()) dialog.querySelector("#project-connect-title").textContent = `Connect an agent to ${loaded.name}`;
    return loaded;
  });
  loading.catch(() => {});
  create.onclick = async () => {
    if (busy || !active()) return;
    busy = true; create.disabled = true;
    const status = dialog.querySelector("[data-project-connect-status]");
    status.textContent = "";
    let changingWing = false;
    try {
      project = project || await loading;
      const result = await requestConnect({
        api, path, project, e, active,
        ask: (copy) => askConfirm(copy, e, { opener: create }),
        onWing: async (wingPath) => {
          changingWing = true;
          const owned = await api(wingPath, { method: "POST", body: {} });
          changingWing = false;
          project = owned; onUpdate(owned);
          if (active()) status.textContent = ownWingStatus(owned);
          return owned;
        },
      });
      if (!result || !active()) return;
      const app = dialog.querySelector("[data-project-app]")?.value || "claude-code";
      showConnectResult(result, e, { api, path, project, current: active, onUpdate: (latest) => { project = latest; onUpdate(latest); }, into: modal, app, copySetup: true });
    } catch (error) {
      if (active()) status.textContent = changingWing ? ownWingError(error.payload) : PROJECT_COPY.connectError;
    } finally {
      busy = false;
      if (create.isConnected) create.disabled = false;
    }
  };
  modal.show("[data-project-create]");
  return dismiss;
}

// Both app.js entry points keep their handlers here.
export async function mountProject(host, { slug, api, e, settings = false, current = () => true, notice = "", origin = globalThis.location?.hash || "", restart = origin.endsWith("/chat"), onBack }) {
  const path = `/api/projects/${encodeURIComponent(slug)}`;
  const active = () => current() && host.isConnected && !host.closest("[hidden]");
  const markSaved = () => { const saved = host.querySelector("[data-project-saved]"); if (saved) saved.textContent = `Changes save automatically · ${savedAt()}`; };
  host.innerHTML = '<p role="status">Loading project…</p>';
  let project, connecting = false, notAnswering = "";
  try { project = await api(path); }
  catch {
    if (active()) host.innerHTML = `<p role="status">${e(PROJECT_COPY.loadError)}</p>`;
    return;
  }
  if (!active()) return;

  function renderMain() {
    const trust = host.querySelector("[data-project-trust]");
    if (trust) mountOrchestratorTrust(trust, {project, api, e, current: active});
    host.querySelector("[data-project-heading]").innerHTML = mainHTML(project, e, { notAnswering });
    const hint = host.querySelector("[data-project-notice]");
    if (hint) { hint.hidden = project.main.connected; hint.textContent = notice; }
    const button = host.querySelector("[data-project-connect]");
    if (button) { button.disabled = connecting; button.onclick = connect; }
    const memory = host.querySelector("[data-project-memory]");
    if (memory) {
      memory.innerHTML = memoryHTML(project, e);
      const own = memory.querySelector("[data-project-own-wing]");
      if (own) { own.disabled = connecting; own.onclick = configureWing; }
    }
  }
  async function configureWing() {
    if (connecting) return;
    const choice = await chooseSharedWing({}, e, { allowShared: false });
    if (choice !== "own" || !active()) return;
    connecting = true; renderMain();
    const status = host.querySelector("[data-project-connect-status]");
    try {
      const result = await api(`${path}/wing`, { method: "POST", body: {} });
      if (!active()) return;
      project = result;
      status.textContent = ownWingStatus(result);
    } catch (error) { if (active()) status.textContent = ownWingError(error.payload); }
    finally { connecting = false; if (active()) renderMain(); }
  }
  // 10f: the chat's last page and status say since when the orchestrator has not answered.
  async function loadNotAnswering() {
    try {
      const chat = `/api/chat/${encodeURIComponent(slug)}`;
      const [{ deriveChatState }, page, status] = await Promise.all([import("./chatstate.js"), api(`${chat}?last=20`), api(`${chat}/status`)]);
      notAnswering = notAnsweringText(project, deriveChatState(page, status, { coordinator: project.main.connected ? project.main.name : null }), page);
    } catch { notAnswering = ""; }
  }
  async function connect(event) {
    const button = event.currentTarget;
    if (connecting || button.disabled) return;
    const fallback = () => host.querySelector("[data-project-connect]");
    connecting = true; button.disabled = true;
    const status = host.querySelector("[data-project-connect-status]");
    status.textContent = "";
    let changingWing = false;
    try {
      let result = await requestConnect({
        api, path, project, e, active,
        ask: (copy) => askConfirm(copy, e, { opener: button, fallback }),
        onWing: async (wingPath) => {
          changingWing = true;
          const owned = await api(wingPath, { method: "POST", body: {} });
          if (!active()) return owned;
          changingWing = false;
          project = owned;
          status.textContent = ownWingStatus(owned);
          renderMain();
          return owned;
        },
      });
      if (!result || !active()) return;
      showConnectResult(result, e, { api, path, project, current: active, opener: button, fallback, onUpdate: latest => { project = latest; if (active()) renderMain(); } });
      result = null;
      project = await api(path);
      if (active()) renderMain();
    } catch (error) {
      if (active()) status.textContent = changingWing ? ownWingError(error.payload) : PROJECT_COPY.connectError;
    } finally {
      connecting = false; button.disabled = false;
      const next = host.querySelector("[data-project-connect]");
      if (next) next.disabled = false;
    }
  }
  host.innerHTML = settings ? projectSettingsHTML(project, e, {origin, restart}) : '<div data-project-heading></div><p data-project-notice hidden></p><p role="status" data-project-connect-status></p>';
  if (settings) {
    installProjectStyles();
    bindSettingsNavigation(host, {slug, origin, onBack, current: active});
    const root = host.querySelector(".workbench-project");
    if (globalThis.matchMedia?.("(max-width: 699px)").matches) { root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true"); }
    mountAgentLimit(host.querySelector("[data-project-limit]"), {project, api, e, current: active, onSaved: markSaved});
    import("./orchstatus.js").then(m => m.mountOrchStatus(host.querySelector("[data-project-trust]"), {project, api, e, current: active})).catch(error => console.error("Coordinator status unavailable", error));
    const check = host.querySelector("[data-project-check]");
    if (check) check.onclick = async () => {
      if (check.disabled || !active()) return;
      check.disabled = true;
      const status = host.querySelector("[data-project-check-status]");
      status.textContent = "Checking…";
      try {
        project = await api(path);
        if (restart) await loadNotAnswering();
        if (active()) { renderMain(); status.textContent = project.main.seen ? "Agent connected. Check the chat for an answer." : "No answer yet. Your message is kept."; }
      } catch { if (active()) status.textContent = "Could not check the agent. Try again."; }
      finally { check.disabled = false; }
    };
    await Promise.all([
      mountChatAnswers(host.querySelector("[data-project-chat]"), {project, api, e, current: active, onSaved: markSaved}),
      mountProjectAgents(host.querySelector("[data-project-agents]"), {project, api, e, current: active, onUpdate: latest => { project = latest; renderMain(); }}),
      restart && loadNotAnswering(),
    ]);
    if (!active()) return;
  }
  renderMain();
  const form = host.querySelector("[data-project-models]");
  if (form && project.can_configure) form.onsubmit = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const status = form.querySelector("[data-project-model-status]"), body = {};
    for (const role of ["main", "worker"]) {
      const provider = form.elements.namedItem(`${role}_provider`).value.trim();
      const model = form.elements.namedItem(`${role}_model`).value.trim();
      if (!!provider !== !!model) { status.textContent = "Enter both provider and model, or leave both empty."; return; }
      body[role] = provider ? { provider, model } : null;
    }
    button.disabled = true;
    status.textContent = "";
    try {
      const saved = await api(`${path}/models`, { method: "PUT", body });
      if (!active()) return;
      project = saved;
      status.textContent = PROJECT_COPY.saved;
      markSaved();
      renderMain();
    } catch {
      if (active()) status.textContent = PROJECT_COPY.saveError;
    } finally { button.disabled = false; }
  };
}

const LOCAL_URL = "http://127.0.0.1:11434";
const CHAT_SAVE_ERROR = "Could not save chat settings";
const savedAt = () => `saved ${new Date().toTimeString().slice(0, 5)}`;

export function lastActive(value, now = Date.now()) {
  const at = Date.parse(value);
  if (!value || Number.isNaN(at)) return "Not seen yet";
  const minutes = Math.floor(Math.max(0, now - at) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes} min ago`;
  const date = new Date(at), clock = date.toTimeString().slice(0, 5);
  return date.toDateString() === new Date(now).toDateString() ? `today, ${clock}` : `${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}, ${clock}`;
}

function loopbackURL(value) {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return host === "localhost" || host === "[::1]" || /^127(?:\.(?:\d{1,2}|1\d\d|2[0-4]\d|25[0-5])){3}$/.test(host);
  } catch { return false; }
}
function answerMode(front) {
  if (front?.enabled === false || front?.provider === "none" || !front?.provider) return "basic";
  return front.provider === "openai_compatible" && loopbackURL(front.base_url) ? "local" : "hosted";
}

// Selecting Hosted changes only the panel. The Turn on handler owns its sole write.
export async function mountChatAnswers(host, { project, api, e, current = () => true, onSaved = () => {} }) {
  const active = () => current() && host.isConnected;
  const owner = project.can_configure;
  const scope = project.board;
  host.innerHTML = '<p role="status">Loading chat settings…</p>';
  let payload;
  try { payload = await api(`/api/chat-settings?board=${encodeURIComponent(scope)}${owner ? "&probe=1" : ""}`); }
  catch { if (active()) host.innerHTML = `<p role="alert">${CHAT_SAVE_ERROR}</p>`; return; }
  if (!active()) return;
  let front = payload.front || {provider: "none"};
  let selected = answerMode(front), busy = false, status = "", error = "";
  let localURL = answerMode(front) === "local" ? front.base_url : LOCAL_URL;
  let localModel = answerMode(front) === "local" ? front.model : payload.local_models?.[0] || "";
  let provider = "", key = "", model = "", base = "";
  const options = payload.hosted || [];
  const option = () => options.find(o => o.provider === provider);
  const ready = () => !!(provider && model.trim() && option()?.keys?.[Number(key)] && key !== "" && (provider !== "openai_compatible" || base));
  const disabled = () => !owner || busy ? "disabled" : "";
  function render() {
    if (!active()) return;
    const focusId = host.contains?.(globalThis.document?.activeElement) ? globalThis.document.activeElement.id : null;
    const models = [...new Set([...(payload.local_models || []), localModel].filter(Boolean))];
    const radio = (value, title, note, fields = "") => `<div class="radio ${selected === value ? "on" : ""}"><input type="radio" name="chat-answer-mode" id="chat-${value}" data-chat-mode="${value}" value="${value}" ${selected === value ? "checked" : ""} ${disabled()} ${value === "hosted" && globalThis.matchMedia?.("(max-width: 699px)").matches ? "disabled" : ""}><div><label for="chat-${value}"><b>${title}</b><small class="mode-note">${note}</small><small class="phone-note">${value === "basic" ? "a short receipt, no model" : value === "hosted" ? "set up on a computer" : (payload.local_reachable ? "reachable" : "not reachable") + " · nothing leaves this machine"}</small></label>${fields}</div></div>`;
    host.innerHTML = `<p class="lead">Which model writes the quick reply you see in chat. The orchestrator still does the work.</p><details class="set-note"><summary>What quick replies are</summary>A short answer from this model, shown in chat before or beside the orchestrator's own answer. Quick replies are saved in chat and memory like every message, under their own name, “Quick reply · local model” or “Quick reply · hosted provider”, never under the orchestrator's name.</details>
      ${front.inherited ? `<p class="set-note">${answerMode(front) === "hosted" ? "Hosted" : answerMode(front) === "local" ? "Local" : "Basic"} · set for the whole workspace${front.applies === false ? " · Off until you press Turn on" : ""}</p>` : ""}
      <fieldset class="radios"><legend class="sr">Chat answer mode</legend>
      ${radio("basic", "Basic", "No model. Instead of an answer, a short receipt confirms your message reached the orchestrator.")}
      ${radio("local", "Local model", `Runs on your machine. Nothing leaves it.${selected === "hosted" && answerMode(front) === "local" ? " Still in use until a hosted provider is turned on." : ""}`, selected === "local" ? `<div class="sub-fields"><label for="chat-local-model">Model</label><select id="chat-local-model" data-chat-local-model ${disabled()}><option value="">Choose a model…</option>${models.map(m => `<option value="${e(m)}" ${m === localModel ? "selected" : ""}>${e(m)}</option>`).join("")}</select><span>Status</span><span>${payload.local_reachable ? "Reachable" : "Not reachable"}</span></div>${payload.local_reachable === false ? '<p class="callout wait">Model not reachable. Check that your local model app is running, then try again. Your chat still reaches the orchestrator.</p>' : ""}` : "")}
      ${radio("hosted", "Hosted provider", answerMode(front) === "hosted" && front.applies !== false ? "On. Chat messages go to the saved hosted provider. Changing it needs Turn on again." : "Off until you press Turn on. Sends chat messages to an outside AI service using a key your workspace owner saved.", selected === "hosted" ? `<div class="sub-fields"><label for="chat-provider">Provider</label><select id="chat-provider" data-chat-provider ${disabled()}><option value="">Choose a provider…</option>${options.map(o => `<option value="${e(o.provider)}" ${o.provider === provider ? "selected" : ""}>${e(o.provider)}</option>`).join("")}</select><label for="chat-key">Saved key</label><select id="chat-key" data-chat-key ${disabled()}><option value="">Choose a saved key…</option>${(option()?.keys || []).map((k, i) => `<option value="${i}" ${String(i) === key ? "selected" : ""}>${e(k.label)}</option>`).join("")}</select><label for="chat-hosted-model">Model</label><input id="chat-hosted-model" data-chat-hosted-model value="${e(model)}" maxlength="120" autocomplete="off" ${disabled()}>${provider === "openai_compatible" ? `<label for="chat-base">Provider address</label><select id="chat-base" data-chat-base ${disabled()}><option value="">Choose an address…</option>${(option()?.base_urls || []).map(url => `<option value="${e(url)}" ${url === base ? "selected" : ""}>${e(url)}</option>`).join("")}</select>` : ""}</div><div class="callout wait"><b>Your chat leaves this machine</b><p>Every chat message in ${e(project.name)} goes to the provider you choose, to write the quick reply. Cards, memory and other projects are not sent. Its replies are saved here labelled “Quick reply · hosted provider”. Nothing is sent until you press Turn on.</p>${!options.length ? '<p>No saved provider is available. Ask your workspace owner to set one up.</p>' : ""}<div class="acts"><button type="button" class="btn sm" data-chat-cancel ${disabled()}>Cancel</button><button type="button" class="btn primary sm" data-chat-turn-on ${disabled()} ${ready() ? "" : "disabled"}>Turn on</button></div></div>` : "")}
      </fieldset>${error ? `<p role="alert">${e(error)}</p>` : ""}<p role="status" data-chat-status>${e(status)}</p>`;
    host.querySelectorAll("[data-chat-mode]").forEach(input => {
      input.onchange = async () => {
        if (!owner || busy) return;
        selected = input.value; error = ""; status = "";
        if (selected === "hosted") { render(); return; }
        await save(selected);
      };
    });
    const bind = (selector, change) => {
      const input = host.querySelector(selector);
      if (input) input.onchange = async () => { if (owner && !busy) { change(input.value); error = ""; status = ""; render(); } };
    };
    bind("[data-chat-provider]", value => { provider = value; key = ""; model = ""; base = ""; });
    bind("[data-chat-key]", value => { key = value; });
    const modelInput = host.querySelector("[data-chat-hosted-model]");
    if (modelInput) modelInput.oninput = modelInput.onchange = () => {
      if (!owner || busy) return;
      model = modelInput.value;
      host.querySelector("[data-chat-turn-on]").disabled = !ready();
    };
    bind("[data-chat-base]", value => { base = value; });
    const localSelect = host.querySelector("[data-chat-local-model]");
    if (localSelect) localSelect.onchange = async () => { if (owner && !busy) { localModel = localSelect.value; await save("local"); } };
    const cancel = host.querySelector("[data-chat-cancel]");
    if (cancel) cancel.onclick = () => { if (!busy) { selected = answerMode(front); error = ""; render(); } };
    const turnOn = host.querySelector("[data-chat-turn-on]");
    if (turnOn) turnOn.onclick = () => { if (owner && !busy && ready()) return save("hosted"); };
    if (focusId) host.querySelector(`[id="${focusId}"]`)?.focus();
  }
  async function save(mode) {
    if (!active() || busy || !owner) return;
    error = ""; status = "";
    if (mode === "local" && !localModel) { error = "Pick a model"; render(); return; }
    let body;
    if (mode === "basic") body = {scope, front: {provider: "none"}};
    else if (mode === "local") body = {scope, mode: "local", front: {provider: "openai_compatible", model: localModel, base_url: localURL}};
    else body = {scope, mode: "hosted", front: {provider, model: model.trim(), key_ref: option().keys[Number(key)].ref, ...(provider === "openai_compatible" ? {base_url: base} : {})}};
    busy = true; render();
    try {
      const saved = await api("/api/chat-settings", {method: "PUT", body});
      if (!active()) return;
      front = saved.front; status = savedAt(); onSaved();
      if (mode === "local") localURL = front.base_url || localURL;
    } catch (failure) {
      if (!active()) return;
      if (mode === "local" && failure.status === 400 && failure.payload?.error === "not_loopback") {
        error = "That address is not on this computer"; selected = "hosted";
      } else error = CHAT_SAVE_ERROR;
    } finally { busy = false; render(); }
  }
  render();
}

export function mountAgentLimit(host, { project, api, e, current = () => true, onSaved = () => {} }) {
  let explicit = project.limits?.max_agents ?? null;
  let value = explicit ?? project.orchestration?.default_max_agents ?? null, busy = false, status = "";
  const active = () => current() && host.isConnected;
  function render() {
    if (!active()) return;
    host.innerHTML = `<div class="inline-row"><span id="project-max-agents">Max agents working at once</span><div class="stepper" role="group" aria-labelledby="project-max-agents"><button type="button" aria-label="One fewer" data-limit-step="-1" ${!project.can_configure || busy || value === null || value <= 1 ? "disabled" : ""}>−</button><output aria-live="polite" data-limit-value>${value === null ? "No limit" : value}</output><button type="button" aria-label="One more" data-limit-step="1" ${!project.can_configure || busy || value === 20 ? "disabled" : ""}>+</button></div><span class="set-note">Extra cards wait in Todo.</span>${project.can_configure && explicit !== null ? `<button type="button" class="btn ghost" data-limit-clear>${project.orchestration ? "Use default" : "Remove limit"}</button>` : ""}</div><p role="status" data-limit-status>${e(status)}</p>`;
    host.querySelectorAll("[data-limit-step]").forEach(button => {
      button.onclick = () => save(value === null ? 1 : value + Number(button.getAttribute("data-limit-step")));
    });
    const clear = host.querySelector("[data-limit-clear]");
    if (clear) { clear.disabled = busy; clear.onclick = () => save(null); }
  }
  async function save(next) {
    if (!active() || !project.can_configure || busy || (next !== null && (next < 1 || next > 20))) return;
    busy = true; status = ""; render();
    try {
      const result = await api(`/api/projects/${encodeURIComponent(project.board)}/limits`, {method: "PUT", body: {max_agents: next}});
      if (active()) { explicit = result.limits.max_agents; value = explicit ?? result.orchestration?.default_max_agents ?? null; status = savedAt(); onSaved(); }
    } catch { if (active()) status = "Could not save maximum agents"; }
    finally { busy = false; render(); }
  }
  render();
}

export function settingsOrigin(slug, origin = "") {
  if (origin === "#/needs") return {href: origin, label: "Back to Needs me"};
  if (origin === `#/board/${encodeURIComponent(slug)}/chat` || origin === `#/chat/${encodeURIComponent(slug)}`) return {href: origin, label: "Back to the chat"};
  return {href: `#/board/${encodeURIComponent(slug)}`, label: "Back to board"};
}

const settingsNavigation = new WeakMap();

export function bindSettingsNavigation(host, {slug, origin, document: doc = document, onBack, current = () => true}) {
  settingsNavigation.get(host)?.();
  const target = settingsOrigin(slug, origin), back = host.querySelector("[data-project-back]");
  back.textContent = target.label; back.setAttribute("href", target.href);
  const go = () => {
    stop();
    if (onBack) onBack(target.href);
    else {
      const panel = host.closest("#card-panel");
      if (panel) panel.hidden = true;
      location.hash = target.href;
      doc.querySelector('[data-action="coordinator"]')?.focus();
    }
  };
  back.onclick = event => { event.preventDefault(); event.stopPropagation(); go(); };
  const keys = event => {
    if (!host.isConnected || !current() || host.closest("[hidden]")) { stop(); return; }
    if (event.key === "Escape" && !doc.querySelector?.("dialog[open]")) { event.preventDefault(); event.stopImmediatePropagation(); go(); }
    if (event.key === "Tab" && globalThis.matchMedia?.("(max-width: 699px)").matches) {
      const controls = [...host.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), summary')].filter(n => n.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  const stop = () => doc.removeEventListener("keydown", keys, true);
  doc.addEventListener("keydown", keys, true);
  settingsNavigation.set(host, stop);
  return stop;
}

export function connectPhaseHTML(phase, project, e) {
  if (phase === "done") return `<h2 id="project-connect-title">Orchestrator connected</h2><p>${e(project.name)} is ready.</p><p role="status">Delivered to the orchestrator</p><button type="button" class="btn primary" data-project-dismiss>Back to board</button>`;
  if (phase === "failed") return '<h2 id="project-connect-title">The agent has not connected yet</h2><p role="alert">No response after 120 seconds. Check that the agent app is open and its MCP setup is saved.</p><button type="button" class="btn primary" data-project-retry>Try again</button><button type="button" class="btn" data-project-dismiss>Close</button>';
  return `<h2>Waiting for the agent</h2><p role="status">Start the agent on your computer. This view checks for its connection automatically.</p>`;
}

const settingsStyle = `
.workbench-project {color:var(--text);background:var(--bg);font-family:var(--font);min-width:0}
.workbench-project .project-top {display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:16px 28px;background:var(--surface,#FEFEFC)}
.workbench-project h2 {font-size:15px;font-weight:650;margin:0}.workbench-project h1 {font-size:20px;margin:0}
.workbench-project .settings {padding:20px 28px 28px;overflow:auto}.workbench-project .settings-inner {max-width:760px;display:flex;flex-direction:column;gap:18px}
.workbench-project .set-sec {background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px 18px;display:flex;flex-direction:column;gap:12px}
.workbench-project .lead,.workbench-project .set-note {font-size:13px;color:var(--text-2);margin:0}.workbench-project .radios {border:0;padding:0;margin:12px 0 0;display:flex;flex-direction:column;gap:6px}
.workbench-project .radio {display:grid;grid-template-columns:20px minmax(0,1fr);gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r)}
.workbench-project .radio.on {border-color:var(--accent);outline:1px solid var(--accent);background:var(--accent-weak)}
.workbench-project input[type=radio] {appearance:auto;width:18px;height:18px;min-height:0;margin:1px 0 0;accent-color:var(--accent)}
.workbench-project .radio b,.workbench-project .radio small {display:block}.workbench-project small {color:var(--text-2);font-size:12.5px}
.workbench-project .sub-fields {display:grid;grid-template-columns:120px minmax(0,1fr);gap:8px;align-items:center;margin-top:10px;font-size:13px}
.workbench-project input,.workbench-project select {min-width:0;width:100%;background:var(--surface);color:var(--text);border:1px solid var(--line-strong);border-radius:var(--r);min-height:34px}
.workbench-project .inline-row,.workbench-project .acts {display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.workbench-project .stepper {display:inline-flex;align-items:center;border:1px solid var(--line-strong);border-radius:var(--r);overflow:hidden}.workbench-project .stepper button {width:36px;height:34px;border:0;background:var(--surface);color:var(--text);font-size:18px}.workbench-project .stepper output {min-width:44px;text-align:center;font-weight:650;padding:0 6px}
.workbench-project .orch-card {background:var(--ok-bg);padding:12px;border-radius:var(--r)}.workbench-project .restart-box,.workbench-project .callout {padding:12px 14px;margin-top:10px;border:1px solid var(--wait-line);border-radius:8px;background:var(--wait-bg)}
.workbench-project .agents-t {width:100%;border-collapse:collapse;font-size:13px}.workbench-project th,.workbench-project td {text-align:left;padding:8px;border-bottom:1px solid var(--line)}
.workbench-project .phone-note {display:none}.workbench-project :focus-visible {outline:3px solid var(--accent);outline-offset:2px}
.workbench-project .orch-card.warn {background:var(--wait-bg);border:1px solid var(--wait-line)}
.workbench-project .project-top h1 .title-sub {font-size:15px;font-weight:400;color:var(--text-2);margin-left:6px}
.workbench-project .project-top .saved {margin-left:auto;font-size:12.5px;font-weight:600;color:var(--ok)}.workbench-project .project-top .saved::before {content:"✓ "}
.workbench-project .project-top .back {display:inline-flex;align-items:center;gap:8px}.workbench-project .project-top .back::before {content:"←";font-size:15px}
.workbench-project .project-top .back[data-keys]::after {content:attr(data-keys);font:11px/1 var(--mono,monospace);color:var(--text-2);border:1px solid var(--line-strong);border-radius:4px;padding:2px 4px;word-spacing:4px}
/* The shared .setting-row span rule (flex:1; min-width:180px) must not stretch the avatar or the tag. */
.workbench-project .orch-row {display:flex;align-items:center;gap:12px;flex-wrap:nowrap;padding:0;border-bottom:0}.workbench-project .orch-row span {flex:none;min-width:0}.workbench-project .orch-row .orch-text {flex:1;min-width:0}.workbench-project .orch-row .av {flex:none;display:grid;place-items:center;width:36px;height:36px;border-radius:6px;background:var(--accent);color:var(--surface);font-weight:700}
.workbench-project .orch-text {flex:1;min-width:0}.workbench-project .orch-text b {font-weight:650}.workbench-project .orch-text small {display:block;margin-top:2px}
.workbench-project .tag.ok {display:inline-flex;align-items:center;height:auto;margin-left:8px;padding:0;box-shadow:none;font-size:12px;font-weight:600;color:var(--ok)}
.workbench-project .radio small.phone-note {display:none}.workbench-project .tag.ok::before {content:"✓ "}.workbench-project [data-project-not-answering] {margin:6px 0 0;color:var(--wait);font-weight:600}
.project-dialog {width:min(600px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;border:1px solid var(--line);border-radius:10px;padding:22px 24px;background:var(--surface);color:var(--text);font-family:var(--font);box-shadow:0 16px 40px rgba(23,24,28,.18)}
.project-dialog::backdrop {background:rgba(23,24,28,.42)}
.project-dialog h2 {font-size:17px;margin:0 0 12px}.project-dialog .form-tile {padding:0;background:transparent;box-shadow:none;border:0}
.project-dialog p,.project-dialog li {font-size:14px;line-height:1.5}.project-dialog .lead,.project-dialog .d,.project-dialog .howto {color:var(--text-2)}
.project-dialog .dsteps {padding-left:20px;display:flex;flex-direction:column;gap:12px}.project-dialog .dsteps b,.project-dialog .dsteps .d {display:block}.project-dialog .howto {padding-left:18px;margin:8px 0 0}
.project-dialog .acts {display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:16px}.project-dialog .dsteps .acts {justify-content:flex-start;margin-top:8px}
.project-dialog .scope {padding:10px 12px;border:1px solid var(--wait-line);border-radius:8px;background:var(--wait-bg)}.project-dialog .alert {padding:10px 12px;border:1px solid var(--block-line);border-radius:8px;background:var(--block-bg)}.project-dialog .alert h3 {font-size:14px;margin:0}.project-dialog .alert p {margin:4px 0 0}
.project-dialog :focus-visible {outline:3px solid var(--accent);outline-offset:2px}
@media(max-width:699px) {.project-dialog .btn {min-height:44px}}
@media(min-width:700px) {#card-panel:has(.workbench-project) {position:fixed;inset:0 0 0 236px;display:block;width:auto;height:100%;box-shadow:none;max-width:none;z-index:30;padding:0;background:var(--bg)}#card-panel:has(.workbench-project)>.form-tile {padding:0;background:transparent}#card-panel:has(.workbench-project)>.form-tile>.close {display:none}}
@media(min-width:700px) and (max-width:1099px) {#card-panel:has(.workbench-project) {left:220px}}
@media(max-width:699px) {.workbench-project {position:fixed;inset:0;z-index:80;overflow:auto;background:var(--surface)}.workbench-project .project-top {padding:8px 12px;gap:8px}.workbench-project .project-top a {order:-1}.workbench-project .project-top h1 {order:1;flex-basis:100%}.workbench-project .settings {padding:12px}.workbench-project .set-sec {padding:14px 0;border:0;border-top:1px solid var(--line);border-radius:0}.workbench-project .settings-inner {gap:0}.workbench-project .radio .sub-fields,.workbench-project .radio .mode-note,.workbench-project [data-project-chat]>.lead,.workbench-project [data-project-chat]>details,.workbench-project .radio .callout {display:none}.workbench-project .radio label {display:flex;flex-direction:row;align-items:center;justify-content:flex-start;gap:10px;text-align:left}.workbench-project .radio small.phone-note {display:block}.workbench-project .project-top h1 .title-sub {font-size:inherit;font-weight:inherit;color:inherit;margin-left:0}.workbench-project .radio label b {flex:none;text-align:left}.workbench-project .radio label small {margin-left:auto;text-align:right}.workbench-project .desk-only {display:none}.workbench-project .project-top .back[data-keys]::after {display:none}.workbench-project .project-top .saved {order:0}.workbench-project .radio.on {outline:0;background:var(--surface);border-color:var(--line)}.workbench-project .sub-fields {grid-template-columns:1fr}.workbench-project .btn,.workbench-project input,.workbench-project select,.workbench-project .stepper button {min-height:44px}.workbench-project .radio {min-height:48px}.workbench-project input[type=radio] {min-height:0}.workbench-project .desktop-agent {display:none}.workbench-project .phone-note {display:block}.workbench-project .agents-t th:nth-child(n+3),.workbench-project .agents-t td:nth-child(n+3) {display:none}}
`;

let projectSheet;
function installProjectStyles() {
  if (projectSheet) return;
  // Constructed stylesheets keep the app's self-only style policy intact.
  projectSheet = new CSSStyleSheet();
  projectSheet.replaceSync(settingsStyle);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, projectSheet];
}

export function projectSettingsHTML(project, e, {origin = "", restart = false} = {}) {
  const back = settingsOrigin(project.board, origin);
  return `<section class="workbench-project" aria-labelledby="project-settings-title"><header class="project-top"><h1 id="project-settings-title">${e(project.name)}<span class="title-sub"> <span class="desk-only">Project </span>settings</span></h1><span class="saved" role="status" data-project-saved>Changes save automatically</span><a class="btn back" data-project-back href="${e(back.href)}"${back.href.startsWith("#/board/") && !back.href.endsWith("/chat") ? ' data-keys="G B"' : ""}>${back.label}</a></header><div class="settings"><div class="settings-inner"><section class="set-sec"><h2>Orchestrator</h2><p class="lead">The project's lead agent: it reads chat and turns requests into cards. With no agent connected, chat requests wait.</p><div class="orch-card${restart ? " warn" : ""}" data-project-heading></div><p data-project-notice hidden></p><p role="status" data-project-connect-status></p><div data-project-trust></div>${restart ? '<div class="restart-box"><h3>How to restart the agent</h3><ol><li>Check that the agent app is open and the computer is awake.</li><li>Quit the agent app and start it again. It reconnects with the same setup.</li><li>Check that the computer can still reach this Commonplace (same network or VPN).</li></ol><button type="button" class="btn primary" data-project-check>Check again</button><p role="status" data-project-check-status></p></div>' : ""}<p class="phone-note set-note">Connect an agent on a computer.</p></section><section class="set-sec"><h2>Chat answers</h2><div data-project-chat></div></section><section class="set-sec"><h2>Agents</h2><div data-project-limit></div><div data-project-agents></div><p class="phone-note set-note">To connect or disconnect an agent, open Settings on a computer. Agents run there.</p></section><section class="set-sec"><h2>Project memory</h2><div data-project-memory></div></section><details class="set-sec"><summary>Agent models</summary>${modelsHTML(project, e)}</details></div></div></section>`;
}

export async function mountProjectAgents(host, {project, api, e, current, onUpdate}) {
  const active = () => current() && host.isConnected;
  let connections = [], cards = [], busy = false, error = "";
  const name = value => String(value || "").replace(/^conn:/, "");
  try {
    const results = await Promise.all([
      project.can_configure ? api("/api/connections") : Promise.resolve({connections: []}),
      api(`/api/boards/${encodeURIComponent(project.board)}/cards`),
    ]);
    connections = results[0].connections || [];
    cards = [...results[1].cards || [], ...results[1].linked || []];
  } catch { error = "Could not load the agent list"; }
  function render() {
    if (!active()) return;
    const assigned = new Set(cards.map(c => c.assignee).filter(ref => ref?.startsWith("conn:")).map(name));
    if (project.main.connected) assigned.add(name(project.main.name));
    const rows = connections.filter(c => !c.revoked_at && (!c.expires_at || Date.parse(c.expires_at) > Date.now()) && assigned.has(c.name));
    const main = name(project.main.name);
    host.innerHTML = `<p class="set-note">Orchestrator and agents assigned to this project's cards.</p><table class="agents-t"><caption class="sr">Project agents</caption><thead><tr><th>Agent</th><th>Role</th><th>Last active</th><th>Actions</th></tr></thead><tbody>${project.main.connected && !rows.some(c => c.name === main) ? `<tr><td>${e(project.name)} orchestrator</td><td>Orchestrator</td><td>—</td><td>Manage in workspace Settings</td></tr>` : ""}${rows.map(c => `<tr><td>${e(c.name === main ? project.name + " orchestrator" : c.name)} (agent)</td><td>${c.name === main ? "Orchestrator" : "Works on cards"}</td><td>${e(lastActive(c.last_used_at))}</td><td>${project.can_configure ? `<button type="button" class="btn ghost sm desktop-agent" data-project-disconnect="${e(c.id)}" ${busy ? "disabled" : ""} aria-label="Disconnect ${e(c.name)}">Disconnect…</button>` : ""}</td></tr>`).join("")}</tbody></table>${!assigned.size ? '<p>No agents connected</p>' : ""}<p role="status">${e(error)}</p>`;
    host.querySelectorAll("[data-project-disconnect]").forEach(button => {
      button.onclick = async () => {
        if (busy || !active()) return;
        const id = button.getAttribute("data-project-disconnect"), connection = rows.find(c => c.id === id);
        busy = true;
        const others = await connectionProjects(api, connection, project.board);
        busy = false;
        if (!active()) return;
        const copy = disconnectCopy(project, connection, {main: connection.name === main, only: assigned.size <= 1, others}, e);
        const fallback = () => host.querySelector(`[data-project-disconnect="${CSS.escape(id)}"]`);
        if (!await askConfirm(copy, e, {opener: button, fallback}) || !active()) return;
        busy = true; error = ""; render();
        try {
          await api(`/api/connections/${encodeURIComponent(connection.id)}`, {method: "DELETE"});
          if (!active()) return;
          connections = connections.filter(c => c.id !== connection.id);
          project = await api(`/api/projects/${encodeURIComponent(project.board)}`);
          if (active()) onUpdate(project);
        } catch { if (active()) error = "Could not disconnect the agent. Runner-managed agents must be managed in workspace Settings."; }
        finally { busy = false; render(); }
      };
    });
  }
  render();
}

let orchestrationSheet;
function installOrchestrationStyles() {
  if (orchestrationSheet || !globalThis.document?.adoptedStyleSheets) return;
  orchestrationSheet = new CSSStyleSheet();
  orchestrationSheet.replaceSync(`
    .orchestration-controls label {display:flex;align-items:center;gap:8px;margin:8px 0;font-size:14px;line-height:1.5;min-height:32px}
    .orchestration-controls input[type=checkbox] {appearance:auto;flex:none;width:18px;height:18px;min-height:0;padding:0;margin:0;box-shadow:none;accent-color:var(--strong)}
    .orchestration-controls p {font-size:14px;line-height:1.5;margin:8px 0}
    @media(max-width:699px) {.orchestration-controls label {min-height:44px}}
  `);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, orchestrationSheet];
}

export function mountWorkspaceOrchestration(host, {api, e, enabled, current = () => true}) {
  installOrchestrationStyles();
  let busy = false, status = "";
  const active = () => current() && host.isConnected;
  function render() {
    if (!active()) return;
    host.innerHTML = `<div class="orchestration-controls"><label><input type="checkbox" data-orchestration-enabled ${enabled ? "checked" : ""} ${busy ? "disabled" : ""}> Orchestrators manage agent starts</label><p>Only each project's orchestrator can request agent starts. Projects default to three agents at once. Turning this off removes trust and runner access granted to project seats.</p><p role="status">${e(status)}</p></div>`;
    host.querySelector("[data-orchestration-enabled]").onchange = async event => {
      const next = event.target.checked;
      if (busy || !active()) return;
      busy = true; status = ""; render();
      try {
        const result = await api("/api/settings/orchestration", {method: "PUT", body: {enabled: next}});
        if (active()) { enabled = result.enabled; status = "Saved"; }
      } catch { if (active()) status = "Could not save orchestration settings"; }
      finally { busy = false; render(); }
    };
  }
  render();
}

export function mountOrchestratorTrust(host, {project, api, e, current = () => true}) {
  installOrchestrationStyles();
  const active = () => current() && host.isConnected;
  const orchestration = project.orchestration;
  if (!orchestration || !project.can_configure || !project.main.connected) { host.replaceChildren(); return; }
  let trusted = orchestration.trusted, busy = false, status = "";
  const machines = orchestration.machines || [];
  function render() {
    if (!active()) return;
    host.innerHTML = `<details class="orchestration-controls"><summary>Orchestrator start permissions</summary><p>Trust lets this seat request starts on selected machines, read permitted project data and repository rules, release and approve cards, annotate cards and stamp trusted submissions. It can view start status and cancel its own starts. It cannot create boards or launch handoffs.</p>${machines.map(m => `<label><input type="checkbox" data-trust-machine value="${e(m.id)}" ${busy ? "disabled" : ""}> ${e(m.label)}</label>`).join("")}<p><label><input type="checkbox" data-trust-confirm ${busy ? "disabled" : ""}> I approve these permissions</label></p><button type="button" class="btn" data-trust-grant ${busy || !machines.length ? "disabled" : ""}>Trust and allow starts</button>${trusted ? `<button type="button" class="btn ghost" data-trust-remove ${busy ? "disabled" : ""}>Remove trust</button>` : ""}<p role="status">${e(status)}</p></details>`;
    host.querySelector("[data-trust-grant]").onclick = () => {
      const selected = [...host.querySelectorAll("[data-trust-machine]:checked")].map(input => input.value);
      if (!host.querySelector("[data-trust-confirm]").checked || !selected.length) {
        host.querySelector("[role=status]").textContent = "Select machines and approve the permissions first";
        return;
      }
      return save({confirm: true, machines: selected});
    };
    const remove = host.querySelector("[data-trust-remove]");
    if (remove) remove.onclick = () => save({trusted: false});
  }
  async function save(body) {
    if (busy || !active()) return;
    busy = true; status = ""; render();
    try {
      const result = await api(`/api/projects/${encodeURIComponent(project.board)}/orchestrator/trust`, {method: "POST", body});
      if (active()) { trusted = result.trusted; orchestration.trusted = trusted; status = "Saved"; }
    } catch { if (active()) status = "Could not save orchestrator permissions"; }
    finally { busy = false; render(); }
  }
  render();
}
