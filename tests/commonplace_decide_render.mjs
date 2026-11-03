import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const decide = await import(url((await read("decide.js")).replace('"./api.js"', `"${apiUrl}"`)));
const swipe = await import(url(await read("decide-swipe.js")));
const {
  COPY, HOLD_SECONDS, RELOAD_ERRORS, answerPayload, decideQueue, hasQuestions, offlineNow,
  queueKey, createSession, reduce, renderDecide, splitChoice, meterHTML, proposal, newOptionsReply, pruneAsked,
} = decide;

const at = "2026-10-31T08:00:00Z";
const card = (number, fields) => ({
  number, board: "lab", title: `Question ${number}`, status: "review", need: "you",
  ask: `Question ${number}?`, ask_options: [`Keep ${number} (advised)`, `Change ${number}`],
  return_to: "conn:asker", version: 1, updated_at: at, created_at: `2026-10-31T0${number}:00:00Z`,
  ...fields,
});
const cards = [card(1), card(2), card(3), card(4), card(5)];

assert.deepEqual(answerPayload(cards[0], 0, "  note  "), {
  version: 1, then: "return", choice: "Keep 1 (advised)", reply: "note",
});
assert.deepEqual(answerPayload(cards[0], 7, "   "), { version: 1, then: "return" });
assert.deepEqual(answerPayload({ ...cards[0], ask_options: [] }, null, "only words"), {
  version: 1, then: "return", reply: "only words",
});
assert.deepEqual(splitChoice("Keep 1 (advised)"), { label: "Keep 1", advised: true });
assert.deepEqual(splitChoice("Keep 1 (advies)"), { label: "Keep 1", advised: true });
assert.deepEqual(splitChoice("Change 1"), { label: "Change 1", advised: false });
assert.equal(splitChoice("(advised)").advised, false);

const older = card(9, { created_at: "2026-10-30T00:00:00Z" });
const queued = decideQueue({ asks: [cards[2], older, cards[0]], done_review: [cards[0], card(8, { ask: "Done?" })] });
assert.deepEqual(queued.map((item) => item.number), [9, 1, 3, 8]);
assert.equal(hasQuestions({ asks: [cards[0]] }), true);
assert.equal(hasQuestions({ ready_to_merge: [cards[0]] }), false);
assert.equal(offlineNow(false, 1), true);
assert.equal(offlineNow(true, 1), false);
assert.equal(offlineNow(true, 2), true);
assert.equal(queueKey("1", true), null);
assert.equal(queueKey("j", false).type, "move");
assert.equal(queueKey("u", false).type, "undo");
assert.equal(queueKey("Escape", false).type, "home");
assert.equal(queueKey("y", false).type, "yes");
assert.equal(queueKey("ArrowRight", false).type, "yes");
assert.equal(queueKey("n", false).type, "no");
assert.equal(queueKey("ArrowLeft", false).type, "no");
assert.equal(queueKey("a", false).type, "ask-new");
assert.equal(queueKey("b", false).type, "back");
assert.equal(queueKey("Backspace", false).type, "back");
assert.equal(queueKey("y", true), null);
assert.ok(RELOAD_ERRORS.has("card is not in review"));

function step(session, event) {
  const next = reduce(session, event);
  return next;
}
function load(list = cards, me = "user:me") {
  return step(createSession(null, me), { type: "load", cards: list }).session;
}

let session = load();
let moved = step(session, { type: "answer", optionIndex: 0, reply: "note" });
assert.equal(moved.effects.length, 0);
assert.equal(moved.session.items[0].kind, "hold");
assert.equal(moved.session.cursor, 1);
assert.equal(moved.session.items[0].pending.choice, "Keep 1 (advised)");
session = moved.session;
for (let i = 0; i < HOLD_SECONDS - 1; i += 1) {
  const tick = step(session, { type: "tick" });
  assert.equal(tick.effects.length, 0);
  session = tick.session;
}
const sent = step(session, { type: "tick" });
assert.equal(sent.effects.length, 1);
assert.equal(sent.effects[0].type, "post");
assert.equal(sent.effects[0].body.then, "return");
assert.equal(sent.effects[0].body.choice, "Keep 1 (advised)");
assert.equal(sent.effects[0].body.reply, "note");
assert.equal(step(sent.session, { type: "flush" }).effects.length, 0);

