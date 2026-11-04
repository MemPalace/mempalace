import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const { ApiError } = await import(apiUrl);
const { clarifyPrompt, clarifySetPrompt, wordDiff, parseSetReply, desktopBridge, waitingCount, rulesView, rulesHandlers } = await import(url((await read("rules.js")).replace('"./api.js"', `"${apiUrl}"`)));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const rule = (id, fields = {}) => ({ id, agent: "conn:writer", key: `rule-${id}`, text: "Always preserve exact words.", agent_text: null, kind: "instruction", scope: "global", scope_ref: null, status: "proposed", source: "agent", pending: 0, version: 3, ...fields });
const first = rule(1);
let conflictMarkup = "";
assert.equal(clarifyPrompt(first), `Rewrite this rule for an AI agent so it has exactly one reading.
Keep its meaning and its strength (must / may / never). Don't add new obligations.
Name the trigger, the action and any exception. One to three sentences.

Agent: conn:writer    Kind: instruction    Scope: global
Rule (quoted, verbatim):
"""
Always preserve exact words.
"""

Return only the improved rule text, nothing else.`);
const board = rule(2, { scope: "board", scope_ref: "work", text: "Never lose\n  whitespace.\n" });
assert.ok(clarifyPrompt(board).includes('Scope: board work\nRule (quoted, verbatim):\n"""\nNever lose\n  whitespace.\n\n"""'));
assert.ok(!clarifyPrompt({ ...first, updated_by: "private-metadata", token: "private-metadata" }).includes("private-metadata"));
const retired = rule(3, { status: "retired", text: "Retired text" });
assert.equal(clarifySetPrompt("conn:writer", [first, board, retired]), `Rewrite these rules for an AI agent so each has exactly one reading.
Keep its meaning and its strength (must / may / never). Don't add new obligations.
Name the trigger, the action and any exception. One to three sentences.
Check for rules that conflict or overlap.

Agent: conn:writer
Rules (quoted, verbatim):
"""
rule-1: Always preserve exact words.
rule-2: Never lose
  whitespace.

"""

Return only <key>: <improved text> lines, nothing else.`);
assert.deepEqual(parseSetReply("rule-1: Improved: exact words.\r\nunknown: Ignore\nrule-2: Better\nrule-3: Old\nrule-1: Latest\nrule-2:   \nNo colon", [first, board, retired]), [{ rule_id: 1, text: "Latest" }, { rule_id: 2, text: "Better" }]);
assert.deepEqual(parseSetReply("An unstructured reply", [first]), []);
assert.equal(wordDiff("one two", "one three"), "one <del>two</del><ins>three</ins>");
assert.equal(wordDiff("  one\n", "  one\n"), "  one\n");
assert.equal(wordDiff("", "<script>"), "<ins>&lt;script&gt;</ins>");
assert.equal(wordDiff('<script> "&', "safe"), "<del>&lt;script&gt; &quot;&amp;</del><ins>safe</ins>");
assert.equal(wordDiff("gone", ""), "<del>gone</del>");
assert.equal(wordDiff(null, null), "");
// Both sides of a diff must recover the original verbatim texts.
for (const [before, after] of [["a b a", "b a b"], ["a  b\nc", "a b c"], ["a", "a a"], ["b a b a", "a b a b"]]) {
  const diff = wordDiff(before, after);
  assert.equal(diff.replace(/<ins>.*?<\/ins>/gs, "").replace(/<\/?del>/g, ""), before);
  assert.equal(diff.replace(/<del>.*?<\/del>/gs, "").replace(/<\/?ins>/g, ""), after);
}
assert.equal(desktopBridge(), null);
globalThis.window = {};
assert.equal(desktopBridge(), null);
window.commonplaceDesktop = { version: 2, ask() {} };
assert.equal(desktopBridge(), null);
window.commonplaceDesktop = { version: 1 };
assert.equal(desktopBridge(), null);
const pending = rule(4, { status: "edited", pending: 1, text: "Keep mine", agent_text: "Try theirs" });
const all = rule(5, { agent: "*", status: "accepted", text: '<script> "&' });
const payload = { waiting: 3, waiting_by_agent: { "conn:writer": 3 }, rules: [first, board, retired, pending, all], suggestions: [{ id: 8, rule_id: 1, base_version: first.version, text: "A clearer rule", from_agent: "reader" }], agents: ["writer", "reader"] };
assert.equal(waitingCount(payload.rules), 3);
assert.equal(waitingCount([rule(6, { pending: 1 })]), 1);
assert.equal(waitingCount([]), 0);
const html = rulesView(payload);
const pendingFromHTML = (markup, required = true) => {
  const match = markup.match(/<p\b([^>]*\bdata-rule-pending\b[^>]*)>([^<]*)<\/p>/);
  if (!required && !match) return null;
  assert.ok(match, "Rules must render a pending-list status node");
  assert.match(match[1], /role="status"/);
  assert.equal(match[2], "Rule list refresh pending");
  return { hidden: /\bhidden\b/.test(match[1]), textContent: match[2] };
};
assert.equal(pendingFromHTML(html).hidden, true, "A freshly rendered list is current");
assert.ok(html.includes("All agents") && html.includes("conn:writer") && html.includes("conn:reader"));
assert.equal((html.match(/class="rule-tile"/g) || []).length, 3);
assert.ok(html.indexOf('data-rule-agent="conn:writer"') < html.indexOf('data-rule-agent="*"'));
assert.ok(!html.includes("Ask an agent") && html.includes("Copy prompt"));
assert.ok(!html.includes("<script>") && html.includes("&lt;script&gt;"));
assert.ok(html.includes('data-rule-action="accept_agent"') && html.includes('data-rule-action="keep_edit"'));
assert.ok(html.includes('<del>Try</del><ins>Keep</ins> <del>theirs</del><ins>mine</ins>'));
assert.ok(html.includes('data-rule-action="suggestion-accept"') && html.includes('data-rule-action="suggestion-dismiss"'));
assert.ok(html.includes('data-rule-form="new"') && html.includes('value="*">All agents'));
assert.ok(html.includes('<details class="rule-retired"><summary>Retired (1)'));
assert.ok(!html.includes('style="') && !html.includes("onclick="));
const proposedActions = html.match(/data-rule-id="1"[\s\S]*?<div class="rule-actions">([\s\S]*?)<details class="rule-clarify"/)[1];
assert.equal((proposedActions.match(/class="blockbtn"/g) || []).length, 1);
assert.ok(proposedActions.includes('data-rule-action="accept"'));
for (const action of ["edit", "retire", "history"]) assert.ok(proposedActions.includes(`class="rule-text-button" data-rule-action="${action}"`));
assert.ok(html.includes('<summary class="rule-text-button">Help me clarify</summary>'));
assert.ok(!html.includes('<span class="pill">0</span>'));
assert.ok(html.includes('<span class="pill you">3</span>'));
assert.ok(!html.includes('>New rule<') && html.includes('id="rule-new" hidden'));
const readonly = rulesView(payload, { owner: false });
for (const action of ["accept", "retire", "restore", "edit", "accept_agent", "keep_edit", "suggestion-accept", "suggestion-dismiss"]) assert.ok(!readonly.includes(`data-rule-action="${action}"`));
window.commonplaceDesktop = { version: 1, ask() {} };
assert.equal(desktopBridge(), window.commonplaceDesktop);
assert.ok(rulesView(payload).includes("Ask an agent"));
assert.ok(!rulesView(payload, { owner: false }).includes("Ask an agent"));
assert.ok(rulesView().includes("No rules"));

