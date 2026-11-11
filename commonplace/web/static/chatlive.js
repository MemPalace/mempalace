// Live delivery state of the person's newest chat message, as the server reports it in
// the chat page (`page.live`, see commonplace/chat/live.py). Pure: text and markup only.

const pad = (n) => String(n).padStart(2, "0");
const clock = (at) => {
  const date = at ? new Date(at) : null;
  return date && !Number.isNaN(date.getTime()) ? `${pad(date.getHours())}:${pad(date.getMinutes())}` : "";
};

// A person can act on these; everything else is a quiet progress line.
const ALERTS = new Set(["no_agent", "no_answer"]);
// While one of these lasts the next state is close, so the pane reads a little faster.
const PENDING = new Set(["delivered", "working"]);
const FAST_MS = 60_000;

/** True while the newest message is on its way or just picked up (the first minute). */
export function livePending(live, now = Date.now()) {
  if (!live?.message_id || !PENDING.has(live.state)) return false;
  const at = Date.parse(live.at);
  return Number.isNaN(at) || now - at < FAST_MS;
}

/** `{ title, detail, alert }` for a live state, or null when there is nothing to say. */
export function liveCopy(live, me) {
  if (!live?.message_id || live.state === "idle" || live.state === "answered") return null;
  const name = live.agent || "the orchestrator", owner = me?.role === "owner";
  const ask = (ownerText, memberText) => owner ? ownerText : memberText;
  const sent = clock(live.at), seen = clock(live.seen_at);
  let title = "", detail = "";
  if (live.state === "delivered") {
    if (live.reason === "wake_pending") { title = `Delivering to ${name}…`; detail = "Waking the agent now."; }
    else if (live.reason === "wake_retrying") { title = `Delivered. Waking ${name} did not work yet.`; detail = "Trying again. Your message is kept."; }
    else if (live.reason === "wake_none") { title = `Delivered to ${name}`; detail = `${name} has no wake-up hook, so it sees this on its next check.`; }
    else { title = `Delivered to ${name}`; detail = "Woken. Waiting for it to pick this up."; }
  } else if (live.state === "working") {
    title = `${name} is working on this`;
    detail = seen ? `Picked up at ${seen}. Its answer appears here.` : "Picked up. Its answer appears here.";
  } else if (live.state === "no_agent") {
    title = live.reason === "gone" ? "The agent is no longer connected" : "No agent connected";
    detail = `Your message is kept. ${ask("Connect an agent in Settings and it is delivered then.", "Ask the workspace owner to connect an agent.")}`;
  } else if (live.state === "no_answer") {
    title = `${name} did not answer`;
    const since = sent ? ` your message from ${sent}` : " your message";
    detail = live.reason === "wake_failed" ? `The wake-up call to ${name} failed.`
      : live.reason === "no_reply" ? `It read${since}${seen ? ` at ${seen}` : ""} and has not replied.`
      : `It has not picked up${since}.`;
    detail += ` ${ask("Check that the agent is running.", "Ask the workspace owner to check the agent.")}`;
  } else return null;
  return { title, detail, alert: ALERTS.has(live.state) };
}

/** The block under the stream. `e` escapes; agent names arrive redacted from the server. */
export function liveHTML(live, me, e) {
  const copy = liveCopy(live, me);
  if (!copy) return "";
  return `<div class="chat-live" data-live-state="${e(live.state)}" role="${copy.alert ? "alert" : "status"}"><b>${e(copy.title)}</b><p>${e(copy.detail)}</p></div>`;
}

/** One short line for the dock header, or "" to keep the dock's own text. */
export function liveLine(live) {
  if (!live?.message_id) return "";
  if (live.state === "working") return "Orchestrator is working on your message";
  if (live.state === "delivered") return live.reason === "wake_pending" ? "Delivering your message…" : "Message delivered";
  if (live.state === "no_answer") return `Not answering since ${clock(live.at) || "earlier"}`;
  if (live.state === "no_agent") return "No agent connected";
  return "";
}
