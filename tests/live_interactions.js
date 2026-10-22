// Run with node tests/live_interactions.js; no browser, network or real account.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/live.js'), 'utf8');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function harness() {
  const calls = [], timers = new Map(), messages = [];
  let sequence = 0;
  const context = vm.createContext({
    chip: 'live', storageOwner: 'user:one', state: {user: 'one'},
    document: {hidden: false, addEventListener() {}, activeElement: {blur() {}}},
    URLSearchParams, saveNavigation() {},
    esc: String, toast: text => messages.push(text),
    setTimeout: (fn, delay) => { timers.set(++sequence, {fn, delay}); return sequence; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(source, context);
  context.lvApi = (url, body) => {
    const request = {...deferred(), url, body};
    calls.push(request);
    return request.promise;
  };
  context.renderLive = () => {};
  return {context, calls, timers, messages, value: expression => vm.runInContext(expression, context)};
}
function form() {
  const button = {disabled: false, textContent: '保存'}, error = {textContent: ''};
  return {button, error, isConnected: true,
    querySelectorAll: () => [],
    querySelector: selector => selector === '#lvFormError' ? error : button};
}

(async () => {
  {
    const h = harness();
    assert.equal(h.value('liveTab'), 'review');
    const filtered = h.context.lvFilter('source', '7');
    assert.equal(new URLSearchParams(h.calls[0].url.split('?')[1]).get('source'), '7');
    h.calls[0].resolve({clips: [], has_more: true, next_cursor: '0:27', total: 151});
    await filtered;
    const next = h.context.lvNextPage();
    assert.equal(new URLSearchParams(h.calls[1].url.split('?')[1]).get('cursor'), '0:27');
    h.calls[1].resolve({clips: [], has_more: false, next_cursor: null, total: 151});
    await next;
    const previous = h.context.lvPreviousPage();
    assert.equal(new URLSearchParams(h.calls[2].url.split('?')[1]).has('cursor'), false);
    h.calls[2].resolve({clips: [], has_more: true, next_cursor: '0:27', total: 151});
    await previous;
    const state = h.context.lvFilter('state', 'failed');
    assert.equal(new URLSearchParams(h.calls[3].url.split('?')[1]).get('state'), 'failed');
    assert.equal(new URLSearchParams(h.calls[3].url.split('?')[1]).get('source'), '7');
    assert.equal(h.value('livePageHistory.length'), 0);
    h.calls[3].resolve({clips: [], has_more: false, total: 0});
    await state;
    console.log('PASS default review workspace and server-side source/state/cursor navigation');
  }
  {
    const h = harness(), modal = {dataset: {}};
    let markup = '';
    h.context.$ = () => modal;
    h.context.lvSheet = (_title, body) => { markup = body; };
    vm.runInContext(`liveData={sources:[],clips:[
      {id:7,lane:'highlight',state:'ready',title:'亮点',preview:'/clip7',start:0,end:30,can_upload:false,can_edit:true,needs_attention:true},
      {id:8,lane:'topic',state:'ready',title:'话题',preview:'/clip8',start:0,end:60,can_upload:true,can_edit:true,needs_attention:true}
    ]}`, h.context);
    h.context.lvPreview(7);
    assert.ok(markup.includes('当前列表 1 / 2'));
    assert.ok(markup.includes('onclick="lvPreview(8)"'));
    assert.ok(!markup.includes('确认投稿 B站'));
    h.context.lvPreview(8);
    assert.ok(markup.includes('当前列表 2 / 2'));
    assert.ok(markup.includes('onclick="lvPreview(7)"'));
    assert.ok(markup.includes('确认投稿 B站'));
    assert.equal(h.calls.length, 0, 'continuous preview never submits a clip');
    console.log('PASS continuous cross-lane preview and explicit server-approved publishing action');
  }
  {
    const h = harness(), a = h.context.loadLive(), b = h.context.loadLive();
    assert.equal(h.calls.length, 1, 'navigation and bootstrap share the same request');
    h.calls[0].resolve({marker: 'first'});
    await Promise.all([a, b]);
    assert.equal(h.value('liveData.marker'), 'first');
    assert.equal(h.timers.size, 1);

    const stale = h.context.loadLive(), fresh = h.context.loadLive({force: true});
    assert.equal(h.calls.length, 3, 'a completed mutation needs a new snapshot');
    h.calls[2].resolve({marker: 'after-save'});
    await fresh;
    h.calls[1].resolve({marker: 'before-save'});
    await stale;
    assert.equal(h.value('liveData.marker'), 'after-save');
    assert.equal(h.timers.size, 1, 'stale responses cannot restart a second poll');

    const failed = h.context.loadLive();
    h.calls[3].reject(Error('连接失败'));
    await failed;
    assert.equal(h.value('liveError'), '连接失败');
    assert.equal(h.value('liveData.marker'), 'after-save');
    const retry = h.context.loadLive();
    h.calls[4].resolve({marker: 'recovered'});
    await retry;
    assert.equal(h.value('liveError'), '');
    console.log('PASS live request coalescing, fresh snapshots, stale response and recovery');
  }
  {
    const h = harness(), old = h.context.loadLive();
    vm.runInContext("liveTab='topic'", h.context);
    const next = h.context.loadLive();
    h.calls[1].resolve({tab: 'topic'});
    await next;
    h.calls[0].resolve({tab: 'highlight'});
    await old;
    assert.equal(h.value('liveData.tab'), 'topic');
    const changingOwner = h.context.loadLive();
    h.context.storageOwner = 'user:two';
    h.calls[2].resolve({tab: 'private'});
    await changingOwner;
    assert.equal(h.value('liveData.tab'), 'topic');
    h.context.document.hidden = true;
    await h.context.loadLive();
    assert.equal(h.calls.length, 3);
    assert.equal(h.timers.size, 0);
    console.log('PASS tab/account switches ignore stale live data and hidden pages stop polling');
  }
  {
    const h = harness(), editor = form();
    let closes = 0, reloads = 0;
    h.context.lvClose = () => { closes++; editor.isConnected = false; };
    h.context.loadLive = async options => { assert.equal(options.force, true); reloads++; };
    const first = h.context.lvSubmit(editor, '/clips', {title: 'one'}, '正在保存…', '已保存');
    await h.context.lvSubmit(editor, '/clips', {title: 'two'}, '正在保存…', '已保存');
    assert.equal(h.calls.length, 1, 'double submit sends only one mutation');
    assert.equal(editor.button.disabled, true);
    h.calls[0].reject(Error('保存失败'));
    await first;
    assert.equal(editor.error.textContent, '保存失败');
    assert.equal(editor.button.disabled, false);
    assert.equal(editor.button.textContent, '保存');
    assert.equal(closes, 0);
    const retry = h.context.lvSubmit(editor, '/clips', {}, '正在保存…', '已保存');
    editor.isConnected = false; // The user has closed this editor and opened another.
    h.calls[1].resolve({});
    await retry;
    assert.equal(closes, 0, 'completion cannot close the replacement modal');
    assert.equal(reloads, 1);
    const detached = form();
    const failure = h.context.lvSubmit(detached, '/clips', {}, '保存中', '已保存');
    detached.isConnected = false;
    h.calls[2].reject(Error('稍后失败'));
    await failure;
    assert.equal(detached.error.textContent, '', 'errors stay with their original connected form');
    const switched = form();
    const saving = h.context.lvSubmit(switched, '/clips', {}, '保存中', '已保存');
    h.context.storageOwner = 'user:two';
    h.calls[3].resolve({});
    await saving;
    assert.equal(reloads, 1);
    assert.equal(h.messages.length, 1, 'old-account completion cannot notify a different account');
    console.log('PASS save locking, retry, detached editors and account isolation');
  }
  {
    const h = harness(), editor = form();
    editor.elements = Object.fromEntries(Object.entries({
      session: 'session-one', start: '00:01:00', end: '00:00:30', title: '标题',
      cover_title: '', description: '', tags: '直播切片',
    }).map(([name, value]) => [name, {value}]));
    await h.context.lvSaveClip({target: editor, preventDefault() {}}, 0);
    assert.equal(h.calls.length, 0);
    assert.match(editor.error.textContent, /结束时间/);
    console.log('PASS invalid clip ranges preserve the form without sending a request');
  }
  {
    const h = harness(), editor = form(), output = {textContent: '', innerHTML: ''};
    const button = {disabled: false, textContent: '生成文案建议', closest: () => editor};
    editor.querySelector = () => output;
    editor.elements = Object.fromEntries(Object.entries({
      session: 'original', start: '00:00:00', end: '00:00:30', copy_focus: '实用建议',
      title: '原来标题', cover_title: '', description: '原来简介', tags: '原标签',
    }).map(([key, value]) => [key, {value, focus() {}}]));
    const original = Object.fromEntries(Object.entries(editor.elements).map(([key, el]) => [key, el.value]));
    const pending = h.context.lvSuggestCopy(button);
    await h.context.lvSuggestCopy(button);
    assert.equal(h.calls.length, 1, 'copy generation requires one explicit request');
    const suggestion = {title: '新标题', cover_title: '封面短句', description: '新简介',
      tags: ['创业', '经营', '租金'], reason: '说明理由', evidence: [1]};
    h.calls[0].resolve({options: [suggestion], excerpts: [{id: 1, start: 0, end: 10, text: '真实字幕'}]});
    await pending;
    assert.equal(editor.elements.title.value, original.title, 'generation never overwrites the editor');
    assert.ok(output.innerHTML.includes('真实字幕'));
    editor.saving = true;
    h.context.lvApplyCopy(button, 0);
    assert.equal(editor.elements.title.value, original.title, 'saving cannot accept edits after its request body was sent');
    editor.saving = false;
    h.context.lvApplyCopy(button, 0);
    assert.equal(editor.elements.title.value, suggestion.title);
    assert.equal(editor.elements.tags.value, '创业,经营,租金');
    assert.equal(h.calls.length, 1, 'applying a suggestion does not save or publish');
    h.context.lvUndoCopy(button);
    assert.equal(editor.elements.title.value, original.title);
    assert.equal(editor.elements.description.value, original.description);

    const changed = h.context.lvSuggestCopy(button);
    editor.elements.end.value = '00:00:45';
    h.calls[1].resolve({options: [suggestion], excerpts: []});
    await changed;
    assert.ok(output.textContent.includes('已改变'));
    assert.equal(editor.copySuggestions, null);
    const switched = h.context.lvSuggestCopy(button);
    h.context.storageOwner = 'user:two';
    h.calls[2].resolve({options: [suggestion], excerpts: []});
    await switched;
    assert.equal(editor.copySuggestions, null);
    console.log('PASS grounded copy preview, explicit apply/undo, no save and stale-input/account guards');
  }
  {
    const h = harness(), editor = form(), output = {textContent: '', innerHTML: ''};
    const button = {disabled: false, textContent: '生成文案建议', closest: () => editor};
    editor.querySelector = () => output;
    editor.elements = Object.fromEntries(Object.entries({
      session: 'complete-story', start: '00:00:00', end: '02:30:00', copy_focus: '',
    }).map(([key, value]) => [key, {value}]));
    const generating = h.context.lvSuggestCopy(button);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].body.end, 9000);
    h.calls[0].resolve({options: [], excerpts: [], limitation: '字幕信息不足'});
    await generating;
    assert.ok(output.innerHTML.includes('字幕信息不足'));
    console.log('PASS complete topics over an hour can request copy suggestions');
  }
  {
    const h = harness();
    let html = '';
    h.context.lvSheet = (_title, body) => { html = body; };
    vm.runInContext(`liveData={sources:[],recordings:[],sessions:[{key:'recent',started:1}],
      clips:[{id:7,session_key:'historical',source_id:1,title:'旧切片',start:0,end:30,tags:[]}]}`, h.context);
    h.context.lvEditClip(7);
    assert.ok(html.includes('value="historical" selected'));
    assert.ok(html.includes('原始场次（历史记录）'));
    assert.ok(!html.includes('value="recent" selected'));
    console.log('PASS editing historical clips keeps the original recording for AI and manual edits');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