// Requests carry the displayed version, including when a newer payload arrives later.
const handlers = {}, errors = [], requests = [];
let refreshes = 0, fail = false;
const root = { addEventListener: (type, fn) => { handlers[type] = fn; }, querySelector: () => null };
const ui = rulesHandlers(root, async (path, options) => { requests.push([path, options]); if (fail) throw Object.assign(new Error("Stale"), { status: 409 }); return {}; }, async () => { refreshes++; }, (text) => errors.push(text));
ui.update(payload, { owner: true });
const row = { dataset: { ruleId: "1", version: "2" }, querySelectorAll: () => [] };
const control = { dataset: { ruleAction: "accept" }, closest: (selector) => selector === "[data-rule-id]" ? row : null };
const event = { target: { closest: () => control } };
await handlers.click(event);
assert.deepEqual(requests[0], ["/api/rules/1/action", { method: "POST", body: { action: "accept", version: 2 } }]);
assert.equal(refreshes, 1);
fail = true;
await handlers.click(event);
assert.equal(refreshes, 2);
assert.deepEqual(errors, ["Rule changed. Review it and try again."]);
ui.update(payload, { owner: false });
await handlers.click(event);
assert.equal(requests.length, 2);
console.log("Rules render and request checks passed");

// The header control toggles the inline form and obeys owner permissions.
let focused = false, reset = false;
const scopeInput = { required: true }, scopeField = { hidden: false };
const newPanel = { hidden: true, querySelector: (selector) => selector === "textarea" ? { focus() { focused = true; } } : selector === "form" ? { reset() { reset = true; } } : selector === "[data-scope-ref]" ? scopeField : scopeInput };
const newButton = { dataset: { ruleAction: "new" }, closest: () => null, setAttribute: (key, value) => { newButton[key] = value; }, focus() { focused = true; } };
root.querySelector = (selector) => selector === ".rule-new" ? newPanel : selector.includes('data-rule-action="new"') ? newButton : null;
const newEvent = { target: { closest: () => newButton } };
await handlers.click(newEvent);
assert.equal(newPanel.hidden, true);
ui.update(payload, { owner: true });
await handlers.click(newEvent);
assert.equal(newPanel.hidden, false);
assert.equal(newButton["aria-expanded"], "true");
assert.equal(focused, true);
const cancelButton = { dataset: { ruleAction: "cancel-new" }, closest: (selector) => selector === ".rule-new" ? newPanel : null };
await handlers.click({ target: { closest: () => cancelButton } });
assert.equal(newPanel.hidden, true);
assert.equal(newButton["aria-expanded"], "false");
assert.equal(reset, true);
assert.equal(scopeField.hidden, true);
assert.equal(scopeInput.required, false);
root.querySelector = () => null;

// All owner actions use the same versioned endpoint; suggestion decisions use their own.
fail = false;
ui.update(payload, { owner: true });
for (const action of ["retire", "restore", "accept_agent", "keep_edit"]) {
  control.dataset.ruleAction = action;
  await handlers.click(event);
  assert.deepEqual(requests.at(-1)[1].body, { action, version: 2 });
}
control.closest = (selector) => selector === "[data-rule-id]" ? row : selector === "[data-suggestion-id]" ? { dataset: { suggestionId: "8" } } : null;
for (const action of ["accept", "dismiss"]) {
  control.dataset.ruleAction = `suggestion-${action}`;
  await handlers.click(event);
  assert.deepEqual(requests.at(-1), ["/api/rules/suggestions/8", { method: "POST", body: { action, ...(action === "accept" ? { version: 2 } : {}) } }]);
}

// Bridge results are stored as proposals, and an unstructured set reply is kept in place.
const bridgeHandlers = {}, bridgePosts = [], bridgeCalls = [], bridgeErrors = [];
let bridgeRefreshes = 0, replyText = "A clearer instruction.";
window.commonplaceDesktop = {
  version: 1,
  ask: async (request) => { bridgeCalls.push(request); return { text: replyText }; },
};
const reply = { textContent: "", innerHTML: "" };
const select = { value: "reader-id", selectedOptions: [{ textContent: "reader" }] };
const panel = { dataset: { clarifyRule: "1" }, querySelectorAll: () => [], querySelector: (selector) => selector === "[data-ask-agent]" ? select : reply };
const askButton = { dataset: { ruleAction: "ask" }, closest: (selector) => selector === ".rule-clarify" ? panel : null };
const bridgeRoot = { addEventListener: (type, fn) => { bridgeHandlers[type] = fn; }, querySelector: () => null };
const bridgeUI = rulesHandlers(bridgeRoot, async (path, options) => { bridgePosts.push([path, options]); }, async () => { bridgeRefreshes++; }, (text) => bridgeErrors.push(text));
bridgeUI.update(payload);
const askEvent = { target: { closest: () => askButton } };
await bridgeHandlers.click(askEvent);
assert.deepEqual(bridgeCalls[0], { agent: "reader-id", prompt: clarifyPrompt(first), purpose: "rule-clarify" });
assert.deepEqual(bridgePosts[0], ["/api/rules/1/suggestions", { method: "POST", body: { text: replyText, from_agent: "reader", base_version: first.version } }]);
assert.equal(bridgeRefreshes, 1);
panel.dataset = { clarifyAgent: "conn:writer" };
replyText = "rule-1: Clear first rule\nrule-2: Clear second rule\nrule-3: Retired\nunknown: Ignore";
await bridgeHandlers.click(askEvent);
assert.equal(bridgePosts.length, 3);
assert.equal(bridgeRefreshes, 2);
replyText = '<script>Unstructured reply</script>';
await bridgeHandlers.click(askEvent);
assert.equal(bridgePosts.length, 3);
assert.equal(bridgeRefreshes, 2);
assert.ok(reply.innerHTML.includes("&lt;script&gt;") && !reply.innerHTML.includes("<script>"));
assert.ok(reply.innerHTML.includes('data-rule-action="copy-reply"'));
window.commonplaceDesktop.ask = async () => { throw new Error("Agent unavailable"); };
await bridgeHandlers.click(askEvent);
assert.deepEqual(bridgeErrors, ["Agent unavailable"]);
assert.equal(reply.textContent, "");
assert.equal(bridgePosts.length, 3);
console.log("Rules actions and bridge checks passed");

