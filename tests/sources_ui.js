// node tests/sources_ui.js — source/inbox actions and privacy lifecycle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/sources.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
function deferred() { let resolve; const promise = new Promise(r=>{resolve=r;}); return {promise,resolve}; }
function response(data,status=200) { return {ok:status<400,status,json:async()=>data}; }
async function flush() { for(let i=0;i<12;i++)await Promise.resolve(); }
function harness() {
  const elements=new Map(),calls=[],messages=[],timers=new Map(),managed=[];let tick=0;
  const $=id=>{if(!elements.has(id))elements.set(id,{hidden:false,value:'',innerHTML:'',textContent:'',disabled:false,
    classList:{contains:()=>false},setAttribute(name,value){this[name]=value;},addEventListener(){},querySelectorAll:()=>[]});return elements.get(id);};
  $('#sourcesSheet').hidden=true;
  const context=vm.createContext({$,URL,URLSearchParams,AbortController,DOMException,Map,Set,
    storageOwner:'user:one',privateViewEpoch:0,document:{hidden:false,addEventListener(){},querySelector:()=>null},
    esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    fetch:(url,options)=>{const call={...deferred(),url,options};calls.push(call);return call.promise;},
    toast:message=>messages.push(message),setTimeout:(fn,delay)=>{timers.set(++tick,{fn,delay});return tick;},clearTimeout:id=>timers.delete(id),
    openCrawl:async()=>managed.push('crawl'),selectCrawl:async id=>managed.push(id),setChip:chip=>managed.push(chip),
    openAdd:()=>managed.push('add'),openWatch:id=>managed.push('job:'+id),
  });
  vm.runInContext(source,context);
  return {context,$,calls,messages,timers,managed,value:text=>vm.runInContext(text,context)};
}
const src=(id=1,extra={})=>({key:'crawl:'+id,type:'crawl',id,title:'课程目录',url:'https://example.test/list',status:'running',unread:3,found:9,...extra});
const item=(id=1,extra={})=>({id,title:'入门课程',url:'https://example.test/video/'+id,state:'unread',created:1790000000,sources:[src()],can_download:true,...extra});
const sources=items=>({items,total:items.length,unread:3,storage_owner:'user:one'});
const inbox=items=>({items,total:items.length,unread:3,more:false,offset:0,limit:50,storage_owner:'user:one'});
async function open(h,items=[item()]) {
  const opening=h.context.openSources();h.calls[0].resolve(response(sources([src()])));h.calls[1].resolve(response(inbox(items)));await opening;
}
(async()=>{
  {
    const h=harness();assert.equal(h.calls.length,0);await open(h,[item(1,{title:'<img src=x>',url:'javascript:bad()'})]);
    assert.deepEqual(h.calls.map(c=>c.url.split('?')[0]),['/api/sources','/api/inbox']);
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0,'opening must never download or mark read');
    assert.ok(!h.$('#inboxItems').innerHTML.includes('<img'));assert.ok(!h.$('#inboxItems').innerHTML.includes('href="javascript:'));
    assert.match(h.$('#sourcesTabInbox').textContent,/未读 3/);assert.equal(h.timers.size,1);
    h.context.closeSources();assert.equal(h.timers.size,0);assert.equal(h.value('sourcesView.items.length'),0);
    console.log('PASS source center loads on demand, uses server counts, escapes metadata and never downloads automatically');
  }
  {
    const h=harness();await open(h);h.$('#inboxState').value='ignored';h.$('#inboxSource').value='crawl:1';
    const first=h.context.filterInbox();assert.match(h.calls[2].url,/state=ignored/);assert.match(h.calls[2].url,/source=crawl%3A1/);
    h.$('#inboxState').value='read';const second=h.context.filterInbox();assert.equal(h.calls[2].options.signal.aborted,true);
    h.calls[3].resolve(response({...inbox([item(2,{state:'read'})]),total:55,more:true}));await second;
    h.calls[2].resolve(response(inbox([item(99,{state:'ignored',title:'stale'})])));await first;
    assert.equal(h.value('sourcesView.items[0].id'),2);
    const next=h.context.pageInbox(1);assert.match(h.calls[4].url,/offset=50/);h.calls[4].resolve(response({...inbox([item(51)]),total:55,offset:50}));await next;
    assert.equal(h.value('sourcesView.offset'),50);assert.equal(h.value('sourcesView.selected.size'),0);
    console.log('PASS filters cancel superseded inbox requests and pagination clears stale selections');
  }
  {
    const h=harness();await open(h,[item(1),item(2,{can_download:false,job_id:12,job_status:'done'})]);
    h.context.pickInboxPage(true);assert.equal(h.value('sourcesView.selected.size'),2);assert.match(h.$('#inboxDownload').textContent,/1/);
    await h.context.refreshSources(true);assert.equal(h.calls.length,2,'poll does not disrupt active selection');
    const downloading=h.context.inboxAction('download');await h.context.inboxAction('download');assert.equal(h.calls.length,3);
    assert.equal(h.calls[2].url,'/api/inbox/import');assert.deepEqual(JSON.parse(h.calls[2].options.body),{ids:[1]});
    h.calls[2].resolve(response({added:1,duplicates:0,ids:[21]}));await flush();
    h.calls[3].resolve(response(inbox([])));h.calls[4].resolve(response(sources([src()])));await downloading;
    assert.equal(h.value('sourcesView.selected.size'),0);assert.match(h.messages[0],/已加入下载 1/);
    console.log('PASS bulk import is explicit, filters already-downloaded rows, blocks double submit and refreshes server state');
  }
  {
    const h=harness();await open(h,[item(1),item(2)]);h.context.pickInboxPage(true);
    const downloading=h.context.inboxAction('download');
    h.calls[2].resolve(response({added:1,duplicates:0,ids:[21],errors:[{id:2,error:'来源已变化'}]}));await flush();
    h.calls[3].resolve(response(inbox([item(1,{can_download:false,job_id:21})])));h.calls[4].resolve(response(sources([src()])));await downloading;
    assert.match(h.messages[0],/已加入下载 1/);assert.match(h.messages[0],/1 项需刷新后重试/);assert.equal(h.value('sourcesView.selected.size'),0);
    console.log('PASS partial imports report successful downloads and failed item counts without silently retrying');
  }
  {
    const h=harness();await open(h);h.context.pickInboxPage(true);
    const ignoring=h.context.inboxAction('ignore');assert.equal(h.calls[2].url,'/api/inbox/action');
    assert.deepEqual(JSON.parse(h.calls[2].options.body),{action:'ignore',ids:[1]});
    h.calls[2].resolve(response({ok:true,updated:1}));await flush();h.calls[3].resolve(response(inbox([])));h.calls[4].resolve(response(sources([src()])));await ignoring;
    assert.match(h.messages[0],/视频文件保留/);
    h.value('sourcesView.items=[{id:4,can_download:true}];sourcesView.selected.add("4");');
    const stale=h.context.inboxAction('read');h.calls[5].resolve(response({error:'条目不可见'},400));await stale;
    assert.equal(h.value('sourcesView.items.length'),0);assert.equal(h.value('sourcesView.selected.size'),0);
    console.log('PASS ignore/read actions use selected IDs only and revoked items clear stale visible cards');
  }
  {
    const h=harness(),body=deferred();const opening=h.context.openSources();
    h.calls[0].resolve(response(sources([src()])));h.calls[1].resolve({ok:true,status:200,json:()=>body.promise});await flush();
    h.context.storageOwner='user:two';h.context.resetSources();
    body.resolve(inbox([item(1,{title:'private previous owner'})]));await opening;
    assert.equal(h.value('sourcesView.items.length'),0);assert.equal(h.$('#sourcesSheet').hidden,true);assert.ok(!h.$('#inboxItems').innerHTML.includes('private previous owner'));
    assert.equal(h.calls[1].options.signal.aborted,true);assert.equal(h.timers.size,0);
    console.log('PASS owner/privacy reset cancels requests, clears sensitive DOM and ignores delayed response bodies');
  }
  {
    const h=harness();await open(h);h.context.pickInboxPage(true);
    const action=h.context.inboxAction('read');h.calls[2].resolve(response({error:'隐私验证失效'},403));await action;
    assert.equal(h.$('#sourcesSheet').hidden,true);assert.equal(h.value('sourcesView.items.length'),0);assert.match(h.messages[0],/验证失效/);
    const h2=harness();await open(h2);const wait=deferred();h2.context.openCrawl=()=>wait.promise;
    const managing=h2.context.manageSource('crawl:1');h2.context.storageOwner='user:two';wait.resolve();await managing;
    assert.equal(h2.managed.length,0,'old owner source must not open after slow modal load');
    assert.equal((html.match(/if \(typeof resetSources === 'function'\) resetSources\(\);/g)||[]).length,2);
    console.log('PASS permission failures close the center and delayed management handoff cannot cross owner boundaries');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
