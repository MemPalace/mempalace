// Phone queue for cards that ask a question: yes or no on the advised option, and a no
// opens the other options. The POST waits here because the server has no undo: a send
// only goes once the hold ends, the next answer starts, or the page hides.
import { actor, elapsed, escape as e } from "./api.js";

export const COPY = {
  offline: "Offline. Answers are paused until the connection is back.",
  notSent: (number) => `#${number} not sent. The server did not answer.`,
  elsewhere: (number) => `#${number} was answered elsewhere. Nothing was sent from here.`,
  changed: "The question changed.",
  loadFailed: "The server did not answer.",
  sendsIn: (seconds) => `Sends in ${seconds} ${seconds === 1 ? "second" : "seconds"}`,
  showLater: (count) => `Show the ${count} for later`,
  backHome: "Back to Home",
  next: "Next question",
  later: "Later",
  undo: "Undo",
  retry: "Retry",
  advised: "advised",
  fresh: "new",
  reply: "Reply",
  yes: "Yes",
  no: "No",
  back: "Back",
  then: "Then which one?",
  thenOne: "Then what?",
  askNew: "Ask for new options",
  writeOwn: "Write my own",
  writeAnswer: "Write my answer",
  not: "Not",
  optionOf: (k, n) => `Option ${k} of ${n}`,
  asked: (who) => `Asked ${who} for new options`,
  askedWait: (who) => `Waiting for new options from ${who}`,
  comesBack: (who) => `It comes back here when ${who} asks again.`,
  waitingLine: "waiting for new options",
  newFor: (number) => `New options for #${number}`,
  releaseYes: "Release for yes",
  releaseNo: "Release for no",
  answered: (count) => `${count} answered.`,
  allAnswered: (count) => `All ${count} answered.`,
  forLater: (count) => `${count} for later.`,
  answeredLine: "answered elsewhere",
};

export const RELOAD_ERRORS = new Set(["card is not in review", "choose one of the offered options"]);
export const HOLD_SECONDS = 5;
export const ASKED_KEY = "cmmn.decide.asked";
const ASKED_DAYS = 7;
const ADVISED = /^(.*\S)\s*\((advies|advised)\)\s*$/i;

export function splitChoice(text) {
  const value = String(text ?? "");
  const match = value.match(ADVISED);
  if (!match) return { label: value, advised: false };
  return { label: match[1], advised: true };
}

// The option the card proposes: the one marked advised, else the first. -1 without options.
export function proposal(card) {
  const options = card?.ask_options || [];
  if (!options.length) return -1;
  const marked = options.findIndex((choice) => splitChoice(choice).advised);
  return marked >= 0 ? marked : 0;
}

export function answerPayload(card, optionIndex, reply) {
  const body = { version: card.version, then: "return" };
  const options = card.ask_options || [];
  if (Number.isInteger(optionIndex) && optionIndex >= 0 && optionIndex < options.length) body.choice = options[optionIndex];
  const text = typeof reply === "string" ? reply.trim() : "";
  if (text) body.reply = text;
  return body;
}

// The agent skill reads this wording: a reply that starts with "New options:" asks again.
export function newOptionsReply(rejected, note) {
  const lines = ["New options:", `Not: ${(rejected || []).join(" | ")}`];
  const text = typeof note === "string" ? note.trim() : "";
  if (text) lines.push(`Note: ${text}`);
  return lines.join("\n");
}

// Stored asks for new options, {number: {at, rejected}}. Old entries drop after a week.
export function pruneAsked(asked, now = Date.now()) {
  const out = {};
  for (const [number, entry] of Object.entries(asked || {})) {
    const at = Date.parse(entry?.at);
    if (Number.isFinite(at) && now - at < ASKED_DAYS * 86400000) out[number] = { at: entry.at, rejected: [...(entry.rejected || [])] };
  }
  return out;
}

function newer(card, entry) {
  if (!entry) return false;
  const at = Date.parse(entry.at), updated = Date.parse(card?.updated_at);
  return !Number.isFinite(at) || !Number.isFinite(updated) || updated > at;
}

export function decideQueue(needs = {}) {
  const seen = new Set();
  const out = [];
  for (const key of ["asks", "done_review"]) {
    const cards = [...(needs[key] || [])].sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || a.number - b.number);
    for (const card of cards) if (card && !seen.has(card.number)) { seen.add(card.number); out.push(card); }
  }
  return out;
}

export function hasQuestions(needs) {
  return decideQueue(needs).length > 0;
}

export function offlineNow(onLine, fails) {
  return onLine === false || fails >= 2;
}

export function queueKey(key, typing) {
  if (typing) return null;
  if (/^[1-6]$/.test(key)) return { type: "answer", optionIndex: Number(key) - 1 };
  if (key === "y" || key === "ArrowRight") return { type: "yes" };
  if (key === "n" || key === "ArrowLeft") return { type: "no" };
  if (key === "a") return { type: "ask-new" };
  if (key === "b" || key === "Backspace") return { type: "back" };
  if (key === "j") return { type: "move", delta: 1 };
  if (key === "k") return { type: "move", delta: -1 };
  if (key === "l") return { type: "later" };
  if (key === "u") return { type: "undo" };
  if (key === "o") return { type: "open" };
  if (key === "Enter") return { type: "focus-reply" };
  if (key === "Escape") return { type: "home" };
  return null;
}