// Replies retain the prompt's versions while newer payloads arrive during Ask.
for (const single of [true, false]) {
  const listeners = {}, posts = [], errors = [], answer = deferred();
  const originals = [rule(1, { version: 4 }), rule(2, { version: 9 }), retired];
  const panel = {
    dataset: single ? { clarifyRule: "1" } : { clarifyAgent: "conn:writer" },
    querySelectorAll: () => [],
    querySelector: (selector) => selector === "[data-ask-agent]" ? select : { textContent: "" },
  };
  const button = { dataset: { ruleAction: "ask" }, closest: (selector) => selector === ".rule-clarify" ? panel : null };
  let refreshes = 0;
  const ui = rulesHandlers({ addEventListener: (name, fn) => { listeners[name] = fn; }, querySelector: () => null }, async (path, options) => {
    posts.push([path, options]);
    // Set posting remains sequential: the first stays stored if a later base clashes.
    if (!single && posts.length === 2) throw new ApiError(409, { error: "rule changed", current: rule(2, { version: 10 }) });
  }, async () => { refreshes++; }, (message) => errors.push(message));
  ui.update({ rules: originals });
  const calls = [];
  window.commonplaceDesktop = { version: 1, ask(request) { calls.push(request); return answer.promise; } };
  const asking = listeners.click({ target: { closest: () => button } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].prompt, single ? clarifyPrompt(originals[0]) : clarifySetPrompt("conn:writer", originals));
  assert.deepEqual(Object.keys(calls[0]).sort(), ["agent", "prompt", "purpose"]);
  assert.equal(posts.length, 0);
  ui.update({ rules: originals.map((rule) => ({ ...rule, version: rule.version + 1, text: "New wording" })) });
  const exact = '  Delayed <reply> & 日本語\n  ';
  answer.resolve({ text: single ? exact : "rule-1: First reply\nrule-2: Second reply\nrule-3: Ignore retired" });
  await asking;
  assert.deepEqual(posts, (single ? [[1, exact, 4]] : [[1, "First reply", 4], [2, "Second reply", 9]]).map(([id, text, version]) => [
    `/api/rules/${id}/suggestions`, { method: "POST", body: { text, from_agent: "reader", base_version: version } },
  ]));
  assert.equal(refreshes, 1);
  assert.deepEqual(errors, single ? [] : ["Rule changed. Review it and try again."]);
}
console.log("Rules delayed clarify snapshot checks passed");

