// node tests/trash_ui.js — permanent-deletion and owner-isolation regressions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/trash.js'), 'utf8');
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
const response = (data, status = 200) => ({ok: status < 400, json: async () => data});
function harness() {
  const calls = [], messages = [], elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {hidden: false, innerHTML: '', textContent: '', addEventListener() {}, scrollIntoView() {}, focus() {}});
    return elements.get(selector);
  };
  const context = vm.createContext({$, AbortController, DOMException,
    storageOwner: 'user:one', toast: text => messages.push(text), hide: el => { el.hidden = true; },
    esc: text => String(text ?? '').replace(/[&<>"']/g, x => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[x])),
    fetch: (url, options) => { const call = {...deferred(), url, options}; calls.push(call); return call.promise; },
  });
  vm.runInContext(source, context);
  return {context, $, calls, messages, value: expr => vm.runInContext(expr, context)};
}
(async () => {
  {
    const h = harness(), late = deferred(), pending = h.context.loadTrash();
    h.calls[0].resolve({ok: true, json: () => late.promise}); await Promise.resolve(); await Promise.resolve();
    h.context.storageOwner = 'user:two'; h.context.resetTrash();
    late.resolve({items: [{id: 1, title: 'private'}], can_scan: true}); await pending;
    assert.equal(h.value('trashView.items.length'), 0); assert.equal(h.value('trashView.canScan'), false);
    assert.equal(h.calls[0].options.signal.aborted, true); assert.equal(h.$('#trashSheet').hidden, true);
    assert.ok(!h.$('#trashList').innerHTML.includes('private'));
    console.log('PASS trash account switch aborts requests and drops stale JSON bodies');
  }
  {
    const h = harness(), first = h.context.loadTrash(), second = h.context.loadTrash();
    h.calls[1].resolve(response({items: [{id: 2, title: '<script>evil</script>', orphan: true, expires_at: null, files_count: 1}], can_scan: true})); await second;
    h.calls[0].resolve(response({items: [{id: 1, title: 'stale'}], can_scan: false})); await first;
    assert.equal(h.value('trashView.items[0].id'), 2); assert.match(h.$('#trashList').innerHTML, /历史遗留 · 待确认/);
    assert.ok(!h.$('#trashList').innerHTML.includes('<script>'));
    assert.equal(h.$('#trashScan').hidden, false);
    console.log('PASS trash refresh ordering, orphan retention label and escaped titles');
  }
  {
    const h = harness(); h.value('trashView.items=[{id:1,title:"one"},{id:2,title:"two"},{id:3,title:"three"}];trashView.total=3;');
    await h.context.deleteTrash(); assert.equal(h.calls.length, 0, 'permanent deletion requires a concrete selection');
    h.context.askDeleteTrash('all'); assert.equal(h.calls.length, 0, 'asking for confirmation never mutates');
    h.context.cancelDeleteTrash(); await h.context.deleteTrash(); assert.equal(h.calls.length, 0);
    h.context.askDeleteTrash('all'); const deleting = h.context.deleteTrash();
    assert.match(h.calls[0].url, /\/1\/delete$/); h.calls[0].resolve(response({ok: true, kept_shared: true}));
    await new Promise(setImmediate);
    assert.match(h.calls[1].url, /\/2\/delete$/); h.calls[1].resolve(response({error: '文件暂时无法删除'}, 409)); await deleting;
    assert.deepEqual(Array.from(h.value('trashView.items.map(x=>x.id)')), [2, 3]);
    assert.deepEqual(Array.from(h.value('trashView.confirm')), [2, 3]);
    assert.equal(h.value('trashView.total'), 2); assert.match(h.value('trashView.error'), /已删除 1 个.*文件暂时无法删除/);
    console.log('PASS explicit permanent-delete confirmation and partial failure keeps remaining entries');
  }
  {
    const h = harness(); h.value('trashView.items=[{id:1},{id:2}];');
    h.context.askDeleteTrash('all'); const deleting = h.context.deleteTrash();
    h.context.suspendTrash(); h.calls[0].resolve(response({ok: true})); await deleting;
    assert.equal(h.calls.length, 1, 'closing the panel must stop the rest of a bulk deletion');
    assert.equal(h.messages.length, 0); assert.equal(h.calls[0].options.signal.aborted, true);
    await h.context.scanTrash(); assert.equal(h.calls.length, 1, 'non-admin state cannot request an orphan scan');
    console.log('PASS close cancels remaining deletion operations and non-admin scan is gated');
  }
  {
    const h = harness(); h.value('trashView.items=[{id:1}];trashView.total=1;');
    const restore = h.context.restoreTrash(1); h.calls[0].resolve(response({error: '恢复失败'}, 409)); await restore;
    assert.equal(h.value('trashView.items.length'), 1); assert.match(h.value('trashView.error'), /恢复失败/);
    const retry = h.context.restoreTrash(1); h.calls[1].resolve(response({ok: true, job_id: 10})); await retry;
    assert.equal(h.value('trashView.items.length'), 0); assert.equal(h.value('trashView.total'), 0);
    assert.deepEqual(h.messages, ['已恢复到片库']);
    console.log('PASS failed restore remains recoverable and successful restore clears only that entry');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
