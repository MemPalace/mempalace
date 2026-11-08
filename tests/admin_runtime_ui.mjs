import assert from 'node:assert/strict';
let source='';for await(const p of process.stdin) source+=p;
const {createAdmin}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
const escape=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const section=(html,key)=>html.match(new RegExp(`<section[^>]*data-admin-section="${key}"[^>]*>[\\s\\S]*?<\\/section>`))?.[0]||'';
let me={workspace:'ws-one',can_admin:true};
const requests=[],listeners=new Map(),timers=new Map();let id=0;
const host={innerHTML:'',addEventListener:(k,f)=>listeners.set(k,f),removeEventListener:k=>listeners.delete(k)};
const api=(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
const ui=createAdmin({mount:()=>host,api,e:escape,current:()=>me,timers:{setTimeout(fn,ms){timers.set(++id,{fn,ms});return id;},clearTimeout(i){timers.delete(i);}}});
const find=key=>requests.findLast(r=>r.path.includes('/'+key));
const click=(key,action='retry',list)=>listeners.get('click')({target:{closest:()=>({dataset:{adminSection:key,adminAction:action,adminList:list}})}});
const stuck=(cards=[],more=false)=>({workspace_id:'ws-one',cards,count:102,reason_counts:{expired_lease:102},has_more:more,next_cursor:more?'opaque-stuck':null});
const card=n=>({number:n,board:'alpha',title:`Card <${n}>`,status:'doing',reasons:['expired_lease'],active_blockers:[],expired_jobs:[],open_url:`#/card/${n}`});
try {
  globalThis.document=new EventTarget();ui.show();
  assert.ok(find('needs-me'),'Admin must request the existing owner queue directly');
  assert.ok(find('runtime')&&find('limits')&&find('stuck'),'All S2 sources load independently');
  assert.equal(requests.length,6);
  assert.ok(!requests.some(r=>/instance|health|backups|deploy/.test(r.path)));
  if(process.argv[2]==='queue') {
    const c={number:7,board:'alpha',title:'Decision <now>',body:'BODY MUST STAY HIDDEN',url:'https://example.invalid/never-use'};
    find('needs-me').resolve({count:1,asks:[c],done_review:[c],approvals:[],approvals_truncated:false});
    find('stuck').resolve(stuck([card(8)]));await flush();
    assert.match(host.innerHTML,/1 card waiting/,'Use the unique server count');
    assert.match(host.innerHTML,/href="#\/card\/7"/);assert.doesNotMatch(host.innerHTML,/BODY MUST STAY HIDDEN|example.invalid|<now>/);
    assert.match(host.innerHTML,/Decision &lt;now&gt;/);
    ui.refreshSection('needs');find('needs-me').reject(new Error('private-canary'));await flush();
    assert.match(host.innerHTML,/Needs-me unavailable\. Retry\./);
    assert.match(host.innerHTML,/Card &lt;8&gt;/);assert.doesNotMatch(host.innerHTML,/private-canary/);
    click('needs');find('needs-me').resolve({count:0});await flush();
    assert.match(host.innerHTML,/No cards waiting/);
  } else if(process.argv[2]==='labels') {
    find('runtime').resolve({workspace_id:'ws-one',orchestration:{state:'ready',enabled:true},orchestrators:{state:'ready',items:[{id:1,name:'Alpha',source:'coordinator',connected:true,activity:'working'},{id:2,name:'Beta',source:'seat',connected:false},{id:3,name:'Gamma',source:'not_configured',connected:false}],count:3},machines:{state:'ready',items:[],count:0}});
    find('limits').resolve({workspace_id:'ws-one',projects:[{id:1,name:'Alpha',counting_mode:'distinct_doing_agents'},{id:2,name:'Beta',counting_mode:'orchestration_slots'}],machines:[]});await flush();
    const html=section(host.innerHTML,'runtime');
    assert.match(html,/Not connected/);assert.match(html,/>Connected</);
    assert.match(html,/Activity records token use, not a running process/);
    assert.match(html,/Coordinator/);assert.match(html,/Stored seat/);assert.match(html,/Not configured/);
    assert.doesNotMatch(host.innerHTML,/distinct_doing_agents|orchestration_slots|not_configured/);
    assert.match(host.innerHTML,/Distinct doing agents/);assert.match(host.innerHTML,/Orchestration slots/);
  } else if(process.argv[2]==='page-error') {
    const payload={workspace_id:'ws-one',orchestration:{state:'ready',enabled:true},orchestrators:{state:'ready',items:[{id:1,name:'Existing project'}],count:101,has_more:true,next_cursor:'page-two'},machines:{state:'ready',items:[],count:0}};
    find('runtime').resolve(payload);await flush();
    click('runtime','more','orchestrators');const page=find('runtime');
    page.resolve({...payload,orchestrators:{state:'error',error:'Orchestrators unavailable; try again'}});await flush();
    let html=section(host.innerHTML,'runtime');
    assert.match(html,/Existing project/);assert.match(html,/Orchestrators unavailable; try again/);
    assert.doesNotMatch(html,/Runtime unavailable\. Retry/);
    click('runtime');assert.equal(find('runtime').path,page.path);
    find('runtime').resolve({...payload,orchestrators:{state:'ready',items:[{id:2,name:'Next project'}],count:101,has_more:false}});await flush();
    html=section(host.innerHTML,'runtime');assert.match(html,/Existing project/);assert.match(html,/Next project/);
    assert.doesNotMatch(html,/Orchestrators unavailable/);
  } else {
    find('runtime').resolve({workspace_id:'ws-one',orchestration:{state:'ready',enabled:true},orchestrators:{state:'ready',items:[],count:0,has_more:false},machines:{state:'error',error:'Machines unavailable; try again'}});
    find('stuck').resolve(stuck([card(1)],true));await flush();
    assert.match(section(host.innerHTML,'runtime'),/Orchestration enabled/);
    assert.match(section(host.innerHTML,'runtime'),/Machines unavailable/);
    assert.match(section(host.innerHTML,'runtime'),/Retry/);
    assert.match(section(host.innerHTML,'stuck'),/Load more/);
    const before=requests.length;click('stuck','more');assert.equal(requests.length,before+1);
    assert.match(find('stuck').path,/\/workspaces\/ws-one\/stuck\?cursor=opaque-stuck/);
    const failedPage=find('stuck');failedPage.reject(new Error('page unavailable'));await flush();click('stuck');
    assert.equal(find('stuck').path,failedPage.path,'Retry preserves the failed cursor');
    find('stuck').resolve(stuck([card(1),card(2)]));await flush();
    assert.equal((host.innerHTML.match(/href="#\/card\/1"/g)||[]).length,1);
    assert.match(host.innerHTML,/Card &lt;2&gt;/);
    click('runtime');assert.equal(requests.length,before+3);
    find('runtime').resolve({workspace_id:'ws-one',orchestration:{state:'ready',enabled:false},orchestrators:{state:'ready',items:[],count:0},machines:{state:'ready',items:[{id:'mach-1',label:'<Machine>',enabled:true,verified:false,binding_state:'owner_attested',activity:'working',worker_activity:'unused'}],count:1}});await flush();
    assert.match(host.innerHTML,/recent contact/);assert.match(host.innerHTML,/not verified/i);assert.match(host.innerHTML,/&lt;Machine&gt;/);
    assert.ok(requests.every(r=>!r.options.method||r.options.method==='GET'));
    ui.refreshSection('stuck');const pending=find('stuck');document.dispatchEvent(new Event('signout'));pending.resolve(stuck([card(9)]));await flush();
    assert.equal(host.innerHTML,'');assert.equal(timers.size,0);
  }
  ui.stop();await flush();assert.equal(timers.size,0);
} catch(e) {console.error(e.stack);process.exitCode=e.code==='ERR_ASSERTION'?42:2;}
