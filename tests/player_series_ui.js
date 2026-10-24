const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code=fs.readFileSync(path.join(__dirname,'../static/player_series.js'),'utf8');
const flush=()=>new Promise(setImmediate);
function harness() {
  const nodes=new Map(),calls=[],timers=[],opened=[];
  const node=()=>({innerHTML:'',textContent:'',hidden:false,disabled:false,scrollTop:0,clientHeight:250,querySelector:()=>null,
    classList:{remove:()=>{},toggle:()=>{}}});
  const $=s=>{if(!nodes.has(s))nodes.set(s,node());return nodes.get(s);};
  const c=vm.createContext({$,storageOwner:'user:alice',privateViewEpoch:0,watching:{id:1,part:0},videoSort:'added',
    esc:String,seriesOf:j=>j.analysis.show,URLSearchParams,AbortController,state:{jobs:[]},document:{hidden:false},
    setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{},
    fetch:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve})),
    openWatch:async id=>{opened.push(id);c.watching={id,part:0};c.setupPlayerSeries({id,analysis:{show:'Drama'}});},
    clearMainSearchPermission:()=>{c.cleared=true;}});
  vm.runInContext(code,c);
  return {c,$,calls,timers,opened,state:()=>vm.runInContext('playerSeries',c)};
}
const item=id=>({id,title:'Episode '+id,analysis:{episode:id},playable:true,status:'done',watched:id===2});
const respond=(call,data,status=200)=>call.resolve({ok:status<400,status,json:async()=>data});
(async()=>{
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    respond(h.calls[0],{series:'Drama',items:[item(1),item(2),{...item(3),playable:false}],total:3,more:false,offset:0});await flush();
    assert.match(h.$('#playerSeriesList').innerHTML,/正在播放/);assert.match(h.$('#playerSeriesList').innerHTML,/已看完/);
    const playing=h.c.playSeriesVideo(2);assert.match(h.calls[1].url,/mode=exact&only=1&ids=2/);
    respond(h.calls[1],{storage_owner:'user:alice',jobs:[{id:2,status:'done',media:[{src:'local'}],analysis:{show:'Drama'}}]});await playing;
    assert.deepEqual(h.opened,[2]);assert.equal(h.calls.length,2);
    await h.c.playSeriesVideo(3);assert.deepEqual(h.opened,[2]);
    assert.match(h.$('#playerSeriesList').innerHTML,/data-series-job="2" aria-current="true"/);
    console.log('PASS sidebar switches the same player, keeps the sibling list, highlights playback/watch state and blocks unavailable entries');
  }
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    h.c.filterPlayerSeries('episode 2');respond(h.calls[0],{items:[{...item(1),title:'STALE_SECRET'}],total:1});await flush();
    assert.equal(h.state().items.length,0);assert.doesNotMatch(h.$('#playerSeriesList').innerHTML,/STALE_SECRET/);
    h.timers[0]();assert.match(h.calls[1].url,/q=episode\+2/);
    respond(h.calls[1],{items:[item(2)],total:1,offset:0,more:false});await flush();
    assert.equal(h.state().items[0].id,2);assert.equal(h.$('#playerSeries').hidden,false);
    console.log('PASS filtering discards delayed old bodies, sends the chosen query/sort and keeps an empty or single-match filter usable');
  }
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    respond(h.calls[0],{items:[item(1),item(2)],total:3,offset:0,more:true});await flush();
    h.c.loadPlayerSeries(h.state().next);assert.match(h.calls[1].url,/offset=2/);
    respond(h.calls[1],{items:[item(2),item(3)],total:3,offset:2,more:false});await flush();
    assert.deepEqual(Array.from(h.state().items,x=>x.id),[1,2,3]);
    console.log('PASS additional sibling pages append without duplicate videos');
  }
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    h.c.stopPlayerSeries();h.c.storageOwner='user:bob';h.c.privateViewEpoch++;
    h.c.setupPlayerSeries({id:9,analysis:{show:'Other'}});
    respond(h.calls[0],{error:'private'},403);await flush();assert.equal(h.c.cleared,undefined);
    assert.equal(h.state().owner,'user:bob');assert.equal(h.state().loading,true);
    respond(h.calls[1],{items:[],total:0});await flush();
    assert.equal(h.state().loading,false);assert.equal(h.calls[0].options.signal.aborted,true);
    console.log('PASS owner/lock resets clear cached siblings and reject delayed permission errors without damaging a newer account');
  }
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    respond(h.calls[0],{error:'permission changed'},403);await flush();
    assert.equal(h.c.cleared,true);assert.equal(h.state(),null);assert.equal(h.$('#playerSeries').innerHTML,'');
    console.log('PASS permission revocation immediately removes all sidebar titles and counts');
  }
  {
    const h=harness();h.c.setupPlayerSeries({id:1,analysis:{show:'Drama'}});
    respond(h.calls[0],{items:[item(1),item(2)],total:2,offset:0,membership:'old'});await flush();
    const check=h.c.validatePlayerSeries(h.state());respond(h.calls[1],{items:[item(1)],total:2,membership:'changed'});await flush();
    assert.equal(h.state().items.length,0);respond(h.calls[2],{items:[item(1),item(3)],total:2,membership:'changed'});await check;
    assert.deepEqual(Array.from(h.state().items,x=>x.id),[1,3]);
    const playing=h.c.playSeriesVideo(3);respond(h.calls[3],{jobs:[],storage_owner:'user:alice'});await playing;
    assert.equal(h.c.cleared,true);assert.deepEqual(h.opened,[]);
    console.log('PASS membership changes invalidate cached titles and every episode click rechecks current playback permission');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
