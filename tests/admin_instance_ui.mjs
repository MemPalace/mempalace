import assert from 'node:assert/strict';
let source=''; for await (const part of process.stdin) source+=part;
const {createAdmin}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const flush=async()=>{for(let i=0;i<35;i++)await Promise.resolve();};
const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fixture(instance=true){
 let me={can_admin:true,can_admin_instance:instance,workspace:'synthetic',workspace_name:'Example'},id=0;
 const timers=new Map(),requests=[],listeners=new Map();
 const mount={innerHTML:'',addEventListener(k,f){listeners.set(k,f);},removeEventListener(k){listeners.delete(k);}};
 const ui=createAdmin({mount:()=>mount,current:()=>me,e,timers:{setTimeout(fn,ms){assert.equal(ms,5000);timers.set(++id,fn);return id;},clearTimeout(id){timers.delete(id);}},api(path,options){let resolve,reject;const p=new Promise((a,b)=>{resolve=a;reject=b;});requests.push({path,options,resolve,reject});return p;}});
 const req=path=>requests.filter(r=>r.path===path).at(-1);
 return {ui,mount,requests,req,setMe(v){me=v;},expire(){for(const fn of [...timers.values()])fn();},click(key){listeners.get('click')({target:{closest:()=>({dataset:{adminSection:key,adminAction:'retry'}})}});}};
}
const deploy={commonplace_version:'0.1.0',mempalace_version:'3.12.0',commit:null,commit_source:'unknown'};
async function main(){
 const f=fixture(); f.ui.show();
 assert.ok(f.req('/api/admin/instance/deploy'),'Instance owner must load deployed identity');
 assert.ok(f.req('/healthz'),'Instance owner must load health independently');
 assert.ok(f.req('/api/admin/instance/backups'),'Instance owner must load receipts');
 f.req('/api/admin/instance/deploy').resolve({...deploy,commonplace_version:'<img src=x>'});await flush();
 assert.doesNotMatch(f.mount.innerHTML,/<img/);assert.match(f.mount.innerHTML,/&lt;img/);
 assert.match(f.mount.innerHTML,/Running commit unknown/);assert.match(f.mount.innerHTML,/3\.12\.0/);
 if(process.argv[2].startsWith('race-')){
   const [,mode,outcome]=process.argv[2].split('-');
   f.req('/api/admin/instance/backups').resolve({online:{state:'unknown',reason:'no_record'},offline:{state:'unknown',reason:'no_record'}});await flush();
   void f.ui.refreshSection('backup-'+mode);const old=f.req('/api/admin/instance/backups');
   void f.ui.refreshSection('backups');const parent=f.req('/api/admin/instance/backups');
   const ready={state:'ready',result:'success',stage:'complete',started_at:'2026-11-07T12:00:00Z',finished_at:'2026-11-07T12:00:00Z'};
   parent.resolve({online:ready,offline:ready});await flush();
   if(outcome==='resolve')old.resolve({[mode]:{state:'error',error:'stale'}});
   else old.reject({status:403});
   await flush();
   assert.doesNotMatch(f.mount.innerHTML,/Backup status unavailable|Instance administrator access required/,'Older mode completion must not replace newer parent data');
   assert.equal((f.mount.innerHTML.match(/data-label="Result">success/g)||[]).length,2);
   assert.ok(old.options.signal.aborted,'The parent refresh cancels the superseded mode request');
   const before=f.requests.length;void f.ui.refreshSection('backup-'+mode);
   assert.equal(f.requests.length,before+1,'The superseded request must not leave a busy mode');
   f.ui.stop();await flush();return;
 }
 if(process.argv[2]==='health'){
   f.req('/healthz').resolve({ok:false});await flush();
   assert.match(f.mount.innerHTML,/Health unavailable\. Retry\./);assert.match(f.mount.innerHTML,/3\.12\.0/);
   f.click('health');await flush();f.req('/healthz').resolve({ok:true});await flush();
   assert.match(f.mount.innerHTML,/Gateway reachable/);
   void f.ui.refreshSection('health'); // starts a pending body; awaiting would hang
 }else{
   f.req('/healthz').resolve({ok:true});
   f.req('/api/admin/instance/backups').resolve({online:{state:'unknown',reason:'no_record'},offline:{state:'error',error:'<img src=x onerror=alert(1)>'}});await flush();
   assert.equal((f.mount.innerHTML.match(/<h2>/g)||[]).length,7);
   assert.match(f.mount.innerHTML,/Last completed recorded attempt/);assert.match(f.mount.innerHTML,/No recorded attempt/);
   assert.doesNotMatch(f.mount.innerHTML,/<img|onerror="|href="http|token=/);
   const before=f.requests.length;f.click('backup-offline');await flush();
   assert.equal(f.requests.length,before+1);assert.equal(f.requests.at(-1).path,'/api/admin/instance/backups');
   f.requests.at(-1).resolve({online:{state:'unknown',reason:'no_record'},offline:{state:'unknown',reason:'no_record'}});await flush();
   assert.doesNotMatch(f.mount.innerHTML,/Backup status unavailable/);
 }
 f.ui.stop();
 const timeout=fixture();timeout.ui.show();timeout.req('/api/admin/instance/deploy').resolve(deploy);await flush();timeout.expire();await flush();
 assert.match(timeout.mount.innerHTML,/Health timed out\. Retry\./);assert.match(timeout.mount.innerHTML,/3\.12\.0/);
 timeout.req('/healthz').resolve({ok:true});await flush();assert.doesNotMatch(timeout.mount.innerHTML,/Gateway reachable/);timeout.ui.stop();
 const denied=fixture(false);denied.ui.show();assert.equal(denied.requests.length,6);assert.match(denied.mount.innerHTML,/Instance administrator access required/);denied.ui.stop();
 const revoked=fixture();revoked.ui.show();revoked.req('/api/admin/instance/deploy').resolve(deploy);await flush();revoked.req('/api/admin/instance/backups').reject({status:403});await flush();
 assert.match(revoked.mount.innerHTML,/Instance administrator access required/);assert.doesNotMatch(revoked.mount.innerHTML,/3\.12\.0/);revoked.ui.stop();
 assert.ok(f.requests.every(r=>!r.options.method||r.options.method==='GET'));
}
try{await main();}catch(error){console.error(error.stack);process.exitCode=error instanceof assert.AssertionError?42:1;}
