// Personal recall is bound to the initial resume checkpoint, not subsequent playback progress.
let recapEpoch = 0, recapView = null;
function stopRecap() {
  ++recapEpoch;
  if (recapView) {recapView.controller?.abort();clearTimeout(recapView.timer);}
  recapView = null;
  const box=$('#resumeRecap');if (box) {box.hidden=true;box.innerHTML='';}
}
function currentRecap(s) {
  return recapView===s && s.epoch===recapEpoch && s.owner===storageOwner && s.privacy===privateViewEpoch
    && watching?.id===s.job && watching?.part===s.part;
}
function startRecap(job,part,resumed) {
  stopRecap();
  const a=job.analysis || {}, w=job.watch;
  if (!resumed || !w || w.done || w.part!==part || !Number.isFinite(w.pos) || w.pos<120
      || !['Tutorials','Talks','Documentaries','News'].includes(a.folder) || ['TV','Movies','Music'].includes(a.library)) return;
  const s=recapView={epoch:recapEpoch,owner:storageOwner,privacy:privateViewEpoch,job:job.id,part,checkpoint:w.pos,
    state:'available',quotes:[],attempts:0,controller:null};
  renderRecap();
  if (Date.now()/1000-(w.at || w.updated || Date.now()/1000)>=1800) requestRecap(false);
}
function renderRecap() {
  const s=recapView,box=$('#resumeRecap');if (!box || !s || !currentRecap(s)) return;
  box.hidden=false;
  const labels={available:'需要时可回顾上次停留前的字幕。',pending:'正在准备停留前的字幕回顾…',
    budget:'本日自动准备已达限，需要时可手动生成。',empty:'这段字幕没有适合回顾的摘录。',unavailable:'暂时没有可用回顾。',
    rejected:'没有通过原文审查的回顾。',paused:'自主准备已暂停。',deferred:'仍在后台准备，稍后可以查看。'};
  box.innerHTML=`<div class="recap-heading"><b>续看回顾${s.state==='ready' ? ' · 字幕摘录' : ''}</b><button class="link" onclick="stopRecap()">隐藏</button></div>
    ${s.state==='ready' ? `<p class="hint">仅引用上次停留 ${dur(s.checkpoint)} 之前的内容。</p>${s.quotes.map((quote,i)=>
      `<blockquote><button class="link" onclick="rewindRecap(${i})">${esc(dur(quote.t) || '0:00')} 回看</button><p>${esc(quote.quote)}</p></blockquote>`).join('')}`
      : `<p class="hint" role="status">${esc(labels[s.state] || labels.unavailable)}</p>${s.state!=='pending' ? `<button class="link" onclick="requestRecap(true)">${s.id && s.state==='deferred' ? '查看回顾' : '生成回顾'}</button>` : ''}`}`;
}
async function requestRecap(manual=false) {
  const s=recapView;if (!s || !currentRecap(s) || s.controller) return;
  const polling=!!s.id && (!manual || s.state==='deferred');
  if (!polling) s.id=null;
  s.state='pending';renderRecap();
  const controller=s.controller=new AbortController();
  try {
    const response=await fetch(`/api/recap/${s.job}${polling ? '?id='+s.id : ''}`,{signal:controller.signal,
      ...(polling ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({manual,part:s.part,checkpoint:s.checkpoint})})});
    const data=await response.json();
    if (!currentRecap(s)) return;
    if (!response.ok || data.storage_owner!==s.owner || data.part!=null && data.part!==s.part
        || Number.isFinite(data.checkpoint) && data.checkpoint>s.checkpoint+1) {stopRecap();return;}
    s.id=data.id || s.id;s.state=data.state;
    if (data.state==='ready' && polling) {
      if (!Array.isArray(data.quotes) || !data.review?.verbatim || !data.review?.before_checkpoint || data.quotes.length>3
          || data.quotes.some(q=>typeof q.quote!=='string' || !Number.isFinite(q.t) || !Number.isFinite(q.end) || q.t<0 || q.end> s.checkpoint || q.end<=q.t)) {stopRecap();return;}
      s.quotes=data.quotes;
    } else if ((data.state==='pending' || data.state==='ready') && s.id) {
      s.state='pending';
      if (++s.attempts<20) s.timer=setTimeout(()=>requestRecap(false),1500);
      else s.state='deferred';
    }
    renderRecap();
  } catch (error) {if (currentRecap(s) && error.name!=='AbortError') {s.state='unavailable';renderRecap();}}
  finally {if (currentRecap(s)) s.controller=null;}
}
function rewindRecap(index) {
  const s=recapView,quote=s?.quotes[index];
  if (s && currentRecap(s) && quote && Number.isFinite(quote.t) && quote.t>=0 && quote.end<=s.checkpoint)
    seekTo(s.job,s.part,quote.t);
}