session = step(load(), { type: "no" }).session;
session = step(session, { type: "answer", optionIndex: 1, reply: "note" }).session;
const undone = step(session, { type: "undo" });
assert.equal(undone.effects.length, 0);
assert.equal(undone.session.cursor, 0);
assert.equal(undone.session.items[0].kind, "open");
assert.equal(undone.session.items[0].reply, "note");
const restored = renderDecide(undone.session, { drafts: { "reply-1": "note" } });
assert.match(restored, /value="note"/);
assert.doesNotMatch(restored, /data-action="queue-undo"/);

session = load();
session = step(session, { type: "answer", optionIndex: 0 }).session;
const second = step(session, { type: "answer", optionIndex: 1, reply: "next" });
assert.equal(second.effects.filter((effect) => effect.type === "post").length, 1);
assert.equal(second.effects[0].number, 1);
assert.equal(second.session.items[1].kind, "hold");
assert.equal(second.session.cursor, 2);

session = load();
session = step(session, { type: "answer", optionIndex: 0 }).session;
const flushed = step(session, { type: "flush" });
assert.equal(flushed.effects.length, 1);
assert.equal(flushed.effects[0].type, "post");
assert.equal(step(flushed.session, { type: "flush" }).effects.length, 0);

session = load();
session = step(session, { type: "later" }).session;
assert.equal(session.items[0].kind, "later");
assert.equal(session.cursor, 1);
for (const item of session.items.slice(1)) session = step(session, { type: "later" }).session;
assert.equal(session.phase, "done");
const done = renderDecide(session);
assert.match(done, /0 answered\./);
assert.match(done, /5 for later\./);
assert.match(done, /Show the 5 for later/);
assert.match(done, /Back to Home/);
const resumed = step(session, { type: "show-later" });
assert.equal(resumed.effects.length, 0);
assert.equal(resumed.session.phase, "queue");
assert.ok(resumed.session.items.every((item) => item.kind === "open"));

session = load();
session = step(session, { type: "offline", offline: true }).session;
const blocked = step(session, { type: "answer", optionIndex: 0, reply: "nope" });
assert.equal(blocked.effects.length, 0);
assert.equal(blocked.session.items[0].kind, "open");
const offline = renderDecide(blocked.session);
assert.match(offline, new RegExp(COPY.offline));
assert.match(offline, /data-action="queue-yes"[^>]*disabled/);
assert.match(offline, /data-action="queue-later"/);
assert.doesNotMatch(offline.slice(offline.indexOf("queue-later")), /^[^<]*disabled/);
const parked = step(blocked.session, { type: "later" });
assert.equal(parked.effects.length, 0);
assert.equal(parked.session.items[0].kind, "later");