export function createSession(start = null, me = "", opts = {}) {
  return {
    phase: "loading", ready: false, start: start ?? null, me: me || "", list: Boolean(opts.list),
    items: [], cursor: 0, replay: [], offline: false, slow: false, notice: null,
    asked: pruneAsked(opts.asked, opts.now),
  };
}

function blank(card, asked) {
  const entry = asked?.[card.number];
  return {
    number: card.number, card, question: card.ask ?? "", kind: "open", screen: "card",
    visited: false, again: false, missing: false, reply: "", pending: null, holdLeft: 0,
    asking: false, rejected: [], seen: false, fresh: Boolean(entry) && newer(card, entry),
  };
}

function copy(session) {
  return {
    ...session,
    items: session.items.map((item) => ({ ...item })),
    replay: [...session.replay],
    notice: session.notice ? { ...session.notice } : null,
    asked: { ...session.asked },
  };
}

function hold(session) {
  return session.items.find((item) => item.kind === "hold") || null;
}

function labels(card) {
  return (card.ask_options || []).map((choice) => splitChoice(choice).label);
}

function release(session, effects) {
  const item = hold(session);
  if (!item) return;
  item.kind = "sending";
  item.holdLeft = 0;
  item.visited = true;
  effects.push({ type: "post", number: item.number, body: item.pending, asking: item.asking, rejected: [...item.rejected] });
}

function landable(item) {
  return item.kind === "open" || (item.kind === "waiting" && !item.seen);
}

function land(session, index) {
  session.cursor = index;
  const item = session.items[index];
  if (item.kind === "waiting") item.seen = true;
}

function advance(session) {
  while (session.replay.length) {
    const number = session.replay.shift();
    const index = session.items.findIndex((item) => item.number === number);
    const item = session.items[index];
    if (item && item.kind === "open") {
      item.again = false;
      land(session, index);
      return true;
    }
  }
  const index = session.items.findIndex((item, i) => i > session.cursor && landable(item));
  if (index >= 0) { land(session, index); return true; }
  // Questions skipped with j/k, a jump or a replay are still open before the cursor.
  const before = session.items.findIndex((item, i) => i < session.cursor && item.kind === "open");
  if (before >= 0) { land(session, before); return true; }
  return false;
}

function finish(session) {
  const current = session.items[session.cursor];
  if (current && current.kind !== "open") current.visited = true;
  if (!advance(session)) session.phase = "done";
  else session.phase = "queue";
  const landed = session.items[session.cursor];
  if (session.phase === "queue" && landed?.missing && landed.kind === "open") return { type: "get", number: landed.number };
  return null;
}

function toEnd(session, index) {
  const [item] = session.items.splice(index, 1);
  session.items.push(item);
  if (session.cursor > index) session.cursor -= 1;
  else if (session.cursor === index) session.cursor = session.items.length - 1;
}

function forget(session, number, effects) {
  if (!(number in session.asked)) return;
  delete session.asked[number];
  effects.push({ type: "asked", asked: { ...session.asked } });
}

function sameAnswer(event, pending) {
  const choice = pending.choice ?? null;
  const reply = pending.reply || "";
  return (event.choice ?? null) === choice && (event.text || "") === reply;
}

function settle(session, item, effects, card, at) {
  item.holdLeft = 0;
  item.again = false;
  item.visited = true;
  session.replay = session.replay.filter((n) => n !== item.number);
  if (session.notice?.number === item.number) session.notice = null;
  if (!item.asking) {
    item.kind = "sent";
    item.fresh = false;
    forget(session, item.number, effects);
  } else {
    item.kind = "waiting";
    item.seen = false;
    item.fresh = false;
    // The time of the ask itself, so an agent that already asked again still counts as new.
    session.asked[item.number] = { at: at || card?.updated_at || new Date().toISOString(), rejected: [...item.rejected] };
    effects.push({ type: "asked", asked: { ...session.asked } });
    toEnd(session, session.items.indexOf(item));
  }
  // A retry or a recovered send on the card on screen moves the queue on.
  if (session.phase === "queue" && session.items[session.cursor] === item) {
    const get = finish(session);
    if (get) effects.push(get);
  }
}

