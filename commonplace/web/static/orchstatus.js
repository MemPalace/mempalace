// Project settings > Orchestrator: who coordinates this project right now, and the owner's "End lease".
// Reads the orchestrator checkpoint (read-only: it claims and renews nothing) and calls force-release.

export const ORCH_ROLE = "main";
const LIST_CAP = 200; // the checkpoint lists at most this many entries of each kind

export const ORCH_COPY = Object.freeze({
  nobody: "Nobody is coordinating this project.",
  loadError: "Could not load the coordinator status.",
  endError: "Could not end the lease. Nothing was changed. Try again.",
  notOwner: "Only the workspace owner can end a lease.",
  gone: "This project is no longer available.",
  alreadyEnded: "The lease had already ended.",
  ended: "Lease ended. Unfinished work stays listed below for the next coordinator to settle.",
  confirm: "The agent can no longer start or change anything under this lease. Work it already started keeps running and stays listed below. The next coordinator can then claim the lease.",
});

const plural = (n, one, many) => `${n >= LIST_CAP ? `${LIST_CAP}+` : n} ${n === 1 ? one : many}`;
const person = name => String(name || "").replace(/^conn:/, "") || "an agent";

/** "3 min ago" / "in 2 min": the distance between an ISO time and now, in plain words. */
export function when(iso, now = Date.now()) {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const seconds = Math.round((at - now) / 1000), span = Math.abs(seconds);
  const [n, unit] = span < 60 ? [span, "s"] : span < 3600 ? [Math.round(span / 60), "min"] : span < 86400 ? [Math.round(span / 3600), "h"] : [Math.round(span / 86400), "d"];
  if (span < 5) return "just now";
  return seconds > 0 ? `in ${n} ${unit}` : `${n} ${unit} ago`;
}

/** One sentence for the lease: held, ran out, given up, or never claimed. */
export function leaseSentence(lease, now = Date.now()) {
  if (!lease) return ORCH_COPY.nobody;
  const name = person(lease.holder), at = Date.parse(lease.expires_at);
  if (lease.live) return `Held by ${name}. The lease runs out ${when(lease.expires_at, now)} unless the agent renews it.`;
  if (!Number.isNaN(at) && at <= now) return `${ORCH_COPY.nobody} ${name} held the lease until it ran out ${when(lease.expires_at, now)}.`;
  return `${ORCH_COPY.nobody} ${name} gave the lease up.`;
}

export function orchCounts(checkpoint) {
  const size = key => (checkpoint[key] || []).length;
  return {open: size("open"), inDoubt: size("in_doubt"), needsPerson: size("needs_person"), unstarted: size("unstarted")};
}

function stepLabel(step, e) {
  const what = step.card ? `${step.kind} for card #${step.card}` : `${step.kind} ${step.step_key}`;
  return `${e(what)}${step.detail ? ` — ${e(step.detail)}` : ""}`;
}

