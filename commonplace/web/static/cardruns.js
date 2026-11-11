import { api as request, escape as e, elapsed } from "./api.js";

// "Runs" in the card panel: the start jobs of one card, from GET /api/card-starts?task=<n>.
// The route answers only a signed-in owner (all jobs) or a trusted agent token (its own jobs); anyone
// else gets 403 or 404, and then the section stays hidden. So whoever sees a row here is the owner or
// the requester, and the owner-only "It started / It failed" choice is the server's call to refuse.

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
export function cardRunsHTML(jobs, note = "") {
  const list = jobs.length ? `<ul class="run-list">${jobs.map(runHTML).join("")}</ul>` : `<p class="run-empty">No agent has been started on this card.</p>`;
  return `<div class="sec-h"><h3>Runs</h3></div>${list}<p class="run-note" role="status" aria-live="polite">${e(note)}</p>`;
}

const slotHTML = (inner = "") => `<section class="card-runs" data-card-runs aria-label="Runs"${inner ? "" : " hidden"}>${inner}</section>`;
// A redraw of the panel shows the last list at once; attachCardRuns then refreshes it.
const cache = new Map();
const keyOf = (card) => `${card.board}/${card.number}`;

// The one insertion renderDetails makes: an empty, hidden section, or the last known list.
export const cardRunsSlot = (card) => slotHTML(cache.has(keyOf(card)) ? cardRunsHTML(cache.get(keyOf(card))) : "");

// Load the card's runs into the slot inside `mount` and wire Cancel and the owner's choice.
export async function attachCardRuns(mount, card, { api = request } = {}) {
  const slot = mount.querySelector?.("[data-card-runs]");
  if (!slot || card?.number == null) return;
  const key = keyOf(card);
  let revision = 0;
  const live = (token) => token === revision && slot.isConnected;
  const show = (jobs, note = "") => {
    cache.set(key, jobs);
    slot.innerHTML = cardRunsHTML(jobs, note);
    slot.hidden = false;
  };
  async function load(note = "") {
    const token = ++revision;
    try {
      const jobs = await api(`/api/card-starts?task=${encodeURIComponent(card.number)}`);
      if (live(token)) show(Array.isArray(jobs) ? jobs : [], note);
    } catch (error) {
      if (!live(token)) return;
      // Not allowed to see start jobs (a member) or no such card: show nothing rather than an error.
      if ([403, 404].includes(error?.status)) { cache.delete(key); slot.hidden = true; slot.innerHTML = ""; }
      else if (cache.has(key)) show(cache.get(key), "Could not refresh the runs. Showing the last list.");
    }
  }
  async function act(button, path, body) {
    const row = button.closest("[data-run]");
    row?.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
    let note;
    try {
      const result = await api(path, { method: "POST", body });
      note = result?.state === "cancelled" ? "Cancelled." : result?.state === "starting" ? "Cancel requested. The machine had already begun, so it may still start." : "Saved.";
    } catch (error) {
      note = refusalWords(error);
    }
    // A refusal usually means the job moved on: reload so the list shows what is true now.
    await load(note);
  }
  slot.addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-cancel]");
    if (button) void act(button, `/api/card-starts/${encodeURIComponent(button.dataset.runCancel)}/cancel`, {});
  });
  slot.addEventListener("submit", (event) => {
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