function applyCard(session, number, card, focus, effects) {
  const index = session.items.findIndex((item) => item.number === number);
  if (index < 0 || !card) return;
  const item = session.items[index];
  const show = Boolean(focus) || session.cursor === index;
  const last = [...(card.events || [])].reverse().find((event) => event.kind === "decide");
  if (last && item.pending && last.actor === session.me && sameAnswer(last, item.pending)) {
    settle(session, item, effects, card, last.at);
    return;
  }
  const previous = item.question;
  const previousOptions = JSON.stringify(item.card.ask_options || []);
  if (card.ask != null) item.question = card.ask;
  item.card = card;
  item.missing = false;
  if (card.status !== "review" || card.ask == null) {
    item.kind = "elsewhere";
    item.pending = null;
    item.holdLeft = 0;
    session.replay = session.replay.filter((n) => n !== number);
    if (session.notice?.number === number) session.notice = null;
    forget(session, number, effects);
    if (show) { session.cursor = index; session.phase = "queue"; }
    return;
  }
  const changed = card.ask !== previous || JSON.stringify(card.ask_options || []) !== previousOptions;
  if (!changed) {
    if (item.pending) item.pending = { ...item.pending, version: card.version };
    if (item.pending) fail(session, item);
    return;
  }
  item.kind = "open";
  item.screen = "card";
  item.pending = null;
  item.asking = false;
  item.holdLeft = 0;
  item.again = false;
  session.replay = session.replay.filter((n) => n !== number);
  if (!show) return;
  session.cursor = index;
  session.phase = "queue";
  session.notice = { kind: "changed", number };
}

function fail(session, item) {
  item.kind = "open";
  item.again = true;
  item.visited = true;
  item.holdLeft = 0;
  session.notice = { kind: "failed", number: item.number, body: item.pending, text: COPY.notSent(item.number) };
  if (session.phase === "done") {
    session.phase = "queue";
    session.cursor = session.items.indexOf(item);
    session.replay = session.replay.filter((n) => n !== item.number);
    return;
  }
  session.replay = [item.number, ...session.replay.filter((n) => n !== item.number)];
}

function drop(session, index, text) {
  const number = session.items[index].number;
  session.items.splice(index, 1);
  if (session.cursor > index) session.cursor -= 1;
  if (session.cursor >= session.items.length) session.cursor = Math.max(0, session.items.length - 1);
  session.replay = session.replay.filter((n) => n !== number);
  session.notice = { kind: "error", text: text || COPY.loadFailed };
  if (!session.items.length) session.phase = "done";
  else if (session.phase === "done") session.phase = "queue";
}

function current(session) {
  if (session.phase !== "queue") return null;
  return session.items[session.cursor] || null;
}

function answerable(session) {
  const item = current(session);
  return item && ["open", "later"].includes(item.kind) ? item : null;
}

// A swipe carries the question it started on; if the queue moved or the question changed under it, it answers nothing.
function drawn(item, event) {
  if (event.number == null) return true;
  return item.number === event.number && item.question === event.question
    && JSON.stringify(item.card.ask_options || []) === event.options && item.screen === "card";
}

function startHold(session, item, body, effects) {
  release(session, effects);
  item.kind = "hold";
  item.pending = body;
  item.holdLeft = HOLD_SECONDS;
  item.visited = true;
  item.again = false;
  session.replay = session.replay.filter((n) => n !== item.number);
  if (session.notice?.kind === "changed" || session.notice?.number === item.number) session.notice = null;
  const get = finish(session);
  if (get) effects.push(get);
}

