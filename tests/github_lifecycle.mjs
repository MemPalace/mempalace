import assert from 'node:assert/strict';
import {createNeeds} from '../commonplace/web/static/needs.js';

// The same synthetic roots as the render harness: inspect the controller's
// rendered output without requiring an installed browser or external packages.
function root() {
  return {innerHTML:'', contains:()=>false, addEventListener(){}, removeEventListener(){}, querySelector:()=>null, querySelectorAll:()=>[]};
}
const mount=root(), detailMount=root();
const document={hidden:false, addEventListener(){}, removeEventListener(){}};
let deferred, reads=0;
const card={number:7,version:1,board:'work',title:'Review',need:'merge',actions:[],pr_url:'https://github.com/example/project/pull/1'};
const api=async path=>{
  if(path.startsWith('/api/cards/')) return {...card,number:Number(path.split('/').at(-1))};
  if(path==='/api/github') {
    reads++;
    if(reads===1 || reads===4) return new Promise(resolve=>{deferred=resolve;});
    return {state:reads===2?'connected':'absent',csrf:'synthetic-csrf',login:'synthetic-person'};
  }
  return {holds:[]};
};
const ui=createNeeds({mount,detailMount,document,api,setInterval:()=>1,clearInterval(){}});
const first=ui.select(7); await new Promise(resolve=>setImmediate(resolve));
assert.match(detailMount.innerHTML, /<button[^>]*disabled[^>]*>Loading GitHub…<\/button>/, 'loading disabled');
await ui.select(8);
assert.match(detailMount.innerHTML, /Open PR/, 'positive connected render');
assert.match(detailMount.innerHTML, /href="https:\/\/github.com\/example\/project\/pull\/1"[^>]*rel="noopener noreferrer"/, 'safe PR link');
await ui.select(9);
assert.ok(!detailMount.innerHTML.includes('Open PR'), 'switch clears connected state');
deferred({state:'connected',login:'stale-person'}); await first;
assert.ok(!detailMount.innerHTML.includes('Open PR'), 'late response cannot restore connection');
const pending=ui.select(10); await new Promise(resolve=>setImmediate(resolve));
ui.stop(); assert.equal(detailMount.innerHTML,'', 'logout clears rendered cache');
deferred({state:'connected',login:'logged-out-person'}); await pending;
assert.equal(detailMount.innerHTML,'', 'logout blocks late cache restoration');
