const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const listeners={},calls=[];
const context=vm.createContext({window:{addEventListener:(type,handler)=>{listeners[type]=handler;}},
  location:{origin:'http://localhost'},URL,Set,JSON,fetch:(...args)=>{calls.push(args);return Promise.resolve();}});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../static/health_monitor.js'),'utf8'),context);
const listener=listeners.error;
const event={filename:'http://localhost/static/steward.js?private=secret',lineno:72,error:{name:'TypeError',message:'PRIVATE TITLE',stack:'SECRET'}};
listener({...event,filename:'http://external.example/static/steward.js'});assert.equal(calls.length,0);
listener(event);listener(event);assert.equal(calls.length,1);
assert.deepEqual(JSON.parse(calls[0][1].body),{file:'/static/steward.js',line:72,name:'TypeError'});
assert.ok(!JSON.stringify(calls).includes('secret'));assert.ok(!JSON.stringify(calls).includes('PRIVATE'));
listeners.unhandledrejection({reason:{name:'ReferenceError',message:'PRIVATE',stack:'at run (http://localhost/static/steward.js?secret=value:78:5)'}});
assert.deepEqual(JSON.parse(calls[1][1].body),{file:'/static/steward.js',line:78,name:'ReferenceError'});
assert.ok(!JSON.stringify(calls).includes('secret'));
for(let line=73;line<80;line++)listener({...event,lineno:line});assert.equal(calls.length,3);
console.log('PASS browser code-position reporting excludes messages, stacks, external scripts and URL queries; repeats are bounded');
