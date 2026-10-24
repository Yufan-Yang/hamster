// node tests/crawl_ui.js — deterministic request/account-isolation regressions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/crawl.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; }
function response(data, status = 200) {
  const visible = c => c && !c.privacy_locked ? {source_classification: 'general', ...c} : c;
  if (data && typeof data === 'object') data = {...data, ...(data.crawl ? {crawl: visible(data.crawl)} : {}), ...(data.crawls ? {crawls: data.crawls.map(visible)} : {})};
  return {ok: status < 400, status, json: async () => data, text: async () => data};
}
function harness() {
  const calls = [], messages = [], elements = new Map(), timers = new Map(); let sequence = 0;
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {hidden: false, value: '', textContent: '', innerHTML: '', dataset: {},
      classList: {contains: () => false, toggle() {}}, setAttribute(name, value) { this[name] = value; }, addEventListener() {}, querySelectorAll: () => [], focus() {}});
    return elements.get(id);
  };
  $('#crawlSearchMode').value = 'exact'; $('#crawlMaxPages').value = '50';
  const context = vm.createContext({$, URL, URLSearchParams, AbortController, DOMException, Map, Set, Blob,
    storageOwner: 'user:one', document: {hidden: false, addEventListener() {}}, window: {}, navigator: {},
    esc: text => String(text ?? '').replace(/[&<>"']/g, x => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[x])),
    toast: value => messages.push(value), hide: el => { el.hidden = true; },
    fetch: (url, options) => { const call = {...deferred(), url, options}; calls.push(call); return call.promise; },
    setTimeout: (fn, delay) => { timers.set(++sequence, {fn, delay}); return sequence; }, clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(html.slice(html.indexOf('function dur('), html.indexOf('function ago(')) + source, context);
  for (const name of ['renderCrawl', 'renderCrawlError', 'renderCrawlControls', 'renderCrawlSelection']) context[name] = () => {};
  vm.runInContext("crawlView.id='7';crawlView.crawl={id:7,source_classification:'general',status:'running',max_pages:50};", context);
  return {context, calls, messages, timers, $, value: expression => vm.runInContext(expression, context)};
}
const event = {preventDefault() {}};
(async () => {
  {
    const h=harness();h.value("crawlView.id='';crawlView.crawl=null;");
    const listing=h.context.refreshCrawl();h.calls[0].resolve(response({revealed:false,crawls:[{id:1,url:'https://example.test/public',source_classification:'general'},{id:7,url:'https://example.test/PRIVATE',source_classification:'adult',error:'PRIVATE'},{id:8,url:'https://example.test/PRIVATE-unknown',source_classification:'unknown'}]}));await new Promise(setImmediate);
    assert.equal(h.calls.length,2);assert.match(h.calls[1].url,/\/1\?/);h.calls[1].resolve(response({revealed:false,crawl:{id:1,source_classification:'general'},items:[],total:0}));await listing;
    h.context.renderCrawlHistory();assert.deepEqual(Array.from(h.value('crawlView.crawls'),c=>c.id),[1]);assert.ok(!h.$('#crawlHistory').innerHTML.includes('PRIVATE'));
    h.value("crawlView.crawl={id:7,source_classification:'adult',url:'https://example.test/PRIVATE',current_url:'https://example.test/PRIVATE-archive',error:'PRIVATE',found:999};crawlView.id='7';");
    h.context.renderCrawlProgress();assert.equal(h.value('crawlView.id'),'');assert.equal(h.$('#crawlDetail').hidden,true);assert.equal(h.$('#crawlPrivacyNotice').hidden,false);assert.equal(h.$('#crawlHistoryRow').hidden,true);
    assert.ok(!h.$('#crawlCurrent').innerHTML.includes('PRIVATE'));assert.equal(h.$('#crawlProgress').value,0);
    console.log('PASS locked adult/unknown sources never enter history or task cards, including defensive rejection of legacy full metadata');
  }
  {
    const h=harness(),body=deferred();
    h.value("crawlView.revealed=true;crawlView.crawl={id:7,source_classification:'adult',url:'https://example.test/PRIVATE'};crawlView.crawls=[crawlView.crawl];crawlView.items=[{id:1,url:'https://example.test/PRIVATE-media'}];crawlView.total=999;crawlView.selected.set('1',crawlView.items[0]);crawlView.interpretation='PRIVATE';");
    for(const id of ['crawlHistory','crawlCurrent','crawlResults','crawlStats','crawlLastError','crawlSourceRating','crawlIssuesList'])h.$('#'+id).innerHTML='PRIVATE';h.$('#crawlEditURL').value='https://example.test/PRIVATE';
    const issues=h.context.toggleCrawlIssues();h.calls[0].resolve({ok:true,json:()=>body.promise});await new Promise(setImmediate);
    const details=h.context.loadCrawlItems();h.calls[1].resolve(response({crawl:{id:7,privacy_locked:true},privacy_locked:true,revealed:false,items:[],total:0}));await details;
    assert.equal(h.value('crawlView.crawl'),null);assert.equal(h.value('crawlView.items.length'),0);assert.equal(h.value('crawlView.selected.size'),0);assert.equal(h.value('crawlView.total'),0);assert.equal(h.value('crawlView.interpretation'),'');
    assert.equal(h.$('#crawlEditURL').value,'');assert.equal(h.$('#crawlExport').disabled,true);assert.equal(h.$('#crawlHistoryRow').hidden,true);assert.equal(h.$('#crawlPrivacyNotice').hidden,false);
    for(const id of ['crawlHistory','crawlCurrent','crawlResults','crawlStats','crawlLastError','crawlSourceRating','crawlIssuesList'])assert.ok(!h.$('#'+id).innerHTML.includes('PRIVATE'),id);
    body.resolve({items:[{url:'https://example.test/PRIVATE-late'}],total:1});await issues;assert.equal(h.$('#crawlIssuesList').innerHTML,'');
    const unlock=h.context.refreshCrawl();h.calls[2].resolve(response({revealed:true,crawls:[{id:7,source_classification:'adult',url:'https://example.test/PRIVATE'}]}));await new Promise(setImmediate);h.calls[3].resolve(response({revealed:true,crawl:{id:7,source_classification:'adult',url:'https://example.test/PRIVATE'},items:[{id:2,title:'Authorized result'}],total:1}));await unlock;
    assert.equal(h.value('crawlView.revealed'),true);assert.equal(h.value('crawlView.items[0].title'),'Authorized result');assert.equal(h.value('crawlView.privacyLocked'),false);
    console.log('PASS redacted detail clears every metadata surface and late issue body, while an authorized reveal restores normal results');
  }
  {
    const h=harness();h.value("crawlView.revealed=true;crawlView.crawl={id:7,source_classification:'adult',url:'https://example.test/PRIVATE'};crawlView.crawls=[crawlView.crawl];crawlView.filter={q:'PRIVATE',mode:'ai'};crawlView.items=[{id:99,title:'PRIVATE'}];");
    const listing=h.context.refreshCrawl();h.calls[0].resolve(response({revealed:false,crawls:[]}));await listing;
    assert.equal(h.calls.length,1);assert.equal(h.value('crawlView.crawl'),null);assert.equal(h.value('crawlView.items.length'),0);assert.equal(h.value('crawlView.filter.q'),'');assert.equal(h.$('#crawlPrivacyNotice').hidden,false);
    console.log('PASS filtered list withdrawal clears a previously revealed AI task even when semantic detail polling is skipped');
  }
  {
    const h=harness();let copied=0,parsed=0;
    h.context.writeCrawlClipboard=async()=>{++copied;return true;};
    h.value("crawlView.items=[{id:1,url:'https://example.test/SECRET'}];crawlView.selected.set('1',crawlView.items[0]);crawlView.total=1;crawlView.interpretation='SECRET';crawlView.notice='SECRET';");
    for(const id of ['crawlResults','crawlSearchNote','crawlCurrent','crawlLastError','crawlSourceRating','crawlHistory'])h.$('#'+id).innerHTML='SECRET';
    h.$('#crawlEditURL').value='https://example.test/SECRET';
    const exporting=h.context.exportCrawlAddresses(),loading=h.context.loadCrawlItems();
    h.calls[1].resolve({ok:false,status:403,json:async()=>{++parsed;throw Error('must clear without waiting for body');}});await loading;
    assert.equal(parsed,0);assert.equal(h.$('#crawlSheet').hidden,true);assert.equal(h.value('crawlView.id'),'');assert.equal(h.value('crawlView.items.length'),0);assert.equal(h.value('crawlView.selected.size'),0);assert.equal(h.value('crawlView.interpretation'),'');
    for(const id of ['crawlResults','crawlSearchNote','crawlCurrent','crawlLastError','crawlSourceRating','crawlHistory'])assert.ok(!h.$('#'+id).innerHTML.includes('SECRET'),id);
    assert.equal(h.$('#crawlEditURL').value,'');assert.equal(h.$('#crawlExport').disabled,true);assert.equal(h.calls[0].options.signal.aborted,true);
    h.calls[0].resolve(response('https://example.test/SECRET'));await exporting;await h.context.copyCrawlAddresses();assert.equal(copied,0);assert.equal(h.calls.length,2);assert.match(h.messages[0],/权限已变化/);
    const h2=harness();const old=h2.context.loadCrawlItems();h2.context.storageOwner='user:two';h2.context.resetCrawl();h2.value("crawlView.id='9';crawlView.items=[{id:9,title:'new owner'}];");h2.calls[0].resolve(response({error:'old expired'},403));await old;assert.equal(h2.value('crawlView.id'),'9');assert.equal(h2.value('crawlView.items[0].title'),'new owner');
    console.log('PASS 403 immediately clears private results/selection/export state, rejects late exports and cannot reset a newer owner');
  }
  {
    const h = harness();
    h.value("crawlView.items=[{id:1,title:'Known',duration:3661},{id:2,title:'Legacy'},{id:3,title:'Invalid',duration:'<img src=x onerror=bad()>'},{id:4,title:'Unknown',duration:null}];crawlView.total=4;");
    h.context.renderCrawlResults();const cards=h.$('#crawlResults').innerHTML;
    assert.match(cards,/时长 1:01:01/);assert.equal((cards.match(/时长未知/g)||[]).length,3);assert.ok(!cards.includes('<img'));assert.equal(h.calls.length,0);
    for(const value of [undefined,null,0,-1,Infinity,NaN,true,'90'])assert.equal(h.context.crawlDuration(value),'时长未知');
    assert.equal(h.context.crawlDuration(65),'时长 1:05');
    console.log('PASS collection durations use safe positive seconds, retain unknown legacy values and perform no extra fetch');
  }
  {
    const h = harness();
    h.value("crawlView.crawl={id:7,source_classification:'general',status:'running',max_pages:50,pages_done:30,details_done:400,errors:2,error:'502 Bad Gateway'}");
    h.context.renderCrawlProgress();
    assert.match(h.$('#crawlLastError').textContent,/最近错误记录/);
    assert.match(h.$('#crawlLastError').textContent,/历史记录不代表仍在失败/);
    assert.match(h.$('#crawlLastError').textContent,/当前失败 2 页/);
    assert.ok(!h.$('#crawlLastError').textContent.includes('已经恢复'));
    console.log('PASS last gateway error is labelled historical without claiming its page has recovered');
  }
  {
    const h = harness();
    h.$('#crawlURL').value = 'javascript:alert(1)'; await h.context.startCrawl(event);
    h.$('#crawlURL').value = 'https://example.com/videos'; h.$('#crawlMaxPages').value = '501'; await h.context.startCrawl(event);
    assert.equal(h.calls.length, 0); assert.match(h.value('crawlView.error'), /1–500/);
    assert.equal(h.context.crawlURL('javascript:alert(1)'), '');
    assert.ok(!h.context.crawlLink('javascript:alert(1)', '<img onerror=x>').includes('<img'));
    assert.deepEqual(Array.from(h.context.crawlAddresses({page_url: 'https://example.com/p', media_urls: ['https://cdn.example.com/v.mp4', 'https://cdn.example.com/v.mp4', 'javascript:1']})), ['https://cdn.example.com/v.mp4']);
    assert.deepEqual(Array.from(h.context.crawlAddresses({page_url: 'https://example.com/p', media_urls: []})), ['https://example.com/p']);
    assert.deepEqual(Array.from(h.context.crawlAddresses({url: 'https://example.com/video/1', page_url: 'https://example.com/list', media_urls: []})), ['https://example.com/video/1']);
    console.log('PASS URL safety, media deduplication/fallback and bounded page input');
  }
  {
    const h = harness(); h.$('#crawlQuery').value = 'Python*入门?'; h.$('#crawlSearchMode').value = 'wildcard';
    const first = h.context.searchCrawl(event);
    assert.equal(new URLSearchParams(h.calls[0].url.split('?')[1]).get('q'), 'Python*入门?');
    assert.equal(new URLSearchParams(h.calls[0].url.split('?')[1]).get('mode'), 'wildcard');
    h.$('#crawlQuery').value = '课程[0-9]+'; h.$('#crawlSearchMode').value = 'regex';
    const second = h.context.searchCrawl(event);
    assert.equal(h.calls[0].options.signal.aborted, true);
    h.calls[1].resolve(response({crawl: {id: 7}, items: [{id: 22, title: 'new'}], total: 1})); await second;
    h.calls[0].resolve(response({crawl: {id: 7}, items: [{id: 11, title: 'stale'}], total: 1})); await first;
    assert.equal(h.value('crawlView.items[0].id'), 22); assert.equal(h.value('crawlView.filter.mode'), 'regex');
    h.$('#crawlQuery').value = '[';
    const invalid = h.context.searchCrawl(event); h.calls[2].resolve(response({error: '无效正则表达式'}, 400)); await invalid;
    assert.equal(h.$('#crawlQuery').value, '['); assert.equal(h.value('crawlView.items[0].id'), 22);
    assert.equal(h.value('crawlView.filter.q'), '课程[0-9]+'); assert.match(h.value('crawlView.error'), /无效正则/);
    console.log('PASS wildcard/regex transport, superseded request cancellation and failed-search preservation');
  }
  {
    const h = harness(); h.value("crawlView.filter={q:'零基础学编程',mode:'ai'};crawlView.items=[{id:22,title:'AI snapshot'}];");
    const poll = h.context.refreshCrawl();
    h.calls[0].resolve(response({crawls: [{id: 7, status: 'running', pages_done: 8, found: 30}]})); await poll;
    assert.equal(h.calls.length, 1, 'metadata poll must never perform an AI search');
    assert.equal(h.value('crawlView.items[0].title'), 'AI snapshot'); assert.equal(h.value('crawlView.crawl.pages_done'), 8);
    assert.equal(h.timers.size, 1);
    h.context.suspendCrawl(); assert.equal(h.timers.size, 0);
    console.log('PASS semantic snapshot is stable and closing stops polling');
  }
  {
    const h = harness(), json = deferred();
    const request = h.context.loadCrawlItems();
    h.calls[0].resolve({ok: true, json: () => json.promise}); await Promise.resolve(); await Promise.resolve();
    h.context.storageOwner = 'user:two'; h.context.resetCrawl();
    json.resolve({crawl: {id: 7}, items: [{id: 99, title: 'private old account'}], total: 1}); await request;
    assert.equal(h.value('crawlView.items.length'), 0); assert.equal(h.value('crawlView.id'), '');
    assert.equal(h.$('#crawlSheet').hidden, true); assert.equal(h.calls[0].options.signal.aborted, true);
    assert.equal(h.messages.length, 0);
    console.log('PASS account switch rejects stale response bodies and clears all private UI state');
  }
  {
    const h = harness(); h.value("crawlView.items=[{id:1,page_url:'https://example.com/1'},{id:2,page_url:'https://example.com/2'}];crawlView.total=2;");
    h.context.pickCrawlPage(true); assert.equal(h.value('crawlView.selected.size'), 2);
    const importing = h.context.importCrawl(false);
    assert.deepEqual(JSON.parse(h.calls[0].options.body).ids, [1, 2]);
    h.calls[0].resolve(response({added: 1, duplicates: 1, linked: 0})); await importing;
    assert.equal(h.value('crawlView.selected.size'), 0); assert.equal(h.value('crawlView.items[0].imported'), true);
    assert.match(h.messages[0], /已添加下载 1 个.*已存在 1 个/);
    h.value("crawlView.crawl={id:7,source_classification:'general',status:'limited',max_pages:480,pages_done:480};");
    h.$('#crawlMaxPages').value = '480'; const continuing = h.context.controlCrawl();
    assert.equal(JSON.parse(h.calls[1].options.body).max_pages, 500);
    h.calls[1].resolve(response({crawl: {id: 7, status: 'running', max_pages: 500}})); await continuing;
    assert.equal(h.$('#crawlMaxPages').value, '500');
    console.log('PASS selected imports and bounded continuation');
  }
  {
    const h = harness();
    h.value("crawlView.crawl={id:7,source_classification:'general',status:'limited',max_pages:351,pages_done:205,found:5000,limit_reason:'previous_results'};");
    h.$('#crawlMaxPages').value = '351';
    assert.equal(h.context.crawlStatus(h.value('crawlView.crawl')), '可继续采集');
    const continuing = h.context.controlCrawl();
    assert.equal(JSON.parse(h.calls[0].options.body).max_pages, 351);
    h.calls[0].resolve(response({crawl:{id:7,status:'queued',max_pages:351}})); await continuing;
    console.log('PASS legacy result cap resumes at the original directory-page budget');
  }
  {
    const h = harness(); h.context.loadCrawlItems = async () => {};
    h.$('#crawlURL').value = 'https://example.com/a\nhttps://example.com/b，https://example.com/a';
    h.$('#crawlRefreshHours').value = '0';
    const creating = h.context.startCrawl(event);
    assert.deepEqual(JSON.parse(h.calls[0].options.body), {urls:['https://example.com/a','https://example.com/b'],max_pages:50,refresh_hours:0});
    h.calls[0].resolve(response({crawls:[{id:9,url:'https://example.com/a',status:'queued'}],errors:[{url:'https://example.com/b',error:'暂时不可用'}]}));
    await creating;
    assert.equal(h.value('crawlView.id'), '9');
    assert.equal(h.$('#crawlURL').value, 'https://example.com/b');
    assert.match(h.value('crawlView.error'), /example.com\/b.*暂时不可用/);
    h.$('#crawlURL').value = Array.from({length:21}, (_, i) => 'https://example.com/' + i).join('\n');
    await h.context.startCrawl(event); assert.equal(h.calls.length, 1); assert.match(h.value('crawlView.error'), /1–20/);
    console.log('PASS batch URL deduplication, 20-task limit, partial success and retry preservation');
  }
  {
    const h = harness(); h.value("crawlView.crawl={id:7,source_classification:'general',url:'https://example.com/old',status:'stopped',max_pages:50,pages_done:12,refresh_hours:6};crawlView.crawls=[crawlView.crawl];");
    h.context.editCrawl(); assert.equal(h.$('#crawlEditURL').value, 'https://example.com/old');
    h.$('#crawlEditMaxPages').value = '10'; await h.context.saveCrawlEdit(event);
    assert.equal(h.calls.length, 0); assert.match(h.value('crawlView.manageError'), /12–500/);
    h.$('#crawlEditURL').value = 'https://example.com/new'; h.$('#crawlEditMaxPages').value = '20';
    h.value("crawlView.items=[{id:99}];crawlView.total=1;crawlView.selected.set('99',{});crawlView.filter={q:'old',mode:'ai'};");
    const edit = h.context.saveCrawlEdit(event);
    assert.deepEqual(JSON.parse(h.calls[0].options.body), {url:'https://example.com/new',max_pages:20,refresh_hours:6});
    h.calls[0].resolve(response({crawl:{id:7,url:'https://example.com/new',status:'queued',max_pages:20}})); await edit;
    assert.equal(h.value('crawlView.editing'), false); assert.equal(h.value('crawlView.items.length'), 0);
    assert.equal(h.value('crawlView.selected.size'), 0); assert.equal(h.value('crawlView.filter.q'), '');
    h.context.editCrawl(); assert.equal(h.value('crawlView.editing'), false, 'running tasks cannot enter editing');
    console.log('PASS edit bounds and changed-directory reset without an automatic AI search');
  }
  {
    const h = harness(); h.context.loadCrawlItems = async () => {};
    h.value("crawlView.crawls=[crawlView.crawl];");
    await h.context.deleteCrawl(); assert.equal(h.calls.length, 0);
    h.context.askDeleteCrawl(); assert.equal(h.calls.length, 0);
    h.context.cancelDeleteCrawl(); await h.context.deleteCrawl(); assert.equal(h.calls.length, 0);
    h.context.askDeleteCrawl(); const deleting = h.context.deleteCrawl();
    assert.equal(h.calls[0].url, '/api/crawls/7/delete');
    h.calls[0].resolve(response({ok:true,id:7})); await deleting;
    assert.equal(h.value('crawlView.id'), ''); assert.equal(h.value('crawlView.crawls.length'), 0);
    assert.equal(h.value('crawlView.deleting'), false);
    console.log('PASS task deletion requires confirmation and clears stale task/results');
  }
  {
    const h = harness();
    assert.equal(h.value('crawlView.issuesOpen'), false); assert.equal(h.calls.length, 0);
    const opening = h.context.toggleCrawlIssues();
    assert.equal(h.calls[0].url, '/api/crawls/7/issues?offset=0&limit=20');
    h.calls[0].resolve(response({generation:3,total:22,failed:20,retrying:2,can_retry:true,
      health:{completed:180,failure_rate:.1}, items:[{id:12,url:'https://example.com/fail',kind:'detail',state:'failed',failures:3,error:'<img src=x onerror=bad()>',heal:{state:'diagnosed'}},{id:13,url:'javascript:alert(1)',kind:'list',state:'running',failures:1,heal:{state:'not_a_bug'}}]}));
    await opening;
    assert.match(h.$('#crawlIssuesSummary').textContent, /10\.00%/);
    assert.match(h.$('#crawlIssuesList').innerHTML, /已诊断，需处理/);
    assert.match(h.$('#crawlIssuesList').innerHTML, /非代码故障/);
    assert.match(h.$('#crawlIssuesList').innerHTML, /正在自动重试/);
    assert.ok(!h.$('#crawlIssuesList').innerHTML.includes('<img'));
    assert.ok(!h.$('#crawlIssuesList').innerHTML.includes('href="javascript:'));
    const next = h.context.pageCrawlIssues(1); assert.match(h.calls[1].url, /offset=20/);
    h.calls[1].resolve(response({generation:3,total:0,failed:0,retrying:0,privacy_locked:true,notice:'需要解锁',items:[]}));
    await next;
    assert.equal(h.$('#crawlIssuesList').innerHTML, ''); assert.equal(h.$('#crawlIssuesRetry').disabled, true);
    assert.equal(h.calls.length,2);assert.equal(h.value('crawlView.id'),'');assert.equal(h.$('#crawlPrivacyNotice').hidden,false);
    h.value('crawlView.issues={health:{completed:0,failure_rate:null},items:[],total:0};');h.context.renderCrawlIssues();
    assert.ok(!h.$('#crawlIssuesSummary').textContent.includes('%'));
    console.log('PASS issue details load on demand, paginate, escape errors and show truthful retry/heal/privacy states');
  }
  {
    const h = harness(), json = deferred();
    const opening = h.context.toggleCrawlIssues();
    h.calls[0].resolve({ok:true,json:()=>json.promise});await Promise.resolve();await Promise.resolve();
    h.context.suspendCrawl();
    assert.equal(h.$('#crawlIssuesPanel').hidden, true);assert.equal(h.$('#crawlIssuesList').innerHTML, '');
    json.resolve({items:[{url:'https://private.example.test'}],total:1});await opening;
    assert.equal(h.value('crawlView.issues'),null);assert.equal(h.calls[0].options.signal.aborted,true);
    const newRequest=h.context.toggleCrawlIssues();h.context.storageOwner='user:two';h.context.resetCrawl();
    h.calls[1].resolve(response({items:[{url:'https://old-owner.example.test'}],total:1}));await newRequest;
    assert.equal(h.value('crawlView.issues'),null);assert.equal(h.$('#crawlIssuesList').innerHTML,'');
    console.log('PASS close, privacy reset and account change cancel issue requests and discard delayed diagnostic URLs');
  }
  {
    const h=harness();h.value("crawlView.issuesOpen=true;crawlView.issues={generation:8,can_retry:true,failed:2,total:2,items:[]};");
    const retry=h.context.retryFailedCrawlPages();await h.context.retryFailedCrawlPages();
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/crawls/7/retry-failed');
    assert.deepEqual(JSON.parse(h.calls[0].options.body),{generation:8});
    h.calls[0].resolve(response({requeued:2,crawl:{id:7,status:'running',max_pages:351}}));
    for(let i=0;i<8;i++)await Promise.resolve();
    h.calls[1].resolve(response({generation:8,can_retry:false,failed:0,retrying:2,total:2,items:[]}));await retry;
    assert.match(h.messages[0],/2 个失败页面/);assert.equal(h.value('crawlView.crawl.max_pages'),351);
    assert.equal(h.value('crawlView.mutating'),false);
    h.value("crawlView.crawl.status='stopped';crawlView.issues.can_retry=true;");await h.context.retryFailedCrawlPages();
    assert.equal(h.calls.length,2,'stopped task must not be revived by retry');
    h.value("crawlView.crawl.status='running';");const stale=h.context.retryFailedCrawlPages();
    h.calls[2].resolve(response({error:'采集进度已变化，请刷新后重试'},409));await stale;
    assert.match(h.value('crawlView.issuesError'),/进度已变化/);assert.equal(h.value('crawlView.issues'),null);
    console.log('PASS failed-page retry is generation fenced, coalesces clicks, preserves page budget and respects stopped tasks');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
