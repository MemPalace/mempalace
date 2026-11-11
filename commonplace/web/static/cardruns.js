import { api as request, escape as e, elapsed } from "./api.js";

// "Runs" in the card panel: the start jobs of one card, from GET /api/card-starts?task=<n>.
// The route answers only a signed-in owner (all jobs) or a trusted agent token (its own jobs); anyone
// else gets 403 or 404, and then the section stays hidden. So whoever sees a row here is the owner or
// the requester, and the owner-only "It started / It failed" choice is the server's call to refuse.
// The same section offers "Start agent". What it may offer comes from GET /api/card-starts/options
// (owner only): the board's own config, or the machines, projects and topics the owner can use.

const THREAD_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$/;

export const STATE_WORDS = {
  queued: "Waiting for the machine to pick it up",
  leased: "The machine has picked it up",
  starting: "Starting on the machine",
  started: "Started",
  failed: "Did not start",
  uncertain: "Not clear whether it started",
  cancelled: "Cancelled",
};

export const OUTCOME_WORDS = {
  project_not_configured: "The machine has no project set up for this card",
  topic_not_allowed: "The machine is not allowed to work on this topic",
  mode_not_allowed: "The machine does not allow this permission mode",
  config_invalid: "The machine's runner setup is not valid",
  binding_not_acked: "The machine has not confirmed its current setup",
  job_invalid: "The machine could not read the request",
  t3_missing: "The machine could not find its coding tool",
  busy: "The machine had reached its hourly limit",
  machine_revoked: "The machine was switched off or removed",
  binding_not_attested: "The machine is no longer vouched for by an owner",
  binding_epoch_changed: "The machine's setup changed after the request",
  restart_unknown: "The runner restarted while starting",
  spawn_ambiguous: "The runner lost track while starting",
};

export const outcomeWords = (code) => code ? OUTCOME_WORDS[code] || `Reason code: ${code}` : "";

const REFUSALS = {
  403: "Only a workspace owner or whoever requested this start can do that.",
  404: "This start no longer exists.",
};
export const refusalWords = (error) => {
  const text = String(error?.payload?.error || error?.message || "").replace(/^no: /, "");
  return text ? `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? "" : "."}` : REFUSALS[error?.status] || "That did not work. Try again.";
};

// Why a start cannot happen, in plain words with the next action. The key is the server's stable
// code (a refused start carries it as `code`; the options route returns it as `refusal`).
const START_REFUSALS = {
  no_machine: "No machine is ready to run an agent. An owner can add one and vouch for it in Workspace settings, under Runner machines.",
  machine_unavailable: "The machine this project starts agents on is switched off, removed or not vouched for. Check it in Workspace settings, under Runner machines.",
  binding_not_attested: "The machine is not vouched for by an owner yet. Vouch for it in Workspace settings, under Runner machines.",
  origin_not_released: "This card came from outside the workspace and has not been released for agents. Release it first, then start an agent.",
  not_writable: "The machine's worker may not write on this project's wing. Give the machine that wing in Workspace settings, under Runner machines.",
  outside_worker_wing: "The machine's worker may not work on this project's wing. Give the machine that wing in Workspace settings, under Runner machines.",
  task_type_not_allowed: "The machine's worker may not take this type of card.",
  worker_unavailable: "The machine's worker connection is not available. Check the machine in Workspace settings, under Runner machines.",
  project_not_allowed: "The machine is not set up for the project this project's config names. Change the config or the machine's projects.",
  topic_not_allowed: "The machine is not allowed to work on the topic this project's config names. Change the config or the machine's topics.",
  mode_not_allowed: "The machine does not allow the permission mode this project's config names.",
  active_start: "An agent is already being started on this card. Wait for it, or cancel it in the list above.",
  started_recently: "An agent was started on this card less than a day ago. Look at its run above, or try again later.",
  busy: "The machine has reached its hourly limit of starts. Try again later.",
  at_cap: "This project already has as many agents working as it may. Start another when one finishes, or raise the limit in Project settings.",
  plan_pending: "This card carries the plan label. Approve the plan first, then start an agent.",
  model_unresolved: "No model is set for the agent's role. Choose one in Project settings.",
  config_changed: "The project's orchestrator config changed just now. Try again.",
  config_mismatch: "This project starts agents from its orchestrator config, so the machine, project and topic cannot be changed here.",
  use_orch_card_start: "This project starts agents from its orchestrator config. Only a signed-in workspace owner can start one from the card.",
  not_configured: "This project has no orchestrator config. Choose a machine, project and topic.",
};
// The card cannot be started whatever the setup: show no Start control and no complaint.
export const NOT_CLAIMABLE = ["not_todo", "assigned", "proposal_open", "waiting_on_person", "blocked_by"];
const TEXT_CODES = { "card already has an active start": "active_start", "card was started recently": "started_recently" };

