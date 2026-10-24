// node tests/saved_search_ui.js — saved queries, explicit execution and private lifecycle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/saved_search.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const nav = html.slice(html.indexOf('function saveNavigation('), html.indexOf('function restoreNavigation()'));
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function response(data, status = 200) { return {ok: status < 400, status, json: async () => data}; }
function harness() {
  const elements = new Map(), calls = [], notices = [], submitted = [], urls = [];
  const $ = key => {
    if (!elements.has(key)) elements.set(key, {hidden: false, value: '', textContent: '', innerHTML: '', disabled: false, addEventListener() {}, focus() {}});
    return elements.get(key);
  };
  $('#savedSearchSheet').hidden = true; $('#mainSearchMode').value = 'exact';
  const location = {href: 'http://example.test/'}, history = {state: {}};
  for(const method of ['replaceState','pushState']) history[method] = (state, title, url) => { history.state = state; urls.push(String(url)); location.href = String(url); };
  const context = vm.createContext({$, URL, URLSearchParams, AbortController, DOMException, Map, Set,
    storageOwner: 'user:one', state: {user:'one',privacy:{revealed:false}}, privacyToken: null,
    query:'',chip:'all',deviceFilter:null,pageLimit:100,videoSort:'added',restoringNavigation:false,cardHtml:[],lastBody:'',
    location,history,window:{scrollY:0,addEventListener(){}},
    esc:text=>String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    toast:text=>notices.push(text), invalidateMainSearch(){},
    mainSearchAvailable:()=>!['notes','books','live','subs'].includes(context.chip),
    mainSearchMode:()=>$('#mainSearchMode').value,
    syncMainSearchControls:()=>context.syncSavedSearchControls(),
    setChip(){throw Error('restoration must not trigger an extra navigation refresh');},
    fetch:(url,options)=>{const call={...deferred(),url,options};calls.push(call);return call.promise;},
    submitMainSearch:async()=>{context.query=$('#search').value;context.saveNavigation();submitted.push({q:context.query,mode:$('#mainSearchMode').value,chip:context.chip});},
  });
  vm.runInContext(source+'\n'+nav,context);
  return {context,$,calls,notices,submitted,urls,location,value:expression=>vm.runInContext(expression,context)};
}
const record = (id=1,extra={})=>({id,name:'入门课程',q:'Python*入门',mode:'wildcard',private:false,...extra});
const page = items=>({items,total:items.length,more:false,limit_per_owner:100,storage_owner:'user:one'});
const event={preventDefault(){}};
(async()=>{
  {
    const h=harness();h.context.syncSavedSearchControls();assert.equal(h.calls.length,0);
    const opening=h.context.openSavedSearches();assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/^\/api\/searches\?/);
    h.calls[0].resolve(response(page([record(1,{name:'<img src=x>',q:'<script>bad</script>'})])));await opening;
    assert.equal(h.submitted.length,0);assert.ok(!h.$('#savedSearchList').innerHTML.includes('<img'));
    assert.ok(!h.$('#savedSearchList').innerHTML.includes('<script>'));
    assert.match(h.$('#savedSearchCount').textContent,/100/);
    h.context.closeSavedSearches();assert.equal(h.calls.length,1);
    console.log('PASS list is lazy, names and queries are escaped, and listing/closing never executes search');
  }
  {
    const h=harness();h.$('#search').value='适合初学者的课程';h.$('#mainSearchMode').value='ai';
    const opening=h.context.openSavedSearches(true);h.calls[0].resolve(response(page([])));await opening;
    h.$('#savedSearchName').value='开始学习';const saving=h.context.saveNamedSearch(event);await h.context.saveNamedSearch(event);
    assert.equal(h.calls.length,2);assert.deepEqual(JSON.parse(h.calls[1].options.body),{name:'开始学习',q:'适合初学者的课程',mode:'ai'});
    h.calls[1].resolve(response({item:record(4,{name:'开始学习',q:'适合初学者的课程',mode:'ai'}),created:true,storage_owner:'user:one'}));await saving;
    assert.equal(h.submitted.length,0);assert.equal(h.value('savedSearchView.total'),1);
    h.context.chip='notes';h.$('#mainSearchMode').value='regex';
    const restore=h.context.restoreSavedSearch(4);await h.context.restoreSavedSearch(4);assert.equal(h.calls.length,3);
    h.calls[2].resolve(response({item:record(4,{q:'适合初学者的课程',mode:'ai'}),storage_owner:'user:one'}));await restore;
    assert.deepEqual(h.submitted,[{q:'适合初学者的课程',mode:'ai',chip:'all'}]);
    assert.equal(h.$('#savedSearchSheet').hidden,true);
    console.log('PASS saving AI query does not run it; fresh restore crosses category/old mode with exactly one explicit submission');
  }
  {
    const h=harness();h.context.state.privacy.revealed=true;h.context.privacyToken='private-token';
    h.value('savedSearchView.items=[{id:7,name:"私密名称",q:"private-query",mode:"ai",private:true}];');
    h.$('#savedSearchSheet').hidden=false;
    const restoring=h.context.restoreSavedSearch(7);h.calls[0].resolve(response({item:record(7,{q:'private-query',mode:'ai',private:true}),storage_owner:'user:one'}));await restoring;
    assert.equal(h.value('savedSearchPrivateQuery'),true);
    assert.ok(h.urls.every(url=>!url.includes('private-query')));assert.ok(!h.location.href.includes('q='));
    h.$('#search').value='private-query-edited';h.context.query='private-query-edited';h.context.saveNavigation();
    assert.ok(h.urls.every(url=>!url.includes('private-query')));
    h.context.resetSavedSearches();assert.equal(h.$('#search').value,'');assert.equal(h.context.query,'');
    assert.equal(h.value('savedSearchPrivateQuery'),false);assert.ok(!h.$('#savedSearchList').innerHTML.includes('私密名称'));
    console.log('PASS private restore and subsequent edits never enter URLs/history; privacy reset clears active query and management input');
  }
  {
    const h=harness(),body=deferred();const opening=h.context.openSavedSearches();
    h.calls[0].resolve({ok:true,status:200,json:()=>body.promise});await Promise.resolve();await Promise.resolve();
    h.context.storageOwner='user:two';h.context.resetSavedSearches();
    body.resolve(page([record(9,{name:'old private owner'})]));await opening;
    assert.equal(h.value('savedSearchView.items.length'),0);assert.equal(h.calls[0].options.signal.aborted,true);
    assert.ok(!h.$('#savedSearchList').innerHTML.includes('old private owner'));
    console.log('PASS account change aborts requests and rejects delayed response bodies');
  }
  {
    const h=harness();h.context.state.privacy.revealed=true;h.context.privacyToken='private-token';h.$('#search').value='secret';h.context.query='secret';
    const opening=h.context.openSavedSearches(true);h.calls[0].resolve(response(page([])));await opening;
    h.$('#savedSearchName').value='私密名称';const save=h.context.saveNamedSearch(event);
    h.context.state.jobs=[{id:9,title:'private result'}];h.$('#grid').innerHTML='private result';
    assert.equal(JSON.parse(h.calls[1].options.body).expected_private,true);
    h.calls[1].resolve(response({error:'隐私显示状态已变化'},403));await save;
    assert.equal(h.$('#savedSearchSheet').hidden,true);assert.equal(h.$('#savedSearchName').value,'');assert.equal(h.$('#search').value,'');
    assert.equal(h.value('savedSearchView.form'),null);assert.equal(h.value('savedSearchView.items.length'),0);
    assert.equal(h.context.state.jobs.length,0);assert.equal(h.$('#grid').innerHTML,'');
    assert.match(h.notices[0],/隐私显示状态/);
    console.log('PASS private save carries intent guard; permission rejection clears sensitive drafts, cached list and restored query');
  }
  {
    const h=harness();const opening=h.context.openSavedSearches();h.calls[0].resolve(response(page([record()])));await opening;
    const restore=h.context.restoreSavedSearch(1);h.calls[1].resolve(response({error:'记录已删除'},404));await restore;
    assert.equal(h.submitted.length,0);assert.match(h.value('savedSearchView.error'),/已删除/);
    h.context.renameSavedSearch(1);h.$('#savedSearchName').value='新名称';const rename=h.context.saveNamedSearch(event);
    assert.equal(h.calls[2].url,'/api/searches/1/rename');assert.deepEqual(JSON.parse(h.calls[2].options.body),{name:'新名称'});
    h.calls[2].resolve(response({item:record(1,{name:'新名称'}),storage_owner:'user:one'}));await rename;
    await h.context.deleteSavedSearch(1);assert.equal(h.calls.length,3);
    h.context.askDeleteSavedSearch(1);h.context.cancelDeleteSavedSearch();await h.context.deleteSavedSearch(1);assert.equal(h.calls.length,3);
    h.context.askDeleteSavedSearch(1);const deleting=h.context.deleteSavedSearch(1);await h.context.deleteSavedSearch(1);assert.equal(h.calls.length,4);
    assert.deepEqual(JSON.parse(h.calls[3].options.body),{});h.calls[3].resolve(response({ok:true,id:1,storage_owner:'user:one'}));await deleting;
    assert.equal(h.value('savedSearchView.items.length'),0);assert.equal(h.value('savedSearchView.total'),0);
    console.log('PASS restore revalidates deleted records, rename preserves query, and delete requires confirmation/coalesces clicks');
  }
  {
    const h=harness();const opening=h.context.openSavedSearches();h.calls[0].resolve(response({...page([record()]),total:101,more:true}));await opening;
    const more=h.context.loadSavedSearches(true);assert.match(h.calls[1].url,/offset=1/);
    h.calls[1].resolve(response({...page([record(2)]),total:101,more:false}));await more;
    assert.equal(h.value('savedSearchView.items.length'),2);assert.equal(h.$('#savedSearchMore').hidden,true);
    assert.equal(h.submitted.length,0);
    assert.equal((html.match(/if \(typeof resetSavedSearches === 'function'\) resetSavedSearches\(\);/g)||[]).length,2);
    console.log('PASS paginated migrated collections remain accessible and owner/privacy lifecycle hooks are installed');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
