// node tests/privacy_ui.js — explicit selection, unlock and stale-owner safety.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const between = (a, b) => html.slice(html.indexOf(a), html.indexOf(b, html.indexOf(a)));
const source = between('let privacySelectionPending =', '// Triple tap, or press and hold')
  + between('function lockPrivacy(quiet)', 'document.addEventListener("visibilitychange"')
  + between('async function privacyApi(body)', '/* ---------- removing (one or many) ---------- */');
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
function harness() {
  const calls = [], messages = [], elements = new Map(); let refreshes = 0, closes = 0;
  const button = {disabled: false};
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {hidden: true, innerHTML: '', textContent: '', value: '', dataset: {},
      focus() {}, select() {}, querySelector: () => button});
    return elements.get(selector);
  };
  const context = vm.createContext({$, storageOwner: 'user:one', privacyToken: 'token', privacyTimer: null,
    state: {user: 'one', privacy: {revealed: false}, jobs: [{id: 1, privacy_editable: true}]},
    refreshEpoch: 0, refreshController: null, workspaceLoading: false, shown: [], cardHtml: [], lastBody: '',
    digestState: {digests: [], pending: false, at: 0}, digestPick: 0, weeklyData: null, weeklyPick: 0,
    selected: new Set(), selecting: false, watching: null,
    document: {querySelectorAll: () => [button]},
    esc: value => String(value ?? '').replace(/[<>]/g, x => x === '<' ? '&lt;' : '&gt;'),
    toast: text => messages.push(text), openAccount() {}, openWatch() {}, updateSelbar() {}, cancelLearningPlan() {},
    refresh: async () => { refreshes++; }, toggleSelect() {},
    closeWatch: () => { closes++; context.watching = null; },
    fetch: (url, options) => { const call = {...deferred(), url, options}; calls.push(call); return call.promise; },
    setTimeout: () => 1, clearTimeout() {},
  });
  vm.runInContext(source, context);
  context.hide = element => {
    if (element === $('#privacySheet')) { context.cancelPrivacySelection(); vm.runInContext('++privacyMenuEpoch', context); }
    element.hidden = true;
  };
  return {context, $, button, calls, messages, refreshes: () => refreshes, closes: () => closes, value: expr => vm.runInContext(expr, context)};
}
(async () => {
  {
    const h = harness();
    h.context.privacyToken = null; h.context.updatePrivacySelectionVisibility(); assert.equal(h.button.hidden, true);
    h.context.privacyToken = 'unlocked'; h.context.updatePrivacySelectionVisibility(); assert.equal(h.button.hidden, false);
    h.context.lockPrivacy(true); assert.equal(h.button.hidden, true);
    assert.match(html, /\$\('#selHide'\)\.hidden = !state\.user \|\| !privacyToken/);
    assert.ok(html.includes("data-privacy-action${privacyToken ? '' : ' hidden'}"));
    console.log('PASS manual hidden-list controls appear only while privacy is unlocked and hide immediately on lock');
  }
  {
    const h = harness(); h.context.watching = {id: 1}; h.context.selecting = true; h.context.selected.add(1);
    const pending = h.context.changePrivacySelection([1, 1], true);
    await h.context.changePrivacySelection([1], true);
    assert.equal(h.calls.length, 1, 'double-click must not submit duplicate mutations');
    assert.deepEqual(JSON.parse(h.calls[0].options.body), {ids: [1], hidden: true});
    h.calls[0].resolve(response({ok: true, ids: [1]})); await pending;
    assert.equal(h.closes(), 1, 'a newly hidden video must close when reveal is off');
    assert.equal(h.context.selected.size, 0); assert.equal(h.context.selecting, false); assert.equal(h.refreshes(), 1);
    assert.match(h.messages[0], /加入隐藏清单/);
    console.log('PASS explicit owner-scoped body, duplicate-submit lock and hidden-player closure');
  }
  {
    const h = harness(); h.context.privacyToken = null;
    await h.context.changePrivacySelection([1], true);
    assert.equal(h.calls.length, 0, 'locked selection must wait for password verification');
    assert.match(h.$('#privacyBody').innerHTML, /验证后.*1 个视频加入/);
    h.$('#unlockPw').value = 'correct-password';
    const unlocking = h.$('#unlockForm').onsubmit({preventDefault() {}, target: h.$('#unlockForm')});
    assert.equal(h.calls[0].url, '/api/privacy/unlock');
    h.calls[0].resolve(response({token: 'new-token', expires_in: 1800})); await new Promise(setImmediate);
    assert.equal(h.calls[1].url, '/api/privacy/ids');
    h.calls[1].resolve(response({ok: true, ids: [1]})); await unlocking;
    assert.equal(h.context.privacyToken, 'new-token'); assert.equal(h.value('privacySelectionPending'), null);
    console.log('PASS password verification resumes only the explicit pending selection');
  }
  {
    const h = harness(); h.context.privacyToken = null;
    await h.context.changePrivacySelection([1], true);
    const unlocking = h.$('#unlockForm').onsubmit({preventDefault() {}, target: h.$('#unlockForm')});
    h.context.hide(h.$('#privacySheet'));
    h.calls[0].resolve(response({token: 'too-late', expires_in: 1800})); await unlocking;
    assert.equal(h.calls.length, 1); assert.equal(h.context.privacyToken, null); assert.equal(h.value('privacySelectionPending'), null);
    console.log('PASS cancelling the unlock dialog rejects late tokens and prevents hidden-list changes');
  }
  {
    const h = harness(), late = deferred(), request = h.context.changePrivacySelection([1], true);
    h.calls[0].resolve({ok: true, status: 200, json: () => late.promise}); await new Promise(setImmediate);
    h.context.storageOwner = 'user:two'; h.context.cancelPrivacySelection();
    late.resolve({ok: true, ids: [1]}); await request;
    assert.equal(h.refreshes(), 0); assert.equal(h.messages.length, 0); assert.equal(h.value('privacySelectionBusy'), false);
    console.log('PASS account switch drops stale mutation completion before refresh or notification');
  }
  {
    const h = harness(), staleToken = h.context.changePrivacySelection([1], true);
    h.calls[0].resolve(response({error: 'locked'}, 403)); await staleToken;
    assert.equal(h.context.privacyToken, null); assert.equal(h.value('privacySelectionPending.ids[0]'), 1);
    assert.match(h.$('#privacyBody').innerHTML, /验证身份/);
    const denied = harness(), invalid = denied.context.changePrivacySelection([2], true);
    denied.calls[0].resolve(response({error: '只能修改自己拥有的视频'}, 404)); await invalid;
    assert.equal(denied.context.privacyToken, 'token'); assert.equal(denied.value('privacySelectionPending'), null);
    assert.match(denied.messages[0], /自己拥有/);
    console.log('PASS token expiry requests unlock while ownership failures preserve the existing session');
  }
  {
    const h = harness(), menu = h.context.openPrivacy();
    h.calls[0].resolve(response({tags: ['私密'], library_tags: ['私密'], ids: [1], revealed: true, manual_items: [{id: 1, title: '<script>private</script>'}]})); await menu;
    assert.match(h.$('#privacyBody').innerHTML, /手动隐藏清单 · 1 个视频/);
    assert.ok(!h.$('#privacyBody').innerHTML.includes('<script>'));
    assert.match(h.$('#privacyBody').innerHTML, /changePrivacySelection\(\[1\], false, true\)/);
    const save = h.context.privacyApi({tags: ['私密', '新标签']});
    assert.deepEqual(JSON.parse(h.calls[1].options.body), {tags: ['私密', '新标签']});
    h.calls[1].resolve(response({error: '保存失败'}, 500)); assert.equal(await save, false);
    console.log('PASS escaped manual list and tag-only writes never overwrite stored manual IDs');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