export function reduce(session, event) {
  const effects = [];
  const next = copy(session);
  const done = () => ({ session: next, effects });
  if (event.type === "offline") {
    next.offline = Boolean(event.offline);
    return done();
  }
  if (event.type === "slow") {
    if (!next.ready) next.slow = true;
    return done();
  }
  if (event.type === "load") {
    if (next.ready) return done();
    const ordered = (event.cards || []).map((card) => blank(card, next.asked));
    if (!ordered.length) return { session: { ...next, ready: true, phase: "empty", items: [], slow: false }, effects: [{ type: "home" }] };
    let items = ordered;
    if (next.start != null) {
      const index = ordered.findIndex((item) => item.number === next.start);
      if (index > 0) items = [...ordered.slice(index), ...ordered.slice(0, index)];
      if (index >= 0 && next.list && (items[0].card.ask_options || []).length) items[0].screen = "list";
    }
    const queued = new Set(items.map((item) => item.number));
    for (const number of Object.keys(next.asked)) if (!queued.has(Number(number))) effects.push({ type: "get-asked", number: Number(number) });
    Object.assign(next, { ready: true, phase: "queue", items, cursor: 0, slow: false });
    return done();
  }
  if (event.type === "asked-card") {
    // A card this device asked new options for, after a reload: show it as waiting, or drop it.
    const number = event.number, card = event.card, entry = next.asked[number];
    if (!entry || next.items.some((item) => item.number === number)) return done();
    if (!card || card.status === "done") { forget(next, number, effects); return done(); }
    const item = blank(card, next.asked);
    if (card.status === "review" && card.ask != null) item.fresh = true;
    else Object.assign(item, { kind: "waiting", seen: true, rejected: [...entry.rejected] });
    next.items.push(item);
    return done();
  }
  if (event.type === "sync") {
    if (!next.ready) return done();
    const cards = event.cards || [];
    const incoming = new Map(cards.map((card) => [card.number, card]));
    const known = new Set(next.items.map((item) => item.number));
    for (const card of cards) if (!known.has(card.number)) next.items.push(blank(card, next.asked));
    for (const item of [...next.items]) {
      if (item.kind !== "waiting" || !incoming.has(item.number)) continue;
      const card = incoming.get(item.number);
      if (!newer(card, next.asked[item.number])) continue;
      // The agent asked again: the card comes back at the end, marked new. The current card never jumps.
      Object.assign(item, { card, question: card.ask ?? "", kind: "open", screen: "card", fresh: true, seen: false, pending: null, asking: false, missing: false, visited: false });
      if (next.items.indexOf(item) !== next.cursor || next.phase !== "queue") toEnd(next, next.items.indexOf(item));
    }
    next.items.forEach((item, index) => {
      if (["hold", "sending", "sent", "waiting"].includes(item.kind)) return;
      if (!incoming.has(item.number)) {
        item.missing = true;
        if (index === next.cursor && next.phase === "queue") effects.push({ type: "get", number: item.number });
        return;
      }
      const card = incoming.get(item.number);
      item.missing = false;
      if (item.kind === "elsewhere") return;
      const changed = card.ask !== item.question || JSON.stringify(card.ask_options || []) !== JSON.stringify(item.card.ask_options || []);
      if (card.ask != null) item.question = card.ask;
      item.card = card;
      if (changed) item.screen = "card";
      if (changed && index === next.cursor && item.kind === "open") next.notice = { kind: "changed", number: item.number };
    });
    return done();
  }
  if (event.type === "tick") {
    const item = hold(next);
    if (!item) return done();
    item.holdLeft -= 1;
    if (item.holdLeft <= 0) release(next, effects);
    return done();
  }
  if (event.type === "flush") {
    release(next, effects);
    return done();
  }
  if (event.type === "undo") {
    const item = hold(next);
    if (!item) return done();
    // Back to the screen the answer came from, with the typed text.
    item.kind = "open";
    item.holdLeft = 0;
    item.visited = false;
    item.again = false;
    item.pending = null;
    item.asking = false;
    next.cursor = next.items.indexOf(item);
    next.phase = "queue";
    next.replay = next.replay.filter((n) => n !== item.number);
    return done();
  }
  if (event.type === "no") {
    const item = answerable(next);
    if (!item || next.offline || proposal(item.card) < 0 || !drawn(item, event)) return done();
    item.screen = "list";
    return done();
  }
  if (event.type === "reply-open") {
    const item = answerable(next);
    if (!item || proposal(item.card) < 0) return done();
    item.screen = "list";
    return done();
  }
  if (event.type === "back") {
    const item = answerable(next);
    if (!item || item.screen !== "list") return done();
    item.screen = "card";
    return done();
  }
  if (event.type === "later") {
    const item = answerable(next);
    if (!item) return done();
    item.kind = "later";
    item.visited = true;
    item.again = false;
    next.replay = next.replay.filter((n) => n !== item.number);
    const get = finish(next);
    if (get) effects.push(get);
    return done();
  }
  if (event.type === "show-later") {
    const later = next.items.filter((item) => item.kind === "later");
    if (!later.length) return done();
    for (const item of later) { item.kind = "open"; item.visited = false; item.again = false; }
    next.replay = [];
    next.cursor = next.items.findIndex((item) => item.kind === "open");
    next.phase = "queue";
    next.notice = next.notice?.kind === "changed" ? null : next.notice;
    return done();
  }
  if (event.type === "next") {
    const item = current(next);
    if (!item || !["elsewhere", "waiting"].includes(item.kind)) return done();
    const get = finish(next);
    if (get) effects.push(get);
    return done();
  }
  if (event.type === "move" || event.type === "jump") {
    if (!["queue", "done"].includes(next.phase) || !next.items.length) return done();
    if (event.type === "jump") {
      const index = next.items.findIndex((item) => item.number === event.number);
      if (index < 0) return done();
      next.cursor = index;
    } else if (next.phase === "queue") next.cursor = Math.max(0, Math.min(next.items.length - 1, next.cursor + (event.delta || 0)));
    else return done();
    next.phase = "queue";
    const landed = next.items[next.cursor];
    if (landed.kind === "waiting") landed.seen = true;
    if (landed?.missing && landed.kind === "open") effects.push({ type: "get", number: landed.number });
    return done();
  }
  if (event.type === "retry") {
    if (next.offline || next.notice?.kind !== "failed" || !next.notice.body) return done();
    const item = next.items.find((row) => row.number === next.notice.number);
    if (!item || item.kind === "sending") return done();
    item.kind = "sending";
    item.pending = next.notice.body;
    item.again = false;
    item.holdLeft = 0;
    next.replay = next.replay.filter((n) => n !== item.number);
    effects.push({ type: "post", number: item.number, body: next.notice.body, asking: item.asking, rejected: [...item.rejected] });
    return done();
  }
  if (event.type === "result") {
    const index = next.items.findIndex((item) => item.number === event.number);
    if (index < 0) return done();
    const item = next.items[index];
    if (event.status === 200) {
      settle(next, item, effects, event.card);
      return done();
    }
    if (event.status === 409 || (event.status === 403 && RELOAD_ERRORS.has(event.error))) {
      if (event.card) applyCard(next, event.number, event.card, true, effects);
      else effects.push({ type: "get", number: event.number });
      return done();
    }
    if (event.status === 400 || event.status === 403 || event.status === 404) {
      drop(next, index, event.error);
      return done();
    }
    if (!item.pending) {
      next.notice = { kind: "error", text: COPY.loadFailed };
      return done();
    }
    fail(next, item);
    return done();
  }
  if (event.type === "card") {
    applyCard(next, event.number, event.card, false, effects);
    return done();
  }
  if (event.type === "yes") {
    // An answer has to match the version on screen. A send from a dead connection is refused, not queued.
    const item = answerable(next);
    if (!item || next.offline || item.screen !== "card" || !drawn(item, event)) return done();
    const index = proposal(item.card);
    if (index < 0) return done();
    item.asking = false;
    startHold(next, item, answerPayload(item.card, index, ""), effects);
    return done();
  }
  if (event.type === "answer" || event.type === "reply") {
    const item = answerable(next);
    if (!item || next.offline) return done();
    const optionIndex = event.type === "reply" ? null : event.optionIndex;
    const reply = typeof event.reply === "string" ? event.reply : item.reply;
    const body = answerPayload(item.card, optionIndex, reply);
    if (!body.choice && !body.reply) return done();
    item.reply = typeof event.reply === "string" ? event.reply : item.reply;
    item.asking = false;
    startHold(next, item, body, effects);
    return done();
  }
  if (event.type === "ask-new") {
    const item = answerable(next);
    if (!item || next.offline || !item.card.return_to || proposal(item.card) < 0) return done();
    const note = typeof event.reply === "string" ? event.reply : item.reply;
    item.reply = note || "";
    item.asking = true;
    item.rejected = labels(item.card);
    startHold(next, item, { version: item.card.version, then: "return", reply: newOptionsReply(item.rejected, note) }, effects);
    return done();
  }
  return done();
}