session = load();
session = step(session, { type: "answer", optionIndex: 0 }).session;
const failed = step(session, { type: "result", number: 1, status: 500 });
assert.equal(failed.session.cursor, 1);
assert.equal(failed.session.items[0].kind, "open");
assert.equal(failed.session.replay[0], 1);
assert.match(renderDecide(failed.session), /#1 not sent/);
assert.match(renderDecide(failed.session), /data-action="queue-retry"/);
assert.match(meterHTML(failed.session), /<i class="done"><\/i><i class="now">/);

const elsewhereCard = { ...cards[0], status: "todo", ask: null, events: [{ kind: "decide", actor: "user:other", choice: "Change 1", text: null }] };
session = load();
session = step(session, { type: "answer", optionIndex: 0 }).session;
const elsewhere = step(session, { type: "result", number: 1, status: 409, card: elsewhereCard });
assert.equal(elsewhere.effects.filter((effect) => effect.type === "post").length, 0);
assert.equal(elsewhere.session.items[0].kind, "elsewhere");
assert.equal(elsewhere.session.cursor, 0);
const stale = renderDecide(elsewhere.session);
assert.match(stale, /#1 was answered elsewhere/);
assert.match(stale, /Next question/);
assert.doesNotMatch(stale, /data-action="queue-(answer|yes|no)"/);

const changedCard = { ...cards[0], ask: "A new question?", ask_options: ["Now"], version: 4, events: [] };
session = load();
const changed = step(session, { type: "result", number: 1, status: 409, card: changedCard });
assert.equal(changed.effects.length, 0);
assert.equal(changed.session.items[0].question, "A new question?");
assert.equal(changed.session.notice.kind, "changed");
assert.match(renderDecide(changed.session), /The question changed\./);
assert.match(renderDecide(changed.session), /data-action="queue-yes"[^>]*aria-label="Yes, Now"|aria-label="Yes, Now"/);

const lost = { ...cards[0], status: "todo", ask: null, events: [{ kind: "decide", actor: "user:me", choice: "Keep 1 (advised)", text: "note" }] };
session = load();
session = step(session, { type: "answer", optionIndex: 0, reply: "note" }).session;
const recovered = step(session, { type: "result", number: 1, status: 409, card: lost });
assert.equal(recovered.session.items[0].kind, "sent");
assert.equal(recovered.session.notice, null);

session = load();
const dropped = step(session, { type: "result", number: 1, status: 403, error: "not allowed" });
assert.equal(dropped.session.items.length, 4);
assert.match(renderDecide(dropped.session), /not allowed/);
assert.doesNotMatch(renderDecide(dropped.session), /Question 1\?/);

const reviewed = { ...cards[0], status: "todo", ask: null, events: [] };
session = load();
const reloaded = step(session, { type: "result", number: 1, status: 403, error: "card is not in review", card: reviewed });
assert.equal(reloaded.session.items[0].kind, "elsewhere");

session = load([cards[0], cards[1]]);
session = step(session, { type: "move", delta: 1 }).session;
const quiet = step(session, { type: "card", number: 1, card: reviewed });
assert.equal(quiet.session.cursor, 1);
assert.equal(quiet.session.items[0].kind, "elsewhere");

const added = step(load([cards[0]]), { type: "sync", cards: [cards[0], cards[4]] });
assert.equal(added.session.cursor, 0);
assert.deepEqual(added.session.items.map((item) => item.number), [1, 5]);

const many = Array.from({ length: 13 }, (_, index) => card(index + 1));
const wide = step(createSession(), { type: "load", cards: many }).session;
assert.match(meterHTML(wide), /1\/13/);
assert.doesNotMatch(meterHTML(wide), /<i /);

const empty = step(createSession(), { type: "load", cards: [] });
assert.equal(empty.session.phase, "empty");
assert.deepEqual(empty.effects, [{ type: "home" }]);

const slow = step(createSession(), { type: "slow" }).session;
const waiting = renderDecide(slow);
assert.match(waiting, /class="cursor"/);
assert.match(waiting, /The server did not answer/);
assert.match(waiting, /data-action="retry"/);
assert.doesNotMatch(waiting, /data-action="queue-answer"/);

// The yes/no card proposes the advised option, else the first.
assert.equal(proposal(card(1, { ask_options: ["Rename it", "Keep the old name (advised)"] })), 1);
assert.equal(proposal(card(1, { ask_options: ["ship", "redo"] })), 0);
assert.equal(proposal(card(1, { ask_options: [] })), -1);
assert.equal(newOptionsReply(["Glyph only", "Both"], ""), "New options:\nNot: Glyph only | Both");
assert.equal(newOptionsReply(["A"], "  shorter please "), "New options:\nNot: A\nNote: shorter please");

const advisedCard = card(1, { ask_options: ["Rename it", "Keep the old name (advised)", "Both"] });
let yesNo = load([advisedCard, cards[1]]);
const cardView = renderDecide(yesNo);
assert.match(cardView, /Option 2 of 3<span class="tag">advised<\/span>/);
assert.match(cardView, /aria-label="Option 2 of 3, advised, Keep the old name\?"[^>]*>Keep the old name\?<\/h2>/);
assert.match(cardView, /aria-label="Yes, Keep the old name"/);
assert.match(cardView, /aria-label="No, show other options"/);
assert.match(cardView, /data-swipe="on"/);
assert.match(cardView, /class="decide-card under"/);
assert.ok(cardView.indexOf('data-action="queue-no"') < cardView.indexOf('data-action="queue-yes"'));
assert.doesNotMatch(cardView, /data-action="queue-answer"/);

// Yes sends the advised option verbatim after the hold.
let yes = step(yesNo, { type: "yes" });
assert.equal(yes.effects.length, 0);
assert.equal(yes.session.items[0].pending.choice, "Keep the old name (advised)");
assert.equal(yes.session.cursor, 1);
assert.match(renderDecide(yes.session), /<span class="num">#1<\/span> <b>Keep the old name<\/b>/);

// No sends nothing and shows the other options; Back returns.
const no = step(yesNo, { type: "no" });
assert.equal(no.effects.length, 0);
assert.equal(no.session.items[0].screen, "list");
const listView = renderDecide(no.session);
assert.match(listView, /Then which one\?/);
assert.match(listView, /Not <s>Keep the old name<\/s>/);
assert.match(listView, /data-option="0"[^>]*><span>Rename it<\/span>/);
assert.match(listView, /data-option="2"[^>]*><span>Both<\/span>/);
assert.doesNotMatch(listView, /data-option="1"/);
assert.ok(listView.indexOf("Rename it") < listView.indexOf("Both") && listView.indexOf("Both") < listView.indexOf("Ask for new options"));
assert.match(listView, /placeholder="Write my own"/);
assert.match(listView, /data-action="queue-back"/);
assert.doesNotMatch(listView, /data-swipe="on"/);
const back = step(no.session, { type: "back" });
assert.equal(back.session.items[0].screen, "card");
assert.equal(back.effects.length, 0);
assert.equal(step(no.session, { type: "yes" }).session.items[0].kind, "open");

// A row sends that option; undo comes back to the list.
const row = step(no.session, { type: "answer", optionIndex: 2, reply: "" });
assert.equal(row.session.items[0].pending.choice, "Both");
const rowUndo = step(row.session, { type: "undo" });
assert.equal(rowUndo.session.items[0].screen, "list");
assert.equal(rowUndo.session.items[0].kind, "open");

// Reply opens the list; a typed reply alone sends.
const replyOpen = step(yesNo, { type: "reply-open" });
assert.equal(replyOpen.session.items[0].screen, "list");
const typed = step(replyOpen.session, { type: "reply", reply: "Neither, ship later" });
assert.deepEqual(typed.session.items[0].pending, { version: 1, then: "return", reply: "Neither, ship later" });

// Ask for new options: the reply names what was rejected, then the card waits at the end.
const asked = step(no.session, { type: "ask-new", reply: "shorter" });
assert.equal(asked.session.items[0].kind, "hold");
assert.equal(asked.session.items[0].pending.reply, "New options:\nNot: Rename it | Keep the old name | Both\nNote: shorter");
assert.equal(asked.session.items[0].pending.then, "return");
assert.equal(asked.session.items[0].pending.choice, undefined);
assert.match(renderDecide(asked.session), /<b>Asked asker for new options<\/b>/);
const askUndo = step(asked.session, { type: "undo" });
assert.equal(askUndo.session.items[0].screen, "list");
assert.equal(askUndo.session.items[0].reply, "shorter");
let flushedAsk = step(asked.session, { type: "flush" });
assert.equal(flushedAsk.effects[0].asking, true);
const sentAsk = step(flushedAsk.session, { type: "result", number: 1, status: 200, card: { ...advisedCard, status: "todo", updated_at: "2026-10-31T09:00:00Z" } });
assert.deepEqual(sentAsk.session.items.map((item) => item.number), [2, 1]);
assert.equal(sentAsk.session.items[1].kind, "waiting");
assert.equal(sentAsk.session.cursor, 0);
assert.deepEqual(sentAsk.effects, [{ type: "asked", asked: { 1: { at: "2026-10-31T09:00:00Z", rejected: ["Rename it", "Keep the old name", "Both"] } } }]);
// The queue reaches the waiting card once, and Next moves on.
let atWait = step(sentAsk.session, { type: "later" }).session;
assert.equal(atWait.phase, "queue");
assert.equal(atWait.items[atWait.cursor].kind, "waiting");
const waitView = renderDecide(atWait);
assert.match(waitView, /aria-label="Waiting for new options from asker"/);
assert.match(waitView, /Asked asker for new options/);
assert.match(waitView, /Not <s>Rename it<\/s>/);
assert.match(waitView, /It comes back here when asker asks again\./);
assert.match(waitView, /data-action="queue-next"/);
assert.doesNotMatch(waitView, /decide-card waiting now|data-action="queue-yes"/);
assert.equal(step(atWait, { type: "yes" }).session.items[1].kind, "waiting");
const afterWait = step(atWait, { type: "next" }).session;
assert.equal(afterWait.phase, "done");
const doneWait = renderDecide(afterWait);
assert.match(doneWait, /decide-dot ring[^<]*<\/i><span class="num">#1<\/span><span class="said">waiting for new options/);

// The agent asks again: the card comes back at the end with the new tag.
const reasked = { ...advisedCard, ask: "Which of these?", ask_options: ["Fresh A", "Fresh B (advised)"], version: 3, updated_at: "2026-10-31T09:30:00Z" };
const back2 = step(afterWait, { type: "sync", cards: [reasked] }).session;
assert.equal(back2.items[back2.items.length - 1].kind, "open");
assert.equal(back2.items[back2.items.length - 1].fresh, true);
assert.match(renderDecide(back2), /New options for #1/);
const fresh = step(back2, { type: "jump", number: 1 }).session;
const freshView = renderDecide(fresh);
assert.match(freshView, /Option 2 of 2<span class="tag">advised<\/span><span class="tag new">new<\/span>/);
const answeredFresh = step(step(fresh, { type: "yes" }).session, { type: "flush" }).session;
const clear = step(answeredFresh, { type: "result", number: 1, status: 200, card: reasked });
assert.deepEqual(clear.effects, [{ type: "asked", asked: {} }]);
// A stale needs-me snapshot from before the ask does not count as new.
const stillWaiting = step(sentAsk.session, { type: "sync", cards: [{ ...advisedCard, updated_at: "2026-10-31T08:59:00Z" }, cards[1]] }).session;
assert.equal(stillWaiting.items.find((item) => item.number === 1).kind, "waiting");

// After a reload on this device: stored asks reload their cards.
const store = { 7: { at: "2026-10-31T09:00:00Z", rejected: ["Old"] }, 8: { at: "2026-10-01T09:00:00Z", rejected: ["Ancient"] } };
assert.deepEqual(Object.keys(pruneAsked(store, Date.parse("2026-10-31T12:00:00Z"))), ["7"]);
const reload = step(createSession(null, "user:me", { asked: store, now: Date.parse("2026-10-31T12:00:00Z") }), { type: "load", cards: [cards[0]] });
assert.deepEqual(reload.effects, [{ type: "get-asked", number: 7 }]);
const waitingAgain = step(reload.session, { type: "asked-card", number: 7, card: card(7, { status: "todo", ask: null }) }).session;
assert.equal(waitingAgain.items[1].kind, "waiting");
assert.deepEqual(waitingAgain.items[1].rejected, ["Old"]);
const goneDone = step(reload.session, { type: "asked-card", number: 7, card: card(7, { status: "done" }) });
assert.deepEqual(goneDone.effects, [{ type: "asked", asked: {} }]);
const freshLoad = step(createSession(null, "", { asked: store, now: Date.parse("2026-10-31T12:00:00Z") }), { type: "load", cards: [card(7, { updated_at: "2026-10-31T10:00:00Z" })] }).session;
assert.equal(freshLoad.items[0].fresh, true);

// Asker unknown: nobody would get the request, so the button is hidden.
const orphan = step(load([card(1, { return_to: null })]), { type: "no" }).session;
assert.doesNotMatch(renderDecide(orphan), /queue-ask-new/);
assert.equal(step(orphan, { type: "ask-new" }).session.items[0].kind, "open");

// One option: a no shows no rows.
const one = step(load([card(1, { ask: "Done?", ask_options: ["Ship"] })]), { type: "no" }).session;
const oneView = renderDecide(one);
assert.match(oneView, /Then what\?/);
assert.match(oneView, /Not <s>Ship<\/s>/);
assert.doesNotMatch(oneView, /queue-answer|decide-rows/);
assert.match(oneView, /queue-ask-new/);

// Four or more other options: the card shrinks and the rows scroll.
const wideList = step(load([card(1, { ask_options: ["A", "B", "C", "D", "E"] })]), { type: "no" }).session;
assert.match(renderDecide(wideList), /decide-stage short/);
assert.match(renderDecide(wideList), /decide-dock list many/);

// No options: free text only, no yes or no, no swipe.
const free = renderDecide(load([card(1, { ask_options: [] })]));
assert.doesNotMatch(free, /queue-yes|queue-no|data-swipe/);
assert.match(free, /placeholder="Write my answer"/);
assert.match(free, /class="decide-ask"[^>]*>Question 1\?<\/h2>/);
assert.match(free, /data-action="queue-later"/);
assert.equal(step(load([card(1, { ask_options: [] })]), { type: "no" }).session.items[0].screen, "card");

// Keys 1 to 6 pick an option directly, from the yes/no card too.
const direct = step(yesNo, { type: "answer", optionIndex: 2, reply: "" });
assert.equal(direct.session.items[0].pending.choice, "Both");

// Offline: yes, no, rows and ask are off, and the card does not swipe.
const dark = step(yesNo, { type: "offline", offline: true }).session;
const darkView = renderDecide(dark);
assert.match(darkView, /data-swipe="off"/);
assert.match(darkView, /data-action="queue-no"[^>]*disabled/);
assert.match(darkView, /data-action="queue-yes"[^>]*disabled/);
assert.equal(step(dark, { type: "yes" }).session.items[0].kind, "open");
assert.equal(step(dark, { type: "no" }).session.items[0].screen, "card");
const darkList = renderDecide(step(no.session, { type: "offline", offline: true }).session);
assert.match(darkList, /data-action="queue-answer"[^>]*disabled/);
assert.match(darkList, /data-action="queue-ask-new"[^>]*disabled/);
assert.doesNotMatch(darkList, /data-action="queue-back"[^>]*disabled/);

// Deep link to the list screen for a later lock-screen No.
const deep = step(createSession(2, "", { list: true }), { type: "load", cards }).session;
assert.equal(deep.items[0].number, 2);
assert.equal(deep.items[0].screen, "list");

// A retry that succeeds on the card on screen moves the queue on (no dead end).
let retried = step(load(cards.slice(0, 3)), { type: "yes" }).session;
retried = step(retried, { type: "flush" }).session;
retried = step(retried, { type: "result", number: 1, status: 500 }).session;
retried = step(retried, { type: "later" }).session;
assert.equal(retried.items[retried.cursor].number, 1);
retried = step(retried, { type: "retry" }).session;
retried = step(retried, { type: "result", number: 1, status: 200 }).session;
assert.equal(retried.phase, "queue");
assert.equal(retried.items[retried.cursor].number, 3);
// A lost response found on the card on screen counts as sent and moves on.
let found = step(load(cards.slice(0, 3)), { type: "yes" }).session;
found = step(found, { type: "flush" }).session;
found = step(found, { type: "move", delta: -1 }).session;
assert.equal(found.items[found.cursor].number, 1);
found = step(found, { type: "card", number: 1, card: { ...cards[0], status: "todo", ask: null, events: [{ kind: "decide", actor: "user:me", choice: "Keep 1 (advised)", text: null, at: "2026-10-31T09:00:00Z" }] } }).session;
assert.equal(found.items[0].kind, "sent");
assert.equal(found.items[found.cursor].number, 2);
// Ask for new options fails, is retried on screen, and lands on the next open question, not on done.
let askRetry = step(step(load(cards.slice(0, 3)), { type: "no" }).session, { type: "ask-new", reply: "" }).session;
askRetry = step(askRetry, { type: "flush" }).session;
askRetry = step(askRetry, { type: "result", number: 1, status: 500 }).session;
askRetry = step(askRetry, { type: "later" }).session;
assert.equal(askRetry.items[askRetry.cursor].number, 1);
askRetry = step(askRetry, { type: "retry" }).session;
askRetry = step(askRetry, { type: "result", number: 1, status: 200, card: { ...cards[0], status: "todo" } }).session;
assert.equal(askRetry.phase, "queue");
assert.equal(askRetry.items[askRetry.cursor].number, 3);
assert.equal(askRetry.items[askRetry.items.length - 1].kind, "waiting");
// A recovered ask stores the time of the ask, so a re-ask that already landed still counts as new.
let lostAsk = step(step(load([advisedCard, cards[1]]), { type: "no" }).session, { type: "ask-new", reply: "" }).session;
lostAsk = step(lostAsk, { type: "flush" }).session;
const reAsked = { ...advisedCard, ask: "Again?", ask_options: ["X", "Y"], version: 5, updated_at: "2026-10-31T10:00:00Z", events: [{ kind: "decide", actor: "user:me", choice: null, text: lostAsk.items[0].pending.reply, at: "2026-10-31T09:00:00Z" }] };
const recoveredAsk = step(lostAsk, { type: "result", number: 1, status: 409, card: reAsked });
assert.equal(recoveredAsk.session.asked[1].at, "2026-10-31T09:00:00Z");
const backAgain = step(recoveredAsk.session, { type: "sync", cards: [reAsked, cards[1]] }).session;
assert.equal(backAgain.items.find((item) => item.number === 1).fresh, true);

// A swipe answers only the question it was drawn from, as shown.
const drawnAs = (item) => ({ number: item.number, question: item.question, options: JSON.stringify(item.card.ask_options) });
let held = step(load(cards.slice(0, 3)), { type: "yes" }).session;
held = step(held, { type: "flush" }).session;
const onTwo = drawnAs(held.items[1]);
assert.equal(held.items[held.cursor].number, 2);
const moved409 = step(held, { type: "result", number: 1, status: 409, card: { ...cards[0], ask: "Changed?", ask_options: ["New (advised)"], version: 9, events: [] } }).session;
assert.equal(moved409.items[moved409.cursor].number, 1);
const crossed = step(moved409, { type: "yes", ...onTwo });
assert.equal(crossed.session.items[0].kind, "open");
assert.equal(crossed.session.items[1].kind, "open");
assert.equal(step(moved409, { type: "no", ...onTwo }).session.items[0].screen, "card");
const fresh1 = load(cards.slice(0, 2));
const onOne = drawnAs(fresh1.items[0]);
assert.equal(step(fresh1, { type: "yes", ...onOne }).session.items[0].kind, "hold");
const reworded = step(fresh1, { type: "sync", cards: [{ ...cards[0], ask_options: ["Other (advised)", "Change 1"], version: 2 }, cards[1]] }).session;
assert.equal(step(reworded, { type: "yes", ...onOne }).session.items[0].kind, "open");
const bumpedOnly = step(fresh1, { type: "sync", cards: [{ ...cards[0], version: 2 }, cards[1]] }).session;
const bumpedYes = step(bumpedOnly, { type: "yes", ...onOne }).session;
assert.equal(bumpedYes.items[0].kind, "hold");
assert.equal(bumpedYes.items[0].pending.version, 2);

// Swipe rules.
const { swipeAxis, swipeResult, swipeLook, swipeAllowed, SWIPE } = swipe;
assert.equal(swipeAxis(4, 3), null);
assert.equal(swipeAxis(12, 3), "x");
assert.equal(swipeAxis(6, 12), "y");
assert.equal(swipeResult(140, 400, 0.1), "yes");
assert.equal(swipeResult(-140, 400, 0.1), "no");
assert.equal(swipeResult(130, 400, 0.2), null);
assert.equal(swipeResult(90, 400, 0.8), "yes");
assert.equal(swipeResult(70, 400, 0.8), null);
assert.equal(swipeResult(90, 400, -0.8), null);
assert.equal(swipeLook(30, 400).side, "");
assert.equal(swipeLook(50, 400).side, "yes");
assert.equal(swipeLook(-50, 400).side, "no");
assert.equal(swipeLook(140, 400).armed, true);
assert.equal(swipeLook(400, 400).tilt, SWIPE.tilt);
assert.equal(swipeLook(-400, 400).tilt, -SWIPE.tilt);
assert.equal(swipeAllowed("mouse", 1000, 0), false);
assert.equal(swipeAllowed("touch", 1000, 0), true);
assert.equal(swipeAllowed("pen", 1000, 0), true);
assert.equal(swipeAllowed("touch", 299, 300), false);
assert.equal(SWIPE.arm, 300);

// A background queue update must preserve a contact before its axis is locked.
const shellSource = await read("app.js");
const paintSource = shellSource.slice(shellSource.indexOf("function paintQueue()"), shellSource.indexOf("// The hold counts down"));
function contactFixture(armedAt = 0) {
  const listeners = new Map(), classes = new Set(), properties = new Map();
  const commits = [], timers = [];
  let settled = 0;
  const node = {
    offsetWidth: 400, dataset: {},
    classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    style: { setProperty: (name, value) => properties.set(name, value), removeProperty: (name) => properties.delete(name) },
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name) => listeners.delete(name),
    setPointerCapture: () => {},
  };
  const emit = (name, fields = {}) => listeners.get(name)?.({
    pointerId: 1, pointerType: "touch", isPrimary: true, clientX: 100, clientY: 100, timeStamp: 0, ...fields,
  });
  swipe.bindSwipe(node, null, { armedAt, onCommit: (side) => commits.push(side), onSettle: () => settled++, labels: {} });
  const nativeTimeout = globalThis.setTimeout;
  return {
    node, emit, classes, properties, commits,
    get settled() { return settled; },
    withTimers(fn) {
      globalThis.setTimeout = (callback) => { timers.push(callback); return timers.length; };
      try { fn(); while (timers.length) timers.shift()(); }
      finally { globalThis.setTimeout = nativeTimeout; }
    },
  };
}
const touching = contactFixture();
touching.emit("pointerdown");
const queueState = { queue: {}, queueDirty: false, drafts: new Map() };
let guarded = 0;
const content = {
  querySelector: (selector) => selector === ".decide-card.dragging" && touching.classes.has("dragging") ? touching.node : null,
};
Object.defineProperty(content, "innerHTML", { set() { assert.fail("Actual queue repaint replaced the card during an accepted contact"); } });
const paintActualQueue = new Function("root", "state", "queueArm", "focusMark", "renderDecide", `${paintSource}; return paintQueue;`)(
  { querySelector: () => content }, queueState, () => guarded++, () => null, () => "replacement card",
);
paintActualQueue();
assert.equal(queueState.queueDirty, true);
assert.equal(guarded, 1);
touching.emit("pointermove", { clientX: 180, timeStamp: 50 });
assert.equal(touching.node.dataset.drag, "yes");
assert.ok(Number(touching.properties.get("--stamp")) > 0);
paintActualQueue();
assert.equal(guarded, 2);
touching.withTimers(() => touching.emit("pointercancel", { clientX: 180, timeStamp: 60 }));
assert.equal(touching.classes.has("dragging"), false);
assert.equal(touching.settled, 1);
assert.deepEqual(touching.commits, []);

const scrolling = contactFixture();
scrolling.emit("pointerdown");
scrolling.emit("pointermove", { clientX: 101, clientY: 120 });
assert.equal(scrolling.classes.has("dragging"), false);
assert.equal(scrolling.settled, 1, "Vertical intent must release a deferred queue repaint");
scrolling.emit("pointerup", { clientX: 250 });
assert.deepEqual(scrolling.commits, []);
for (const ending of ["pointerup", "pointercancel"]) {
  const tap = contactFixture();
  tap.emit("pointerdown");
  tap.emit("pointermove", { clientX: 104 });
  tap.emit(ending, { clientX: 104 });
  assert.equal(tap.classes.has("dragging"), false);
  assert.equal(tap.settled, 1, "Subthreshold contact must release a deferred queue repaint");
  assert.deepEqual(tap.commits, []);
}
for (const fields of [{ pointerType: "mouse" }, { isPrimary: false }]) {
  const ignored = contactFixture();
  ignored.emit("pointerdown", fields);
  assert.equal(ignored.classes.has("dragging"), false);
}
const unarmed = contactFixture(Infinity);
unarmed.emit("pointerdown");
assert.equal(unarmed.classes.has("dragging"), false);

const css = await read("decide.css");
assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/);
assert.match(css, /min-height: 56px/);
assert.match(css, /min-height: 44px/);
assert.match(css, /180ms/);
assert.match(css, /touch-action: pan-y/);
assert.match(css, /max-height: 60dvh/);
assert.match(css, /env\(safe-area-inset-bottom\)/);
assert.match(css, /100dvh/);
assert.match(css, /prefers-reduced-motion: reduce/);
assert.match(css, /animation: none/);

// Decision context is original text, escaped, and body excerpts remain bounded.
const contextual = renderDecide(load([card(1, { status_line: 'Tests <pass> & ready', body: 'The full release context. ' + 'x'.repeat(700) })]));
assert.ok(contextual.includes('Tests &lt;pass&gt; &amp; ready'));
assert.ok(contextual.includes('The full release context.'));
assert.ok(!contextual.includes('x'.repeat(700)));
assert.ok(contextual.includes('decide-context'));

const separateContext = renderDecide(load([card(1, { status_line: 'Original progress', body: 'Original work details', review_context: 'Separate <decision> context' })]));
assert.ok(separateContext.includes('Separate &lt;decision&gt; context'));
assert.ok(separateContext.includes('Original progress') && separateContext.includes('Original work details'));