// Submit through the real handler with live controls; no HTML reconstruction is
// allowed to lose edits typed after FormData was read or a hidden raw scope ref.
{
  const NativeFormData = globalThis.FormData;
  globalThis.FormData = class {
    constructor(form) { return Object.entries(form.controls).map(([name, control]) => [name, control.value]); }
  };
  try {
    const editor = (mode = "edit") => {
      const listeners = {}, writes = [], errors = [], controls = {}, comparison = { innerHTML: "" };
      let marker = pendingFromHTML(rulesView(payload));
      const save = { disabled: false }, cancel = { disabled: false };
      let mounted = mode === "new", markup = "", refreshes = 0, focused = false;
      for (const [name, value] of Object.entries({ text: first.text, summary: "", kind: "instruction", scope: "global", scope_ref: "" })) controls[name] = { value, focus() { focused = true; } };
      if (mode === "new") controls.agent = { value: "*" };
      const row = { dataset: { ruleId: "1", version: "3" }, querySelector: (selector) => selector === "[data-rule-body]" ? body : controls.text };
      const form = {
        dataset: { ruleForm: mode }, controls,
        closest: (selector) => selector === "[data-rule-form]" ? form : mode === "edit" && selector === "[data-rule-id]" ? row : null,
        querySelectorAll: () => [save, cancel],
        querySelector: (selector) => selector === "[data-rule-current]" ? comparison : controls.text,
      };
      const body = { set innerHTML(value) { markup = value; mounted = true; } };
      const ui = rulesHandlers({
        addEventListener: (name, fn) => { listeners[name] = fn; },
        querySelector: (selector) => selector === "[data-rule-pending]" ? marker : mounted && selector.includes(mode === "edit" ? 'form[data-rule-form="edit"]' : '.rule-new:not([hidden])') ? form : null,
      }, (path, options) => {
        const request = deferred(); writes.push({ path, options, ...request }); return request.promise;
      }, async () => { refreshes++; mounted = false; ui.update(payload); marker = pendingFromHTML(rulesView(payload)); }, (message) => errors.push(message));
      ui.update(payload);
      const click = (action) => listeners.click({ target: { closest: () => ({ dataset: { ruleAction: action }, closest: (selector) => selector === "[data-rule-id]" ? row : null }) } });
      const submit = () => listeners.submit({ target: form, preventDefault() {} });
      return { ui, form, row, controls, comparison, writes, errors, save, cancel, click, submit,
        get marker() { return marker; },
        get mounted() { return mounted; }, get markup() { return markup; }, get refreshes() { return refreshes; }, get focused() { return focused; } };
    };
    const draft = { text: '  My <draft> "& 日本語\n\t', summary: '  My <summary> &\n  ', kind: "style", scope: "repo", scope_ref: '  example.test/<draft> &  ' };
    for (const global of [false, true]) {
      const edit = editor();
      await edit.click("edit");
      assert.ok(edit.mounted && edit.focused);
      assert.ok(edit.markup.includes('data-rule-current role="status" aria-live="polite"'));
      const values = { ...draft, ...(global ? { scope: "global", scope_ref: '  hidden <ref> &  ' } : {}) };
      for (const [key, value] of Object.entries(values)) edit.controls[key].value = value;
      const submitting = edit.submit();
      assert.equal(edit.save.disabled, true);
      assert.equal(edit.writes.length, 1);
      assert.deepEqual(edit.writes[0].options.body, { ...values, scope_ref: global ? null : values.scope_ref, version: 3 });
      // Typing is still allowed while only the buttons are disabled.
      values.text += "typed while saving\n";
      values.summary += "later summary";
      values.scope_ref += "later ref";
      for (const [key, value] of Object.entries(values)) edit.controls[key].value = value;
      const current = rule(1, { version: 4, text: '<current> "& 日本語\n  ', agent: "conn:<target>", status: "edited", kind: "<kind>", scope: "repo", scope_ref: '<scope> "&', summary: '<summary> "&\n ' });
      assert.equal(edit.ui.stale, false);
      const initialMarker = edit.marker;
      edit.errors.push("Unrelated failed Rules read");
      edit.writes[0].reject(new ApiError(409, { error: "rule changed", current }));
      await submitting; // Includes mutate's finally and refreshIfStale.
      assert.equal(edit.refreshes, 0);
      assert.equal(edit.mounted, true);
      assert.equal(edit.ui.stale, true);
      assert.equal(edit.marker.hidden, false, "A real edit conflict needs a full list refresh");
      assert.equal(edit.errors.at(-1), "Unrelated failed Rules read", "Conflict must preserve the separate read error");
      assert.equal(edit.ui.force, false);
      assert.equal(edit.save.disabled, false);
      assert.equal(edit.cancel.disabled, false);
      assert.equal(edit.row.dataset.version, "4");
      assert.deepEqual(Object.fromEntries(Object.entries(edit.controls).map(([key, control]) => [key, control.value])), values);
      assert.equal(edit.writes.length, 1, "Conflict must not automatically retry or accept");
      assert.match(edit.comparison.innerHTML, /Your draft is kept.*Save again/);
      conflictMarkup = edit.comparison.innerHTML;
      for (const label of ["&lt;current&gt; &quot;&amp; 日本語\n  ", "&lt;target&gt;", "edited", "&lt;kind&gt;", "repo &lt;scope&gt; &quot;&amp;", "Summary: &lt;summary&gt; &quot;&amp;\n "]) assert.ok(edit.comparison.innerHTML.includes(label), label);
      assert.ok(!edit.comparison.innerHTML.includes("<current>") && !edit.comparison.innerHTML.includes("<target>"));
      const retry = edit.submit();
      assert.equal(edit.writes.length, 2);
      assert.equal(edit.writes[1].path, "/api/rules/1");
      assert.equal(edit.writes[1].options.method, "PATCH");
      assert.deepEqual(edit.writes[1].options.body, { ...values, scope_ref: global ? null : values.scope_ref, version: 4 });
      edit.writes[1].reject(new ApiError(409, { error: "rule changed", current: { ...current, version: 5 } }));
      await retry;
      assert.equal(edit.writes.length, 2);
      assert.equal(edit.row.dataset.version, "5");
      assert.equal(edit.controls.text.value, values.text);
      assert.equal(edit.refreshes, 0);
      if (global) {
        const save = edit.submit();
        assert.deepEqual(edit.writes[2].options.body, { ...values, scope_ref: null, version: 5 });
        edit.writes[2].resolve({});
        await save;
        assert.equal(edit.mounted, false);
      } else await edit.click("cancel-edit");
      assert.equal(edit.refreshes, 1);
      assert.notEqual(edit.marker, initialMarker, "Full render creates a fresh status node");
      assert.equal(edit.marker.hidden, true, "Save and Cancel each finish with a current list");
    }
    for (const mode of ["edit", "new"]) {
      for (const error of [
        new ApiError(409, { error: "store confirmation required", store_lost: true, current: rule(1, { version: 8 }) }),
        new ApiError(409, { error: "unrecognized conflict" }),
        new ApiError(409, { error: "wrong rule", current: rule(999, { version: 8 }) }),
        Object.assign(new Error("no payload"), { status: 409 }),
        new ApiError(500, { error: "Write failed" }),
      ]) {
        const edit = editor(mode);
        if (mode === "edit") await edit.click("edit");
        for (const [key, value] of Object.entries(draft)) edit.controls[key].value = value;
        const submitting = edit.submit();
        edit.controls.text.value += "late typing";
        edit.ui.stale = true;
        edit.writes[0].reject(error);
        await submitting;
        assert.equal(edit.errors.at(-1), error.message);
        assert.equal(edit.row.dataset.version, "3");
        assert.equal(edit.controls.text.value, draft.text + "late typing");
        for (const key of ["summary", "kind", "scope", "scope_ref"]) assert.equal(edit.controls[key].value, draft[key]);
        assert.equal(edit.mounted, true);
        assert.equal(edit.refreshes, 0);
        assert.equal(edit.ui.force, false);
        assert.equal(edit.writes.length, 1);
      }
    }
    const edit = editor();
    await edit.click("edit");
    edit.controls.text.value = draft.text;
    const submitting = edit.submit();
    edit.writes[0].reject(new ApiError(409, { error: "rule changed", current: rule(1, { version: 6, status: "retired", agent: "*", summary: null }) }));
    await submitting;
    assert.equal(edit.row.dataset.version, "6");
    assert.equal(edit.controls.text.value, draft.text);
    assert.match(edit.comparison.innerHTML, /Restore it before saving/);
    assert.ok(edit.comparison.innerHTML.includes("All agents") && edit.comparison.innerHTML.includes("retired"));
    assert.equal(edit.writes.length, 1, "Never restore or approve a retired snapshot automatically");
    assert.equal(edit.refreshes, 0);
  } finally { globalThis.FormData = NativeFormData; }
}
console.log("Rules live edit draft and conflict checks passed");