export function orchStatusHTML(view, e, {canEnd = false, confirming = false, busy = false, status = "", now = Date.now()} = {}) {
  if (!view) return `<p role="status" data-orch-status>${e(status || ORCH_COPY.loadError)}</p>`;
  const {lease, config} = view, counts = orchCounts(view), live = Boolean(lease?.live);
  const unsettled = [...(view.in_doubt || []), ...(view.needs_person || [])];
  const parts = [plural(counts.open, "step open", "steps open"), `${counts.inDoubt >= LIST_CAP ? `${LIST_CAP}+` : counts.inDoubt} in doubt`];
  if (counts.needsPerson) parts.push(plural(counts.needsPerson, "needs a person", "need a person"));
  parts.push(plural(counts.unstarted, "Todo card not started", "Todo cards not started"));
  const configLine = config ? `Accepted config: version ${e(config.version)}.` : "No orchestrator config is accepted yet.";
  const stays = !live && counts.open ? '<p class="set-note" data-orch-stays>Unfinished work stays listed here until a new coordinator claims the lease and settles it.</p>' : "";
  const end = canEnd && live ? (confirming
    ? `<div class="callout wait" data-orch-confirm role="group" aria-label="End the lease"><p>End ${e(person(lease.holder))}'s lease? ${e(ORCH_COPY.confirm)}</p><button type="button" class="btn sm" data-orch-end-confirm ${busy ? "disabled" : ""}>End lease</button> <button type="button" class="btn ghost sm" data-orch-end-cancel ${busy ? "disabled" : ""}>Keep it</button></div>`
    : `<button type="button" class="btn sm" data-orch-end ${busy ? "disabled" : ""}>End lease…</button>`) : "";
  return `<h3>Coordinator</h3><p data-orch-lease><span class="tag ${live ? "ok" : ""}">${live ? "Live" : lease ? "Ended" : "None"}</span> ${e(leaseSentence(lease, now))}</p><p class="set-note" data-orch-counts>${e(parts.join(" · "))}. ${configLine}</p>${stays}${unsettled.length ? `<details data-orch-doubt><summary>${e(plural(unsettled.length, "step to settle", "steps to settle"))}</summary><ul>${unsettled.slice(0, 20).map(s => `<li>${stepLabel(s, e)} (${e(String(s.state).replace("_", " "))})</li>`).join("")}</ul></details>` : ""}${end}<button type="button" class="btn ghost sm" data-orch-refresh ${busy ? "disabled" : ""}>Refresh</button><p role="status" data-orch-status>${e(status)}</p>`;
}

/** Mounts below `anchor` (a node inside the Orchestrator section). Shows nothing when the
 *  workspace does not use orchestration or the board's status cannot be read. */
export async function mountOrchStatus(anchor, {project, api, e, current = () => true, now = () => Date.now()}) {
  if (!anchor || !project.orchestration) return;
  let host = anchor.nextElementSibling?.hasAttribute?.("data-project-orch-status") ? anchor.nextElementSibling : null;
  if (!host) {
    host = anchor.ownerDocument.createElement("div");
    host.setAttribute("data-project-orch-status", "");
    anchor.after(host);
  }
  const active = () => current() && host.isConnected;
  const base = `/api/orch/boards/${encodeURIComponent(project.board)}/roles/${ORCH_ROLE}`;
  let view = null, confirming = false, busy = false, status = "";
  // false when the status is not for this person to see (hidden); a failed read shows the error.
  async function load() {
    try { view = await api(`${base}/checkpoint`); }
    catch (error) {
      if (error?.status === 404 || error?.status === 403) { host.replaceChildren(); host.hidden = true; return false; }
      view = null;
      status = status || ORCH_COPY.loadError;
    }
    host.hidden = false;
    return true;
  }
  function render() {
    if (!active()) return;
    host.innerHTML = orchStatusHTML(view, e, {canEnd: Boolean(project.can_configure), confirming, busy, status, now: now()});
    const on = (name, fn) => { const node = host.querySelector(`[data-orch-${name}]`); if (node) node.onclick = fn; };
    on("end", () => { confirming = true; status = ""; render(); });
    on("end-cancel", () => { confirming = false; render(); });
    on("end-confirm", endLease);
    on("refresh", async () => { status = ""; if (await load() && active()) render(); });
  }
  async function endLease() {
    if (busy) return;
    busy = true; status = ""; render();
    let message;
    try {
      const result = await api(`${base}/force-release`, {method: "POST", body: {}});
      message = result?.released ? ORCH_COPY.ended : ORCH_COPY.alreadyEnded;
    } catch (error) {
      message = error?.status === 403 ? ORCH_COPY.notOwner : error?.status === 404 ? ORCH_COPY.gone : ORCH_COPY.endError;
    }
    busy = false; confirming = false; status = message;
    if (await load() && active()) render();
  }
  if (await load() && active()) render();
}
