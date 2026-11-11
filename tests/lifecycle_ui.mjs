import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const url = (s) => `data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const root = new URL('../commonplace/web/static/', import.meta.url);
const api = url(await readFile(new URL('api.js', root), 'utf8'));
const mod = url((await readFile(new URL('lifecycle.js', root), 'utf8')).replace("'./api.js'", JSON.stringify(api)));
const { lifecycleSection, lifecycleRequest, placeLifecycle, pocketList } = await import(mod);
const open = { number: 7, version: 2, status: 'todo', final: false, archived: false, pocket: [], follow_ups: [], follow_up_of: null, can_archive: false, can_restore: false, can_follow_up: false, can_pocket: true };
const done = { ...open, status: 'done', final: true, can_archive: true, can_follow_up: true };
const archived = { ...done, archived: true, archived_at: '2026-11-11T10:00:00Z', can_archive: false, can_restore: true, can_pocket: false };
const cases = {
  legacy() { assert.equal(lifecycleSection({ number: 1 }), ''); },
  done() {
    const html = lifecycleSection(done);
    assert.match(html, /data-action="archive"/); assert.match(html, /data-action="carry-on"/);
    assert.doesNotMatch(html, /data-action="restore"/); assert.match(html, /This card is final/);
  },
  open() {
    const html = lifecycleSection(open);
    assert.doesNotMatch(html, /data-action="(archive|restore|carry-on)"/); assert.match(html, /data-form="pocket"/);
  },
  archived() {
    const html = lifecycleSection(archived);
    assert.match(html, /data-action="restore"/); assert.doesNotMatch(html, /data-action="archive"/);
    assert.doesNotMatch(html, /data-form="pocket"/); assert.match(html, /still readable/);
  },
  readonly() {
    const html = lifecycleSection({ ...done, can_archive: false, can_follow_up: false, can_pocket: false, pocket: [{ id: 3, text: 'Kept' }] });
    assert.doesNotMatch(html, /<button/); assert.match(html, /Kept/);
  },
  links() {
    const html = lifecycleSection({ ...done, follow_up_of: 3, follow_ups: [8, 9] });
    assert.match(html, /href="#\/card\/3"/); assert.match(html, /href="#\/card\/8"/); assert.match(html, /href="#\/card\/9"/);
  },
  escape() {
    const html = pocketList({ ...open, pocket: [{ id: 1, text: '<img src=x onerror=1> "q" & more' }] });
    assert.doesNotMatch(html, /<img/); assert.match(html, /&lt;img/);
    assert.match(html, /data-action="unpin" data-pin="1"/);
  },
  request() {
    assert.deepEqual(lifecycleRequest(done, 'archive'), { path: '/api/cards/7/archive', body: { version: 2 } });
    assert.deepEqual(lifecycleRequest(done, 'restore'), { path: '/api/cards/7/restore', body: { version: 2 } });
    assert.deepEqual(lifecycleRequest(done, 'carry-on'), { path: '/api/cards/7/follow-up', body: { version: 2 } });
    assert.deepEqual(lifecycleRequest(done, 'pin', { text: 'A fact' }), { path: '/api/cards/7/pocket', body: { version: 2, text: 'A fact' } });
    assert.deepEqual(lifecycleRequest(done, 'unpin', { pin: '4' }), { path: '/api/cards/7/unpocket', body: { version: 2, pin: 4 } });
    assert.equal(lifecycleRequest(done, 'delete'), null);
  },
  place() {
    const calls = [];
    const body = { querySelector: (q) => (q === '[data-details-description]' ? { insertAdjacentHTML: (w, h) => calls.push(['desc', w, h]) } : null), insertAdjacentHTML: (w, h) => calls.push(['body', w, h]) };
    const mount = { querySelector: (q) => (q === '.ctx-body' ? body : null) };
    placeLifecycle(mount, done);
    assert.equal(calls.length, 1); assert.equal(calls[0][0], 'desc'); assert.equal(calls[0][1], 'beforebegin');
    const bare = { querySelector: (q) => (q === '[data-details-description]' ? null : null), insertAdjacentHTML: (w, h) => calls.push(['body', w, h]) };
    placeLifecycle({ querySelector: (q) => (q === '.ctx-body' ? bare : null) }, done);
    assert.equal(calls[1][0], 'body');
    placeLifecycle({ querySelector: () => null }, done);
    placeLifecycle({ querySelector: (q) => (q === '.ctx-body' ? body : {}) }, done);
    assert.equal(calls.length, 2);
    placeLifecycle(mount, { number: 1 });
    assert.equal(calls.length, 2);
  },
};
const name = process.argv[2];
assert.ok(cases[name], `unknown case ${name}`);
cases[name]();
