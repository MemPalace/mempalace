import assert from 'node:assert/strict';
const dir = '../commonplace/web/static/';
const types = await import(new URL(dir + 'tasktypes.js', import.meta.url));
const { renderDetails } = await import(new URL(dir + 'details.js', import.meta.url));
const card = { number: 7, board: 'work', version: 3, title: 'T', body: 'b', status: 'todo', priority: 'normal', labels: [], events: [], task_type: '' };
const opts = { board: { slug: 'work', name: 'Work' }, me: 'user:owner' };

// A tiny event target stands in for the document.
function target() {
  const handlers = [];
  return { addEventListener: (name, fn) => handlers.push([name, fn]), fire: (name, event) => handlers.filter(([n]) => n === name).forEach(([, fn]) => fn(event)) };
}
const field = (value = '') => ({ value, disabled: true, placeholder: '', textContent: '' });
function section(id) {
  const parts = { '[data-agent-types-input]': field(), '[data-agent-types-status]': field() };
  const node = { open: true, dataset: { agentId: id }, querySelector: (s) => parts[s], closest: () => node, parts };
  return node;
}
const cases = {
  parse() {
    assert.deepEqual(types.parseTypes('review, code  code'), ['code', 'review']);
    assert.deepEqual(types.parseTypes(''), []);
    assert.throws(() => types.parseTypes('Code'), /not a valid task type/);
    assert.throws(() => types.parseTypes('a_b'), /not a valid task type/);
    assert.throws(() => types.parseTypes('x'.repeat(49)), /not a valid task type/);
  },
  card() {
    const owner = renderDetails({ ...card, task_type: 'co"de', can_prioritize: true }, opts);
    assert.ok(owner.includes('id="card-task-type"') && owner.includes('data-card-type-save'));
    assert.ok(owner.includes('value="co&quot;de"'), 'type is escaped');
    const reader = renderDetails({ ...card, task_type: 'code', can_prioritize: false }, opts);
    assert.ok(!reader.includes('card-task-type') && reader.includes('<dt>Task type</dt><dd>code</dd>'));
    const untyped = renderDetails({ ...card, can_prioritize: false }, opts);
    assert.ok(!untyped.includes('Task type'), 'untyped cards render as before');
  },
  async save() {
    const input = { value: ' review ' }, error = { textContent: 'old' };
    const mount = { querySelector: (s) => (s === '#card-task-type' ? input : error) };
    const calls = [];
    const api = async (path, options) => { calls.push([path, options]); return { number: 7, task_type: 'review' }; };
    assert.equal((await types.saveCardType(mount, card, api)).task_type, 'review');
    assert.deepEqual(calls, [['/api/cards/7', { method: 'PATCH', body: { version: 3, task_type: 'review' } }]]);
    assert.equal(error.textContent, '');
    input.value = '';
    assert.equal(await types.saveCardType(mount, card, api), null, 'unchanged value sends nothing');
    assert.equal(calls.length, 1);
    input.value = 'Bad';
    assert.equal(await types.saveCardType(mount, card, api), null);
    assert.match(error.textContent, /not a valid task type/);
    assert.equal(calls.length, 1);
    input.value = 'code';
    const refused = await types.saveCardType(mount, card, async () => { throw new Error('Only the owner'); });
    assert.equal(refused, null);
    assert.equal(error.textContent, 'Only the owner');
  },
  async agents() {
    const html = types.agentTypesControl({ id: 'c"1' });
    assert.ok(html.includes('data-agent-id="c&quot;1"') && html.includes('data-agent-types-save'));
    const doc = target(), calls = [];
    const api = async (path, options) => {
      calls.push([path, options?.method || 'GET', options?.body]);
      return { allowed_types: options ? options.body.allowed_types : ['code', 'review'] };
    };
    types.installAgentTypes(doc, api);
    const node = section('c/1');
    doc.fire('toggle', { target: node });
    doc.fire('toggle', { target: node });
    await new Promise((resolve) => setTimeout(resolve));
    assert.deepEqual(calls, [['/api/task-types/connections/c%2F1', 'GET', undefined]], 'loads once');
    assert.equal(node.parts['[data-agent-types-input]'].value, 'code review');
    assert.equal(node.parts['[data-agent-types-input]'].disabled, false);
    node.parts['[data-agent-types-input]'].value = 'review, design';
    doc.fire('click', { target: { closest: () => ({ closest: () => node }) } });
    await new Promise((resolve) => setTimeout(resolve));
    assert.deepEqual(calls[1], ['/api/task-types/connections/c%2F1', 'POST', { allowed_types: ['design', 'review'] }]);
    assert.equal(node.parts['[data-agent-types-status]'].textContent, 'Saved');
    node.parts['[data-agent-types-input]'].value = 'Bad Type';
    doc.fire('click', { target: { closest: () => ({ closest: () => node }) } });
    await new Promise((resolve) => setTimeout(resolve));
    assert.equal(calls.length, 2, 'invalid input is not sent');
    assert.match(node.parts['[data-agent-types-status]'].textContent, /not a valid task type/);
  },
};
const name = process.argv[2];
assert.ok(cases[name], `unknown case ${name}`);
await cases[name]();