export const startRefusalWords = (error) => {
  const text = String(error?.payload?.error || error?.message || "").replace(/^no: /, "");
  const code = error?.payload?.code || TEXT_CODES[text] || text.match(/^(?:card is not startable: )?([a-z_]+)$/)?.[1];
  return START_REFUSALS[code] || refusalWords(error);
};

const ACTIVE = ["queued", "leased", "starting", "uncertain"];
const choices = (list, picked) => list.map((item) => `<option value="${e(item)}"${item === picked ? " selected" : ""}>${e(item)}</option>`).join("");

// The Start control for what the options route answered; empty when nothing should show.
function startHTML(start, jobs) {
  if (!start || NOT_CLAIMABLE.includes(start.refusal)) return "";
  const why = (text) => `<p class="run-start-why" data-run-start-why>${e(text)}</p>`;
  if (start.refusal) return why(START_REFUSALS[start.refusal] || "An agent cannot be started on this card right now.");
  if (jobs.some((job) => ACTIVE.includes(job.state))) return why(START_REFUSALS.active_start);
  const button = `<button class="btn sm" type="submit">Start agent</button>`;
  if (start.configured) {
    return `<form class="run-start" data-run-start><p>Start an agent on <strong>${e(start.machine?.label || "the project's machine")}</strong> · project ${e(start.project)} · topic ${e(start.topic)}.</p>${button}</form>`;
  }
  const [first] = start.machines || [];
  if (!first) return "";
  const field = (name, label, list) => `<label class="run-start-field">${label}<select name="${name}">${choices(list)}</select></label>`;
  return `<form class="run-start" data-run-start><p>This project has no orchestrator config. Choose where the agent runs.</p><div class="run-start-row"><label class="run-start-field">Machine<select name="machine">${start.machines.map((m) => `<option value="${e(m.id)}">${e(m.label)}</option>`).join("")}</select></label>${field("project", "Project", first.projects)}${field("topic", "Topic", first.topics)}</div>${button}</form>`;
}

const ago = (at) => {
  const value = elapsed(at);
  return value === "now" ? "just now" : value === "—" ? "" : `${value} ago`;
};

function runHTML(job) {
  const reason = outcomeWords(job.outcome_code);
  const machine = job.machine_label ? ` · ${e(job.machine_label)}` : "";
  const when = job.updated_at ? ` · <time datetime="${e(job.updated_at)}" title="${e(job.updated_at)}">${e(ago(job.updated_at))}</time>` : "";
  const cancel = ["queued", "leased", "starting"].includes(job.state) ? `<button class="btn sm" type="button" data-run-cancel="${e(job.job)}">Cancel</button>` : "";
  const resolve = job.state === "uncertain"
    ? `<form class="run-resolve" data-run-resolve="${e(job.job)}"><p>If it did start, paste the thread id of the agent that is running it.</p><div class="run-resolve-row"><label class="sr" for="run-thread-${e(job.job)}">Thread id</label><input id="run-thread-${e(job.job)}" name="thread" autocomplete="off" spellcheck="false" maxlength="256" placeholder="Thread id"><button class="btn sm" type="submit" value="started">It started</button><button class="btn sm" type="submit" value="failed" formnovalidate>It failed</button></div></form>`
    : "";
  return `<li data-run="${e(job.job)}" data-state="${e(job.state)}"><p class="run-line"><span class="tag run-state">${e(STATE_WORDS[job.state] || job.state)}</span>${machine}${when}</p>${reason ? `<p class="run-reason">${e(reason)}</p>` : ""}${job.thread_id ? `<p class="run-thread">Thread <code>${e(job.thread_id)}</code></p>` : ""}${cancel}${resolve}</li>`;
}

// The inner HTML of the section for a loaded list; `note` is the last refusal or confirmation.
export function cardRunsHTML(jobs, note = "", start = null) {
  const list = jobs.length ? `<ul class="run-list">${jobs.map(runHTML).join("")}</ul>` : `<p class="run-empty">No agent has been started on this card.</p>`;
  return `<div class="sec-h"><h3>Runs</h3></div>${list}${startHTML(start, jobs)}<p class="run-note" role="status" aria-live="polite">${e(note)}</p>`;
}

const slotHTML = (inner = "") => `<section class="card-runs" data-card-runs aria-label="Runs"${inner ? "" : " hidden"}>${inner}</section>`;
// A redraw of the panel shows the last list (and Start control) at once; attachCardRuns then refreshes it.
const cache = new Map();
const keyOf = (card) => `${card.board}/${card.number}`;

