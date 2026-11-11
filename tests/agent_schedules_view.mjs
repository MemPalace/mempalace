// Row rendering of the Scheduled prompts section. Run: node tests/agent_schedules_view.mjs
import assert from "node:assert/strict";

const { scheduleRow } = await import(new URL("../commonplace/web/static/agent-schedules.js", import.meta.url));
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const agents = [{ id: "c1", name: "Builder <bot>" }];
const base = { id: "sch1", agent_id: "c1", board: "work", name: "Morning <b>check</b>", schedule: { cron: "0 9 * * 1-5" }, timezone: "Europe/Amsterdam", state: "active", paused_reason: "", next_due: "2030-01-02T08:00:00Z", last_status: "", last_code: "" };

const active = scheduleRow(base, agents, e);
assert.match(active, /Morning &lt;b&gt;check&lt;\/b&gt;/);
assert.match(active, /Builder &lt;bot&gt; on work · cron 0 9 \* \* 1-5 · Europe\/Amsterdam/);
assert.match(active, /data-schedule-act="pause"/);
assert.match(active, /data-schedule-act="cancel"/);
assert.doesNotMatch(active, /data-schedule-act="resume"/);
assert.match(active, /Not run yet/);

const paused = scheduleRow({ ...base, state: "paused", paused_reason: "not_coordinator", next_due: null, last_status: "failed", last_code: "not_coordinator", schedule: { every_minutes: 30 } }, agents, e);
assert.match(paused, /data-schedule-act="resume"/);
assert.doesNotMatch(paused, /data-schedule-act="pause"/);
assert.match(paused, /every 30 min/);
assert.match(paused, /Paused: the agent is no longer the board&#39;s coordinator/);
assert.match(paused, /Not scheduled/);
console.log("ok");
