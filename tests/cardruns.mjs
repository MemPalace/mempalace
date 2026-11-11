import assert from 'node:assert/strict';
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const runs = await load('cardruns.js');
const { cardRunsHTML, cardRunsSlot, attachCardRuns, outcomeWords, refusalWords, startRefusalWords } = runs;
const { renderDetails, createDetails } = await load('details.js');
const card = { number: 7, board: 'atlas', version: 2, title: 'Offline queue', body: '', status: 'todo', events: [] };
const job = (extra = {}) => ({ job: 'job_1', machine_label: 'Studio', state: 'queued', thread_id: null, outcome_code: null, updated_at: '2026-11-11T10:00:00Z', ...extra });
const wait = () => new Promise((done) => setTimeout(done, 0));

// A fake slot: just enough surface for attachCardRuns, with the click and submit handlers kept.
function fakeSlot() {
  const handlers = {};
  const slot = {
    hidden: true, innerHTML: '', isConnected: true,
    addEventListener: (type, fn) => { handlers[type] = fn; },
    querySelector: (selector) => selector === '.run-note' ? slot.note : null,
    note: { textContent: '' },
  };
  return { slot, handlers, mount: { querySelector: (selector) => selector === '[data-card-runs]' ? slot : null } };
}
const cancelEvent = (id) => {
  const button = { dataset: { runCancel: id }, closest: () => ({ querySelectorAll: () => [] }) };
  return { target: { closest: (selector) => selector === '[data-run-cancel]' ? button : null } };
};
const submitEvent = (id, outcome, thread = '') => {
  const form = { dataset: { runResolve: id }, elements: { thread: { value: thread, focus() { form.focused = true; } } }, querySelector: () => ({ closest: () => ({ querySelectorAll: () => [] }) }), focused: false };
  return { form, event: { target: { closest: (selector) => selector === '[data-run-resolve]' ? form : null }, submitter: { value: outcome }, preventDefault() { form.prevented = true; } } };
};

const startEvent = (form = { elements: {} }) => {
  const button = { disabled: false };
  form.querySelector = (selector) => selector === 'button' ? button : null;
  return { form, event: { target: { closest: (selector) => selector === '[data-run-start]' ? form : null }, preventDefault() { form.prevented = true; } } };
};