// The one insertion renderDetails makes: an empty, hidden section, or the last known list.
export const cardRunsSlot = (card) => slotHTML(cache.has(keyOf(card)) ? cardRunsHTML(cache.get(keyOf(card)).jobs, "", cache.get(keyOf(card)).start) : "");

// Load the card's runs into the slot inside `mount` and wire Cancel and the owner's choice.
export async function attachCardRuns(mount, card, { api = request } = {}) {
  const slot = mount.querySelector?.("[data-card-runs]");
  if (!slot || card?.number == null) return;
  const key = keyOf(card);
  let revision = 0;
  const live = (token) => token === revision && slot.isConnected;
  let current = { jobs: [], start: null };
  const show = (jobs, note = "", start = current.start) => {
    // A refresh must not undo what the owner picked in the Start form.
    const picked = Object.fromEntries(["machine", "project", "topic"].map((name) => [name, slot.querySelector?.(`[data-run-start] [name="${name}"]`)?.value]));
    current = { jobs, start };
    cache.set(key, current);
    slot.innerHTML = cardRunsHTML(jobs, note, start);
    slot.hidden = false;
    for (const [name, value] of Object.entries(picked)) {
      const select = value ? slot.querySelector?.(`[data-run-start] [name="${name}"]`) : null;
      if (select && [...(select.options || [])].some((option) => option.value === value)) select.value = value;
    }
  };
  async function load(note = "") {
    const token = ++revision;
    try {
      const task = encodeURIComponent(card.number);
      // Only the owner may read the options; for anyone else the Start control stays away.
      const [jobs, start] = await Promise.all([api(`/api/card-starts?task=${task}`), api(`/api/card-starts/options?task=${task}`).catch(() => null)]);
      const usable = start && typeof start === "object" && !Array.isArray(start) ? start : null;
      if (live(token)) show(Array.isArray(jobs) ? jobs : [], note, usable);
    } catch (error) {
      if (!live(token)) return;
      // Not allowed to see start jobs (a member) or no such card: show nothing rather than an error.
      if ([403, 404].includes(error?.status)) { cache.delete(key); slot.hidden = true; slot.innerHTML = ""; }
      else if (cache.has(key)) show(cache.get(key).jobs, "Could not refresh the runs. Showing the last list.");
    }
  }
  async function act(button, path, body, { done, words = refusalWords } = {}) {
    const row = button.closest?.("[data-run]");
    row?.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
    let note;
    try {
      const result = await api(path, { method: "POST", body });
      note = done || (result?.state === "cancelled" ? "Cancelled." : result?.state === "starting" ? "Cancel requested. The machine had already begun, so it may still start." : "Saved.");
    } catch (error) {
      note = words(error);
    }
    // A refusal usually means the job moved on: reload so the list shows what is true now.
    await load(note);
  }
  slot.addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-cancel]");
    if (button) void act(button, `/api/card-starts/${encodeURIComponent(button.dataset.runCancel)}/cancel`, {});
  });
  // Another machine has other projects and topics: offer only what it allows.
  slot.addEventListener("change", (event) => {
    const select = event.target;
    const form = select?.closest?.("[data-run-start]");
    const machine = select?.name === "machine" && current.start?.machines?.find((item) => item.id === select.value);
    if (!form || !machine) return;
    form.elements.project.innerHTML = choices(machine.projects);
    form.elements.topic.innerHTML = choices(machine.topics);
  });
  slot.addEventListener("submit", (event) => {
    const start = event.target.closest("[data-run-start]");
    if (start) {
      event.preventDefault();
      const button = start.querySelector("button");
      if (button?.disabled) return;
      if (button) button.disabled = true;
      const body = { task: card.number };
      if (!current.start?.configured) for (const name of ["machine", "project", "topic"]) body[name] = start.elements[name].value;
      void act(button || start, "/api/card-starts", body, { done: "Asked the machine to start an agent. It appears in the list above.", words: startRefusalWords });
      return;
    }
    const form = event.target.closest("[data-run-resolve]");
    if (!form) return;
    event.preventDefault();
    const outcome = event.submitter?.value;
    if (!["started", "failed"].includes(outcome)) return;
    const thread = form.elements.thread.value.trim();
    const note = slot.querySelector(".run-note");
    if (outcome === "started" && !THREAD_ID.test(thread)) {
      if (note) note.textContent = "Paste the thread id so the card can link to it, or choose It failed.";
      form.elements.thread.focus();
      return;
    }
    void act(form.querySelector("button"), `/api/card-starts/${encodeURIComponent(form.dataset.runResolve)}/resolve`, outcome === "started" ? { outcome, thread_id: thread } : { outcome });
  });
  await load();
}