// Owner-retired proposals remain outside the collapsed retired list.
const retiredProposal = rule(9, { status: "retired", retired_by: "owner", pending: 1, agent_text: "New proposal" });
const retiredHTML = rulesView({ rules: [retired, retiredProposal], waiting: 1, waiting_by_agent: { "conn:writer": 1 } });
assert.ok(retiredHTML.indexOf('data-rule-id="9"') < retiredHTML.indexOf('<details class="rule-retired">'));
assert.ok(retiredHTML.indexOf('data-rule-id="3"') > retiredHTML.indexOf('<details class="rule-retired">'));
assert.ok(retiredHTML.includes('data-waiting="1"'));
assert.equal(waitingCount([retired, retiredProposal]), 1);
assert.equal(waitingCount([rule(10, { status: "retired", retired_by: "agent", pending: 1 })]), 0);
// Counts and sorting are supplied by the server, independent of rule text.
const serverCounts = rulesView({ ...payload, waiting: 8, waiting_by_agent: { "*": 8 } });
assert.ok(serverCounts.includes('data-waiting="8"'));
assert.ok(serverCounts.indexOf('data-rule-agent="*"') < serverCounts.indexOf('data-rule-agent="conn:writer"'));
assert.ok(!readonly.includes("New rule") && readonly.includes("Copy prompt"));

// Suggestion conflicts use the same refresh-and-review path as rule writes.
control.dataset.ruleAction = "suggestion-accept";
fail = true;
const previousRefreshes = refreshes;
await handlers.click(event);
assert.equal(refreshes, previousRefreshes + 1);
assert.equal(errors.at(-1), "Rule changed. Review it and try again.");

// Exercise the shell refresh itself: other screens request only the count;
// rule events update the badge immediately and defer the screen while busy.
const shellSource = await read("app.js");
const refreshSource = shellSource.slice(shellSource.indexOf("async function refresh("), shellSource.indexOf("async function route()"));
const shellState = { me: {}, screen: 1, refreshRequest: 0, base: "#/rules", route: "#/rules" };
const shellCalls = [], updates = [], badgeCounts = [], shellErrors = [], shellErrorCalls = [];
let shellMarker = null, shellHTML = "";
const content = {
  get innerHTML() { return shellHTML; },
  set innerHTML(value) { shellHTML = value; shellMarker = pendingFromHTML(value, false); },
  children: [{}],
};
let shellPayload = { ...payload, can_edit: false };
const noWork = () => {};
let headerActions = "", shellBusy = true, shellRefresh, shellWrite = async () => {};
const shellReads = [];
const queueShellRead = () => {
  const read = { ...deferred(), requested: deferred() };
  shellReads.push(read);
  return read;
};
const shellHandlers = {};
const shellUI = rulesHandlers({
  addEventListener: (type, fn) => { shellHandlers[type] = fn; },
  querySelector: (selector) => selector === "[data-rule-pending]" ? shellMarker : shellBusy && (shellBusy === true || selector.includes(shellBusy)) ? {} : null,
}, (...args) => shellWrite(...args), () => shellRefresh(), (error) => shellErrors.push(error));
const updateShellUI = shellUI.update;
shellUI.update = (...args) => { updates.push(args); updateShellUI(...args); };
const shellDeps = {
  state: shellState,
  api: async (path) => {
    shellCalls.push(path);
    if (path === "/api/rules") {
      const read = shellReads.shift();
      if (read) { read.requested.resolve(); return read.promise; }
      return shellPayload;
    }
    if (path === "/api/rules/count") return { waiting: 7, seq: 12 };
    return { boards: [] };
  },
  root: { querySelector: (selector) => selector === "#content" ? content : selector === "[data-rule-pending]" ? shellMarker : null },
  rulesUI: shellUI,
  rulesView,
  navigation: () => badgeCounts.push(shellState.rulesWaiting),
  title: (_text, actions = "") => { headerActions = actions; }, remember: noWork, capture: () => noWork, restore: noWork,
  boardsView: noWork, showError: (error) => { shellErrorCalls.push(error); if (error) shellErrors.push(error); },
};
shellRefresh = new Function(...Object.keys(shellDeps), `${refreshSource}; return refresh;`)(...Object.values(shellDeps));
assert.equal(await shellRefresh(), true);
assert.equal(shellMarker.hidden, true);
assert.deepEqual(updates.at(-1)[1], { owner: false });
assert.ok(!content.innerHTML.includes("New rule") && !content.innerHTML.includes("Ask an agent"));
assert.ok(content.innerHTML.includes("Copy prompt"));
assert.equal(headerActions, "");
assert.ok(shellCalls.includes("/api/rules") && !shellCalls.includes("/api/rules/count"));
shellPayload = { ...shellPayload, can_edit: true, waiting: 4 };
const busyHTML = content.innerHTML;
for (const kind of ["rule.changed", "rule.suggestion"]) {
  assert.equal(await shellRefresh({ background: true, events: [{ kind }] }), true);
  assert.deepEqual(updates.at(-1)[1], { owner: false });
  assert.equal(content.innerHTML, busyHTML);
  assert.equal(shellDeps.rulesUI.stale, true);
  assert.equal(shellMarker.hidden, false, "The actual background refresh must disclose its deferred list");
  assert.equal(badgeCounts.at(-1), 4);
}
shellBusy = false;
await shellHandlers.toggle({ target: { matches: () => true, open: false } });
assert.deepEqual(updates.at(-1)[1], { owner: true });
assert.equal(shellDeps.rulesUI.stale, false);
assert.equal(shellMarker.hidden, true);
assert.notEqual(content.innerHTML, busyHTML);
assert.ok(headerActions.includes('data-rule-action="new"'));
// A finished write renders even when a background refresh overtakes its own
// refresh while the edit form is still open; the force is used up by that render.
shellBusy = true;
shellPayload = { ...shellPayload, waiting: 2 };
const beforeRace = updates.length;
shellUI.force = true;
const own = shellRefresh();
const overtaking = shellRefresh({ background: true, events: [{ kind: "rule.changed" }] });
await Promise.all([own, overtaking]);
assert.equal(updates.length, beforeRace + 1);
assert.equal(shellUI.force, false);
assert.equal(shellUI.stale, false);
assert.equal(shellMarker.hidden, true, "An overtaking successful forced read recreates a hidden marker");
const afterRace = content.innerHTML;
shellPayload = { ...shellPayload, waiting: 3 };
assert.equal(await shellRefresh({ background: true, events: [{ kind: "rule.changed" }] }), true);
assert.equal(updates.length, beforeRace + 1);
assert.equal(content.innerHTML, afterRace);
assert.equal(shellUI.stale, true);
shellBusy = false;
shellState.base = shellState.route = "#/boards";
shellCalls.length = 0;
assert.equal(await shellRefresh({ background: true, events: [{ kind: "rule.changed" }] }), true);
assert.ok(shellCalls.includes("/api/rules/count") && !shellCalls.includes("/api/rules"));
assert.equal(badgeCounts.at(-1), 7);
assert.deepEqual(shellErrors, []);
console.log("Rules permissions, retirement, counts and shell refresh checks passed");

