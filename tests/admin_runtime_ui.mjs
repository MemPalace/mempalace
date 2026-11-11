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
  } else if(process.argv[2]==='requesters') {
    const machines=(requesters)=>({state:'ready',count:2,has_more:false,requester_candidates:[{id:'conn_a',name:'planner <a>'},{id:'conn_b',name:'planner-b'}],items:[{id:'mach_1',label:'<Machine>',enabled:true,binding_state:'unbound',max_in_flight:1,max_starts_per_hour:2,grant_version:7,requesters},{id:'mach_2',label:'Gone',enabled:false,revoked_at:'2026-01-01T00:00:00Z',binding_state:'unbound',requesters:[]}]});
    const payload=requesters=>({workspace_id:'ws-one',orchestration:{state:'ready',enabled:true},orchestrators:{state:'ready',items:[],count:0},machines:machines(requesters)});
    const capped=machines([{id:'conn_a',name:'planner <a>',state:'active'}]);capped.items[0].requesters_total=40;capped.requester_candidates_more=true;
    find('runtime').resolve(payload([{id:'conn_a',name:'planner <a>',state:'active'},{id:'conn_old',name:'retired',state:'inactive'}]));await flush();
    let html=section(host.innerHTML,'runtime');
    assert.match(html,/planner &lt;a&gt;/);assert.match(html,/retired[^<]*inactive/);assert.doesNotMatch(html,/<a>/);
    assert.match(html,/data-admin-action="requester-add"[^>]*data-admin-requester="conn_b"/);
    assert.doesNotMatch(html,/data-admin-action="requester-add"[^>]*data-admin-requester="conn_a"/,'a chosen requester is not offered again');
    assert.doesNotMatch(html,/data-admin-machine="mach_2"/,'a revoked machine has no controls');
    const write=(action,machine,requester)=>listeners.get('click')({target:{closest:()=>({dataset:{adminSection:'runtime',adminAction:action,adminMachine:machine,adminRequester:requester}})}});
    const writes=()=>requests.filter(r=>r.options?.method&&r.options.method!=='GET');
    write('requester-add','mach_1','conn_b');
    assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/runners/mach_1/requesters');assert.equal(writes()[0].options.method,'POST');
    assert.deepEqual(writes()[0].options.body,{requester:'conn_b'},'an add names one agent, never the list the page shows');
    assert.equal(writes()[0].options.headers['If-Match'],'7','an add carries the grant version the page read');
    write('requester-add','mach_1','conn_b');assert.equal(writes().length,1,'one write per machine at a time');
    writes()[0].resolve({});await flush();
    const reload=requests.filter(r=>r.path.includes('/runtime')).length;assert.equal(reload,2,'the list is read again after a save');
    find('runtime').resolve(payload([{id:'conn_a',name:'planner <a>',state:'active'},{id:'conn_old',name:'retired',state:'inactive'},{id:'conn_b',name:'planner-b',state:'active'}]));await flush();
    assert.match(section(host.innerHTML,'runtime'),/Requesters saved/);
    write('requester-remove','mach_1','conn_old');
    assert.equal(writes()[1].path,'/api/runners/mach_1/requesters/conn_old');assert.equal(writes()[1].options.method,'DELETE');
    assert.equal(writes()[1].options.body,undefined);assert.equal(writes()[1].options.headers['If-Match'],'7');
    writes()[1].reject(Object.assign(new Error('Request failed (400)'),{status:400,payload:{error:'private detail'}}));await flush();
    html=section(host.innerHTML,'runtime');
    assert.match(html,/Could not save requesters/);assert.doesNotMatch(html,/private detail/);
    // Another owner edited first: the server answers 409, the page says so and reads the list again.
    find('runtime').resolve(payload([{id:'conn_a',name:'planner <a>',state:'active'}]));await flush();
    const reads=requests.filter(r=>r.path.includes('/runtime')).length;
    write('requester-add','mach_1','conn_b');writes()[2].reject(Object.assign(new Error('conflict'),{status:409,payload:{error:'private detail'}}));await flush();
    html=section(host.innerHTML,'runtime');
    assert.match(html,/changed since the page loaded/);assert.doesNotMatch(html,/Requesters saved|private detail/);
    assert.equal(requests.filter(r=>r.path.includes('/runtime')).length,reads+1,'a conflict reloads the list');
    find('runtime').resolve(payload([{id:'conn_a',name:'planner <a>',state:'active'}]));await flush();
    // Adding one that is already stored (hidden by the cap) is not reported as saved.
    write('requester-add','mach_1','conn_b');writes()[3].resolve({changed:false});await flush();
    assert.match(section(host.innerHTML,'runtime'),/Already a requester/);assert.doesNotMatch(section(host.innerHTML,'runtime'),/Requesters saved/);
    find('runtime').resolve(payload([{id:'conn_a',name:'planner <a>',state:'active'}]));await flush();
    find('runtime');ui.refreshSection('runtime');find('runtime').resolve({...payload([{id:'conn_a',name:'planner <a>',state:'active'}]),machines:capped});await flush();
    html=section(host.innerHTML,'runtime');assert.match(html,/Showing 1 of 40 requesters/);assert.match(html,/Showing the first 2 trusted agents/);
    // Fail closed: once the session is no longer an owner, nothing is written.
    me={...me,can_admin:false};const before=writes().length;write('requester-remove','mach_1','conn_a');assert.equal(writes().length,before);
  } else if(process.argv[2]==='requesters-chain') {
    // Three edits in a row without a reload; the reload that is in flight lands before or after the second edit.
    const runtime=(version,requesters)=>({workspace_id:'ws-one',orchestration:{state:'ready',enabled:true},orchestrators:{state:'ready',items:[],count:0},machines:{state:'ready',count:1,has_more:false,requester_candidates:[{id:'conn_a',name:'a'},{id:'conn_b',name:'b'},{id:'conn_c',name:'c'}],items:[{id:'mach_1',label:'M',enabled:true,binding_state:'unbound',max_in_flight:1,max_starts_per_hour:2,grant_version:version,requesters}]}});
    const write=(action,requester)=>listeners.get('click')({target:{closest:()=>({dataset:{adminSection:'runtime',adminAction:action,adminMachine:'mach_1',adminRequester:requester}})}});
    const writes=()=>requests.filter(r=>r.options?.method&&r.options.method!=='GET');
    const reads=()=>requests.filter(r=>r.path.includes('/runtime'));
    const sent=()=>writes().map(w=>w.options.headers['If-Match']);
    const seen=[{id:'conn_a',name:'a',state:'active'}];
    find('runtime').resolve(runtime(7,[]));await flush();
    // Late: the reload started after edit 1 lands after edit 2 completed (it still reads version 8).
    write('requester-add','conn_a');writes()[0].resolve({changed:true,grant_version:8});await flush();
    assert.equal(reads().length,2);
    write('requester-add','conn_b');writes()[1].resolve({changed:true,grant_version:9});await flush();
    reads()[1].resolve(runtime(8,seen));await flush();
    write('requester-add','conn_c');
    assert.deepEqual(sent(),['7','8','9'],'late reload: the third edit sends the version the second returned');
    writes()[2].reject(Object.assign(new Error('x'),{status:500}));await flush();
    for(const r of reads().slice(2))r.resolve(runtime(9,seen));await flush();
    // Early: the reload lands before edit 2 completes, and a newer reload is pending for edit 3.
    requests.length=0;
    ui.refreshSection('runtime');reads().at(-1).resolve(runtime(7,[]));await flush();
    write('requester-add','conn_a');writes()[0].resolve({changed:true,grant_version:8});await flush();
    write('requester-add','conn_b');
    reads().at(-1).resolve(runtime(8,seen));await flush();
    writes()[1].resolve({changed:true,grant_version:9});await flush();
    write('requester-add','conn_c');
    assert.deepEqual(sent(),['7','8','9'],'early reload: the third edit sends the version the second returned');
    writes()[2].reject(Object.assign(new Error('x'),{status:500}));await flush();
    reads().at(-1).resolve(runtime(9,seen));await flush();
    // Newer: a reload that already shows another session's later edit (version 10) is not rolled back by our answer (8).
    requests.length=0;
    ui.refreshSection('runtime');reads().at(-1).resolve(runtime(7,[]));await flush();
    write('requester-add','conn_a');
    ui.refreshSection('runtime');reads().at(-1).resolve(runtime(10,seen));await flush();
    writes()[0].resolve({changed:true,grant_version:8});await flush();
    write('requester-add','conn_b');
    assert.deepEqual(sent(),['7','10'],'a newer version read by the page is kept');
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
