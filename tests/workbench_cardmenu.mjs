import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const path = new URL('../commonplace/web/static/cardmenu.js', import.meta.url);
assert.ok(await readFile(path, 'utf8').catch(() => null), 'Card menu module missing');
const menu = await import(path);
for (const name of ['renderCardMenu', 'createCardMenu']) assert.equal(typeof menu[name], 'function', `${name} missing`);
const board = { name:'Atlas', slug:'atlas' };
const card = { number:21, title:'Queue offline edits', status:'doing', version:4 };
const decode = (s) => s.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&#39;', "'").replaceAll('&amp;', '&');

// The modal's native browser behaviour is supplied by <dialog>. This small DOM
// double exercises controller events, focus targets and request ordering in Node.
function fixture(options = {}) {
  const handlers = new Map(), calls = [], changes = [], copies = [], targets = new Map();
  const doc = { activeElement:null, getElementById:(id) => targets.get(id) };
  function control(dataset = {}) {
    return { dataset, disabled:false, value:'', focus() { doc.activeElement = this; },
      closest() { return this; }, matches:(selector) => selector === '[data-menu-title]' };
  }
  const opener = control();
  targets.set('more-A-21', opener);
  opener.focus();
  let html = '', dialog;
  const mount = { ownerDocument:doc,
    addEventListener:(name, fn) => handlers.set(name, fn),
    removeEventListener:(name) => handlers.delete(name),
    querySelector:(selector) => selector === 'dialog' ? dialog : null,
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      if (!value) { dialog = null; return; }
      const controls = [...value.matchAll(/<(button|input)\b([^>]*)>/g)].map(([, tag, attrs]) => {
        const el = control(); el.tag = tag; el.attrs = attrs;
        el.disabled = /\bdisabled\b/.test(attrs);
        for (const [attr, key] of [['data-menu-move', 'menuMove'], ['data-menu-action', 'menuAction']]) {
          const found = attrs.match(new RegExp(`${attr}="([^"]*)"`));
          if (found) el.dataset[key] = found[1];
        }
        el.value = decode(attrs.match(/value="([^"]*)"/)?.[1] || '');
        return el;
      });
      const alert = { textContent:'' }, listeners = new Map();
      dialog = { controls, alert, listeners, open:false,
        showModal() { this.open = true; }, close() { this.open = false; },
        addEventListener:(key, fn) => listeners.set(key, fn),
        querySelectorAll:(selector) => selector.includes(':disabled') ? controls.filter((el) => !el.disabled) : controls,
        querySelector(selector) {
          if (selector === '[data-menu-error]') return alert;
          if (selector === 'input') return controls.find((el) => el.tag === 'input');
          const action = selector.match(/data-menu-action="([^"]+)"/);
          return action ? controls.find((el) => el.dataset.menuAction === action[1]) : null;
        },
      };
    },
  };
  const controller = menu.createCardMenu({ mount,
    api:async (...args) => { calls.push(args); return options.api ? options.api(...args) : { ...card, ...args[1].body }; },
    onChange:async (value) => { changes.push(value); await options.onChange?.(value); },
    clipboard:{ writeText:async (text) => { copies.push(text); if (options.copyError) throw new Error('denied'); } },
    href:() => 'https://example.test/app/?signin=temporary#/board/atlas',
    onReview:options.onReview,
  });
  for (const key of ['show', 'close', 'stop']) assert.equal(typeof controller[key], 'function', `${key} missing`);
  controller.show(card, { board, from:opener });
  const get = (key, value) => { const el = dialog.controls.find((el) => el.dataset[key] === value); assert.ok(el, `${key}=${value} missing`); return el; };
  const event = (target, extra = {}) => ({ target, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...extra });
  return { controller, mount, calls, changes, copies, targets, doc, opener, handlers,
    get dialog() { return dialog; },
    click:(key, value) => handlers.get('click')(event(get(key, value))),
    key(key, shiftKey = false) { const ev = event(doc.activeElement, { key, shiftKey }); handlers.get('keydown')(ev); return ev; },
    submit() { return handlers.get('submit')(event(control())); },
  };
}
const cases = {
  surface() {
    const html = menu.renderCardMenu({ ...card, title:'<img src=x> & "words"' }, { board });
    for (const marker of ['<dialog', 'class="sheet menu-sheet card-menu"', 'aria-modal="true"', 'aria-labelledby="cm-h"', 'data-return-focus="more-A-21"', 'aria-label="Move to column"', 'Edit title', 'Copy link', 'Close', 'aria-current="true"', 'now here']) assert.ok(html.includes(marker), marker);
    for (const forbidden of ['Remind', 'Starts after', '>Due', '<img src=x>']) assert.ok(!html.includes(forbidden), forbidden);
    assert.ok(html.includes('&lt;img src=x&gt; &amp; &quot;words&quot;'));
    assert.deepEqual([...html.matchAll(/data-menu-move="([^"]+)"/g)].map((m) => m[1]), ['todo','doing','review','done']);
    const f = fixture(); assert.equal(f.dialog.open, true); assert.equal(f.doc.activeElement.dataset.menuMove, 'todo'); f.controller.stop();
  },
  async move() {
    for (const status of ['todo','done']) {
      const f = fixture(); await f.click('menuMove', status);
      assert.deepEqual(f.calls, [[ '/api/cards/21', { method:'PATCH', body:{ status, version:4 } } ]]);
      assert.equal(f.changes.length, 1); assert.equal(f.mount.innerHTML, ''); assert.equal(f.doc.activeElement, f.opener);
    }
    const f = fixture(); await f.click('menuMove', 'doing'); assert.equal(f.calls.length, 0);
  },
  async title() {
    const f = fixture(); await f.click('menuAction', 'edit');
    assert.ok(f.dialog.querySelector('input'), 'Title input missing');
    assert.equal(f.doc.activeElement.value, card.title);
    f.doc.activeElement.value = '  Exact <title> & words  ';
    await f.submit();
    assert.deepEqual(f.calls[0], ['/api/cards/21', { method:'PATCH', body:{ title:'  Exact <title> & words  ', version:4 } }]);
    const blank = fixture(); await blank.click('menuAction', 'edit'); blank.doc.activeElement.value = '  '; await blank.submit();
    assert.equal(blank.calls.length, 0); assert.equal(blank.dialog.alert.textContent, 'Enter a title.');
    await blank.click('menuAction', 'cancel-edit'); assert.equal(blank.doc.activeElement.dataset.menuAction, 'edit');
  },
  async copy() {
    const f = fixture(); await f.click('menuAction', 'copy');
    assert.deepEqual(f.copies, ['https://example.test/app/#/card/21']);
    assert.equal(f.calls.length, 0); assert.equal(f.changes.length, 0); assert.equal(f.doc.activeElement, f.opener);
    const denied = fixture({ copyError:true }); await denied.click('menuAction', 'copy');
    assert.match(denied.dialog.alert.textContent, /Could not copy/); assert.equal(denied.dialog.open, true);
  },
  async focus() {
    const f = fixture(); const newer = { focus() { f.doc.activeElement = this; } };
    f.targets.set('more-A-21', newer); await f.click('menuAction', 'close'); assert.equal(f.doc.activeElement, newer);
    f.controller.show(card, { board }); f.controller.close(); assert.equal(f.doc.activeElement, newer);
    const fallback = fixture(); fallback.targets.clear(); fallback.controller.close(); assert.equal(fallback.doc.activeElement, fallback.opener);
    const native = fixture(), ev = { preventDefault() {}, stopPropagation() {} };
    assert.equal(typeof native.dialog.listeners.get('cancel'), 'function'); native.dialog.listeners.get('cancel')(ev);
    assert.equal(native.doc.activeElement, native.opener); assert.equal(native.mount.innerHTML, '');
  },
  keys() {
    const f = fixture(), first = f.doc.activeElement;
    assert.equal(f.key('Tab', true).prevented, true); assert.equal(f.doc.activeElement.dataset.menuAction, 'close');
    assert.equal(f.key('Tab').prevented, true); assert.equal(f.doc.activeElement, first);
    assert.equal(f.key('x').stopped, true);
    const escape = f.key('Escape'); assert.equal(escape.prevented, true); assert.equal(f.doc.activeElement, f.opener); assert.equal(f.mount.innerHTML, '');
  },
  async failure() {
    for (const status of [409,403,500]) {
      const f = fixture({ api:async () => { throw Object.assign(new Error('private server text'), { status }); } });
      await f.click('menuAction', 'edit'); f.doc.activeElement.value = 'Kept title'; await f.submit();
      assert.equal(f.dialog.open, true); assert.equal(f.dialog.querySelector('input').value, 'Kept title');
      assert.equal(f.changes.length, 0); assert.equal(f.calls.length, 1);
      assert.ok(!f.dialog.alert.textContent.includes('private')); assert.match(f.dialog.alert.textContent, status === 409 ? /changed/ : /Could not save/);
      assert.ok(f.dialog.controls.every((el) => !el.disabled));
    }
  },
  async race() {
    let resolve;
    const f = fixture({ api:() => new Promise((done) => { resolve = done; }) });
    const pending = f.click('menuMove', 'todo');
    await f.click('menuMove', 'done'); assert.equal(f.calls.length, 1);
    assert.equal(f.dialog.controls.filter((el) => !el.disabled).length, 1);
    await f.click('menuAction', 'close'); f.controller.show({ ...card, number:22, title:'New selection' }, { board });
    resolve({ ...card, status:'todo' }); await pending;
    assert.equal(f.changes.length, 0); assert.ok(f.mount.innerHTML.includes('New selection')); assert.equal(f.dialog.open, true);
    f.controller.stop(); assert.equal(f.handlers.size, 0); assert.equal(f.mount.innerHTML, '');
    const refresh = fixture({ onChange:() => refresh.targets.set('more-A-21', { focus() { refresh.doc.activeElement = this; } }) });
    await refresh.click('menuMove', 'done'); assert.equal(refresh.doc.activeElement, refresh.targets.get('more-A-21'));
  },
  async review() {
    const reviews = [], f = fixture({ onReview:(value) => reviews.push(value) });
    await f.click('menuMove', 'review'); assert.equal(f.calls.length, 0); assert.equal(reviews.length, 1); assert.deepEqual(reviews[0], card);
    assert.equal(f.mount.innerHTML, '');
    const unavailable = fixture(); await unavailable.click('menuMove', 'review'); assert.equal(unavailable.calls.length, 0); assert.match(unavailable.dialog.alert.textContent, /request a review/);
  },
  async proposal() {
    const f = fixture(); f.controller.show({ ...card, proposal_status:'pending', status:'backlog' }, { board });
    assert.ok(f.dialog.controls.filter((el) => el.dataset.menuMove).every((el) => el.disabled));
    await f.click('menuMove', 'done'); assert.equal(f.calls.length, 0);
    await f.click('menuAction', 'copy'); assert.equal(f.copies.length, 1);
  },
};
assert.ok(Object.hasOwn(cases, process.argv[2]), 'Unknown case');
await cases[process.argv[2]]();
console.log(`${process.argv[2]} PASS`);
