// Needs me queue order, shared by the shell's needs count and card lookups. Data in, data out.
const BUCKETS = ["asks", "done_review", "approvals", "backlog_from_agents", "plans", "ready_to_merge", "stale"];
const LABELS = { you: "Needs you", plan: "Awaiting plan review", merge: "Ready to merge", stale: "Stuck", checks: "Checks running", working: "Working", expired: "Expired", idle: "Idle", done: "Done", quiet: "Quiet" };

// One entry per card in the Needs you queue, ordered like the spoken digest:
// addressed asks first, then need, then card number.
export function needItems(needs) {
  const byNumber = new Map();
  for (const key of BUCKETS) for (const card of needs[key] || []) {
    if (!byNumber.has(card.number)) byNumber.set(card.number, { card, buckets: new Set() });
    byNumber.get(card.number).buckets.add(key);
  }
  const asked = ({ buckets }) => buckets.has("asks") || buckets.has("done_review");
  const key = (item) => (asked(item) ? 0 : Math.max(0, ["you", "release", "plan", "merge", "checks", "expired", "working", "idle", "done"].indexOf(item.card.need)));
  return [...byNumber.values()]
    .sort((a, b) => key(a) - key(b) || a.card.number - b.card.number)
    .map((item) => ({ ...item, reason: reason(item, asked(item)) }));
}
function reason({ card, buckets }, asked) {
  if (card.why != null) return card.why;
  if (asked && card.ask) return `asks: ${card.ask.split("\n")[0]}`;
  if (card.need === "plan") return "awaiting plan review";
  if (card.need === "merge") return "ready to merge";
  if (buckets.has("stale")) return "stopped checking in and has no available supervisor";
  if (buckets.has("backlog_from_agents")) return "filed by an agent";
  return LABELS[card.need] || "";
}