// Retry reads current rules without replacing a mounted edit or new-rule form.
// Run the actual shell click listener, including its call to the real refresh.
const shellClickSource = shellSource.slice(shellSource.indexOf('root.addEventListener("click",'), shellSource.indexOf('root.addEventListener("submit",'));
let retryClick;
const retryOptions = [];
new Function("root", "state", "rulesUI", "refresh", "showError", "consoleUI", shellClickSource)(
  { addEventListener: (_name, listener) => { retryClick = listener; } }, shellState, shellUI,
  (options) => { retryOptions.push(options); return shellRefresh(options); },
  (error) => { throw new Error(error); }, null,
);
const clickRetry = () => retryClick({ target: { closest: () => ({ dataset: { action: "retry" } }) } });
shellState.base = shellState.route = "#/rules";
for (const busy of ['form[data-rule-form="edit"]', '.rule-new:not([hidden])']) {
  shellUI.update(shellPayload, { owner: true });
  shellBusy = busy;
  content.innerHTML = `<form><div data-rule-current role="status">${conflictMarkup}</div></form>`;
  const before = updates.length, reads = shellCalls.length, html = content.innerHTML;
  await clickRetry();
  assert.ok(shellCalls.length > reads, "Retry must fetch current state");
  assert.equal(updates.length, before, "Retry must preserve a busy rule form");
  assert.equal(content.innerHTML, html);
  assert.match(content.innerHTML, /Your draft is kept.*Save again/);
  assert.equal(shellErrorCalls.at(-1), "", "Clearing the shell banner must not clear inline status");
  await shellRefresh({ background: true, events: [{ kind: "rule.changed" }] });
  assert.match(content.innerHTML, /Your draft is kept.*Save again/);
  assert.equal(shellUI.stale, true);
  shellBusy = false;
  await clickRetry();
  assert.equal(updates.length, before + 1, "A second Retry on idle Rules must render current state");
  assert.equal(shellUI.stale, false);
}
shellState.base = shellState.route = "#/boards";
await clickRetry();
assert.equal(retryOptions.at(-1)?.background, false, "Other screens keep foreground Retry");
console.log("Rules shell Retry preserves busy forms and refreshes idle screens");

// A successful write or generic action conflict can outlive a failed refresh.
// Once that CURRENT refresh fails, later background reads must respect forms.
shellState.base = shellState.route = "#/rules";
const shellRow = { dataset: { ruleId: "1", version: "3" }, querySelectorAll: () => [] };
const shellClick = () => shellHandlers.click({ target: { closest: () => ({ dataset: { ruleAction: "accept" }, closest: (selector) => selector === "[data-rule-id]" ? shellRow : null }) } });
for (const writeConflict of [false, true]) {
  for (const busy of ['form[data-rule-form="edit"]', '.rule-new:not([hidden])']) {
    shellUI.update(shellPayload, { owner: true });
    shellBusy = busy;
    shellWrite = async () => { if (writeConflict) throw new ApiError(409, { error: "rule changed", current: first }); };
    const read = queueShellRead(), before = updates.length, html = content.innerHTML;
    const writing = shellClick();
    await read.requested.promise;
    assert.equal(shellUI.force, true);
    read.reject(new Error("Refresh unavailable"));
    await writing;
    assert.equal(shellUI.force, false);
    assert.equal(updates.length, before);
    assert.ok(shellErrors.includes("Refresh unavailable"));
    if (writeConflict) assert.equal(shellErrors.at(-1), "Rule changed. Review it and try again.");
    await shellRefresh({ background: true, events: [{ kind: "rule.changed" }] });
    assert.equal(updates.length, before);
    assert.equal(content.innerHTML, html);
    assert.equal(shellUI.stale, true);
    shellBusy = false;
    await shellHandlers.toggle({ target: { matches: () => true, open: false } });
    assert.equal(updates.length, before + 1);
    assert.equal(shellUI.stale, false);
  }
}

// An acknowledged Save updates the retained editor's version even if its GET
// fails. Late typing stays intact and another explicit Save uses that version.
{
  const NativeFormData = globalThis.FormData;
  globalThis.FormData = class {
    constructor(form) { return Object.entries(form.controls).map(([name, control]) => [name, control.value]); }
  };
  try {
    shellUI.update(shellPayload, { owner: true });
    shellBusy = 'form[data-rule-form="edit"]';
    const row = { dataset: { ruleId: "1", version: "3" } }, comparison = { innerHTML: conflictMarkup };
    const controls = { text: { value: "Submitted exact text" }, summary: { value: "Exact summary" }, scope: { value: "global" } };
    const form = { dataset: { ruleForm: "edit" }, controls, querySelectorAll: () => [], querySelector: () => comparison,
      closest: (selector) => selector === "[data-rule-id]" ? row : form };
    const writes = [], firstWrite = deferred(), saved = rule(1, { version: 4, text: controls.text.value });
    shellWrite = (path, options) => { writes.push([path, options]); return writes.length === 1 ? firstWrite.promise : Promise.resolve({ ...saved, version: 5 }); };
    const read = queueShellRead();
    const submit = () => shellHandlers.submit({ target: { closest: () => form }, preventDefault() {} });
    const writing = submit();
    controls.text.value += " with late café  typing";
    firstWrite.resolve(saved);
    await read.requested.promise;
    assert.equal(row.dataset.version, "4");
    assert.equal(comparison.innerHTML, "", "Acknowledged Save clears the old conflict explanation");
    read.reject(new Error("Refresh unavailable after Save"));
    await writing;
    assert.equal(shellUI.force, false);
    assert.equal(row.dataset.version, "4");
    assert.equal(controls.text.value, "Submitted exact text with late café  typing");
    await clickRetry();
    assert.equal(writes.length, 1, "Read retry must not repeat the write");
    await submit();
    assert.equal(writes[1][1].body.version, 4);
    assert.equal(writes[1][1].body.text, controls.text.value);
    assert.equal(row.dataset.version, "5");
    for (const invalid of [{}, { ...saved, id: 999 }, { ...saved, version: "8" }]) {
      shellUI.update(shellPayload, { owner: true });
      row.dataset.version = "3";
      shellWrite = async () => invalid;
      const read = queueShellRead(), writing = submit();
      await read.requested.promise;
      read.reject(new Error("Retain malformed-response form"));
      await writing;
      assert.equal(row.dataset.version, "3", "Never adopt a malformed or mismatched response version");
    }
  } finally { globalThis.FormData = NativeFormData; }
}
console.log("Rules acknowledged versions survive failed refresh without losing late typing");

