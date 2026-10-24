const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/intro.js'), 'utf8');
const flush = () => new Promise(setImmediate);
function harness() {
  const panel = {innerHTML:'', hidden:true}, listeners = {}, calls = [], timers = [], preferences = new Map();
  const video = {currentTime:0, duration:1500, readyState:4, paused:false,
    addEventListener:(event, fn) => { (listeners[event] ||= []).push(fn); }};
  const c = vm.createContext({storageOwner:'user:alice', privateViewEpoch:1, watching:{id:1, part:0}, casting:null,
    $:s => s === '#introPanel' ? panel : video, esc:String, dur:String, AbortController,
    setTimeout:fn => (timers.push(fn), timers.length), clearTimeout:()=>{}, setInterval:()=>1, clearInterval:()=>{},
    localStorage:{getItem:k=>preferences.get(k) ?? null,setItem:(k,v)=>preferences.set(k,v)},
    fetch:(url, options) => new Promise(resolve => calls.push({url,options,resolve})),
    castSeek:async(device,at)=>{c.castAt=at;}});
  vm.runInContext(code,c);
  return {c,panel,video,listeners,calls,timers,preferences};
}
const tv = {id:1,analysis:{library:'TV',show:'Drama'}};
const marker = {status:'ready',start:5,end:65,method:'chapter',automatic:true};
const respond = (call,data) => call.resolve({ok:true,json:async()=>data});
(async()=>{
  {
    const h=harness();h.c.setupIntro(tv,0,{duration:1500});
    assert.equal(h.calls[0].options.method,'POST');respond(h.calls[0],marker);await flush();
    assert.equal(h.video.currentTime,0);h.video.currentTime=6;h.c.tickIntro();assert.equal(h.video.currentTime,65);
    await h.c.replayIntro();assert.equal(h.video.currentTime,5);h.c.tickIntro();assert.equal(h.video.currentTime,5);
    h.video.currentTime=500;await h.c.skipIntro(false);assert.equal(h.video.currentTime,500);
    console.log('PASS local opening waits for its start, skips once, allows replay and never seeks backward from plot');
  }
  {
    const h=harness();h.video.currentTime=20;h.c.setupIntro(tv,0,{duration:1500},{explicit:true});
    respond(h.calls[0],marker);await flush();assert.equal(h.video.currentTime,20);
    await h.c.skipIntro(false);assert.equal(h.video.currentTime,65);
    console.log('PASS explicit subtitle/chapter navigation suppresses automatic skipping while manual skip remains available');
  }
  {
    const h=harness();h.c.setupIntro(tv,0,{duration:1500});respond(h.calls[0],{...marker,automatic:false});await flush();
    h.video.currentTime=10;h.c.tickIntro();assert.equal(h.video.currentTime,10);
    await h.c.skipIntro(false);assert.equal(h.video.currentTime,65);
    console.log('PASS an uncorroborated audio candidate requires a manual action');
  }
  {
    const h=harness();h.preferences.set('introAuto:user:alice','0');h.c.setupIntro(tv,0,{duration:1500});
    assert.equal(h.calls[0].options.method,'GET');respond(h.calls[0],{status:'unknown'});await flush();
    h.c.requestIntro(true);respond(h.calls[1],{status:'pending'});await flush();assert.equal(h.timers.length,1);
    h.timers[0]();respond(h.calls[2],marker);await flush();h.video.currentTime=10;h.c.tickIntro();assert.equal(h.video.currentTime,10);
    console.log('PASS owner-scoped opt-out prevents automatic work and skipping; explicit detection still polls to completion');
  }
  {
    const h=harness();h.c.setupIntro(tv,0,{duration:1500});h.c.storageOwner='user:bob';h.c.privateViewEpoch++;
    h.c.stopIntro();respond(h.calls[0],marker);await flush();assert.equal(h.panel.innerHTML,'');assert.equal(h.panel.hidden,true);
    assert.equal(h.calls[0].options.signal.aborted,true);
    console.log('PASS locking or account changes abort detection, clear marker DOM and reject late responses');
  }
  {
    const h=harness();h.c.casting={id:1,part:0,device:'tv',ready:true,pending:false,seeking:false,state:'PLAYING',position:10,duration:1500};
    h.c.setupIntro(tv,0,{duration:1500});respond(h.calls[0],marker);await flush();assert.equal(h.c.castAt,65);
    assert.equal(h.video.currentTime,0);await h.c.replayIntro();assert.equal(h.c.castAt,5);
    assert.equal(h.c.validIntro({...marker,end:2000},1500),false);
    console.log('PASS matching TV cast seeks remotely; invalid bounds never seek local or remote media');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
