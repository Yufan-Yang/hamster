// node tests/main_search_ui.js — unified search, AI snapshots and owner isolation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const main = fs.readFileSync(path.join(__dirname, '../static/main_search.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const crawl = fs.readFileSync(path.join(__dirname, '../static/crawl.js'), 'utf8');
const refreshCode = html.slice(html.indexOf('async function refresh(options = {})'), html.indexOf('let wentOffline = false;'));
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function response(data, status=200) { return {ok:status<400, status, json:async()=>data, text:async()=>JSON.stringify(data)}; }
function harness() {
  const elements = new Map(), calls = [], timers = new Map(), notices = []; let count=0,clock=0;
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {hidden:true,value:'',innerHTML:'',textContent:'',dataset:{},
      setAttribute(){},removeAttribute(){},addEventListener(){},focus(){}});
    return elements.get(selector);
  };
  $('#mainSearchMode').value='exact';
  const context = vm.createContext({$, URL, URLSearchParams, AbortController, Map, Set,
    chip:'all', query:'', deviceFilter:null, storageOwner:'user:one', refreshEpoch:0, refreshController:null, searchTimer:null,
    state:{jobs:[],privacy:{revealed:false}}, cardHtml:[], lastBody:'', pageLimit:100, selecting:false,
    workspaceLoading:false,workspaceLoaded:true,workspaceError:'',wentOffline:false,smartSearch:false,exactSearch:false,visualSearch:false,
    FOLDERS:{},LIBS:{},videoSort:'added', document:{hidden:false}, window:{addEventListener(){}},
    saveNavigation(){},render(){},renderWorkspaceStatus(){},updateSearchOptions(){},schedule(){},waitingDownloadRetry:()=>false,
    setStorageOwner(owner){context.storageOwner=owner;},
    crawlInterpretation: v => typeof v==='string' ? v : v ? JSON.stringify(v) : '',
    crawlLink:(url,title)=>title||url,esc:v=>String(v??''),
    toast:v=>notices.push(v),
    fetch:(url,options)=>{const call={...deferred(),url,options};calls.push(call);return call.promise;},
    setTimeout:(fn,delay)=>{timers.set(++count,{fn,delay,at:clock+delay});return count;},clearTimeout:id=>timers.delete(id),
  });
  vm.runInContext(html.slice(html.indexOf('function dur('),html.indexOf('function ago(')) + crawl.slice(crawl.indexOf('function crawlDuration('),crawl.indexOf('function crawlURL(')) + main+'\n'+refreshCode,context);
  context.render=()=>context.renderMainSearch();
  const advance=ms=>{const until=clock+ms;let due;while((due=[...timers.entries()].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0])){clock=due[1].at;timers.delete(due[0]);due[1].fn();}clock=until;};
  return {context,$,calls,timers,notices,advance,value:exp=>vm.runInContext(exp,context)};
}
function installPrivacy(h) {
  Object.assign(h.context, {privacyToken:'unlocked',privacyTimer:null,privacyMenuEpoch:0,privateViewEpoch:0,
    watching:null,shown:[],digestState:{digests:[],pending:false,at:0},digestPick:0,weeklyData:null,weeklyPick:0,
    usageDays:7,cacheIndex:new Map(),refreshCacheIndex:async()=>{},cancelPrivacySelection(){},
    updatePrivacySelectionVisibility(){},cancelLearningPlan(){},hide(el){el.hidden=true;},
    renderWeekly(){throw Error('stale weekly rendered');},renderSubs(){throw Error('stale digest rendered');}});
  const ranges=[['function resetPrivateViews()', 'async function changePrivacySelection'],
    ['function lockPrivacy(quiet)', 'document.addEventListener("visibilitychange"'],
    ['async function loadDigests()', 'async function makeDigest'],
    ['async function openWeekly()', 'function mins'],['async function ask(e)', 'function askOpenNote'],
    ['async function openUsage(days)', '/* ---------- account']];
  for(const [start,end] of ranges){const at=html.indexOf(start),stop=html.indexOf(end,at);assert.ok(at>=0&&stop>at, start);vm.runInContext(html.slice(at,stop),h.context);}
}
const event={preventDefault(){}};
const jobs=(items=[])=>({storage_owner:'user:one',jobs:items,privacy:{revealed:false},subs:[],more:false});
(async()=>{
  {
    const h=harness();
    for(const prefix of ['x','xi','xia','xiao','xiaoL','xiaoLi','xiaoLin']){h.$('#search').value=prefix;h.context.mainSearchInput();h.advance(300);assert.equal(h.calls.length,0);}
    h.advance(100);assert.equal(h.calls.length,2);for(const call of h.calls){assert.equal(new URLSearchParams(call.url.split('?')[1]).get('q'),'xiaoLin');call.resolve(response(call.url.startsWith('/api/jobs')?jobs([{id:7}]):{items:[{id:70}],total:1}));}
    await new Promise(setImmediate);assert.equal(h.context.state.jobs[0].id,7);assert.equal(h.value('mainSearchView.items[0].id'),70);
    console.log('PASS seven xiaoLin prefixes at 300 ms send only the final two requests after 400 ms trailing debounce');
  }
  {
    const h=harness(),pending=[];h.$('#search').value='x';pending.push(h.context.submitMainSearch(event));assert.equal(h.calls.length,2);
    const libraryBody=deferred(),crawlBody=deferred();h.calls[0].resolve({ok:true,text:()=>libraryBody.promise});h.calls[1].resolve({ok:true,json:()=>crawlBody.promise});await new Promise(setImmediate);
    for(const text of ['xi','xia','xiaoLin']){h.$('#search').value=text;pending.push(h.context.submitMainSearch(event));}
    assert.equal(h.calls.length,2);assert.equal(h.calls[0].options.signal.aborted,false);assert.equal(h.calls[1].options.signal.aborted,false);
    libraryBody.resolve(JSON.stringify(jobs([{id:1,title:'stale'}])));await new Promise(setImmediate);assert.equal(h.calls.length,3);assert.equal(h.context.state.jobs.length,0);assert.equal(new URLSearchParams(h.calls[2].url.split('?')[1]).get('q'),'xiaoLin');
    crawlBody.resolve({items:[{id:10,title:'stale'}],total:1});await new Promise(setImmediate);assert.equal(h.calls.length,4);assert.equal(new URLSearchParams(h.calls[3].url.split('?')[1]).get('q'),'xiaoLin');
    h.calls[2].resolve(response(jobs([{id:7}])));h.calls[3].resolve(response({items:[{id:70}],total:1}));await Promise.all(pending);
    assert.deepEqual(Array.from(h.context.state.jobs,j=>j.id),[7]);assert.equal(h.value('mainSearchView.items[0].id'),70);
    console.log('PASS each lane waits through the response body, sends only its latest queued query and discards stale results without abort flooding');
  }
  {
    const h=harness();h.$('#search').value='old';const old=h.context.submitMainSearch(event);h.context.storageOwner='user:two';h.context.resetMainSearch();
    assert.equal(h.calls[0].options.signal.aborted,true);assert.equal(h.calls[1].options.signal.aborted,true);
    h.$('#search').value='new';const next=h.context.submitMainSearch(event);assert.equal(h.calls.length,4,'new owner cannot wait for an abandoned old response');
    h.calls[2].resolve(response({...jobs([{id:2}]),storage_owner:'user:two'}));h.calls[3].resolve(response({storage_owner:'user:two',items:[{id:20}],total:1}));await next;await old;
    h.calls[0].resolve(response(jobs([{id:1}])));h.calls[1].resolve(response({storage_owner:'user:one',items:[{id:10}],total:1}));await new Promise(setImmediate);
    assert.equal(h.context.state.jobs[0].id,2);assert.equal(h.value('mainSearchView.items[0].id'),20);
    console.log('PASS owner reset releases and aborts both lanes immediately without allowing late old-owner completion to touch new results');
  }
  {
    const h=harness();h.context.query='lesson';const original=h.context.refresh();
    h.context.videoSort='title';const middle=h.context.refresh();h.context.videoSort='added';const latest=h.context.refresh();
    assert.equal(h.calls.length,1);h.calls[0].resolve(response(jobs([{id:1,title:'invalidated original'}])));await new Promise(setImmediate);
    assert.equal(h.calls.length,2);assert.equal(new URLSearchParams(h.calls[1].url.split('?')[1]).has('sort'),false);assert.equal(h.context.state.jobs.length,0);
    h.calls[1].resolve(response(jobs([{id:9,title:'current result'}])));await Promise.all([original,middle,latest]);
    assert.equal(h.context.workspaceLoading,false);assert.equal(h.context.state.jobs[0].id,9);
    console.log('PASS A-B-A sorting reruns invalidated active work once and cannot strand the latest view in loading');
  }
  {
    const h=harness();let covered=true;h.context.crawlVisible=()=>covered;h.context.downloadActive=()=>false;h.context.query='AI phrase';h.$('#mainSearchMode').value='ai';
    await h.context.refresh({background:true});assert.equal(h.calls.length,0);
    const explicit=h.context.refresh({explicitSearch:true,background:true});assert.equal(h.calls.length,1);assert.equal(new URLSearchParams(h.calls[0].url.split('?')[1]).get('mode'),'ai');h.calls[0].resolve(response(jobs([{id:1}])));await explicit;
    vm.runInContext('let pollTimer;'+html.slice(html.indexOf('function schedule()'),html.indexOf('document.addEventListener("visibilitychange"',html.indexOf('function schedule()'))),h.context);
    h.context.schedule();assert.equal(h.timers.size,0);
    const classes=new Set(),sheet=h.$('#crawlSheet');sheet.id='crawlSheet';sheet.hidden=false;sheet.classList={contains:key=>classes.has(key),add:key=>classes.add(key),remove:key=>classes.delete(key)};
    h.context.suspendCrawl=()=>{};vm.runInContext(html.slice(html.indexOf('function hide(el, after)'),html.indexOf('function toast(t)')),h.context);
    h.context.hide(sheet);const closing=[...h.timers.entries()][0];h.timers.delete(closing[0]);covered=false;closing[1].fn();assert.equal(sheet.hidden,true);assert.equal(h.timers.size,1);
    const poll=[...h.timers.entries()][0];h.timers.delete(poll[0]);const checking=poll[1].fn();assert.equal(h.calls.length,2);const params=new URLSearchParams(h.calls[1].url.split('?')[1]);assert.equal(params.get('mode'),'exact');assert.equal(params.get('only'),'1');h.calls[1].resolve(response(jobs([{id:1}])));await checking;
    console.log('PASS covered home polling pauses, explicit search still runs, and closing resumes AI snapshot validation without another AI call');
  }
  {
    const h=harness();h.context.query='duration';h.value("mainSearchView.items=[{id:1,title:'Known',duration:65},{id:2,title:'Legacy'},{id:3,title:'Invalid',duration:'<img src=x>'}]");
    h.context.renderMainSearch();const cards=h.$('#mainCrawlResults').innerHTML;
    assert.match(cards,/时长 1:05/);assert.equal((cards.match(/时长未知/g)||[]).length,2);assert.ok(!cards.includes('<img'));assert.equal(h.calls.length,0);
    console.log('PASS homepage collected cards show known/unknown durations safely without extra requests');
  }
  {
    const h=harness();h.$('#search').value='字幕*原文';h.$('#mainSearchMode').value='wildcard';
    const first=h.context.submitMainSearch(event);
    h.calls[0].resolve(response({...jobs([{id:1,hits:[{part:0,t:12,src:'字幕',text:'字幕中的原文'}]}]),search_total:201,search_offset:0,search_limit:100,more:true}));
    h.calls[1].resolve(response({items:[],total:0}));await first;
    assert.equal(h.$('#mainLibraryPages').hidden,false);assert.match(h.$('#mainLibraryCount').textContent,/共 201 条/);
    const next=h.context.pageMainLibrary(1);await h.context.pageMainLibrary(1);assert.equal(h.calls.length,3);
    const p=new URLSearchParams(h.calls[2].url.split('?')[1]);assert.equal(p.get('offset'),'100');assert.equal(p.get('limit'),'100');assert.equal(p.get('mode'),'wildcard');assert.equal(p.get('q'),'字幕*原文');
    h.calls[2].resolve(response({...jobs([{id:101}]),search_total:201,search_offset:100,search_limit:100,more:true}));await next;
    assert.deepEqual(Array.from(h.context.state.jobs,j=>j.id),[101]);assert.equal(h.calls.filter(c=>c.url.startsWith('/api/crawls/search')).length,1);
    h.context.videoSort='title';const sorted=h.context.refresh();const sp=new URLSearchParams(h.calls[3].url.split('?')[1]);assert.equal(sp.get('offset'),'0');assert.equal(sp.get('sort'),'title');h.calls[3].resolve(response({...jobs(),search_total:0,search_offset:0,search_limit:100}));await sorted;
    h.context.chip='active';const active=h.context.refresh();assert.equal(new URLSearchParams(h.calls[4].url.split('?')[1]).get('state'),'active');h.calls[4].resolve(response(jobs()));await active;
    h.context.chip='device';h.context.deviceFilter='手机';const device=h.context.refresh();const dp=new URLSearchParams(h.calls[5].url.split('?')[1]);assert.equal(dp.get('state'),'device');assert.equal(dp.get('device'),'手机');assert.equal(dp.get('offset'),'0');h.calls[5].resolve(response(jobs()));await device;
    console.log('PASS library pagination replaces one bounded page, preserves query/mode, coalesces clicks and scopes sorting/activity/device before pagination');
  }
  {
    const h=harness();h.context.query='old';h.$('#search').value='old';
    const first=h.context.refresh();h.calls[0].resolve(response({...jobs([{id:1}]),search_total:200,search_offset:0,search_limit:100,more:true}));await first;
    const pending=h.context.pageMainLibrary(1);h.$('#search').value='new';h.context.mainSearchInput();
    h.calls[1].resolve(response({...jobs([{id:99,title:'SECRET old query'}]),search_total:200,search_offset:100,search_limit:100}));await pending;
    assert.equal(h.context.state.jobs.length,0);assert.equal(h.value('mainLibraryPage.offset'),0);assert.ok(!h.$('#mainLibraryCount').textContent.includes('200'));
    h.context.state.jobs=[{id:9,title:'SECRET'}];h.value("mainSearchView.items=[{id:8,title:'SECRET'}]");h.$('#grid').innerHTML='SECRET';
    const denied=h.context.refresh();h.calls[2].resolve(response({error:'隐私权限已失效'},403));await denied;
    assert.equal(h.context.state.jobs.length,0);assert.equal(h.value('mainSearchView.items.length'),0);assert.equal(h.$('#grid').innerHTML,'');assert.ok(!h.$('#mainCrawlResults').innerHTML.includes('SECRET'));assert.match(h.context.workspaceError,/失效/);
    console.log('PASS query changes discard late pages and revoked permissions synchronously clear all search evidence');
  }
  {
    const h=harness();h.context.query='课程[0-9]';h.$('#mainSearchMode').value='regex';
    const first=h.context.refresh();assert.equal(new URLSearchParams(h.calls[0].url.split('?')[1]).get('offset'),'0');h.calls[0].resolve(response({...jobs([{id:1}]),search_total:120,search_offset:0,search_limit:100,more:true}));await first;
    assert.equal(h.$('#mainLibraryPages').hidden,false);const next=h.context.pageMainLibrary(1);assert.equal(new URLSearchParams(h.calls[1].url.split('?')[1]).get('mode'),'regex');h.calls[1].resolve(response({...jobs([{id:101}]),search_total:120,search_offset:100,search_limit:100}));await next;
    h.$('#mainSearchMode').value='exact';h.context.visualSearch=true;const visual=h.context.refresh();h.calls[2].resolve(response({...jobs([{id:9}]),more:true}));await visual;
    assert.equal(h.$('#mainLibraryPages').hidden,true);await h.context.pageMainLibrary(1);assert.equal(h.calls.length,3,'legacy visual results must not invent pagination from more alone');
    h.context.visualSearch=false;const textual=h.context.refresh();assert.equal(new URLSearchParams(h.calls[3].url.split('?')[1]).get('offset'),'0');h.calls[3].resolve(response({...jobs(),search_total:0,search_offset:0,search_limit:100}));await textual;
    console.log('PASS bounded regex pages use server metadata while legacy visual and AI paths never fabricate pages');
  }
  {
    const h=harness();h.$('#search').value='Python*入门?';h.$('#mainSearchMode').value='wildcard';
    const submitted=h.context.submitMainSearch(event);
    assert.equal(h.calls.length,2);
    for(const call of h.calls){const p=new URLSearchParams(call.url.split('?')[1]);assert.equal(p.get('q'),'Python*入门?');assert.equal(p.get('mode'),'wildcard');}
    h.calls.find(x=>x.url.startsWith('/api/jobs')).resolve(response(jobs([{id:1,title:'local'}])));
    h.calls.find(x=>x.url.startsWith('/api/crawls/search')).resolve(response({storage_owner:'user:one',items:[{id:8,crawl_id:3,title:'collected'}],total:1}));
    await submitted;assert.equal(h.context.state.jobs[0].id,1);assert.equal(h.value('mainSearchView.items[0].id'),8);
    console.log('PASS one input sends identical wildcard query/mode to library and deduplicated crawl search');
  }
  {
    const h=harness();h.$('#search').value='适合零基础学习编程';h.$('#mainSearchMode').value='ai';h.context.mainSearchInput();
    assert.equal(h.calls.length,0);assert.equal(h.timers.size,0);
    const submitted=h.context.submitMainSearch(event);assert.equal(h.calls.length,2);
    h.calls[0].resolve(response(jobs([{id:2,title:'private later',score:0.96},{id:1,title:'keep',score:0.8}])));
    h.calls[1].resolve(response({storage_owner:'user:one',items:[{id:10,crawl_id:3,title:'result',score:0.9}],total:1}));
    await submitted;
    const poll=h.context.refresh();assert.equal(h.calls.length,3);
    const p=new URLSearchParams(h.calls[2].url.split('?')[1]);assert.equal(p.get('mode'),'exact');assert.equal(p.get('only'),'1');assert.equal(p.get('ids'),'2,1');assert.equal(p.has('q'),false);
    h.calls[2].resolve(response(jobs([{id:1,title:'current visible title'}])));await poll;
    assert.deepEqual(Array.from(h.context.state.jobs,x=>x.id),[1]);assert.equal(h.context.state.jobs[0].score,0.8);
    assert.equal(h.value('mainSearchView.items[0].id'),10);
    assert.equal(h.calls.filter(x=>new URLSearchParams(x.url.split('?')[1]).get('mode')==='ai').length,2);
    console.log('PASS AI requires explicit submit; polling revalidates existing IDs and removes hidden/deleted matches without AI');
  }
  {
    const h=harness();h.context.query='old';h.$('#search').value='old';
    const old=h.context.loadMainCrawlResults();
    h.$('#search').value='new';h.context.mainSearchInput();
    assert.equal(h.calls[0].options.signal.aborted,false);
    const newer=h.context.loadMainCrawlResults();
    assert.equal(h.calls.length,1);h.calls[0].resolve(response({items:[{id:1,title:'stale'}],total:1}));await old;await new Promise(setImmediate);
    h.calls[1].resolve(response({items:[{id:2,title:'new'}],total:1}));await newer;
    assert.equal(h.value('mainSearchView.items[0].id'),2);
    h.$('#search').value='';h.context.mainSearchInput();assert.equal(h.value('mainSearchView.items.length'),0);assert.equal(h.$('#mainCrawlSearch').hidden,true);
    console.log('PASS input edits/clear coalesce superseded requests and remove stale results immediately');
  }
  {
    const h=harness();h.context.query='private';h.$('#search').value='private';
    const pending=h.context.loadMainCrawlResults(),body=deferred();
    h.calls[0].resolve({ok:true,json:()=>body.promise});await new Promise(setImmediate);
    h.context.storageOwner='user:two';h.context.resetMainSearch();body.resolve({storage_owner:'user:one',items:[{id:99,title:'old owner'}],total:1});await pending;
    assert.equal(h.value('mainSearchView.items.length'),0);assert.equal(h.value('mainLibrarySnapshot'),null);
    console.log('PASS account switch rejects even delayed response bodies and clears semantic snapshots');
  }
  {
    const h=harness();h.context.query='[';h.$('#search').value='[';h.$('#mainSearchMode').value='regex';
    const pending=h.context.loadMainCrawlResults();h.calls[0].resolve(response({error:'无效正则表达式'},400));await pending;
    assert.equal(h.$('#search').value,'[');assert.match(h.value('mainSearchView.error'),/无效正则/);assert.equal(h.value('mainSearchView.loading'),false);
    console.log('PASS regex errors remain visible and preserve the editable query');
  }
  {
    for(const [query,mode] of [['','exact'],['private','exact'],['private','ai']]){
      const h=harness();installPrivacy(h);h.context.query=query;h.$('#search').value=query;h.$('#mainSearchMode').value=mode;
      h.context.state={jobs:[{id:9,title:'SECRET',hidden:true}],cats:{SECRET:1},privacy:{revealed:true}};
      h.context.cardHtml=['SECRET'];h.context.shown=[9];h.context.lastBody='SECRET';
      h.context.digestState={digests:[{title:'SECRET'}]};h.context.weeklyData={title:'SECRET'};
      const sensitive=['grid','controw','chips','subs','weeklyBody','usageBody','askOut','learningResult','privacyBody'];
      for(const id of sensitive)h.$('#'+id).innerHTML='SECRET';
      const panels=['weeklySheet','usageSheet','askSheet','learningSheet','privacySheet'];
      for(const id of panels)h.$('#'+id).hidden=false;
      h.context.lockPrivacy(true);
      assert.equal(h.context.privacyToken,null);assert.equal(h.context.state.jobs.length,0);assert.equal(Object.keys(h.context.state.cats).length,0);
      assert.equal(h.context.cardHtml.length,0);assert.equal(h.context.shown.length,0);assert.equal(h.context.lastBody,'');
      assert.equal(h.context.weeklyData,null);assert.equal(h.context.digestState.digests.length,0);
      for(const id of sensitive)assert.equal(h.$('#'+id).innerHTML,'',id);
      for(const id of panels)assert.equal(h.$('#'+id).hidden,true,id);
      for(const call of h.calls)call.resolve(response({error:'offline'},503));
      await new Promise(setImmediate);
      assert.equal(h.context.state.jobs.length,0);assert.equal(h.$('#grid').innerHTML,'');
    }
    console.log('PASS locking clears revealed cards, categories and reports synchronously with empty, exact or AI search even offline');
  }
  {
    const h=harness();installPrivacy(h);h.context.chip='subs';h.$('#askText').value='private question';
    const pending=[h.context.loadDigests(),h.context.openWeekly(),h.context.openUsage(),h.context.ask(event)];
    const bodies=h.calls.map(call=>{const body=deferred();call.resolve({ok:true,json:()=>body.promise});return body;});
    await new Promise(setImmediate);h.context.lockPrivacy(true);
    for(const body of bodies)body.resolve({digests:[{title:'SECRET'}],answer:'SECRET',current:{title:'SECRET'}});
    for(const call of h.calls.slice(bodies.length))call.resolve(response({error:'offline'},503));
    await Promise.all(pending);await new Promise(setImmediate);
    assert.equal(h.context.digestState.digests.length,0);assert.equal(h.context.weeklyData,null);
    for(const id of ['weeklyBody','usageBody','askOut','subs'])assert.equal(h.$('#'+id).innerHTML,'');
    console.log('PASS delayed digest, weekly, usage and answer bodies cannot restore hidden content after locking');
  }
  {
    const h=harness();h.context.closeWatch=()=>{};h.context.window.scrollTo=()=>{};
    vm.runInContext(html.slice(html.indexOf('function searchFor(t)'),html.indexOf('let searchTimer;')),h.context);
    vm.runInContext(html.slice(html.indexOf('function updateSearchOptions()'),html.indexOf('let restoringNavigation = false;')),h.context);
    h.$('#mainSearchMode').value='ai';h.context.searchFor('#Programming');
    assert.equal(h.$('#mainSearchMode').value,'exact');assert.equal(h.calls.length,2);
    for(const call of h.calls){const p=new URLSearchParams(call.url.split('?')[1]);assert.equal(p.get('mode'),'exact');assert.equal(p.get('q'),'#Programming');call.resolve(response(call.url.startsWith('/api/jobs')?jobs():{items:[],total:0}));}
    await new Promise(setImmediate);h.context.query='subtitle';
    for(const mode of ['exact','wildcard','regex','ai']){h.$('#mainSearchMode').value=mode;h.context.updateSearchOptions();assert.equal(h.$('#searchOptions').hidden,mode!=='exact');}
    console.log('PASS tag chips force exact search and only exact exposes subtitle/optional visual search controls');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
