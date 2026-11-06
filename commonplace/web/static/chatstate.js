const ACK_WAIT_MS = 300_000;
const SEEN_PREFIX = "cmmn.chat.seen.";

// Keep the deterministic work-request rule aligned with chat/handoff.py.
const NOT_WORK_WORDS = new Set("so much very there morning afternoon evening night good hi hello hey thanks thank you ok okay cool great nice yes no yep nope bye cheers hoi hallo dank je bedankt top prima ja nee oké".split(" "));
const STATUS_PREFIXES = ["status", "progress", "update", "news", "any news", "how is", "how's", "is the", "did the", "where is", "what's the status", "what is the status", "where are we", "any update", "is it done", "hoe staat", "wat is de status", "is het klaar"];
const tokens = (text) => String(text || "").toLowerCase().match(/[\p{L}\p{N}_']+/gu) || [];

function isWorkRequest(text) {
  const words = tokens(text);
  if (!words.length || (words.length <= 4 && words.every((word) => NOT_WORK_WORDS.has(word)))) return false;
  if (words.length === 1 && ["status", "progress", "update", "news"].includes(words[0])) return false;
  return !String(text).trimEnd().endsWith("?") || !STATUS_PREFIXES.some((prefix) => tokens(prefix).every((word, i) => words[i] === word));
}

const id = (value) => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const latest = (rows) => (rows || []).reduce((max, row) => Math.max(max, id(row.id)), 0);
const cursors = (page = {}) => ({
  message: Math.max(id(page.cursor), latest(page.messages)),
  reply: Math.max(id(page.reply_cursor), latest(page.replies)),
});

/** Pure projection of a merged, person-scoped chat page. `now` is epoch milliseconds.
 * Call seenStore(...).read(page) first to baseline a user's first visit.
 */
export function deriveChatState(page = {}, status = {}, board = {}, now = Date.now(), seen = {}) {
  const messages = page.messages || [], replies = page.replies || [];
  const user = page.me?.user;
  const mine = (reply) => reply.for_user == null || reply.for_user === user;
  const agent = (message) => message.actor?.startsWith("conn:");
  const unread = new Set(messages.filter((message) => agent(message) && id(message.id) > id(seen.message)).map((message) => message.id)).size
    + new Set(replies.filter((reply) => mine(reply) && ["answer", "handoff"].includes(reply.kind) && id(reply.id) > id(seen.reply)).map((reply) => reply.id)).size;
  const orchestrator = status?.orchestrator ?? page.orchestrator;
  const last_seen = orchestrator?.visible ? orchestrator.last_seen ?? null : null;
  const newest = (rows, keep) => rows.reduce((found, row) => keep(row) && (!found || row.id > found.id) ? row : found, null);
  const person = (message) => message.actor?.startsWith("user:");
  const request = newest(messages, (message) => person(message) && isWorkRequest(message.text));
  const latest = newest(messages, person);
  // A message is open until an agent writes after it.
  const open = (message) => !!message && !messages.some((other) => agent(other) && other.id > message.id);
  // Receipts and supersession follow the newest message; the deadline follows the newest work request.
  const receiptFor = (message) => open(message)
    ? newest(replies, (reply) => mine(reply) && reply.kind === "receipt" && reply.message_id === message.id) : null;
  const receipt = receiptFor(latest) || receiptFor(request);
  let state = messages.length || replies.length ? "ok" : "none", message_id = request?.id ?? null;
  if (["no_orchestrator", "orchestrator_gone"].includes(receipt?.code)) {
    state = "waiting";
    if (!open(request)) message_id = latest.id;
  } else if (open(request)) {
    state = !orchestrator && !board?.coordinator ? "waiting"
      : now - Date.parse(request.at) > ACK_WAIT_MS ? "no_answer" : "working";
  }
  return { state, unread, message_id, last_seen };
}

// Seen ids persist on the device (contract: localStorage), so a new tab or a later visit
// still knows what was read. Only the two ids are stored.
function device() {
  try { return globalThis.localStorage; } catch { return undefined; }
}

// Escape dots as well, so user/board boundaries cannot collide.
const keyPart = (value) => encodeURIComponent(value).replace(/\./g, "%2E");
const userPrefix = (user) => `${SEEN_PREFIX}${keyPart(user)}.`;
const validSeen = (value) => value && Number.isSafeInteger(value.message) && value.message >= 0
  && Number.isSafeInteger(value.reply) && value.reply >= 0;

/** Device ids for one user and board (localStorage `cmmn.chat.seen.<user>.<board>`). read(page) baselines only if absent;
 * markSeen(page) advances when chat is read, never when it is merely polled.
 * Storage denial falls back to this store instance's memory.
 */
export function seenStore(user, board, storage = device()) {
  const key = `${userPrefix(user)}${keyPart(board)}`;
  let fallback = null;
  let memoryOnly = !storage;
  function load() {
    if (memoryOnly) return fallback;
    let raw;
    try {
      raw = storage.getItem(key);
    } catch {
      memoryOnly = true;
      return fallback;
    }
    try {
      const value = JSON.parse(raw);
      return validSeen(value) ? { message: value.message, reply: value.reply } : null;
    } catch { return null; }
  }
  function save(value) {
    fallback = { message: value.message, reply: value.reply };
    try {
      if (!memoryOnly) storage.setItem(key, JSON.stringify(fallback));
    } catch { memoryOnly = true; }
    return { ...fallback };
  }
  return {
    read(page = {}) {
      const value = load();
      return value ? { ...value } : save(cursors(page));
    },
    /** The stored ids, or null on first sight; never baselines. */
    peek() {
      const value = load();
      return value ? { ...value } : null;
    },
    markSeen(page = {}) {
      const previous = load() || { message: 0, reply: 0 }, next = cursors(page);
      return save({ message: Math.max(previous.message, next.message), reply: Math.max(previous.reply, next.reply) });
    },
    clearSeen() {
      fallback = null;
      try { storage?.removeItem(key); } catch { /* Clearing must also work without storage. */ }
    },
  };
}

/** Clear this user's seen ids across boards on sign-out; preserve other keys. */
export function clearSeen(user, storage = device()) {
  const prefix = userPrefix(user);
  try {
    const keys = Array.from({ length: storage?.length || 0 }, (_, index) => storage.key(index));
    for (const key of keys) if (key?.startsWith(prefix)) storage.removeItem(key);
  } catch { /* There is no persisted state to clear when storage is denied. */ }
}
