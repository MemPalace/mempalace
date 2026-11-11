import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../commonplace/web/static/rail.js', import.meta.url), 'utf8');
const css = await readFile(new URL('../commonplace/web/static/workbench.css', import.meta.url), 'utf8');
const apiSource = await readFile(new URL('../commonplace/web/static/api.js', import.meta.url), 'utf8');
const apiURL = `data:text/javascript;base64,${Buffer.from(apiSource).toString('base64')}`;
const rail = await import(`data:text/javascript;base64,${Buffer.from(source.replace("'./api.js'", JSON.stringify(apiURL))).toString('base64')}`);
for (const fn of ['renderRail', 'renderPhoneBar', 'renderPhoneTabs', 'projectSubline']) assert.equal(typeof rail[fn], 'function', `${fn} missing`);
const board = { slug: 'atlas', name: 'Atlas', agents_working: 2, counts: { doing: 3, review: 10 } };
const options = { workspace: 'Northwind', user: 'Maya', needs: 3, botsWaiting: 2 };
const links = (html) => [...html.matchAll(/<a\b[^>]*>[\s\S]*?<\/a>/g)].map(m => m[0]);
const hrefs = (html) => links(html).map(a => /href="([^"]*)"/.exec(a)[1]);
const renderAll = () => rail.renderRail({ ...options, can_admin: true }) + rail.renderPhoneBar({ ...options, can_admin: true }) + rail.renderPhoneTabs(options);
const text = (html) => html.replace(/<span class="sr">[^<]*<\/span>/g, '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
switch (process.argv[2]) {
  case 'nav': {
    // Primary navigation: exactly Bots and Memory. Everything else lives in the account menu.
    const html = rail.renderRail({ ...options, can_admin: true });
    const primary = html.slice(0, html.indexOf('<div class="rail-foot">'));
    assert.deepEqual(hrefs(primary), ['#/bots', '#/memory']);
    assert.deepEqual(links(primary).map(text), ['Bots 2', 'Memory']);
    for (const gone of ['#/needs', '#/console', '#/boards', '#/board/', 'rail-h', 'new-project', 'new-board', 'class="proj"']) assert.ok(!primary.includes(gone), gone);
    // The Workspace group: Projects, Needs me, Console, in that order, then the existing entries.
    const menu = html.match(/<div class="user-links">([\s\S]*?)<\/div><\/details>/)[1];
    const group = menu.match(/<div class="menu-group"[^>]*>([\s\S]*?)<\/div>/)[1];
    assert.match(menu, /<div class="menu-group" role="group" aria-label="Workspace">/);
    assert.match(group, /Workspace</);
    assert.deepEqual(hrefs(group), ['#/boards', '#/needs', '#/console']);
    assert.deepEqual(links(group).map(text), ['Projects', 'Needs me 3', 'Console']);
    assert.deepEqual(hrefs(menu), ['#/boards', '#/needs', '#/console', '#/rules', '#/settings', '#/admin']);
    assert.ok(!/\bBoards?\b/.test(text(html)), 'the new navigation says Projects, not Boards');
    break;
  }
  case 'ids': {
    // The rail and the phone top bar are both in the page: no id may appear twice, and no label points at an id.
    const all = renderAll();
    const ids = [...all.matchAll(/\sid="([^"]*)"/g)].map(m => m[1]);
    assert.equal(new Set(ids).size, ids.length, `duplicate ids: ${ids}`);
    assert.ok(!all.includes('aria-labelledby'), 'the Workspace group is named by aria-label');
    assert.equal((all.match(/role="group" aria-label="Workspace"/g) || []).length, 2);
    break;
  }
  case 'phone': {
    const html = rail.renderPhoneTabs({ ...options, view: 'bots' });
    assert.deepEqual(hrefs(html), ['#/bots', '#/memory']);
    assert.deepEqual(links(html).map(text), ['Bots 2', 'Memory']);
    for (const gone of ['Board', 'Chat', 'Needs me', 'm-chat-tab', '#/needs', 'newb']) assert.ok(!html.includes(gone), gone);
    // The same Workspace group is reachable from the phone top bar's menu.
    const bar = rail.renderPhoneBar({ ...options, can_admin: true });
    assert.match(bar, /<details class="m-menu"><summary[^>]*aria-label="Menu"/);
    const group = bar.match(/<div class="menu-group"[^>]*>([\s\S]*?)<\/div>/)[1];
    assert.deepEqual(hrefs(group), ['#/boards', '#/needs', '#/console']);
    assert.deepEqual(hrefs(bar), ['#/boards', '#/needs', '#/console', '#/rules', '#/settings', '#/admin']);
    assert.match(bar, /data-action="palette"/);
    assert.match(bar, /data-action="logout"/);
    assert.ok(!bar.includes('switch-project') && !bar.includes('m-settings'), 'no project switcher outside project screens');
    const project = rail.renderPhoneBar({ ...options, board });
    assert.match(project, /Project: Atlas. Switch project/);
    assert.match(project, /aria-label="Settings"/);
    assert.ok(!rail.renderPhoneBar({ can_admin: false }).includes('#/admin'));
    break;
  }
  case 'counts': {
    const html = rail.renderRail(options);
    assert.match(html, /Bots <span class="count need">2<span class="sr"> waiting on you<\/span>/);
    assert.match(html, />3<span class="sr"> items<\/span>/);
    for (const none of [undefined, 0, -1, 1.5, '3', NaN]) {
      const empty = rail.renderRail({ ...options, botsWaiting: none });
      assert.doesNotMatch(empty.match(/<a class="nav" href="#\/bots"[\s\S]*?<\/a>/)[0], /class="count/, String(none));
      assert.doesNotMatch(rail.renderPhoneTabs({ botsWaiting: none }), /class="count/, String(none));
    }
    assert.match(rail.renderPhoneTabs({ botsWaiting: 4 }), /Bots <span class="count need">4</);
    assert.equal(rail.projectSubline(board), '2 agents working');
    assert.equal(rail.projectSubline({ agents_working: 1 }), '1 agent working');
    assert.equal(rail.projectSubline({ counts: { doing: 4 } }), '4 doing');
    assert.equal(rail.projectSubline({}), 'Idle');
    assert.ok(!rail.renderRail().includes('class="count need"'));
    break;
  }
  case 'current': {
    for (const view of ['bots', 'memory']) {
      for (const html of [rail.renderRail({ ...options, view }), rail.renderPhoneTabs({ ...options, view })]) {
        const active = links(html).filter(a => a.includes('aria-current="page"'));
        assert.equal(active.length, 1, view);
        assert.ok(active[0].includes(`#/${view}`));
      }
    }
    for (const view of ['board', 'chat', 'settings', 'space', 'needs', 'console', 'page']) {
      assert.equal(links(rail.renderRail({ ...options, view })).filter(a => a.includes('aria-current="page"')).length, 0, view);
      assert.equal(links(rail.renderPhoneTabs({ ...options, view })).filter(a => a.includes('aria-current="page"')).length, 0, view);
    }
    break;
  }
  case 'menu': {
    const html = rail.renderRail(options);
    for (const route of ['rules', 'console', 'settings', 'needs', 'boards']) assert.ok(html.includes(`href="#/${route}"`));
    for (const action of ['logout', 'palette', 'shortcuts', 'switch-workspace']) assert.ok(html.includes(`data-action="${action}"`));
    assert.match(html, /<details class="user-menu"><summary/);
    assert.match(html, /Workspace settings/);
    assert.match(html, /Control\+K Meta\+K/);
    assert.match(html, /Shortcuts <kbd>\?<\/kbd>/);
    assert.match(css, /\.m-tabs \{ display: grid;/);
    // Admin keeps its permission rule: only an exact true shows it.
    assert.equal((rail.renderRail({ can_admin: true }).match(/href="#\/admin"/g) || []).length, 1);
    for (const can_admin of [false, undefined, 'true']) assert.doesNotMatch(rail.renderRail({ can_admin }), /href="#\/admin"/);
    break;
  }
  case 'nostyle': {
    // Rail styles live in the shared stylesheet; re-rendering the rail must not add a copy.
    assert.doesNotMatch(rail.renderRail(options), /<style>/);
    assert.doesNotMatch(rail.renderPhoneBar(options) + rail.renderPhoneTabs(options), /<style>/);
    for (const rule of ['.cmdk {', '.m-top {', '.m-tabs {', '.rail .user-links a {']) assert.ok(css.includes(rule), rule);
    break;
  }
  case 'rules': {
    const html = rail.renderRail({ ...options, rulesWaiting: 2 });
    const rules = html.match(/<a href="#\/rules">[\s\S]*?<\/a>/), summary = html.match(/<summary[\s\S]*?<\/summary>/);
    assert.ok(rules && summary, 'Rules entry or user menu missing');
    assert.match(rules[0], />2</);
    assert.match(summary[0], />2</);
    const none = rail.renderRail({ ...options, rulesWaiting: 0 });
    assert.doesNotMatch(none.match(/<summary[\s\S]*?<\/summary>/)[0], /class="count/);
    assert.doesNotMatch(none.match(/<a href="#\/rules">[\s\S]*?<\/a>/)[0], /class="count/);
    break;
  }
  case 'escaping': {
    const bad = '<img src=x onerror="bad()">';
    const b = { slug: 'a/"<>', name: bad };
    for (const html of [rail.renderRail({ workspace: bad, user: bad }), rail.renderPhoneBar({ board: b }), rail.renderPhoneTabs({ view: bad, botsWaiting: 1 })]) {
      assert.ok(!html.includes('<img'));
    }
    assert.ok(rail.renderRail({ workspace: bad, user: bad }).includes('&lt;'));
    assert.ok(rail.renderPhoneBar({ board: b }).includes('&lt;'));
    break;
  }
  default: assert.fail('Unknown case');
}