// Mutate's obsolete own refresh can finish or fail before the winning one.
// Its finally must leave force intact until that winning request consumes it.
shellWrite = async () => {};
for (const obsoleteFails of [false, true]) {
  for (const winnerFails of [false, true]) {
    shellUI.update(shellPayload, { owner: true });
    shellBusy = 'form[data-rule-form="edit"]';
    const old = queueShellRead(), before = updates.length, errorCount = shellErrors.length;
    const writing = shellClick();
    await old.requested.promise;
    const winner = queueShellRead();
    const overtaking = shellRefresh({ background: true, events: [{ kind: "rule.changed" }] });
    await winner.requested.promise;
    if (obsoleteFails) old.reject(new Error("Obsolete failure"));
    else old.resolve(shellPayload);
    await writing;
    assert.equal(shellUI.force, true);
    assert.equal(updates.length, before);
    assert.equal(shellErrors.length, errorCount, "An obsolete failure must not surface");
    if (winnerFails) winner.reject(new Error("Current failure"));
    else winner.resolve(shellPayload);
    assert.equal(await overtaking, !winnerFails);
    assert.equal(shellUI.force, false);
    assert.equal(updates.length, before + (winnerFails ? 0 : 1));
    const rendered = content.innerHTML;
    await shellRefresh({ background: true, events: [{ kind: "rule.suggestion" }] });
    assert.equal(content.innerHTML, rendered);
    assert.equal(shellUI.stale, true);
  }
}
// A failure from a screen we have left is also obsolete.
shellUI.update(shellPayload, { owner: true });
shellUI.force = true;
const leftRead = queueShellRead(), leftRefresh = shellRefresh();
await leftRead.requested.promise;
const leftErrors = shellErrors.length;
shellState.screen++;
shellState.base = shellState.route = "#/boards";
leftRead.reject(new Error("Previous screen failure"));
await leftRefresh;
assert.equal(shellUI.force, true);
assert.equal(shellErrors.length, leftErrors);
shellState.base = shellState.route = "#/rules";
shellUI.update(shellPayload, { owner: true });
// Store loss is not a generic action version conflict and never auto-accepts.
shellWrite = async () => { throw new ApiError(409, { error: "Confirm the store", store_lost: true }); };
const beforeStoreLoss = shellCalls.length;
await shellClick();
assert.equal(shellCalls.length, beforeStoreLoss);
assert.equal(shellUI.force, false);
assert.equal(shellErrors.at(-1), "Confirm the store");
assert.equal(shellReads.length, 0);
console.log("Rules failed refresh and controlled overtaking checks passed");


// Outdated and retired suggestions stay visible with Dismiss as their only action.
for (const [current, baseVersion, outdated] of [
  [first, first.version, false],
  [first, first.version - 1, true],
  [retired, retired.version, false],
  [retired, retired.version - 1, true],
  [retiredProposal, retiredProposal.version, false],
]) {
  const view = rulesView({ rules: [current], suggestions: [
    { id: 20, rule_id: current.id, base_version: baseVersion, text: "Suggestion", from_agent: "reader", status: "open" },
    { id: 21, rule_id: current.id, base_version: baseVersion, text: "Dismissed", from_agent: "reader", status: "dismissed" },
  ] });
  const proposal = view.match(/<section class="rule-proposal" data-suggestion-id="20">([\s\S]*?)<\/section>/)[1];
  assert.equal(proposal.includes('<span class="pill">outdated</span>'), outdated);
  assert.equal(proposal.includes('data-rule-action="suggestion-accept"'), !outdated && current.status !== "retired");
  assert.ok(proposal.includes('data-rule-action="suggestion-dismiss"'));
  assert.ok(!view.includes('data-suggestion-id="21"'));
}

// Each busy-state exit releases a deferred render, including a failed mutation.
const exitHandlers = {};
let exitBusy = false, exitRefreshes = 0;
const exitTrigger = { setAttribute() {}, focus() {} };
const exitPanel = { hidden: false, querySelector: (selector) => selector === "form" ? { reset() {} } : {} };
const exitHistory = { hidden: false };
const exitRow = { dataset: { ruleId: "1", version: "3" }, querySelector: () => exitHistory, querySelectorAll: () => [] };
const exitRoot = {
  addEventListener: (type, fn) => { exitHandlers[type] = fn; },
  querySelector: (selector) => selector === '[data-rule-action="new"]' ? exitTrigger : exitBusy ? {} : null,
};
let exitUI;
exitUI = rulesHandlers(exitRoot, async () => { throw new Error("Write failed"); }, async () => { exitRefreshes++; exitUI.update(payload); }, () => {});
exitUI.update(payload);
const exitClick = async (action) => {
  const button = { dataset: { ruleAction: action }, closest: (selector) => selector === ".rule-new" ? exitPanel : selector === "[data-rule-id]" ? exitRow : null };
  await exitHandlers.click({ target: { closest: () => button } });
};
for (const action of ["cancel-new", "cancel-edit", "history", "retire"]) {
  exitUI.stale = true;
  const before = exitRefreshes;
  await exitClick(action);
  assert.equal(exitRefreshes, before + 1, action);
  assert.equal(exitUI.stale, false);
}
for (const open of [true, false]) {
  exitUI.stale = true;
  const before = exitRefreshes;
  await exitHandlers.toggle({ target: { matches: () => true, open } });
  assert.equal(exitRefreshes, before + (open ? 0 : 1));
}
exitUI.stale = true;
exitBusy = true;
await exitHandlers.toggle({ target: { matches: () => true, open: false } });
assert.equal(exitUI.stale, true);
exitBusy = false;
await exitHandlers.toggle({ target: { matches: () => true, open: false } });
assert.equal(exitUI.stale, false);
for (const selector of ['.rule-clarify[open] [data-ask-agent]', '.rule-clarify[open] [data-reply-text]']) {
  exitRoot.querySelector = (query) => query.includes(selector) ? {} : null;
  assert.equal(exitUI.busy(), true);
}
console.log("Rules suggestion and deferred-render checks passed");

