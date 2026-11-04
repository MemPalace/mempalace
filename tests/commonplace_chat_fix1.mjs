import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
test('app route passes the complete project slug', async () => {
  const app = await readFile('commonplace/web/static/app.js', 'utf8');
  const expression = app.match(/decodeURIComponent\((state\.base\.slice\(\d+\))\)/g)?.find(x => x.includes('slice(7)'));
  assert.ok(expression);
  const parse = new Function('state', `return ${expression}`);
  assert.equal(parse({base:'#/chat/work'}), 'work');
});
test('chat consumes me and renders linked cards', async () => {
  const source = await readFile('commonplace/web/static/chat.js', 'utf8');
  const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  assert.match(source, /state.me = page.me/);
  const html = ui.chatHTML({messages:[{id:1, actor:'user:me',text:'hello',at:''}],linked:[{message_id:1,number:42}]}, {user:'me'}, String);
  assert.match(html, /You/);
  assert.match(html, /#\/card\/42/);
});
