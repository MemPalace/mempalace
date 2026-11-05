import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const decideUrl = url((await read("decide.js")).replace('"./api.js"', `"${apiUrl}"`));
const { homeView, needItems } = await import(url((await read("home.js")).replace('"./api.js"', `"${apiUrl}"`).replace('"./decide.js"', `"${decideUrl}"`)));
const rows = {
  dot: (card) => `<i class="status-dot ${card.need}" data-need="${card.need}"></i>`,
  taskRow: (card, context) => `<div class="trow">#${card.number}${context}</div>`,
};
const at = new Date(Date.now() - 3 * 3600000).toISOString();
const card = (number, fields) => ({ number, version: 3, board: "work", title: `Card ${number}`, status: "doing", need: "working", updated_at: at, ...fields });
const ask = card(1, { title: "Ship <script>alert(1)</script>?", status: "review", need: "you", ask: "Which one?\nSecond line stays in the card.", ask_options: ["A", "B"], why: "Worker asks you: Which one?", actions: ["answer"] });
const plan = card(2, { status: "review", need: "plan", why: "Worker asks you: Approve <this> plan?", actions: ["answer"] });
const merge = card(3, { status: "review", need: "merge", ask: "Ship?", ask_options: ["Merge", "Wait"], branch: "feat/x", pr_number: 19, pr_url: "https://example.test/o/r/pull/19", why: "Worker asks you to merge PR #19 (checks passed on aaaaaaa): Ship?", actions: ["answer", "open_pr"] });
const stale = card(4, { board: "lab", need: "idle", why: "Worker has a claim that ran out, and it has no available supervisor.", actions: ["requeue", "reassign", "park"] });
const working = card(5, { board: "lab", need: "working" });
const backlog = card(6, { status: "backlog", need: "idle", why: "asks: this prefix is prose, not routing.", actions: ["approve", "reject"] });
const quiet = card(7, { board: "quiet", status: "todo", need: "idle" });
const needs = { asks: [plan, ask, merge], done_review: [], backlog_from_agents: [backlog], ready_to_merge: [merge], stale: [stale], count: 5 };
const boards = [
  { slug: "work", name: "Work <b>", counts: { todo: 0, doing: 1, review: 2 } },
  { slug: "lab", name: "Lab", counts: { doing: 2 } },
  { slug: "quiet", name: "Quiet", counts: { todo: 1 } },
];
const html = homeView({ needs, boards, open: [ask, plan, merge, stale, working, quiet], total: 240, selected: 1, speak: true }, rows);

// Three parts, top to bottom: the box, one tile per space, the task table.
const at_ = (s) => html.indexOf(s);
assert.ok(at_('class="needbox waiting"') >= 0 && at_('class="needbox') < at_('class="home-spaces"') && at_('class="home-spaces"') < at_('class="home-tasks"'));
assert.match(html, /<h2 id="needbox-title"><b class="n">5<\/b> things need you<\/h2><div class="need-actions"><a class="opt go answer" href="#\/decide">Answer<\/a><button class="opt speak" data-action="speak-digest"/);
assert.ok(html.includes('id="digest-captions"'));

// The server's sentence is the whole reason; routing comes from buckets, not prose.
assert.deepEqual(needItems(needs).map((i) => [i.card.number, i.reason]), [
  [1, ask.why], [2, plan.why], [3, merge.why], [4, stale.why], [6, backlog.why],
]);
assert.ok(html.includes('<a class="tt" href="#/decide/1">Ship &lt;script&gt;alert(1)&lt;/script&gt;?</a><span class="why">Worker asks you: Which one?</span>'));
assert.ok(html.includes('<a class="tt" href="#/decide/2">'));
assert.ok(html.includes('<a class="tt" href="#/card/6">'));
assert.ok(html.includes('<span class="why">Worker asks you: Approve &lt;this&gt; plan?</span>'));
assert.ok(!html.includes("<script>") && !html.includes("Second line"));
assert.ok(html.includes('class="nline sel" data-row="1"'));
// The ask is answered in its card, its one clickable place; Home never repeats the choices.
assert.ok(!html.includes('data-action="decide"') && !html.includes("data-form=\"reply\""));
const line = (number) => html.slice(html.indexOf(`data-key="${number}"`), html.indexOf("</li>", html.indexOf(`data-key="${number}"`)));
for (const [number, actions] of [[4, ["requeue", "reassign", "park"]], [6, ["approve", "reject"]]]) {
  for (const action of actions) assert.ok(line(number).includes(`data-action="need-${action}" data-card="${number}" data-version="3"`));
}
assert.match(line(1), /class="opt" href="#\/decide\/1">Answer<\/a>/);
assert.match(line(3), /class="opt" href="#\/decide\/3">Answer<\/a>/);
assert.match(line(3), /href="https:\/\/example.test\/o\/r\/pull\/19" target="_blank" rel="noopener noreferrer">Open PR<\/a>/);
assert.doesNotMatch(line(1), /need-(?:approve|reject|park|accept)/);

// Spaces: the worst state among open cards, else quiet, two facts, one Open.
const tile = (slug) => html.slice(at_(`data-key="space-${slug}"`), html.indexOf("</article>", at_(`data-key="space-${slug}"`)));
assert.match(tile("work"), /<h3>Work &lt;b&gt;<\/h3><a class="opt" href="#\/board\/work">Open<\/a>/);
assert.match(tile("work"), /class="pill you">.*Needs you<\/span><span>3 open<\/span><span><span class="hot">4 waiting<\/span>/);
assert.match(tile("lab"), /class="pill stale">.*Stuck<\/span><span>2 open<\/span><span><span class="hot">1 waiting/);
assert.match(tile("quiet"), /class="pill quiet"><i class="status-dot idle".*Quiet<\/span><span>1 open<\/span><span>0 doing<\/span>/);
assert.equal(tile("quiet").match(/<a /g).length, 1);

