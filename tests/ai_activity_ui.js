// node tests/ai_activity_ui.js — lazy, bounded receipts and private request lifecycle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/ai_activity.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {resolve, promise}; }
function response(data, status = 200) { return {ok: status < 400, status, json: async () => data}; }
function harness() {
  const elements = new Map(), calls = [], notices = [];
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {hidden: false, innerHTML: ''});
    return elements.get(selector);
  };
  $('#aiActivitySheet').hidden = true;
  const context = vm.createContext({$, Map, Number, Date, URLSearchParams, AbortController,
    state: {admin: false}, storageOwner: 'user:alice', privateViewEpoch: 0,
    toast: message => notices.push(message),
    esc: text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
    fetch: (url, options) => { const call = {...deferred(), url, options}; calls.push(call); return call.promise; },
  });
  vm.runInContext(source, context);
  return {context, $, calls, notices, value: expression => vm.runInContext(expression, context)};
}
const row = (id = 1, extra = {}) => ({id, ts: 1000, purpose: 'search', label: 'AI 搜索', engine: 'codex',
  model: 'gpt-6-luna', status: 'done', queue_seconds: null, run_seconds: 3, total_seconds: 6,
  retries: null, fallback: null, ...extra});
const page = (items = [row()], extra = {}) => ({items, summary: [{purpose:'search',label:'AI 搜索',total:1,fallbacks:0,failed:0,fallback_rate:0}],
  purposes: [{value:'search',label:'AI 搜索'}], total: items.length, more: false, storage_owner: 'user:alice', can_system: false, ...extra});
(async () => {
  {
    const h = harness(); assert.equal(h.calls.length, 0);
    const opening = h.context.openAIActivity(); assert.equal(h.calls.length, 1);
    assert.match(h.calls[0].url, /^\/api\/ai\/activity\?/); assert.match(h.calls[0].url, /limit=30/);
    h.calls[0].resolve(response(page([row(1, {label:'<img src=x>', model:'<script>secret', fallback:'<b>error'})]))); await opening;
    const body = h.$('#aiActivityBody').innerHTML;
    assert.ok(!body.includes('<img')); assert.ok(!body.includes('<script>')); assert.ok(!body.includes('<b>error'));
    assert.match(body, /未观测/); assert.ok(!body.includes('AI 记录范围'));
    console.log('PASS lazy GET only, output escaped, null timings not zero, ordinary users get no system control');
  }
  {
    const h = harness(); h.context.state.admin = true;
    const opening = h.context.openAIActivity(); h.calls[0].resolve(response(page([row()], {total: 2, more: true, can_system: true}))); await opening;
    const more = h.context.loadAIActivity(true); assert.match(h.calls[1].url, /offset=1/);
    h.calls[1].resolve(response(page([row(2)], {total: 2, can_system: true}))); await more;
    assert.equal(h.value('aiActivityView.items.length'), 2); assert.match(h.$('#aiActivityBody').innerHTML, /AI 记录范围/);
    const switching = h.context.changeAIActivity('scope', 'system'); assert.match(h.calls[2].url, /scope=system/);
    assert.equal(h.value('aiActivityView.items.length'), 0);
    h.calls[2].resolve(response(page([], {scope:'system',can_system:true}))); await switching;
    console.log('PASS bounded paging and explicitly selected system scope');
  }
  {
    for (const transition of ['close', 'owner', 'privacy']) {
      const h = harness(), opening = h.context.openAIActivity();
      if (transition === 'owner') h.context.storageOwner = 'user:bob';
      if (transition === 'privacy') ++h.context.privateViewEpoch;
      h.context.resetAIActivity();
      assert.equal(h.calls[0].options.signal.aborted, true);
      h.calls[0].resolve(response(page([row(1, {label:'PRIVATE LABEL'})]))); await opening;
      assert.equal(h.$('#aiActivitySheet').hidden, true); assert.equal(h.$('#aiActivityBody').innerHTML, '');
    }
    console.log('PASS close, owner change and privacy reset abort and ignore delayed private output');
  }
  {
    const h = harness(); const opening = h.context.openAIActivity();
    h.calls[0].resolve(response(page())); await opening;
    const loading = h.context.loadAIActivity();
    h.calls[1].resolve(response({error: '隐私权限已失效'}, 403)); await loading;
    assert.equal(h.$('#aiActivitySheet').hidden, true); assert.equal(h.$('#aiActivityBody').innerHTML, '');
    assert.deepEqual(h.notices, ['隐私权限已失效']);
    console.log('PASS expired/revoked access clears existing rows and aggregates immediately');
  }
  {
    const h = harness(); const opening = h.context.openAIActivity();
    const changing = h.context.changeAIActivity('days', '30'); assert.equal(h.calls[0].options.signal.aborted, true);
    h.calls[1].resolve(response(page([row(2)]))); await changing;
    h.calls[0].resolve(response(page([row(1, {label:'STALE'})]))); await opening;
    assert.equal(h.value('aiActivityView.items[0].id'), 2); assert.ok(!h.$('#aiActivityBody').innerHTML.includes('STALE'));
    assert.match(h.calls[1].url, /days=30/);
    console.log('PASS changing filters invalidates in-flight response and keeps current selection');
  }
  {
    assert.match(html, /id="aiActivitySheet"/); assert.match(html, /onclick="openAIActivity\(\)"/);
    assert.ok((html.match(/resetAIActivity\(\)/g) || []).length >= 3);
    const h = harness(); const opening = h.context.openAIActivity();
    h.calls[0].resolve(response(page([row(1,{label:'FOREIGN'})],{storage_owner:'user:bob'}))); await opening;
    assert.equal(h.$('#aiActivityBody').innerHTML, '');
    console.log('PASS page hooks are present and mismatched owner cannot render');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
