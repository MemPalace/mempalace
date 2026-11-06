import assert from 'node:assert/strict';
const b = await import('../commonplace/web/static/board.js');
for (const name of ['cardId', 'isBlocked', 'cardTags', 'boardView', 'filterCards', 'addCardForm', 'submitCard', 'bindBoard']) assert.equal(typeof b[name], 'function');
const board = {name:'Atlas', slug:'atlas'}, me = 'user:maya';
const card = {number:121, title:'Sync', status:'review', ask:'Which limit?', ask_to:'user:leon'};
const render = (cards, options = {}) => b.boardView({board, me, cards, ...options});
switch (process.argv[2]) {
case 'ids':
  assert.equal(b.cardId(board,121),'A-121'); assert.equal(b.cardId({name:'Élan'},2),'E-2'); assert.equal(b.cardId({name:'123',slug:'harbor'},3),'H-3'); assert.equal(b.cardId({name:'日本'},1),'C-1'); break;
case 'blocked':
  assert.equal(b.isBlocked(card,me),true);
  assert.equal(b.isBlocked({...card,status:'doing'},me),false);
  assert.equal(b.isBlocked({...card,ask:null},me),false);
  assert.equal(b.isBlocked({...card,review_kind:'completion'},me),false);
  assert.ok(b.cardTags({...card,ask_to:me},{me}).includes('Needs me'));
  assert.ok(b.cardTags({...card,review_kind:'completion',return_to:'conn:Iris'},{me}).includes('Reviewer: leon'));
  assert.ok(b.cardTags({...card,return_to:'conn:Iris'},{me}).includes('Built by Iris (agent)')); break;
case 'chat': {
  const at = '2026-11-05T14:03:00', tags = b.cardTags({...card,message_id:10,created_at:at},{messages:[{id:10,at}]});
  assert.ok(tags.includes('From chat · 14:03')); assert.ok(!render([{...card,starts_after:118}]).includes('Starts after')); assert.ok(!render([card]).includes('Linked in chat')); break;
}
case 'columns': {
  const html = render([card]); for (const s of ['todo','doing','review','done']) assert.ok(html.includes(`id="col-${s}"`));
  assert.ok(html.includes('Jump to a column')); assert.ok(html.includes('Blocked 1')); assert.ok(html.includes('data-card="121"')); assert.ok(!html.includes('Due')); break;
}
case 'backlog': {
  const html = render([{...card,status:'backlog'}]); assert.ok(html.includes('<summary>Backlog (1)</summary>'));
  assert.ok(!html.includes('<details class="backlog" open')); assert.ok(html.indexOf('Backlog (1)') > html.indexOf('id="col-done"')); assert.ok(html.includes('href="#/card/121"')); break;
}
case 'assignee': {
  const html = b.addCardForm({people:[{ref:'user:leon',name:'Leon'}]}); assert.ok(html.includes('name="assignee"')); assert.ok(html.includes('value="user:leon"'));
  const calls = []; await b.submitCard({board, values:{title:'Test',assignee:'user:leon',body:'Exact\nwords',label:'copy'},api:async (...args) => {calls.push(args);return {number:1};}});
  assert.equal(calls.length,1); assert.equal(calls[0][0],'/api/boards/atlas/cards'); assert.equal(calls[0][1].body.assignee,'user:leon'); assert.equal(calls[0][1].body.body,'Exact\nwords'); assert.deepEqual(calls[0][1].body.labels,['copy']); break;
}
case 'filters': {
  const cards = [card,{...card,number:2,assignee:me,ask_to:me},{...card,number:3,assignee:'conn:Iris',status:'doing'}];
  assert.deepEqual(b.filterCards(cards,'mine',me).map(c=>c.number),[2]); assert.deepEqual(b.filterCards(cards,'agents',me).map(c=>c.number),[3]); assert.deepEqual(b.filterCards(cards,'blocked',me).map(c=>c.number),[121]); break;
}
case 'empty': assert.ok(render([]).includes('Add your first card')); assert.ok(render([]).includes('Connect an agent')); assert.ok(render([]).includes('Ask for something')); assert.ok(b.cardTags({status:'todo'}).includes('Available for an agent')); assert.ok(!b.cardTags({status:'doing',assignee:'conn:Iris',need:'idle'}).some(t=>t.startsWith('Working'))); break;
case 'escaping': {
  const html = render([{...card,title:'<script>bad</script>',ask:'<img>',labels:['<label>']}]); assert.ok(!html.includes('<script>')); assert.ok(html.includes('&lt;script&gt;')); assert.ok(html.includes('&lt;img&gt;')); break;
}
case 'roving': {
  // One tab stop per column: the selected card, else the first.
  const cards = [{...card,number:1,status:'todo'},{...card,number:2,status:'todo'},{...card,number:3,status:'doing'}];
  const stops = [...render(cards,{selected:2}).matchAll(/<article [^>]*data-card="(\d+)"[^>]*tabindex="(-?\d)"/g)].map((m) => [m[1],m[2]]);
  assert.deepEqual(stops,[['1','-1'],['2','0'],['3','0']]);
  const handlers = {}, groups = [];
  const root = {addEventListener:(type,fn) => { handlers[type] = fn; }, removeEventListener() {}, querySelectorAll:() => groups};
  const make = (n) => ({dataset:{card:String(n)}, tabIndex:-1, focused:false, getClientRects:() => [{}], focus() { this.focused = true; }, closest(s) { return s === '.cards' ? this.group : s === '.card[data-card]' ? this : null; }});
  const todo = [make(1),make(2)], doing = [make(3)];
  for (const list of [todo,doing]) { const group = {querySelectorAll:() => list}; list.forEach((c) => { c.group = group; }); groups.push(group); }
  todo[0].tabIndex = 0; doing[0].tabIndex = 0;
  const selected = [], opened = [];
  b.bindBoard(root,{api() {}, board, onSelect:(n) => selected.push(n), onOpen:(n) => opened.push(n)});
  assert.equal(typeof handlers.keydown,'function','Board has no keyboard handler');
  const press = (target,key) => { let prevented = false; handlers.keydown({target,key,preventDefault() { prevented = true; }}); return prevented; };
  assert.ok(press(todo[0],'ArrowDown')); assert.ok(todo[1].focused); assert.deepEqual([todo[0].tabIndex,todo[1].tabIndex],[-1,0]); assert.deepEqual(selected,[2]);
  assert.ok(press(todo[1],'ArrowRight')); assert.ok(doing[0].focused); assert.deepEqual(selected,[2,3]);
  assert.ok(press(doing[0],'ArrowLeft')); assert.deepEqual(selected,[2,3,2]);
  assert.ok(!press(todo[1],'ArrowDown')); assert.deepEqual(selected,[2,3,2]);
  assert.ok(press(todo[1],'Enter')); assert.ok(press(todo[0],' ')); assert.deepEqual(opened,[2,1]);
  // Keys on a control inside a card belong to that control.
  assert.ok(!press({closest:() => todo[0]},'Enter')); assert.deepEqual(opened,[2,1]);
  break;
}
case 'chat-time': {
  // Without its source message the tag has no time; the card's creation time is never shown.
  const tags = b.cardTags({...card,message_id:10,created_at:'2026-11-05T14:04:00'},{messages:[]});
  assert.ok(tags.includes('From chat')); assert.ok(!tags.some((t) => /\d\d:\d\d/.test(t)));
  assert.equal(typeof b.loadChatTimes,'function','loadChatTimes missing');
  const calls = [];
  const found = await b.loadChatTimes({board, cards:[{...card,message_id:10},{...card,number:2}], api:async (url) => { calls.push(url); return {board:'atlas',messages:[{id:10,at:'2026-11-05T14:03:00'}]}; }});
  assert.deepEqual(calls,['/api/chat/atlas?before=11&last=1']);
  assert.ok(b.cardTags({...card,message_id:10,created_at:'2026-11-05T14:04:00'},{messages:found}).includes('From chat · 14:03'));
  assert.deepEqual(await b.loadChatTimes({board, cards:[{...card,message_id:10}], api:async () => ({board:'harbor',messages:[{id:10,at:'2026-11-05T14:03:00'}]})}),[]);
  assert.deepEqual(await b.loadChatTimes({board, cards:[{...card,message_id:10}], api:async () => { throw new Error('gone'); }}),[]);
  break;
}
case 'drag': assert.match(render([card]),/<article [^>]*draggable="true"/); break;
default: assert.fail('Unknown case');
}
