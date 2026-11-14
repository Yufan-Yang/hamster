// Isolated browser lifecycle: actions are explicit, private results cannot outlive their view.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/steward.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const data = (extra = {}) => ({storage_owner:'user:alice', items:[], counts:{unchecked:1},
  policy:{automatic:false,ai_planning:false,profile:'standard'}, more:false, busy:false,...extra});
function harness() {
  const elements = new Map(), calls = [], timers = new Map(), notices = [];
  let nextTimer = 0;
  const $ = key => {
    if (!elements.has(key)) elements.set(key, {hidden:false,innerHTML:'',value:'standard',checked:false});
    return elements.get(key);
  };
  $('#stewardSheet').hidden = true;
  const context = vm.createContext({$, Map, Number, Date, Object, JSON, AbortController,
    storageOwner:'user:alice', privateViewEpoch:0,
    toast: message => notices.push(message),
    esc: text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
    setTimeout: callback => {timers.set(++nextTimer, callback);return nextTimer;},
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => {
      let resolve;
      const promise = new Promise(done => {resolve = done;});
      calls.push({url,options,resolve});return promise;
    },
  });
  vm.runInContext(source, context);
  return {context,$,calls,timers,notices};
}
(async () => {
  {
    const h = harness(); assert.equal(h.calls.length, 0);
    const opening = h.context.openSteward();
    assert.equal(h.calls[0].url, '/api/steward?offset=0');
    assert.equal(h.calls[0].options.method, undefined);
    h.calls[0].resolve(response(data())); await opening;
    assert.equal(h.timers.size, 0);
    const scan = h.context.scanSteward(false);
    assert.equal(h.calls[1].options.method, 'POST');
    assert.deepEqual(JSON.parse(h.calls[1].options.body), {apply:false});
    h.calls[1].resolve(response({storage_owner:'user:alice',queued:true}, 202)); await scan;
    assert.equal(h.timers.size, 1);
    h.context.resetSteward(); assert.equal(h.timers.size, 0);
    console.log('PASS lazy read, explicit inventory, polling ends on close');
  }
  {
    const h = harness(), opening = h.context.openSteward();
    h.calls[0].resolve(response(data({items:[{job_id:1,title:'<img src=x>',state:'blocked',checked:1,
      observation:{issues:['<script>bad</script>']}, actions:[{kind:'summarize',state:'pending',reason:'<b>reason'}]}]})));await opening;
    const body = h.$('#stewardBody').innerHTML;
    assert.ok(!body.includes('<img'));assert.ok(!body.includes('<script>'));assert.ok(!body.includes('<b>reason'));
    h.$('#stewardAutomatic').checked = true;
    h.$('#stewardAI').checked = true;
    h.$('#stewardProfile').value = 'deep';
    const saving = h.context.saveStewardPolicy();
    assert.deepEqual(JSON.parse(h.calls[1].options.body), {automatic:true,ai_planning:true,profile:'deep'});
    h.calls[1].resolve(response({storage_owner:'user:alice',policy:{automatic:true,ai_planning:true,profile:'deep'}}));await saving;
    console.log('PASS escaped media output and explicit editable processing policy');
  }
  {
    for (const transition of ['owner','privacy','close']) {
      const h = harness(), opening = h.context.openSteward();
      if (transition === 'owner') h.context.storageOwner = 'user:bob';
      if (transition === 'privacy') ++h.context.privateViewEpoch;
      h.context.resetSteward();
      assert.equal(h.calls[0].options.signal.aborted, true);
      h.calls[0].resolve(response(data({items:[{title:'PRIVATE',observation:{},actions:[]}]})));await opening;
      assert.equal(h.$('#stewardBody').innerHTML, '');assert.equal(h.timers.size, 0);
    }
    console.log('PASS owner, privacy and close invalidate delayed responses');
  }
  {
    const h = harness(), opening = h.context.openSteward();
    h.calls[0].resolve(response(data()));await opening;
    const loading = h.context.loadSteward();
    h.calls[1].resolve(response({storage_owner:'user:alice',error:'隐私权限已失效'}, 403));await loading;
    assert.equal(h.$('#stewardSheet').hidden, true);assert.equal(h.$('#stewardBody').innerHTML, '');
    assert.deepEqual(h.notices, ['隐私权限已失效']);
    console.log('PASS expired privacy access clears rows and controls');
  }
  {
    const h = harness(), opening = h.context.openSteward();
    h.calls[0].resolve(response(data({storage_owner:'user:bob'})));await opening;
    assert.equal(h.$('#stewardBody').innerHTML, '');
    assert.match(html, /onclick="openSteward\(\)"/);
    assert.ok((html.match(/resetSteward\(\)/g) || []).length >= 3);
    console.log('PASS foreign response rejected and account/privacy/close hooks present');
  }
  {
    const h = harness(), opening = h.context.openSteward();
    h.calls[0].resolve(response(data({busy:true})));await opening;
    assert.equal(h.timers.size,1);
    h.$('#stewardAutomatic').checked = true;
    h.$('#stewardProfile').value = 'essential';
    h.context.editStewardPolicy();
    const refresh = h.context.loadSteward();
    h.calls[1].resolve(response(data({busy:true})));await refresh;
    const body = h.$('#stewardBody').innerHTML;
    assert.match(body,/id="stewardAutomatic"[^>]*checked/);
    assert.match(body,/value="essential" selected/);
    h.context.resetSteward();assert.equal(h.timers.size,0);
    console.log('PASS active batch polling preserves unsaved policy changes');
  }
})().catch(error => {console.error(error);process.exitCode = 1;});