// ---------- Render ----------

function who(card) {
  return actor(card.return_to);
}

function metaHTML(card) {
  const age = card.updated_at ? `<span>${e(elapsed(card.updated_at))}</span>` : "";
  return `<p class="meta"><b>#${card.number}</b><span>${e(card.board)}</span>${card.return_to ? `<span>${e(who(card))}</span>` : ""}${age}</p>`;
}

function shown(item) {
  if (item.asking || item.kind === "waiting") return COPY.asked(who(item.card));
  if (item.pending?.choice) return splitChoice(item.pending.choice).label;
  return item.pending?.reply || item.reply || "";
}

function asQuestion(label) {
  return /[?？]$/.test(label) ? label : `${label}?`;
}

function cellClass(session, item, index) {
  if (session.phase === "done") return item.kind === "later" ? "now" : "done";
  if (session.phase === "queue" && index === session.cursor) return "now";
  if (["sent", "hold", "sending", "waiting"].includes(item.kind)) return "done";
  if (item.again && item.visited) return "done";
  return "wait";
}

export function meterHTML(session) {
  const count = session.items.length;
  const fraction = session.phase === "done" ? "done" : session.phase === "queue" && count ? `${session.cursor + 1}/${count}` : "";
  const cells = count > 12 || !count ? "" : session.items.map((item, index) => `<i class="${cellClass(session, item, index)}"></i>`).join("");
  const only = count > 12 || session.phase === "done" ? " only" : "";
  return `<span class="decide-meter" aria-hidden="true">${cells}</span><span class="frac${only}">${e(fraction)}</span>`;
}

export function leftCount(session) {
  if (session.phase !== "queue") return null;
  return session.items.filter((item) => item.kind === "open" || item.kind === "later").length;
}

export function toolsHTML(session) {
  return `<div class="decide-tools">${meterHTML(session)}<a class="close" href="#/" aria-label="Back to Home">×</a></div>`;
}

function blocksHTML(left) {
  return `<span class="blocks" aria-hidden="true">${Array.from({ length: HOLD_SECONDS }, (_, i) => `<i class="${i < left ? "on" : ""}"></i>`).join("")}</span>`;
}

