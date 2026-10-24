const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../static/library_updates.js'), 'utf8');
const flush = () => new Promise(setImmediate);
function harness() {
  const box = {hidden:true, innerHTML:''}, calls = [];
  const c = vm.createContext({storageOwner:'user:alice', privateViewEpoch:0, state:{jobs:[]}, chip:'all', query:'',
    document:{hidden:false}, $:()=>box, esc:String, AbortController, toast:()=>{}, render:()=>c.renderLibraryUpdates(),
    fetch:(url, options)=>new Promise((resolve, reject)=>calls.push({url,options,resolve,reject}))});
  vm.runInContext(code,c);
  return {c, box, calls, current:()=>vm.runInContext('libraryUpdates',c)};
}
const answer = (call, items, owner='user:alice') => call.resolve({ok:true,json:async()=>({items,total:items.length,storage_owner:owner})});
(async()=>{
  {
    const h=harness(), pending=h.c.refreshLibraryUpdates();
    assert.equal(h.calls[0].url,'/api/inbox?state=unread&limit=10');
    answer(h.calls[0],[{id:1,job_id:9,title:'New episode',sources:[{title:'Creator'}]}]);await pending;
    assert.equal(h.box.hidden,false);assert.match(h.box.innerHTML,/New episode/);assert.equal(h.c.libraryUpdateFor(9),true);
    await h.c.refreshLibraryUpdates();assert.equal(h.calls.length,1);
    h.c.state.jobs=[{id:9,watch:{done:true}}];h.c.renderLibraryUpdates();
    assert.equal(h.box.hidden,true);assert.equal(h.c.libraryUpdateFor(9),false);
    console.log('PASS library reminders use one incremental request, throttle polling and immediately exclude completed videos');
  }
  {
    const h=harness(), pending=h.c.refreshLibraryUpdates();
    h.c.resetLibraryUpdates();h.c.storageOwner='user:bob';h.c.privateViewEpoch++;
    answer(h.calls[0],[{id:1,title:'STALE_PRIVATE'}]);await pending;
    assert.equal(h.box.innerHTML,'');assert.equal(h.box.hidden,true);
    const next=h.c.refreshLibraryUpdates();answer(h.calls[1],[{id:2,title:'FOREIGN_PRIVATE'}],'user:alice');await next;
    assert.equal(h.box.innerHTML,'');assert.equal(h.box.hidden,true);
    console.log('PASS owner/lock transitions abort reminders and discard delayed or foreign-owner responses');
  }
  {
    const h=harness(), pending=h.c.refreshLibraryUpdates();answer(h.calls[0],[{id:1,title:'Private episode'}]);await pending;
    h.c.query='other';h.c.renderLibraryUpdates();assert.equal(h.box.hidden,true);assert.equal(h.box.innerHTML,'');
    h.c.query='';h.c.chip='notes';h.c.renderLibraryUpdates();assert.equal(h.box.hidden,true);
    h.c.chip='all';const dismiss=h.c.dismissLibraryUpdate(1);h.calls[1].reject(Error('offline'));await dismiss;await flush();
    assert.equal(h.current().busy,false);
    console.log('PASS reminders stay in the library and failed dismissals leave controls usable');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
