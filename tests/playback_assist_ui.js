const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/playback_assist.js'),'utf8');
function harness() {
  const elements=new Map(),calls=[],timers=new Map(),seeks=[];let timer=0;
  const $=key=>{if(!elements.has(key))elements.set(key,{hidden:true,innerHTML:''});return elements.get(key);};
  const context=vm.createContext({$,storageOwner:'user:alice',privateViewEpoch:0,watching:{id:7,part:0},
    Date,Number,Array,JSON,AbortController,esc:t=>String(t).replaceAll('<','&lt;').replaceAll('>','&gt;'),dur:t=>String(t),
    setTimeout:cb=>{timers.set(++timer,cb);return timer;},clearTimeout:id=>timers.delete(id),
    seekTo:(...args)=>seeks.push(args),fetch:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}))});
  vm.runInContext(source,context);return{context,$,calls,timers,seeks};
}
const job=extra=>({id:7,analysis:{library:'Videos',folder:'Tutorials'},watch:{part:0,pos:300,at:Date.now()/1000-4000,done:false},...extra});
const response=(body,status=200)=>({ok:status<400,status,json:async()=>({storage_owner:'user:alice',...body})});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  {
    const h=harness();h.context.startRecap(job(),0,false);assert.equal(h.calls.length,0);
    h.context.startRecap(job({analysis:{library:'TV',folder:'Tutorials'}}),0,true);assert.equal(h.calls.length,0);
    h.context.startRecap(job(),0,true);assert.equal(h.calls.length,1);
    assert.deepEqual(JSON.parse(h.calls[0].options.body),{manual:false,part:0,checkpoint:300});
    h.calls[0].resolve(response({id:1,state:'pending',checkpoint:300},202));await tick();
    const pending=h.context.requestRecap();h.calls[1].resolve(response({id:1,state:'ready',part:0,checkpoint:300,
      quotes:[{t:200,end:220,quote:'<source quote>'}],review:{verbatim:true,before_checkpoint:true}}));await pending;
    assert.ok(h.$('#resumeRecap').innerHTML.includes('&lt;source quote&gt;'));
    h.context.rewindRecap(0);assert.deepEqual(h.seeks,[[7,0,200]]);
    console.log('PASS eligible resume requests a fixed checkpoint and only renders reviewed, escaped, past quotes');
  }
  {
    for(const change of ['owner','privacy','part','close']){
      const h=harness();h.context.startRecap(job(),0,true);const req=h.calls[0];
      if(change==='owner')h.context.storageOwner='user:bob';
      if(change==='privacy')h.context.privateViewEpoch++;
      if(change==='part')h.context.watching.part=1;
      h.context.stopRecap();assert.equal(req.options.signal.aborted,true);
      req.resolve(response({state:'ready',id:1,quotes:[{quote:'SECRET'}]}));await tick();
      assert.equal(h.$('#resumeRecap').innerHTML,'');assert.equal(h.timers.size,0);
    }
    console.log('PASS account, privacy, part and close transitions synchronously clear and abort private recall');
  }
  {
    const h=harness();h.context.startRecap(job(),0,true);
    h.calls[0].resolve(response({id:1,state:'pending',checkpoint:300},202));await tick();
    const p=h.context.requestRecap();h.calls[1].resolve(response({id:1,state:'ready',part:0,checkpoint:300,
      quotes:[{t:290,end:330,quote:'FUTURE_SECRET'}],review:{verbatim:true,before_checkpoint:true}}));await p;
    assert.equal(h.$('#resumeRecap').innerHTML,'');assert.equal(h.seeks.length,0);
    console.log('PASS future or unverified recall is rejected independently in the player');
  }
  {
    const h=harness();h.context.startRecap(job(),0,true);
    h.calls[0].resolve(response({id:1,state:'rejected',checkpoint:300}));await tick();
    const p=h.context.requestRecap(true);assert.equal(h.calls[1].url,'/api/recap/7');
    assert.equal(h.calls[1].options.method,'POST');assert.equal(JSON.parse(h.calls[1].options.body).manual,true);
    h.calls[1].resolve(response({state:'budget'}));await p;
    console.log('PASS explicit retry creates a fresh request while automatic polling never spends a new generation');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