export function statusHTML(session) {
  const item = hold(session);
  if (item) {
    const said = item.asking ? `<b>${e(shown(item))}</b>` : `<span class="num">#${item.number}</span> <b>${e(shown(item))}</b>`;
    return `<div class="decide-status" role="status"><i class="decide-dot ok" aria-hidden="true"></i><span class="text">${said}</span><span class="end">${blocksHTML(item.holdLeft)}<span class="sr-only">${e(COPY.sendsIn(item.holdLeft))}</span><button type="button" class="opt" data-action="queue-undo">${COPY.undo}</button></span></div>`;
  }
  if (session.notice?.kind === "failed") {
    const sending = session.items.some((row) => row.number === session.notice.number && row.kind === "sending");
    return `<div class="decide-status bad" role="alert"><i class="decide-dot bad" aria-hidden="true"></i><span class="text">${e(session.notice.text)}</span><button type="button" class="opt" data-action="queue-retry" ${sending || session.offline ? "disabled" : ""}>${COPY.retry}</button></div>`;
  }
  if (session.notice?.kind === "error") return `<div class="decide-status bad" role="alert"><i class="decide-dot bad" aria-hidden="true"></i><span class="text">${e(session.notice.text)}</span></div>`;
  if (session.offline && session.phase === "queue") return `<div class="decide-status" role="status"><i class="decide-dot hollow" aria-hidden="true"></i><span class="text">${e(COPY.offline)}</span></div>`;
  const now = current(session);
  if (now?.kind === "elsewhere") return `<div class="decide-status" role="status"><i class="decide-dot ok" aria-hidden="true"></i><span class="text">${e(COPY.elsewhere(now.number))}</span></div>`;
  if (session.notice?.kind === "changed" && now && session.notice.number === now.number) return `<div class="decide-status" role="status"><i class="decide-dot now" aria-hidden="true"></i><span class="text">${e(COPY.changed)}</span></div>`;
  if (session.slow && session.phase === "loading") return `<div class="decide-status bad" role="alert"><i class="decide-dot bad" aria-hidden="true"></i><span class="text">${e(COPY.loadFailed)}</span><button type="button" class="opt" data-action="retry">${COPY.retry}</button></div>`;
  return "";
}

