import assert from 'node:assert/strict';
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const { reviewerRows, reviewerChange, DEFAULT_WORDS } = await load('card-reviewer.js');
const { renderDetails } = await load('details.js');
const { cardTags } = await load('board.js');
const people = { 'user:owner': 'Olive', 'user:ada': 'Ada <b>' };
const author = (ref) => people[ref] || ref;
const card = { number: 7, board: 'atlas', version: 4, title: 'Offline queue', body: '', status: 'doing', events: [] };

const cases = {
  default_words() {
    const html = reviewerRows({ ...card, reviewer: null, can_set_reviewer: true }, { people, author });
    assert.ok(html.includes(DEFAULT_WORDS) && html.includes('id="card-reviewer"') && html.includes('data-action="panel-reviewer"'));
    assert.ok(html.includes('<option value="" selected>'), html);
    // Someone who cannot choose and sees no choice gets no row at all.
    assert.equal(reviewerRows({ ...card, reviewer: null, can_set_reviewer: false }, { people, author }), '');
  },
  owner_choice() {
    const html = reviewerRows({ ...card, reviewer: 'user:ada', can_set_reviewer: true }, { people, author });
    assert.ok(html.includes('<option value="user:ada" selected>Ada &lt;b&gt;</option>'), html);
    assert.ok(html.includes('<option value="user:owner">Olive</option>'));
    // A reviewer who left the workspace is still shown rather than silently dropped.
    const gone = reviewerRows({ ...card, reviewer: 'user:gone', can_set_reviewer: true }, { people, author });
    assert.ok(gone.includes('value="user:gone" selected'));
  },
  read_only() {
    const html = reviewerRows({ ...card, reviewer: 'user:ada', can_set_reviewer: false }, { people, author });
    assert.ok(html.includes('Ada &lt;b&gt;') && !html.includes('<select') && !html.includes('panel-reviewer'));
    assert.ok(reviewerRows({ ...card, reviewer: null, can_set_reviewer: false, ask_to: null }, { people, author }) === '');
  },
  change() {
    const chosen = { ...card, reviewer: 'user:ada' };
    assert.equal(reviewerChange(chosen, { value: 'user:ada' }), null);
    assert.deepEqual(reviewerChange(chosen, { value: 'user:owner' }), { version: 4, reviewer: 'user:owner' });
    assert.deepEqual(reviewerChange(chosen, { value: '' }), { version: 4, reviewer: null });
    assert.equal(reviewerChange({ ...card, reviewer: null }, { value: '' }), null);
  },
  panel() {
    const html = renderDetails({ ...card, reviewer: 'user:ada', can_set_reviewer: true }, { board: { slug: 'atlas' }, me: 'user:owner', people });
    assert.ok(html.includes('<dt>Reviewer</dt>') && html.includes('value="user:ada" selected'));
    // A completion review shows the reviewer once, not as a second "Reviewer" row.
    const review = renderDetails({ ...card, status: 'review', review_kind: 'completion', ask: 'Done?', ask_to: 'user:ada', reviewer: 'user:ada', can_set_reviewer: false }, { board: { slug: 'atlas' }, me: 'user:owner', people });
    assert.equal(review.split('<dt>Reviewer</dt>').length - 1, 1);
  },
  tag() {
    assert.ok(cardTags({ ...card, reviewer: 'user:ada' }, { me: 'user:owner' }).some((tag) => tag.startsWith('Reviewer: ')));
    assert.ok(!cardTags({ ...card, reviewer: null }, { me: 'user:owner' }).some((tag) => tag.startsWith('Reviewer: ')));
    assert.ok(!cardTags({ ...card, status: 'done', reviewer: 'user:ada' }, { me: 'user:owner' }).some((tag) => tag.startsWith('Reviewer: ')));
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