// Owners move active rules between agents; proposals can be accepted for all agents at once.
{
  const moveHTML = rulesView(payload);
  const row = (id) => moveHTML.match(new RegExp(`data-rule-id="${id}"[\\s\\S]*?<div data-rule-history hidden></div></article>`))[0];
  const targets = (id) => [...row(id).matchAll(/data-rule-action="move" data-agent="([^"]+)">([^<]+)</g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(targets(1), [["*", "All agents"], ["conn:reader", "reader"]]);
  assert.deepEqual(targets(5), [["conn:reader", "reader"], ["conn:writer", "writer"]]);
  assert.ok(row(1).includes('<details class="rule-move"><summary class="rule-text-button">Applies to…</summary>'));
  assert.ok(row(1).includes('class="rule-text-button" data-rule-action="accept-all" >Accept for all agents<'));
  for (const id of [4, 5]) assert.ok(!row(id).includes('data-rule-action="accept-all"'));
  assert.ok(!row(3).includes("rule-move"));
  assert.ok(!rulesView(payload, { owner: false }).includes("rule-move") && !rulesView(payload, { owner: false }).includes("accept-all"));
  assert.ok(!rulesView({ rules: [all] }).includes("rule-move"));

  const sent = [];
  let moveRefreshes = 0;
  const moveHandlers = {};
  const moveUI = rulesHandlers({ addEventListener: (type, fn) => { moveHandlers[type] = fn; }, querySelector: () => null }, async (path, options) => { sent.push([path, options]); return {}; }, async () => { moveRefreshes++; }, () => {});
  moveUI.update(payload, { owner: true });
  const moveRow = { dataset: { ruleId: "1", version: "3" }, querySelectorAll: () => [] };
  const click = (dataset) => moveHandlers.click({ target: { closest: () => ({ dataset, closest: (selector) => selector === "[data-rule-id]" ? moveRow : null }) } });
  await click({ ruleAction: "move", agent: "*" });
  await click({ ruleAction: "accept-all" });
  assert.deepEqual(sent, [
    ["/api/rules/1/agent", { method: "POST", body: { agent: "*", version: 3 } }],
    ["/api/rules/1/agent", { method: "POST", body: { agent: "*", version: 3, accept: true } }],
  ]);
  assert.equal(moveRefreshes, 2);
  moveUI.update(payload, { owner: false });
  await click({ ruleAction: "move", agent: "*" });
  assert.equal(sent.length, 2);
}
// Pending wording on a moved rule names the agent that wrote it.
{
  const shared = rule(11, { agent: "*", status: "accepted", pending: 1, agent_text: "Theirs", agent_text_by: "conn:writer", updated_by: "user:owner" });
  assert.ok(rulesView({ rules: [shared] }).includes("<h4>writer’s wording</h4>"));
  assert.ok(rulesView({ rules: [{ ...shared, agent_text_by: null }] }).includes("<h4>Agent’s wording</h4>"));
  assert.ok(rulesView({ rules: [{ ...shared, agent: "conn:writer" }] }).includes("<h4>Agent’s wording</h4>"));
  assert.ok(rulesView({ rules: [{ ...shared, agent_text_by: "conn:<b>" }] }).includes("<h4>&lt;b&gt;’s wording</h4>"));
}
console.log("Rules agent-target checks passed");

// Summaries are escaped, full text is initially hidden, and expansion is local/read-only.
{
  const text = "Keep the entire original. Preserve all context. Preserve the last line.";
  const summarized = rule(42, { text, summary: "Keep <every> word. Preserve context.", display_summary: "Keep <every> word. Preserve context." });
  const view = rulesView({ rules: [summarized] }, { owner: false });
  assert.ok(view.includes('type="button" class="rule-summary" data-rule-action="expand" aria-expanded="false" aria-controls="rule-full-42"'));
  assert.ok(view.includes('<span>Keep &lt;every&gt; word. Preserve context.</span>'));
  assert.ok(view.includes(`<div id="rule-full-42" data-rule-full hidden><p class="rule-text">${text}</p>`));
  assert.ok(rulesView({ rules: [summarized] }).includes('name="summary" maxlength="400"'));
  const listeners = {}, full = { hidden: true }, icon = { textContent: "+" }, attrs = {};
  const ruleRow = { dataset: { ruleId: "42" }, querySelector: () => full };
  const toggle = { dataset: { ruleAction: "expand" }, closest: (selector) => selector === "[data-rule-id]" ? ruleRow : null,
    setAttribute: (key, value) => { attrs[key] = value; }, querySelector: () => icon };
  let mutations = 0;
  const root = { addEventListener: (name, fn) => { listeners[name] = fn; }, querySelector: (selector) => selector.includes("data-rule-full") && !full.hidden ? full : null };
  const disclosure = rulesHandlers(root, () => { mutations++; }, async () => {}, assert.fail);
  disclosure.update({ rules: [summarized] }, { owner: false });
  const click = { target: { closest: () => toggle } };
  await listeners.click(click);
  assert.equal(full.hidden, false);
  assert.equal(attrs["aria-expanded"], "true");
  assert.equal(icon.textContent, "−");
  assert.equal(disclosure.busy(), true, "Background refresh must preserve an open rule");
  await listeners.click(click);
  assert.equal(full.hidden, true);
  assert.equal(attrs["aria-expanded"], "false");
  assert.equal(disclosure.busy(), false);
  assert.equal(mutations, 0, "All readers may expand without a rule mutation");
}
console.log("Rules summary disclosure checks passed");