function listHTML(session) {
  return `<ol class="decide-list">${session.items.map((item, index) => {
    const selected = session.phase === "queue" && index === session.cursor;
    const answered = ["sent", "hold", "sending"].includes(item.kind);
    const dot = answered ? "ok" : item.kind === "waiting" ? "ring" : item.kind === "elsewhere" ? "hollow" : "now";
    const sub = answered ? `answered: ${shown(item)}` : item.kind === "waiting" ? COPY.waitingLine : item.kind === "later" ? COPY.later : item.kind === "elsewhere" ? COPY.answeredLine : `#${item.number} ${item.card.board} ${who(item.card)}`;
    return `<li class="${selected ? "sel" : ""}" ${selected ? 'aria-current="true"' : ""}><button type="button" data-action="queue-jump" data-card="${item.number}"><i class="decide-dot ${dot}" aria-hidden="true"></i><span class="lt">${e(item.card.title)}</span><span class="ls">${e(sub)}</span></button></li>`;
  }).join("")}</ol>`;
}

function bar(session) {
  return `<header class="decide-bar"><a class="decide-mark" href="#/" aria-label="Commonplace"><svg viewBox="0 0 16 16" aria-hidden="true"><path fill-rule="evenodd" d="M1 12 L8 16 L15 12 L15 9 L11 9 L11 10 L8 12 L5 10 L5 6 L8 4 L11 6 L11 7 L15 7 L15 4 L8 0 L1 4Z"/></svg></a><h1>Decide</h1>${meterHTML(session)}<a class="close" href="#/" aria-label="Back to Home">×</a></header>`;
}

function headHTML(item, extra = "") {
  return `<div class="decide-body">${metaHTML(item.card)}<a class="decide-title" href="#/card/${item.number}">${e(item.card.title)}</a>${item.card.review_context && ![item.card.status_line, item.card.body, [item.card.status_line, item.card.body].filter(Boolean).join("\n\n")].includes(item.card.review_context) ? `<p class="decide-context">${e(item.card.review_context)}</p>` : ""}${item.card.status_line ? `<p class="decide-context">${e(item.card.status_line)}</p>` : ""}${item.card.body ? `<p class="decide-context">${e(item.card.body.slice(0, 600))}${item.card.body.length > 600 ? "…" : ""}</p>` : ""}${extra}</div>`;
}

function questionText(item) {
  return item.question || item.card.ask || "";
}

function replyForm(item, draft, opts) {
  const { placeholder, disabled, solo } = opts;
  return `<form class="decide-field${solo ? " solo" : ""}" data-form="queue-reply" data-card="${item.number}"><input name="reply" aria-label="${e(placeholder)}" placeholder="${e(placeholder)}" data-draft="reply-${item.number}" autocomplete="off" enterkeyhint="send" value="${e(draft)}" required><button class="send" type="submit" aria-label="Send" ${disabled ? "disabled" : ""}>↵</button></form>`;
}

function yesNoHTML(session, item) {
  const options = item.card.ask_options || [];
  const index = proposal(item.card);
  const { label, advised } = splitChoice(options[index]);
  const off = session.offline;
  const tags = `${advised ? `<span class="tag">${COPY.advised}</span>` : ""}${item.fresh ? `<span class="tag new">${COPY.fresh}</span>` : ""}`;
  const name = [COPY.optionOf(index + 1, options.length), advised ? COPY.advised : "", item.fresh ? COPY.fresh : "", asQuestion(label)].filter(Boolean).join(", ");
  const card = `<article class="decide-card now" data-swipe="${off ? "off" : "on"}" data-card="${item.number}"><span class="stamp yes" aria-hidden="true">YES</span><span class="stamp no" aria-hidden="true">NO</span>${headHTML(item, `<p class="decide-q">${e(questionText(item))}</p>`)}<div class="decide-foot"><p class="decide-of">${e(COPY.optionOf(index + 1, options.length))}${tags}</p><h2 class="decide-prop" tabindex="-1" data-question="${item.number}:card" aria-label="${e(name)}">${e(asQuestion(label))}</h2></div></article>`;
  const dock = `<div class="decide-dock"><div class="decide-track" aria-hidden="true"><span class="rail"><i class="tick l"></i><i class="tick r"></i><i class="fill"></i></span><span class="label"></span></div><div class="decide-yn"><button type="button" class="opt no" data-action="queue-no" aria-label="No, show other options" ${off ? "disabled" : ""}><span aria-hidden="true">←</span> ${COPY.no}<kbd class="k">N</kbd></button><button type="button" class="opt go yes" data-action="queue-yes" aria-label="${e(`Yes, ${label}`)}" ${off ? "disabled" : ""}>${COPY.yes} <span aria-hidden="true">→</span><kbd class="k">Y</kbd></button></div><div class="decide-more"><button type="button" class="opt soft" data-action="queue-reply-open">${COPY.reply}</button><button type="button" class="opt soft" data-action="queue-later">${COPY.later}<kbd class="k">L</kbd></button></div></div>`;
  return { card, dock };
}

function noListHTML(session, item, draft) {
  const options = item.card.ask_options || [];
  const index = proposal(item.card);
  const off = session.offline;
  const rest = options.map((choice, i) => ({ choice, i })).filter(({ i }) => i !== index);
  const heading = rest.length ? COPY.then : COPY.thenOne;
  const rejected = splitChoice(options[index]).label;
  const card = `<article class="decide-card now list" data-card="${item.number}">${headHTML(item, `<p class="decide-q">${e(questionText(item))}</p><p class="decide-not">${COPY.not} <s>${e(rejected)}</s></p>`)}<div class="decide-foot"><h2 class="decide-then" tabindex="-1" data-question="${item.number}:list">${heading}</h2></div></article>`;
  const rows = rest.map(({ choice, i }) => {
    const { label } = splitChoice(choice);
    return `<button type="button" class="opt row" data-action="queue-answer" data-option="${i}" aria-label="${e(label)}" ${off ? "disabled" : ""}><span>${e(label)}</span>${i < 6 ? `<kbd class="k">${i + 1}</kbd>` : ""}</button>`;
  }).join("");
  const ask = item.card.return_to ? `<button type="button" class="opt ask" data-action="queue-ask-new" ${off ? "disabled" : ""}>${COPY.askNew}<kbd class="k">A</kbd></button>` : "";
  const many = rest.length >= 4 ? " many" : "";
  const dock = `<div class="decide-dock list${many}">${rows ? `<div class="decide-rows">${rows}</div><hr class="decide-rule">` : ""}${ask}<div class="decide-reply">${replyForm(item, draft, { placeholder: COPY.writeOwn, disabled: off })}<button type="button" class="opt back" data-action="queue-back"><span aria-hidden="true">←</span> ${COPY.back}</button></div></div>`;
  return { card, dock, many: Boolean(many) };
}

function freeHTML(session, item, draft) {
  const card = `<article class="decide-card now free" data-card="${item.number}">${headHTML(item)}<div class="decide-foot"><h2 class="decide-ask" tabindex="-1" data-question="${item.number}:card">${e(questionText(item))}</h2></div></article>`;
  const dock = `<div class="decide-dock">${replyForm(item, draft, { placeholder: COPY.writeAnswer, disabled: session.offline, solo: true })}<button type="button" class="opt soft wide" data-action="queue-later">${COPY.later}<kbd class="k">L</kbd></button></div>`;
  return { card, dock };
}

function waitingHTML(item) {
  const by = who(item.card);
  const nots = item.rejected.map((label) => `<p class="decide-not">${COPY.not} <s>${e(label)}</s></p>`).join("");
  const card = `<article class="decide-card waiting" data-card="${item.number}">${headHTML(item, `<p class="decide-q">${e(questionText(item))}</p>`)}<div class="decide-foot"><div class="decide-wait"><h2 tabindex="-1" data-question="${item.number}:waiting" aria-label="${e(COPY.askedWait(by))}"><i class="decide-dot ring" aria-hidden="true"></i>${e(COPY.asked(by))}</h2>${nots}<p class="decide-back">${e(COPY.comesBack(by))}</p></div></div></article>`;
  const dock = `<div class="decide-dock"><button type="button" class="opt go wide" data-action="queue-next">${COPY.next} <span aria-hidden="true">→</span></button></div>`;
  return { card, dock };
}

function underHTML(session) {
  // The next question peeks out under the current one, so the queue reads as a stack.
  const rest = session.items.filter((item, index) => index > session.cursor && item.kind === "open");
  if (!rest.length) return "";
  const item = rest[0];
  const options = item.card.ask_options || [];
  const index = proposal(item.card);
  const foot = index >= 0 ? `<div class="decide-foot"><p class="decide-of">${e(COPY.optionOf(index + 1, options.length))}</p><p class="decide-prop">${e(asQuestion(splitChoice(options[index]).label))}</p></div>` : "";
  const edge = rest.length > 1 ? `<div class="decide-edge" aria-hidden="true"></div>` : "";
  return `${edge}<div class="decide-card under" aria-hidden="true" inert>${headHTML(item, `<p class="decide-q">${e(questionText(item))}</p>`)}${foot}</div>`;
}

function questionHTML(session, drafts) {
  const item = session.items[session.cursor];
  const status = statusHTML(session);
  if (!item) return `${status}<div class="decide-stage"><article class="decide-card"><span class="cursor" aria-hidden="true"></span></article></div>`;
  if (item.kind === "elsewhere") {
    return `${status}<div class="decide-stage"><article class="decide-card stale">${headHTML(item)}<div class="decide-foot"><h2 class="decide-ask" tabindex="-1" data-question="${item.number}:stale">${e(questionText(item))}</h2></div></article></div><div class="decide-dock"><button type="button" class="opt go wide" data-action="queue-next">${COPY.next} <span aria-hidden="true">→</span></button></div>`;
  }
  if (item.kind === "waiting") {
    const { card, dock } = waitingHTML(item);
    return `${status}<div class="decide-stage">${card}</div>${dock}`;
  }
  if (["sent", "sending", "hold"].includes(item.kind)) {
    return `${status}<div class="decide-stage"><article class="decide-card">${headHTML(item, `<p class="decide-q">${e(questionText(item))}</p>`)}<div class="decide-foot"><h2 class="decide-ask" tabindex="-1" data-question="${item.number}:said">${e(shown(item))}</h2></div></article></div>`;
  }
  const draft = drafts[`reply-${item.number}`] ?? item.reply ?? "";
  let view;
  if (proposal(item.card) < 0) view = freeHTML(session, item, draft);
  else if (item.screen === "list") view = noListHTML(session, item, draft);
  else view = yesNoHTML(session, item);
  const under = item.screen === "card" && proposal(item.card) >= 0 ? underHTML(session) : "";
  return `${status}<div class="decide-stage${view.many ? " short" : ""}">${under}${view.card}</div>${view.dock}`;
}

function doneHTML(session) {
  const answered = session.items.filter((item) => ["sent", "hold", "sending", "waiting"].includes(item.kind)).length;
  const later = session.items.filter((item) => item.kind === "later").length;
  const heading = later ? `<span>${e(COPY.answered(answered))}</span><span>${e(COPY.forLater(later))}</span>` : `<span>${e(COPY.allAnswered(answered))}</span>`;
  const rows = session.items.filter((item) => ["sent", "hold", "sending", "waiting", "elsewhere"].includes(item.kind)).map((item) => {
    const dot = item.kind === "waiting" ? "ring" : item.kind === "elsewhere" ? "hollow" : "ok";
    const text = item.kind === "waiting" ? COPY.waitingLine : item.kind === "elsewhere" ? COPY.answeredLine : shown(item);
    return `<li><i class="decide-dot ${dot}" aria-hidden="true"></i><span class="num">#${item.number}</span><span class="said">${e(text)}</span></li>`;
  }).join("");
  const fresh = session.items.filter((item) => item.kind === "open" && item.fresh).map((item) => `<button type="button" class="opt wide" data-action="queue-jump" data-card="${item.number}"><i class="decide-dot ring" aria-hidden="true"></i>${e(COPY.newFor(item.number))}</button>`).join("");
  const show = later ? `<button type="button" class="opt wide" data-action="queue-show-later">${e(COPY.showLater(later))}</button>` : "";
  return `${statusHTML(session)}<div class="decide-done"><h2 class="done-title" tabindex="-1" data-question="done">${heading}</h2><ol>${rows}</ol></div><div class="decide-dock">${fresh}${show}<a class="opt go wide" href="#/">${COPY.backHome}</a></div>`;
}