// Table: attention first, branch and PR in the title cell, an action only where needed.
const table = html.slice(at_("<tbody>"), at_("</tbody>"));
assert.deepEqual([...table.matchAll(/data-key="task-(\d+)"/g)].map((m) => Number(m[1])), [1, 2, 3, 4, 5, 7]);
const row = (n) => table.slice(table.indexOf(`data-key="task-${n}"`), table.indexOf("</tr>", table.indexOf(`data-key="task-${n}"`)));
assert.match(row(1), /<a class="opt" href="#\/card\/1">Answer<\/a>/);
assert.match(row(2), /<a class="opt" href="#\/card\/2">Answer<\/a>/);
assert.match(row(3), /<span class="sub"><span>feat\/x<\/span><span>PR #19<\/span><\/span>/);
assert.match(row(3), /<a class="opt" href="#\/card\/3">Answer<\/a>/);
assert.doesNotMatch(html, /Retake/);
assert.match(row(4), /<a class="opt" href="#\/card\/4">Unblock<\/a>/);
assert.match(row(4), />Lab<\/a>/);
assert.match(row(4), /<time data-elapsed="[^"]+" datetime="[^"]+">3h<\/time>/);
for (const n of [5, 7]) assert.doesNotMatch(row(n), /class="opt"/);
// Rows select like the box lines; the header counts every open task, not just those sent.
assert.ok(table.includes('<tr data-row="1" data-key="task-1" class="sel">') && table.includes('<tr data-row="2" data-key="task-2" class="">'));
assert.ok(html.includes('Open tasks <span class="n">240</span>'));
// Phone: the same rows through the shared task-row component.
assert.equal((html.match(/class="trow-card[ "]/g) || []).length, 6);
assert.ok(html.includes('<article class="trow-card sel" data-row="1" data-key="stack-1"><div class="trow">#1<a href="#/board/work">Work &lt;b&gt;</a></div><div class="opts"><a class="opt" href="#/card/1">Answer</a></div></article>'));

const empty = homeView({ needs: { count: 0 }, boards: [], open: [], speak: false }, rows);
assert.ok(empty.includes('<section class="needbox " aria-labelledby="needbox-title"><header><h2 id="needbox-title">Nothing needs you</h2></header>'));
assert.ok(empty.includes("No open tasks") && !empty.includes("<table>") && !empty.includes("speak-digest") && !empty.includes('href="#/decide"'));
const mergeOnly = homeView({ needs: { asks: [merge], ready_to_merge: [merge], count: 1 }, boards, open: [merge], speak: true }, rows);
assert.ok(mergeOnly.includes("speak-digest") && mergeOnly.includes('href="#/decide"'));
assert.ok(mergeOnly.includes('<b class="n">1</b> thing needs you'));
const one = homeView({ needs: { asks: [ask] }, boards, open: [ask] }, rows);
assert.ok(one.includes("<b class=\"n\">1</b> thing needs you"));

// Completion review stays in the card even when its prose resembles an ask.
const done = card(8, { status: "review", need: "you", why: "asks: accept this completed work.", actions: ["accept", "answer"] });
const completion = homeView({ needs: { done_review: [done] } }, rows);
assert.ok(completion.includes('<a class="tt" href="#/card/8">'));
assert.ok(completion.includes('data-action="need-accept" data-card="8" data-version="3"'));
assert.match(completion, /class="opt" href="#\/card\/8">Answer<\/a>/);
const readOnly = homeView({ needs: { stale: [{ ...stale, actions: [] }] } }, rows);
assert.doesNotMatch(readOnly, /data-action="need-/);

// Older servers omit why/actions; keep their reason and safe card actions.
const legacyAsk = { ...ask }, legacyStale = { ...stale };
delete legacyAsk.why; delete legacyAsk.actions;
delete legacyStale.why; delete legacyStale.actions;
assert.deepEqual(needItems({ asks: [legacyAsk], stale: [legacyStale] }).map((i) => i.reason), ["asks: Which one?", "stopped checking in and has no available supervisor"]);
const legacy = homeView({ needs: { asks: [legacyAsk], stale: [legacyStale] }, open: [legacyStale] }, rows);
assert.ok(legacy.includes('href="#/decide/1"') && legacy.includes('href="#/card/4">Unblock'));
assert.doesNotMatch(legacy, /Retake|data-action="need-/);
const notReady = homeView({ needs: { ready_to_merge: [card(9, { need: "working" })] }, open: [card(9, { need: "working" })] }, rows);
assert.doesNotMatch(notReady, /[Rr]eady to merge/);

// Legacy or number-only PR metadata still opens the full repository PR URL.
for (const fields of [
  { repo: "example/project", pr_number: 42 },
  { repo: "github.com/example/project", pr_number: 42, pr_url: "https://example/" },
  { repo: "https://github.com/example/project.git", pr_number: 42 },
]) {
  const rendered = homeView({ open: [card(99, { ...fields, need: "merge" })] }, rows);
  assert.match(rendered, /href="https:\/\/github.com\/example\/project\/pull\/42"[^>]*>Merge<\/a>/);
}
