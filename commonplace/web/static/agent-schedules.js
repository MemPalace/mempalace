// Scheduled prompts per agent: list, create, pause, resume and cancel (owners only).
const when = (stamp) => (stamp ? new Date(stamp).toLocaleString() : "none");
const labels = { owner_gone: "the owner is no longer an owner", agent_gone: "the agent is revoked or expired", not_coordinator: "the agent is no longer the board's coordinator", board_gone: "the board is gone", missed: "missed while the hub was down", interrupted: "interrupted by a restart", error: "an unexpected error" };
const cadence = (s) => (s.schedule.cron ? `cron ${s.schedule.cron}` : `every ${s.schedule.every_minutes} min`);

export function scheduleRow(s, agents, e) {
  const agent = agents.find((a) => a.id === s.agent_id);
  const result = s.last_status ? `Last: ${s.last_status}${s.last_code ? ` (${labels[s.last_code] || s.last_code})` : ""}` : "Not run yet";
  const paused = s.state === "paused" ? `<small>Paused${s.paused_reason && s.paused_reason !== "owner" ? `: ${e(labels[s.paused_reason] || s.paused_reason)}` : ""}</small>` : "";
  const resume = s.state === "paused" ? `<button type="button" data-schedule-act="resume" data-schedule-id="${e(s.id)}">Resume</button>` : `<button type="button" data-schedule-act="pause" data-schedule-id="${e(s.id)}">Pause</button>`;
  return `<div class="setting-row" data-schedule="${e(s.id)}"><span>${e(s.name)}<br><small>${e(agent ? agent.name : s.agent_id)} on ${e(s.board)} · ${e(cadence(s))} · ${e(s.timezone)}</small><br><small>${s.state === "active" ? `Next: ${e(when(s.next_due))}` : "Not scheduled"} · ${e(result)}</small> ${paused}</span>${resume}<button type="button" data-schedule-act="cancel" data-schedule-id="${e(s.id)}">Cancel</button></div>`;
}

export function mountAgentSchedules(host, { api, e, current = () => true }) {
  const active = () => current() && host.isConnected;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  let data = null, agents = [], boards = [], status = "";
  async function load() {
    try {
      [data, { connections: agents = [] }, { boards = [] }] = await Promise.all([api("/api/agent-schedules"), api("/api/connections"), api("/api/boards")]);
      agents = agents.filter((c) => !c.revoked_at && (!c.expires_at || Date.parse(c.expires_at) > Date.now()));
    } catch (error) {
      if (active()) { if (error.status === 403) host.replaceChildren(); else host.innerHTML = `<p class="message" role="alert">Could not load scheduled prompts</p>`; }
      return;
    }
    render();
  }
  function render() {
    if (!active()) return;
    const limit = data.limits;
    host.innerHTML = `<div data-schedule-list>${data.schedules.map((s) => scheduleRow(s, agents, e)).join("") || '<p class="message">No scheduled prompts</p>'}</div>
<form class="agent-form" data-schedule-form><label>Name<input name="name" required maxlength="80" autocomplete="off"></label><label>Agent<select name="agent" required>${agents.map((a) => `<option value="${e(a.id)}">${e(a.name)}</option>`).join("")}</select></label><label>Board (the agent must coordinate it)<select name="board" required>${boards.map((b) => `<option value="${e(b.slug)}">${e(b.slug)}</option>`).join("")}</select></label><label>Prompt<textarea name="prompt" required maxlength="${limit.max_prompt_chars}"></textarea></label><label>Repeat<select name="kind"><option value="every_minutes">Every N minutes</option><option value="cron">Cron (minute hour day month weekday)</option></select></label><label>Schedule<input name="value" required value="60" autocomplete="off"></label><small>Intervals start at ${limit.min_interval_minutes} minutes. Up to ${limit.max_schedules} schedules per workspace.</small><label>Time zone<input name="timezone" required value="${e(zone)}" autocomplete="off"></label><button class="blockbtn" data-schedule-create>Add schedule</button><p role="status" data-schedule-status>${e(status)}</p></form>`;
    host.querySelector("[name=kind]").onchange = (event) => { host.querySelector("[name=value]").value = event.target.value === "cron" ? "0 9 * * 1-5" : "60"; };
    host.querySelector("[data-schedule-form]").onsubmit = async (event) => {
      event.preventDefault();
      const f = new FormData(event.target), value = f.get("value").trim();
      const schedule = f.get("kind") === "cron" ? { cron: value } : { every_minutes: /^\d+$/.test(value) ? Number(value) : value };
      await act(() => api("/api/agent-schedules", { method: "POST", body: { name: f.get("name"), agent: f.get("agent"), board: f.get("board"), prompt: f.get("prompt"), schedule, timezone: f.get("timezone").trim() } }), "Added");
    };
    host.querySelectorAll("[data-schedule-act]").forEach((button) => {
      button.onclick = () => act(() => api(`/api/agent-schedules/${encodeURIComponent(button.dataset.scheduleId)}/${button.dataset.scheduleAct}`, { method: "POST", body: {} }), button.dataset.scheduleAct === "cancel" ? "Cancelled" : "Saved");
    });
  }
  async function act(run, done) {
    host.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { await run(); status = done; } catch (error) { status = error.message || "Request failed"; }
    if (!active()) return;
    try { data = await api("/api/agent-schedules"); } catch { /* keep the last list */ }
    render();
  }
  load();
}