const cases = {
  words() {
    const html = cardRunsHTML([
      job(), job({ job: 'job_2', state: 'leased' }), job({ job: 'job_3', state: 'starting' }),
      job({ job: 'job_4', state: 'started', thread_id: 'thread-9' }), job({ job: 'job_5', state: 'failed', outcome_code: 'busy' }),
      job({ job: 'job_6', state: 'uncertain' }), job({ job: 'job_7', state: 'cancelled', outcome_code: 'machine_revoked' }),
    ]);
    for (const text of ['Waiting for the machine to pick it up', 'The machine has picked it up', 'Starting on the machine', 'Started', 'Did not start', 'Not clear whether it started', 'Cancelled', 'The machine had reached its hourly limit', 'The machine was switched off or removed', '<code>thread-9</code>', 'Studio']) {
      assert.ok(html.includes(text), `missing: ${text}`);
    }
    // Cancel only while it can still be stopped; the owner's choice only when uncertain.
    const cancels = [...html.matchAll(/data-run-cancel="(job_\d)"/g)].map((match) => match[1]);
    assert.deepEqual(cancels, ['job_1', 'job_2', 'job_3']);
    const resolves = [...html.matchAll(/data-run-resolve="(job_\d)"/g)].map((match) => match[1]);
    assert.deepEqual(resolves, ['job_6']);
    assert.ok(html.includes('It started') && html.includes('It failed'));
    assert.equal(outcomeWords('weird_code'), 'Reason code: weird_code');
    assert.equal(outcomeWords(null), '');
  },
  empty() {
    const html = cardRunsHTML([]);
    assert.ok(html.includes('No agent has been started on this card.'));
    assert.ok(!html.includes('data-run-cancel'));
  },
  escape() {
    const html = cardRunsHTML([job({ machine_label: '<img src=x>', thread_id: '"><b>' })]);
    assert.ok(!html.includes('<img') && !html.includes('<b>'));
    assert.ok(html.includes('&lt;img src=x&gt;'));
    assert.equal(refusalWords({ payload: { error: 'no: start is already finished' } }), 'Start is already finished.');
    assert.ok(refusalWords({ status: 403 }).includes('owner'));
  },
  mount() {
    // The panel gets one hidden section after the fields list, and nothing else changes for it.
    const html = renderDetails(card, { board: { slug: 'atlas' }, me: 'user:maya' });
    assert.equal(html.match(/data-card-runs/g).length, 1);
    assert.ok(html.indexOf('</dl><section class="card-runs"') > 0);
    assert.ok(/<section class="card-runs" data-card-runs aria-label="Runs" hidden><\/section>/.test(cardRunsSlot({ number: 99, board: 'atlas' })));
  },
  async list() {
    const { slot, mount } = fakeSlot();
    const calls = [];
    await attachCardRuns(mount, card, { api: async (url) => { calls.push(url); return [job()]; } });
    assert.deepEqual(calls, ['/api/card-starts?task=7', '/api/card-starts/options?task=7']);
    assert.equal(slot.hidden, false);
    assert.ok(slot.innerHTML.includes('Waiting for the machine to pick it up'));
    // The next draw shows the last list at once, before the refresh answers.
    assert.ok(cardRunsSlot(card).includes('Waiting for the machine to pick it up'));
    const again = fakeSlot();
    await attachCardRuns(again.mount, card, { api: async () => [] });
    assert.ok(again.slot.innerHTML.includes('No agent has been started'));
  },
  async hidden() {
    // A member (403) or a card that is gone (404): the section stays out of the panel.
    for (const status of [403, 404]) {
      const { slot, mount } = fakeSlot();
      await attachCardRuns(mount, { ...card, number: 70 + status }, { api: async () => { throw Object.assign(new Error('no'), { status }); } });
      assert.equal(slot.hidden, true);
      assert.equal(slot.innerHTML, '');
    }
    assert.ok(!cardRunsSlot({ ...card, number: 473 }).includes('Runs</h3>'));
    // A failed refresh keeps the last list and says so.
    const ok = fakeSlot();
    await attachCardRuns(ok.mount, { ...card, number: 8 }, { api: async () => [job()] });
    const bad = fakeSlot();
    await attachCardRuns(bad.mount, { ...card, number: 8 }, { api: async () => { throw Object.assign(new Error('down'), { status: 500 }); } });
    assert.ok(bad.slot.innerHTML.includes('Waiting for the machine') && bad.slot.innerHTML.includes('Could not refresh'));
  },
  async cancel() {
    const { slot, handlers, mount } = fakeSlot();
    const posts = [];
    let jobs = [job()];
    const api = async (url, options) => {
      if (options?.method === 'POST') { posts.push([url, options]); jobs = [job({ state: 'cancelled' })]; return { state: 'cancelled' }; }
      return jobs;
    };
    await attachCardRuns(mount, { ...card, number: 9 }, { api });
    handlers.click(cancelEvent('job_1'));
    await wait(); await wait();
    assert.deepEqual(posts.map(([url, options]) => [url, options.method, options.body]), [['/api/card-starts/job_1/cancel', 'POST', {}]]);
    assert.ok(slot.innerHTML.includes('Cancelled.') && !slot.innerHTML.includes('data-run-cancel'));
  },
  async refusal() {
    // A 409 shows its words inside the panel and the list reloads to what is true now.
    const { slot, handlers, mount } = fakeSlot();
    const api = async (url, options) => {
      if (options?.method === 'POST') throw Object.assign(new Error('x'), { status: 409, payload: { error: 'no: start is already finished' } });
      return [job({ state: 'started', thread_id: 'thread-3' })];
    };
    await attachCardRuns(mount, { ...card, number: 10 }, { api });
    handlers.click(cancelEvent('job_1'));
    await wait(); await wait();
    assert.ok(slot.innerHTML.includes('Start is already finished.'));
    assert.ok(slot.innerHTML.includes('Started') && !slot.innerHTML.includes('data-run-cancel'));
    // 403 for a resolve by a non-owner.
    const second = fakeSlot();
    await attachCardRuns(second.mount, { ...card, number: 11 }, { api: async (url, options) => { if (options?.method === 'POST') throw Object.assign(new Error('x'), { status: 403, payload: { error: 'no: a signed-in workspace owner is required' } }); return [job({ state: 'uncertain' })]; } });
    const { event } = submitEvent('job_1', 'failed');
    second.handlers.submit(event);
    await wait(); await wait();
    assert.ok(second.slot.innerHTML.includes('A signed-in workspace owner is required.'));
  },
  async resolve() {
    const { slot, handlers, mount } = fakeSlot();
    const posts = [];
    const api = async (url, options) => { if (options?.method === 'POST') { posts.push([url, options.body]); return { state: 'started' }; } return [job({ state: 'uncertain' })]; };
    await attachCardRuns(mount, { ...card, number: 12 }, { api });
    // "It started" needs a valid thread id; nothing is sent without one.
    for (const bad of ['', '  ', '-x', 'has space', 'a'.repeat(257)]) {
      const { form, event } = submitEvent('job_1', 'started', bad);
      handlers.submit(event);
      assert.equal(form.prevented, true);
      assert.equal(form.focused, true, `kept focus for ${JSON.stringify(bad)}`);
    }
    await wait();
    assert.deepEqual(posts, []);
    handlers.submit(submitEvent('job_1', 'started', ' thread-42 ').event);
    await wait(); await wait();
    handlers.submit(submitEvent('job_1', 'failed', 'ignored').event);
    await wait(); await wait();
    assert.deepEqual(posts, [
      ['/api/card-starts/job_1/resolve', { outcome: 'started', thread_id: 'thread-42' }],
      ['/api/card-starts/job_1/resolve', { outcome: 'failed' }],
    ]);
    assert.equal(slot.hidden, false);
  },
  async stale() {
    // A slow answer for a panel that has since been redrawn must not paint the old slot.
    const { slot, mount } = fakeSlot();
    const release = [];
    const pending = attachCardRuns(mount, { ...card, number: 13 }, { api: () => new Promise((done) => { release.push(done); }) });
    slot.isConnected = false;
    release.forEach((done) => done([job()]));
    await pending;
    assert.equal(slot.innerHTML, '');
  },
  // ---- Start agent (372-B)
  startwords() {
    // Every refusal the owner can meet says what is wrong and what to do; codes win over text.
    for (const code of ['origin_not_released', 'machine_unavailable', 'active_start', 'started_recently', 'busy', 'at_cap', 'binding_not_attested']) {
      const words = startRefusalWords({ status: 409, payload: { error: 'no: x', code } });
      assert.ok(words.length > 40 && !words.includes(code), code);
    }
    assert.ok(startRefusalWords({ status: 409, payload: { error: 'no: card already has an active start' } }).includes('already being started'));
    assert.ok(startRefusalWords({ status: 403, payload: { error: 'no: binding_not_attested' } }).includes('vouched for'));
    assert.ok(startRefusalWords({ status: 409, payload: { error: 'no: card is not startable: blocked_by' } }).length > 0);
    // An unknown refusal still shows the server's own sentence.
    assert.equal(startRefusalWords({ status: 403, payload: { error: 'no: something new' } }), 'Something new.');
    assert.ok(!startRefusalWords({ status: 403, payload: { error: 'no: origin_not_released' } }).includes('origin_not_released'));
  },
  startview() {
    const none = cardRunsHTML([], '', null);
    assert.ok(!none.includes('data-run-start'));
    // The board's config decides: one button, no choices to make.
    const configured = cardRunsHTML([], '', { configured: true, machine: { id: 'mach_1', label: 'Studio' }, project: 'demo', topic: 'worker', refusal: null });
    assert.ok(configured.includes('data-run-start') && configured.includes('Start agent') && configured.includes('Studio'));
    assert.ok(!configured.includes('<select'));
    // Without a config the owner picks from what the app can resolve.
    const free = cardRunsHTML([], '', { configured: false, refusal: null, machines: [{ id: 'mach_1', label: 'Studio', projects: ['demo', 'site'], topics: ['worker'] }, { id: 'mach_2', label: 'Laptop', projects: ['lab'], topics: ['review'] }] });
    assert.ok(free.includes('name="machine"') && free.includes('Laptop') && free.includes('<option value="site">') && free.includes('name="topic"'));
    assert.ok(!free.includes('<option value="lab">'), 'only the first machine\'s projects are listed at first');
    // Nothing can be started: a plain reason and no button.
    const refused = cardRunsHTML([], '', { configured: false, refusal: 'no_machine', machines: [] });
    assert.ok(refused.includes('No machine is ready') && refused.includes('Runner machines') && !refused.includes('Start agent'));
    // A card nobody may take shows neither the control nor a complaint.
    for (const refusal of ['not_todo', 'assigned', 'blocked_by', 'waiting_on_person', 'proposal_open']) {
      const html = cardRunsHTML([], '', { configured: true, refusal });
      assert.ok(!html.includes('data-run-start'), refusal);
    }
    // While a start is waiting there is no second button, only the reason.
    const busy = cardRunsHTML([job()], '', { configured: true, machine: { id: 'm', label: 'S' }, project: 'p', topic: 't', refusal: null });
    assert.ok(!busy.includes('Start agent') && busy.includes('already being started'));
    // Hostile labels stay text.
    const hostile = cardRunsHTML([], '', { configured: false, refusal: null, machines: [{ id: 'm"><b>', label: '<img src=x>', projects: ['a"b'], topics: ['t'] }] });
    assert.ok(!hostile.includes('<img') && !hostile.includes('<b>'));
    // A malformed answer shows nothing rather than failing.
    assert.ok(!cardRunsHTML([], '', { configured: false, refusal: null }).includes('data-run-start'));
  },
  async startconfigured() {
    const { slot, handlers, mount } = fakeSlot();
    const posts = [];
    let jobs = [];
    const api = async (url, options) => {
      if (options?.method === 'POST') { posts.push([url, options.body]); jobs = [job()]; return { job_id: 'job_1' }; }
      if (url.startsWith('/api/card-starts/options')) return { configured: true, machine: { id: 'mach_1', label: 'Studio' }, project: 'demo', topic: 'worker', refusal: null };
      return jobs;
    };
    await attachCardRuns(mount, { ...card, number: 21 }, { api });
    assert.ok(slot.innerHTML.includes('Start agent'));
    const { form, event } = startEvent();
    handlers.submit(event);
    await wait(); await wait();
    // Two clicks in all: open the card, press Start. Only the card number goes out.
    assert.equal(form.prevented, true);
    assert.deepEqual(posts, [['/api/card-starts', { task: 21 }]]);
    // The new run shows in the list, there is no second button, and the note says what happened.
    assert.ok(slot.innerHTML.includes('Waiting for the machine to pick it up'));
    assert.ok(!slot.innerHTML.includes('Start agent') && slot.innerHTML.includes('already being started'));
    assert.ok(slot.innerHTML.includes('Asked the machine'));
  },
  async startfree() {
    const { slot, handlers, mount } = fakeSlot();
    const posts = [];
    const machines = [{ id: 'mach_1', label: 'Studio', projects: ['demo', 'site'], topics: ['worker'] }, { id: 'mach_2', label: 'Laptop', projects: ['lab'], topics: ['review'] }];
    const api = async (url, options) => {
      if (options?.method === 'POST') { posts.push([url, options.body]); return { job_id: 'job_1' }; }
      if (url.startsWith('/api/card-starts/options')) return { configured: false, refusal: null, machines };
      return [];
    };
    await attachCardRuns(mount, { ...card, number: 22 }, { api });
    // Picking another machine swaps the projects and topics to what that machine allows.
    const form = { elements: { machine: { value: 'mach_2' }, project: { innerHTML: '', value: 'lab' }, topic: { innerHTML: '', value: 'review' } } };
    handlers.change({ target: { name: 'machine', value: 'mach_2', closest: (selector) => selector === '[data-run-start]' ? form : null } });
    assert.ok(form.elements.project.innerHTML.includes('value="lab"') && !form.elements.project.innerHTML.includes('demo'));
    assert.ok(form.elements.topic.innerHTML.includes('value="review"'));
    handlers.submit(startEvent(form).event);
    await wait(); await wait();
    assert.deepEqual(posts, [['/api/card-starts', { task: 22, machine: 'mach_2', project: 'lab', topic: 'review' }]]);
  },
  async startrefused() {
    // The server's refusal shows in the panel in words with the next action, and nothing was added.
    const { slot, handlers, mount } = fakeSlot();
    const options = { configured: true, machine: { id: 'mach_1', label: 'Studio' }, project: 'demo', topic: 'worker', refusal: null };
    const refusals = [
      [403, { error: 'no: origin_not_released', code: 'origin_not_released' }, 'has not been released'],
      [409, { error: 'no: card already has an active start', code: 'active_start' }, 'already being started'],
      [409, { error: 'no: the board is at its cap of 3 agents', code: 'at_cap' }, 'as many agents working as it may'],
      [403, { error: 'no: only a workspace owner can start an agent from a card' }, 'Only a workspace owner can start an agent from a card.'],
    ];
    for (const [status, payload, expected] of refusals) {
      let posts = 0;
      const api = async (url, requestOptions) => {
        if (requestOptions?.method === 'POST') { posts += 1; throw Object.assign(new Error('x'), { status, payload }); }
        return url.startsWith('/api/card-starts/options') ? options : [];
      };
      const one = fakeSlot();
      await attachCardRuns(one.mount, { ...card, number: 23 + status }, { api });
      one.handlers.submit(startEvent().event);
      await wait(); await wait();
      assert.equal(posts, 1);
      assert.ok(one.slot.innerHTML.includes(expected), expected);
      assert.ok(one.slot.innerHTML.includes('No agent has been started on this card.'));
    }
    void slot; void handlers; void mount;
  },
  async startnotoffered() {
    // Not the owner (403 on the options) or an options error: the runs show, with no Start control.
    for (const failure of [Object.assign(new Error('no'), { status: 403 }), Object.assign(new Error('down'), { status: 500 })]) {
      const { slot, mount } = fakeSlot();
      await attachCardRuns(mount, { ...card, number: 24 }, { api: async (url) => { if (url.startsWith('/api/card-starts/options')) throw failure; return [job({ state: 'started', thread_id: 't-1' })]; } });
      assert.equal(slot.hidden, false);
      assert.ok(slot.innerHTML.includes('Started') && !slot.innerHTML.includes('data-run-start'));
    }
    // An old server (no options route answers with a list) is treated the same way.
    const old = fakeSlot();
    await attachCardRuns(old.mount, { ...card, number: 25 }, { api: async () => [] });
    assert.ok(!old.slot.innerHTML.includes('data-run-start'));
  },
  async startpicks() {
    // A refresh of the card keeps what the owner picked in the Start form.
    const values = { machine: 'mach_2', project: 'lab', topic: 'review' };
    const selects = Object.fromEntries(Object.entries(values).map(([name, value]) => [name, { value, options: [{ value: 'x' }, { value }] }]));
    const slot = { hidden: true, innerHTML: '', isConnected: true, addEventListener() {}, querySelector: (selector) => { const match = selector.match(/\[name="(\w+)"\]/); return match ? selects[match[1]] : null; } };
    const machines = [{ id: 'mach_1', label: 'Studio', projects: ['demo'], topics: ['worker'] }, { id: 'mach_2', label: 'Laptop', projects: ['lab'], topics: ['review'] }];
    for (const select of Object.values(selects)) select.value = 'x';
    selects.machine.value = 'mach_2'; selects.project.value = 'lab'; selects.topic.value = 'review';
    await attachCardRuns({ querySelector: () => slot }, { ...card, number: 26 }, { api: async (url) => url.startsWith('/api/card-starts/options') ? { configured: false, refusal: null, machines } : [] });
    assert.deepEqual(Object.fromEntries(Object.entries(selects).map(([name, select]) => [name, select.value])), values);
  },
  async panel() {
    // The details panel loads the runs on every show of the card, and a tab change redraws them.
    const slot = { hidden: true, innerHTML: '', isConnected: true, addEventListener() {}, querySelector: () => null };
    const mount = { innerHTML: '', addEventListener() {}, removeEventListener() {}, querySelector: (selector) => selector === '[data-card-runs]' ? slot : null };
    const calls = [];
    const component = createDetails({ mount, api: async (url) => { calls.push(url); return url.startsWith('/api/card-starts') ? [job()] : {}; } });
    await component.show({ ...card, number: 14 }, { board: { slug: 'atlas' }, me: 'user:maya' });
    await wait();
    // Other panel parts (card links) make their own calls; only the runs calls count here.
    const starts = () => calls.filter((url) => url.startsWith('/api/card-starts?'));
    assert.deepEqual(starts(), ['/api/card-starts?task=14']);
    assert.ok(slot.innerHTML.includes('Waiting for the machine'));
    await component.show({ ...card, number: 14, version: 3 }, { board: { slug: 'atlas' }, me: 'user:maya' });
    await wait();
    assert.equal(starts().length, 2);
    component.stop();
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
