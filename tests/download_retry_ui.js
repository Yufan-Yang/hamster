// node tests/download_retry_ui.js — real homepage helpers, no network or downloads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const nodes = new Map();
const $ = key => {
  if (key.includes('video, ')) return null;
  if (!nodes.has(key)) nodes.set(key, {innerHTML:'',textContent:'',hidden:false,disabled:false,value:'',
    classList:{toggle(){}},setAttribute(){}});
  return nodes.get(key);
};
let now = 100000;
const ctx = vm.createContext({$, Date:{now:()=>now*1000}, ACTIVE:['queued','downloading','processing'],
  state:{jobs:[],subs:[],cats:{}}, FOLDERS:{},LIBS:{}, chip:'all',query:'',deviceFilter:null,videoSort:'added',
  esc:value=>String(value??'').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])),
  category:()=>'',catName:value=>value,storageOwner:'user:qa',watchOpenEpoch:0,casting:null,watching:null,
  document:{body:{style:{}}},stopMedia(){},ago:()=>'',phone:()=>'',hl:value=>value,episodeOf:()=>'',titleOf:j=>j.title,
  hitList:()=>'',loadSimilar(){},loadPointTimes(){},renderCastNotice(){},toast(){},clearTimeout(){},
});
vm.runInContext(html.slice(html.indexOf('function renderChips('),html.indexOf('\nfunction card(')),ctx);
vm.runInContext(html.slice(html.indexOf('async function openWatch('),html.indexOf('// the summary\'s key points')),ctx);
vm.runInContext(html.slice(html.indexOf('function updateWatchStatus('),html.indexOf('\nfunction closeWatch(')),ctx);
const retry={id:7,status:'failed',retry_at:now+65,attempts:2,error:'network\n（5 分钟后自动重试，第 2/3 次，已下载的部分会保留）',url:'https://example.test/video',title:'Synthetic lesson'};
assert.equal(ctx.waitingDownloadRetry(retry),true);
assert.equal(ctx.downloadActive(retry),true);
assert.match(ctx.downloadRetryLabel(retry),/约 2 分钟后，第 2\/3 次/);
now+=40; assert.match(ctx.downloadRetryLabel(retry),/25 秒后/);
now+=30; assert.match(ctx.downloadRetryLabel(retry),/正在等待调度/);
assert.match(ctx.downloadRetryLabel({...retry,attempts:4,error:'第 4/5 次'}),/第 4\/5 次/);
assert.equal(ctx.waitingDownloadRetry({...retry,retry_at:null}),false);
assert.equal(ctx.downloadActive({...retry,retry_at:null}),false);
ctx.state.jobs=[retry,{id:8,status:'failed',retry_at:null}];ctx.renderChips();
assert.match($('#chips').innerHTML,/下载中 1/);assert.match($('#chips').innerHTML,/失败 1/);
console.log('PASS waiting retries are active, terminal failures are separate, and countdowns preserve 3/5-attempt limits');
(async()=>{
  await ctx.openWatch(7);
  assert.match($('#wstatus').innerHTML,/等待自动重试/);
  assert.match($('#watchBody').innerHTML,/act\(7,'cancel'\).*取消自动重试/);
  assert.match($('#watchBody').innerHTML,/act\(7,'retry'\).*立即重试/);
  retry.status='cancelled';retry.retry_at=null;await ctx.openWatch(7);
  assert.equal($('#wstatus').innerHTML,'已取消');assert.ok(!$('#watchBody').innerHTML.includes('取消自动重试'));
  retry.status='failed';await ctx.openWatch(7);
  assert.match($('#wstatus').innerHTML,/自动重试已结束或此错误不能自动恢复/);
  assert.ok(!$('#watchBody').innerHTML.includes('取消自动重试'));
  console.log('PASS real detail renderer offers cancellation and immediate retry only for the correct states');
})().catch(error=>{console.error(error);process.exitCode=1;});
