// node tests/search_evidence_ui.js — subtitle evidence survives fresh authorization and never changes its target.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
function harness(){
  const nodes=new Map(),calls=[],notices=[];let stopped=0;
  const $=key=>{if(key.includes('video, '))return null;if(!nodes.has(key))nodes.set(key,{innerHTML:'',hidden:true,scrollTo(){}});return nodes.get(key);};
  const context=vm.createContext({$,state:{jobs:[]},query:'学习*原文',storageOwner:'user:one',watchOpenEpoch:0,privateViewEpoch:0,mainSearchEpoch:0,watching:null,casting:null,
    ACTIVE:['queued','downloading','processing'],mainSearchMode:()=> 'wildcard',document:{body:{style:{}}},navigator:{userAgent:''},
    esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    fetch:url=>{const call={url,...deferred()};calls.push(call);return call.promise;},stopMedia:()=>++stopped,toast:v=>notices.push(v),
    category:()=>'',catName:v=>v,phone:()=>'',episodeOf:()=>'',titleOf:j=>j.title,ago:()=>'',loadSimilar(){},loadPointTimes(){},renderCastNotice(){},setupMediaSession(){},airplayAvailable:()=>false,
    videoTag:(j,m,t)=>`<video data-source="${m.src}" data-time="${t}"></video>`,
  });
  for(const [start,end] of [['function hl(', 'function ago('],['function waitingDownloadRetry(', '\nfunction card('],['function validSearchHit(', '/* ---------- player ----------'],['async function openWatch(', '// the summary\'s key points']])vm.runInContext(html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start))),context);
  context.updateWatchStatus=()=>{};
  return{context,$,calls,notices,get stopped(){return stopped;}};
}
const hit={part:1,t:12,src:'字幕',text:'学习过程中找到原文 <script>bad()</script>'};
const compact=()=>({id:7,compact:true,title:'Metadata title',status:'done',url:'https://example.test/video',hits:[hit],match:hit.text,match_where:'字幕'});
const detailed=()=>({id:7,title:'Current title',status:'done',url:'https://example.test/video',media:[{src:'/part0',name:'Part 1',duration:100},{src:'/part1',name:'Part 2',duration:100}]});
const reply=job=>({ok:true,status:200,json:async()=>({jobs:job?[job]:[],storage_owner:'user:one'})});
(async()=>{
  {
    const h=harness();h.context.state.jobs=[compact()];const pending=h.context.openWatch(7,1,12);assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/mode=exact&only=1&ids=7/);
    h.calls[0].resolve(reply(detailed()));await pending;
    assert.equal(h.context.watching.part,1);assert.match(h.$('#watchBody').innerHTML,/data-source="\/part1" data-time="12"/);
    assert.match(h.$('#watchBody').innerHTML,/原文命中 1/);assert.match(h.$('#watchBody').innerHTML,/P2/);assert.match(h.$('#watchBody').innerHTML,/<mark>学习<\/mark>/);
    assert.ok(h.$('#watchBody').innerHTML.includes('&lt;script&gt;'));assert.ok(!h.$('#watchBody').innerHTML.includes('<script>'));
    assert.equal(h.context.state.jobs[0].hits.length,1);assert.equal(h.stopped,1);
    h.context.seekTo(7,1,12);assert.equal(h.calls.length,2,'each evidence jump rechecks current visibility');h.calls[1].resolve(reply(null));await new Promise(setImmediate);
    assert.equal(h.stopped,1,'deleted evidence must not start a different file');
    console.log('PASS subtitle evidence survives compact-to-detail authorization, keeps part/time, escapes original text and verifies every jump');
  }
  {
    for(const change of ['owner','privacy','query','epoch']){
      const h=harness();h.context.state.jobs=[compact()];const pending=h.context.openWatch(7,1,12);
      if(change==='owner')h.context.storageOwner='user:two';if(change==='privacy')++h.context.privateViewEpoch;if(change==='query')h.context.query='new';if(change==='epoch')++h.context.mainSearchEpoch;
      h.calls[0].resolve(reply(detailed()));await pending;
      assert.equal(h.stopped,0,change);assert.equal(h.context.watching,null,change);assert.equal(h.$('#watchBody').innerHTML,'',change);
    }
    const h=harness();h.context.state.jobs=[compact()];const unavailable=h.context.openWatch(7,1,12);const changed=detailed();changed.media=[changed.media[0]];h.calls[0].resolve(reply(changed));await unavailable;
    assert.equal(h.stopped,0);assert.match(h.notices[0],/位置已不可用/);
    console.log('PASS owner/privacy/query changes discard late details and removed parts never fall back to another video');
  }
  {
    const h=harness();h.context.state.jobs=[compact()];h.$('#watchBody').innerHTML='PRIVATE';
    h.context.clearMainSearchPermission=message=>{h.notices.push(message);h.context.state.jobs=[];h.$('#watchBody').innerHTML='';};
    const pending=h.context.openWatch(7,1,12);assert.match(h.calls[0].url,/mode=exact&only=1/);
    h.calls[0].resolve({ok:false,status:403,json:async()=>({error:'revoked'})});await pending;
    assert.equal(h.context.state.jobs.length,0);assert.equal(h.$('#watchBody').innerHTML,'');assert.equal(h.stopped,0);assert.match(h.notices[0],/权限已变化/);
    console.log('PASS strict search detail validation sends mode=exact and invokes shared permission clearing on revoked tokens');
  }
  {
    const h=harness();
    for(const bad of [{part:0,t:null},{part:-1,t:2},{part:'0);bad()',t:2},{part:0,t:Infinity},{part:0,t:-1}]){
      assert.equal(h.context.validSearchHit(bad),false);const rendered=h.context.hitList({id:7,hits:[{...bad,text:'plain original'}]});assert.ok(!rendered.includes('onclick='));
    }
    h.context.seekTo(7,0,-1);assert.equal(h.calls.length,0);assert.match(h.notices[0],/位置无效/);
    console.log('PASS untimed or invalid transcript evidence is readable but never emits an unsafe playback action');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