export function renderDecide(session, opts = {}) {
  const drafts = opts.drafts || {};
  let main = "";
  if (session.phase === "loading") main = `${statusHTML(session)}<div class="decide-stage"><article class="decide-card"><span class="cursor" aria-hidden="true"></span></article></div>`;
  else if (session.phase === "done") main = doneHTML(session);
  else if (session.phase === "queue") main = questionHTML(session, drafts);
  else return "";
  return `<section class="decide" data-phase="${e(session.phase)}">${bar(session)}${listHTML(session)}<div class="decide-main">${main}</div></section>`;
}


// Shared history row; all event text, including reasons, is quoted data.
export function eventItem(event, name = actor) {
  const clock = (at) => `<time datetime="${e(at)}" title="${e(at)}">${e(at?.slice(11, 16) || "")}</time>`;
  return `<li class="ev">${clock(event.at)}<span><span class="actor">${e(name(event.actor))}</span> <span class="v">${e(event.kind)}${event.target ? ` → ${e(name(event.target))}` : ""}</span></span>${event.text != null || event.choice ? `<p class="said">${e(event.choice || "")}${event.choice && event.text ? "\n" : ""}${e(event.text || "")}</p>` : ""}${event.reason != null ? `<p class="said">Reason: ${e(event.reason)}</p>` : ""}</li>`;
}
