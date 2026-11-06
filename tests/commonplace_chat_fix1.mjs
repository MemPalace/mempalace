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
  const ui = await import('../commonplace/web/static/chat.js');
  assert.match(source, /state.me = page.me/);
  const html = ui.chatHTML({messages:[{id:1, actor:'user:me',text:'hello',at:''}],linked:[{message_id:1,number:42}]}, {user:'me'}, String);
  assert.match(html, /You/);
  assert.match(html, /#\/card\/42/);
});

test('RED chat imports the shared state derivation', async () => {
  const source = await readFile('commonplace/web/static/chat.js', 'utf8');
  assert.match(source, /import\s*\{[^}]*deriveChatState[^}]*seenStore[^}]*\}\s*from\s*["']\.\/chatstate\.js["']/);
  assert.match(source, /deriveChatState\(state.page/);
});

test('RED quick reply references resolve only cards on this board', async () => {
  const ui = await import('../commonplace/web/static/chat.js');
  assert.equal(typeof ui.chatHTML, 'function');
  const html = ui.chatHTML({
    project: {slug:'atlas', name:'Atlas'}, messages:[{id:1, actor:'user:me',text:'Question',at:''}],
    cards:[{number:121,title:'Same board',board:'atlas'},{number:122,title:'Wrong board',board:'harbor'},{number:123,title:'Unmentioned',board:'atlas'}],
    replies:[{id:1,message_id:1,kind:'answer',model:'anthropic/fast',text:'Check #121, A-121, H-122, #122, and A-999.'}]
  }, {user:'me'}, String);
  assert.match(html, /Cards in this answer/);
  assert.equal((html.match(/data-chat-card="121"/g) || []).length, 1);
  assert.doesNotMatch(html, /data-chat-card="(?:122|123|999)"/);
});

test('RED From this request groups only cards with this message_id', async () => {
  const ui = await import('../commonplace/web/static/chat.js'); assert.equal(typeof ui.chatHTML,'function');
  const html = ui.chatHTML({project:{slug:'work',name:'Work'}, messages:[{id:1,actor:'user:me',text:'Exact request'}],linked:[{number:42,message_id:1,title:'Made'},{number:43,message_id:2,title:'Unrelated'}]}, {user:'me'}, String);
  assert.match(html, /role="group" aria-labelledby="request-1"/);
  assert.match(html, /From this request · on the board now/);
  assert.match(html, /W-42/); assert.doesNotMatch(html, /Unrelated/);
});

test('RED writer labels use people and orchestrator identity', async () => {
  const ui = await import('../commonplace/web/static/chat.js'); assert.equal(typeof ui.chatHTML,'function');
  const {setPeople} = await import('../commonplace/web/static/api.js');
  setPeople({'user:leon':'Leon'});
  const html = ui.chatHTML({project:{slug:'work',name:'Work',coordinator:'conn:main'}, messages:[{id:1,actor:'user:leon',text:'First'},{id:2,actor:'conn:main',text:'Second'},{id:3,actor:'conn:Iris',text:'Third'}]}, {user:'me'}, String);
  assert.match(html, /Work orchestrator/); assert.match(html, /Iris \(agent\)/); assert.match(html, /Leon/);
  assert.doesNotMatch(html, /conn:|user:/); setPeople({});
});

test('RED unread divider is text as well as a line', async () => {
  const ui = await import('../commonplace/web/static/chat.js'); assert.equal(typeof ui.chatHTML,'function');
  const html = ui.chatHTML({seen:{message:1,reply:0},messages:[{id:1,actor:'user:me',text:'Request'},{id:2,actor:'conn:agent',text:'Response'},{id:3,actor:'conn:agent',text:'More'}]}, {user:'me'}, String);
  assert.equal((html.match(/New since you last looked/g) || []).length,1);
  assert.ok(html.indexOf('Request') < html.indexOf('New since')); assert.ok(html.indexOf('New since') < html.indexOf('Response'));
});

test('RED empty pane offers three Try asking chips', async () => {
  const ui = await import('../commonplace/web/static/chat.js'); assert.equal(typeof ui.chatHTML,'function');
  const html = ui.chatHTML({messages:[]},{user:'me'},String);
  assert.match(html,/Try asking/); assert.equal((html.match(/data-chat-suggest=/g) || []).length,3);
});
