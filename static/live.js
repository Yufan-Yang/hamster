// Live clipping workspace; loaded after the shared page helpers.
let liveData = null, liveTab = "highlight", liveStatus = "all", livePoll = null, liveQrPoll = null, liveQrEpoch = 0, liveEpoch = 0;
let liveSourceFilter = "";
const liveSelected = new Set();
const LV_STATES = {queued:"等待制作",rendering:"正在制作",ready:"可预览 / 待投稿",awaiting_upload:"排队投稿",uploading:"正在投稿",
  submitted:"已投稿",uncertain:"投稿结果待核实",rejected:"已忽略",failed:"处理失败",transcribing:"正在转写",expired:"原始缓存已清理"};
const LV_SOURCE = {waiting:"等待检查",offline:"未开播",live:"已开播",recording:"边播边录制",error:"需要处理",paused:"已暂停"};
function lvTime(t) { t=Math.max(0,Math.round(+t||0)); return `${Math.floor(t/3600).toString().padStart(2,"0")}:${Math.floor(t/60)%60<10?"0":""}${Math.floor(t/60)%60}:${(t%60).toString().padStart(2,"0")}`; }
function lvSeconds(t) { return String(t).trim().split(":").reduce((sum,n)=>sum*60+Number(n),0); }
function lvSource(id) { return (liveData?.sources||[]).find(s=>s.id===id); }
function lvClip(id) { return (liveData?.clips||[]).find(c=>c.id===id); }
async function lvApi(url, body) {
  const response=await fetch(url,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data=await response.json(); if(!response.ok) throw Error(data.error||"操作失败"); return data;
}
async function loadLive() {
  clearTimeout(livePoll); if(chip!=="live"||document.hidden)return;
  const epoch = ++liveEpoch, owner = storageOwner;
  try { const data=await lvApi("/api/live?tab="+encodeURIComponent(liveTab)); if(epoch!==liveEpoch||owner!==storageOwner)return; liveData=data; renderLive(); }
  catch(e) { if(epoch!==liveEpoch||owner!==storageOwner)return; if(liveData){toast("直播工作台连接中断，保留当前内容，稍后自动重连");}else $("#liveWorkspace").innerHTML=`<div class="lv-panel">${esc(e.message)}${!state.user?'<p><button class="btn" onclick="openAccount()">登录拾光</button></p>':""}</div>`; }
  if(chip==="live")livePoll=setTimeout(loadLive,5000);
}
function lvTab(tab) {liveSelected.clear();liveTab=tab;saveNavigation(true);renderLive();loadLive();}
function lvVisibleClips(){return (liveData?.clips||[]).filter(c=>c.lane===liveTab&&(!liveSourceFilter||String(c.source_id)===liveSourceFilter)&&(liveStatus==="all"?c.content_kind!=="advertisement"&&!c.merged_into:c.state===liveStatus));}
function lvSelectable(c){return !['awaiting_upload','uploading','submitted','uncertain'].includes(c.state);}
function renderLive() {
  if(!liveData){$("#liveWorkspace").innerHTML='<p class="hint">正在加载直播切片…</p>';return;}
  const data=liveData, account=data.account;
  const clips=lvVisibleClips();
  const selectable=new Set(clips.filter(lvSelectable).map(c=>c.id));
  for(const id of liveSelected)if(!selectable.has(id))liveSelected.delete(id);
  const workspace=$('#liveWorkspace');
  if(workspace.contains(document.activeElement)&&['SELECT','INPUT','TEXTAREA'].includes(document.activeElement.tagName)&&document.activeElement.type!=='checkbox')return;
  const detailKey=el=>el.dataset.lvDetail||`${el.closest('[data-lv-source]')?.dataset.lvSource||''}|${el.querySelector('summary')?.textContent||''}`;
  const expanded=new Set([...workspace.querySelectorAll('details[open]')].map(detailKey));
  const html=`<div class="lv-bar"><div><b>直播切片工作台</b><div class="lv-meta">每分钟分段 · 两条线并行 · 跨分段保留上下文</div></div>
    <div class="lv-actions"><button class="btn plain" onclick="lvLogin()">${account?`投稿账号：${esc(account.name)}`:"扫码绑定 B站投稿账号"}</button><button class="btn" onclick="lvEditSource()">＋ 追踪 UP</button></div></div>
    <p class="lv-intro">亮点线按话题性、传播潜力和趣味性评分，话题线按讨论内容切换分段。低于 75 分、创建满 12 小时仍未投稿的片段会自动清除；待评分、投稿处理中和已投稿的片段保留。自动投稿会先等待评分预校准通过，也可以预览、编辑并手动投稿。</p>
    ${!data.uploader_ready?'<p class="lv-error">投稿组件尚未就绪；录制、分析和预览仍可使用。</p>':""}
    <div class="lv-tabs">${[["highlight","亮点切片"],["topic","话题分段"],["sources","追踪 UP"],["recordings","直播记录"],["calibration","评分校准"]].map(([key,label])=>`<button class="chip${liveTab===key?" on":""}" onclick="lvTab('${key}')">${label}</button>`).join("")}</div>
    ${["highlight","topic"].includes(liveTab)?`<div class="lv-bar"><span class="lv-meta">${clips.length} 个${liveTab==="topic"?"话题片段":"亮点"}</span><label>主播 <select onchange="liveSourceFilter=this.value;liveSelected.clear();this.blur();renderLive()"><option value="">全部主播</option>${data.sources.map(s=>`<option value="${s.id}"${liveSourceFilter===String(s.id)?' selected':''}>${esc(s.name)}${s.archived?'（已取消追踪）':''}</option>`).join('')}</select></label><label>状态 <select onchange="liveStatus=this.value;liveSelected.clear();saveNavigation();this.blur();renderLive()"><option value="all">全部</option>${Object.entries(LV_STATES).filter(([k])=>!["transcribing","expired"].includes(k)).map(([k,v])=>`<option value="${k}"${liveStatus===k?" selected":""}>${v}</option>`).join("")}</select></label><button class="btn plain" onclick="lvEditClip()">＋ 手动切片</button></div><div class="lv-actions lv-bulk"><button class="btn plain" onclick="lvSelectAll()">${liveSelected.size&&liveSelected.size===selectable.size?'取消全选':'全选当前列表'}</button><span>已选 ${liveSelected.size} 条</span><button class="btn plain" onclick="lvBulk('reject')" ${!liveSelected.size?'disabled':''}>批量忽略</button><button class="btn plain" onclick="lvBulk('delete')" ${!liveSelected.size?'disabled':''}>批量删除</button><span class="lv-meta">忽略保留文件；删除释放空间。投稿记录会保留。</span></div><div class="lv-grid">${clips.map(lvCard).join("")||'<p class="hint">当前筛选下没有片段。</p>'}</div>`:
      liveTab==="sources"?`<div class="lv-grid">${data.sources.filter(s=>!s.archived).map(lvSourceCard).join("")||'<p class="hint">添加一个直播 UP 主开始追踪。</p>'}</div>${data.sources.some(s=>s.archived)?`<details class="lv-rule"><summary>已取消追踪的 UP</summary>${data.sources.filter(s=>s.archived).map(s=>`<p>${esc(s.name)} <button class="link" onclick="lvManageSource(${s.id})">管理已有切片</button><button class="link" onclick="lvPause(${s.id})">重新追踪</button></p>`).join('')}</details>`:''}`:
      liveTab==="recordings"?lvRecordings():lvCalibrations()}`;
  if(workspace.dataset.html===html)return;
  const y=window.scrollY;
  workspace.innerHTML=html;workspace.dataset.html=html;
  workspace.querySelectorAll('details').forEach(el=>{el.open=expanded.has(detailKey(el));});
  window.scrollTo(0,y);
}
function lvSourceCard(s) {const c=s.settings;return `<div class="lv-panel"><h3>${esc(s.name)}</h3><span class="lv-label">${s.enabled?(LV_SOURCE[s.live_status]||s.live_status):"已暂停"}</span>
  ${s.enabled?`<p class="lv-meta">${s.monitor?.state==="connected"&&Date.now()/1000-s.monitor.updated<120?"开播事件监听中":s.monitor?.state==="retrying"?"开播监听重连中":"正在连接开播监听"} · 每 30 分钟补查${s.monitor?.error?`<br>${esc(s.monitor.error)}`:""}</p>`:""}
  <p class="lv-meta">亮点门槛 ${c.score_min} 分 · ${c.min_seconds}–${c.max_seconds} 秒<br>亮点${c.auto_highlights?"自动投稿":"手动投稿"} · 话题${c.auto_topics?"自动投稿":"手动投稿"} · 每天最多 ${c.daily_limit} 个<br>${s.rubric?`专属标准 v${s.rubric.version}`:"使用默认标准 · 专属规则待校准"}</p>
  ${s.error?`<p class="lv-error">${esc(s.error)}</p>`:""}<div class="lv-actions"><button class="btn plain" onclick="lvEditSource(${s.id})">编辑</button><button class="link" onclick="lvSourceAction(${s.id},'check')">立即检查</button><button class="link" onclick="lvPause(${s.id})">${s.enabled?"暂停":"恢复"}</button><button class="link" onclick="lvUnfollow(${s.id})">取消追踪</button><button class="link" onclick="lvManageSource(${s.id})">管理已有切片</button><a class="link" href="${esc(s.url)}" target="_blank" rel="noopener">直播间 ↗</a></div></div>`;}
function lvCard(c) {const s=lvSource(c.source_id); const stat=c.performance?.stats;
  const scored=['topic','traffic','fun','completeness'].every(k=>typeof c.scores?.[k]==='number'&&Number.isFinite(c.scores[k]));
  const showCover=c.preview&&c.cover_revision&&['awaiting_upload','uploading','submitted','uncertain'].includes(c.state);
  return `<article class="lv-panel">
  ${lvSelectable(c)?`<label class="lv-select"><input type="checkbox" aria-label="选择切片：${esc(c.title)}" ${liveSelected.has(c.id)?'checked':''} onchange="lvSelect(${c.id},this.checked)">选择</label>`:''}
  ${showCover?`<img class="lv-cover" src="/api/live/clips/${c.id}/cover?v=${c.version}&c=${encodeURIComponent(c.cover_revision||'')}" alt="" loading="lazy">`:""}<span class="lv-label${c.lane==="highlight"?" lv-hot":""}">${c.lane==="highlight"?"亮点":"完整话题"} · ${scored?`${c.score} 分`:"待评分"}</span><span class="lv-label">${LV_STATES[c.state]||c.state}</span>
  ${c.needs_review?'<span class="lv-label">已编辑 · 待人工确认</span>':""}<h3>${esc(c.title)}</h3><div class="lv-meta">${esc(c.source_name||s?.name||"直播")} · ${lvTime(c.start)}–${lvTime(c.end)} · ${Math.round(c.duration)} 秒</div>
  ${scored?`<div class="lv-score">话题 ${c.scores.topic} · 传播 ${c.scores.traffic} · 趣味 ${c.scores.fun} · 完整 ${c.scores.completeness}</div>${c.scores.reason?`<p class="lv-meta">评分依据：${esc(c.scores.reason)}</p>`:''}`:""}
  <p class="lv-meta">${esc(c.reason||c.description||"")}</p>${stat?`<p class="lv-meta">实际表现：播放 ${stat.view||0} · 赞 ${stat.like||0} · 收藏 ${stat.favorite||0}</p>`:""}
  ${c.error?`<p class="lv-error">${esc(c.error)}</p>`:""}<div class="lv-actions">
  ${(liveData.clips||[]).some(part=>part.merged_into===c.id&&part.state==='submitted')?'<p class="hint">其中短片段已有投稿记录；完整版本待你确认，不会因合并而重复投稿。</p>':''}
  ${c.preview?`<button class="btn plain" onclick="lvPreview(${c.id})">预览成片</button>`:""}
  ${!["uploading","submitted","uncertain"].includes(c.state)?`<button class="link" onclick="lvEditClip(${c.id})">编辑</button>`:""}
  ${c.state==="ready"?`<button class="btn" onclick="lvClipAction(${c.id},'upload')">投稿 B站</button>`:""}
  ${c.state==="failed"?`<button class="btn plain" onclick="lvClipAction(${c.id},'render')">重试制作</button>`:""}
  ${!["uploading","submitted","uncertain","rejected"].includes(c.state)?`<button class="link" onclick="lvClipAction(${c.id},'reject')">忽略</button>`:""}
  ${c.state==="uncertain"?`<a class="link" href="https://member.bilibili.com/platform/upload-manager/article" target="_blank" rel="noopener">去创作中心核实 ↗</a><button class="link" onclick="lvReconcile(${c.id})">填写已投稿 BV 号</button><button class="link" onclick="lvRetryUpload(${c.id})">确认未成功后重试</button>`:""}
  ${c.state==="submitted"?`<a class="link" href="${c.bvid?`https://www.bilibili.com/video/${esc(c.bvid)}`:"https://member.bilibili.com/platform/upload-manager/article"}" target="_blank" rel="noopener">查看投稿 ↗</a>`:""}</div></article>`;}
function lvRecordingGroups(sessions) {
  const groups=[],latest=new Map();
  for(const session of [...sessions].sort((a,b)=>a.started-b.started)){
    let origin={};try{origin=JSON.parse(session.origin||'{}');}catch{}
    const identity=JSON.stringify([session.owner,session.source_id,origin.room_id,origin.uid]);
    const group=latest.get(identity),previous=group?.sessions.at(-1),gap=session.started-(previous?.ended||0);
    if(previous?.status==='interrupted'&&previous.ended&&gap>=0&&gap<=300){group.sessions.push(session);}
    else{const next={source_id:session.source_id,started:session.started,sessions:[session]};groups.push(next);latest.set(identity,next);}
  }
  return groups.sort((a,b)=>b.sessions.at(-1).started-a.sessions.at(-1).started);
}
function lvRecordingStatus(session){return session.status==='recording'?'正在录制':session.status==='interrupted'?'录制中断':'录制已停止';}
function lvRecordingPart(session) {return `<div data-lv-recording-part="${esc(session.key)}"><p class="lv-meta">${esc(new Date(session.started*1000).toLocaleString('zh-CN',{hour12:false}))} · ${lvRecordingStatus(session)}</p><p>${esc(session.title)}</p>
  <p class="lv-meta">已分析至 ${lvTime(session.cursor)} · 当前话题：${esc(session.topic_title||"等待分析")}</p><button class="btn plain" onclick="lvEditClip(null,'${session.key}')">从这段录制新增切片</button>
  ${liveData.recordings.some(r=>r.session_key===session.key&&r.state==="failed")?'<p class="hint">有分段未能识别文字，后续分析会继续；可点击该分段预览原始视频。</p>':""}
  <div class="lv-actions">${liveData.recordings.filter(r=>r.session_key===session.key).map(r=>`<button class="link" ${r.state==="expired"?"disabled":""} onclick="lvRecording(${r.id})">${lvTime(r.offset)} · ${LV_STATES[r.state]||r.state}</button>`).join("")}</div>${session.status==='interrupted'?'<p class="hint">后台重启使录制连接中断，不表示主播下播。</p>':''}${session.error?`<p class="lv-error">${esc(session.error)}</p>`:""}</div>`;}
function lvRecordings(){return lvRecordingGroups(liveData.sessions).map(group=>{
  const latest=group.sessions.at(-1),name=lvSource(group.source_id)?.name||'直播';
  return `<details class="lv-panel lv-session" data-lv-detail="broadcast:${esc(group.sessions[0].key)}"><summary>${esc(name)} · ${esc(new Date(group.started*1000).toLocaleString('zh-CN',{hour12:false}))} · ${lvRecordingStatus(latest)}${group.sessions.length>1?` · ${group.sessions.length} 段录制连接`:''}</summary>${group.sessions.length>1?'<p class="hint">后台重启前后的录制归在同一条记录中。中断期间可能有内容未录到，各段仍使用独立时间轴。</p>':''}${group.sessions.map(lvRecordingPart).join('<hr>')}</details>`;
}).join('')||'<p class="hint">UP 主开播后会在这里生成直播记录，不需要等到下播。</p>';}
function lvCalibrations() {return liveData.sources.filter(s=>!s.archived).map(s=>{const r=s.rubric,t=s.calibration_task,a=s.last_calibration,c=s.settings,search=s.search_audit;
  const refs=liveData.references.filter(v=>v.source_id===s.id),e=a?.evidence||{},draft=a?.rubric;
  let reason="";
  if(a&&!a.current)reason="历史候选规则保留作参考；采样方式已更新，等待重新验证。";
  else if(a&&!e.validated)reason=e.rejection_reasons?.length?e.rejection_reasons.join("；")+"。继续使用当前标准。":e.recent_validation_count<4?"近期验证样本不足，候选规则仅供初始参考，暂未启用自动投稿。":e.revised_accuracy<e.baseline_accuracy?"校准后验证区分率下降，候选规则未启用。":"验证表现尚不足以支持启用新规则，继续使用默认标准。";
  return `<div class="lv-panel lv-session" data-lv-source="${s.id}"><h3>${esc(s.name)} · 评分校准</h3>
  ${s.sample_progress?.calculating?'<p class="hint">样本统计正在后台更新，完成后自动显示。</p>':''}
  <p>当前${r?`使用专属标准 v${r.version}`:"使用默认评分标准"}：话题性 35% · 传播潜力 35% · 趣味性 30%。</p>
  <p class="lv-meta">亮点门槛 ${c.score_min} 分，完整性至少 80 分，证据置信度至少 70%。默认规则可生成候选切片；专属校准通过后才允许自动投稿。</p>
  <p class="lv-meta">搜索近一年的直播切片，本人投稿和历史素材可用于学习风格；手动补充 BV 号可加入更早的代表作。来源不明先保留，再结合转写核验。传播效果只用发布满一天、近 90 天的第三方样本独立验证；同账号、发布相差不超过 21 天、时长相差不超过 3 倍的视频配对，至少 10 对、每轮最多 24 对。先生成局部修正，用单独调优集选方案，再用全新保留集验证。</p>
  <p class="lv-meta">近期效果验证样本 ${s.sample_progress?.independent||0} / ${s.sample_progress?.required||20}${(s.sample_progress?.independent||0)<(s.sample_progress?.required||20)?`，还需至少 ${(s.sample_progress?.required||20)-(s.sample_progress?.independent||0)} 个；可以补充视频 BV 号或切片 UP 主空间。`:'，可使用已核验的样本验证。'}</p>
  <p class="lv-meta">独立风格素材 ${s.sample_progress?.style||0} 个 · 下载转写待处理 ${s.sample_progress?.pending||0} 个 · 待转写核验 ${s.sample_progress?.verifying||0} 个 · 来源仍不明确 ${s.sample_progress?.uncertain||0} 个 · 核验排除 ${s.sample_progress?.rejected||0} 个。匹配样本全部入库，分批下载。</p>
  <p class="lv-meta">当前可比较 ${s.sample_progress?.comparable_pairs||0} 对。够一批即可校准，其余素材继续处理；相同快照复用评分和结果，不重复抽签。</p>
  ${s.calibration_wait?`<p class="hint">校准准备：可用 ${s.calibration_wait.comparable_pairs} / ${s.calibration_wait.required_pairs} 对；全新验证素材 ${s.calibration_wait.fresh_test_pairs} / ${s.calibration_wait.required_fresh_test_pairs} 对。历史验证素材不回流到训练。</p>`:''}
  ${s.learned_aliases?.length?`<p class="lv-meta">AI 提取的搜索别名：${s.learned_aliases.map(a=>`${esc(a.name)}（${a.support_bvids.map(bv=>`<a class="link" href="https://www.bilibili.com/video/${esc(bv)}" target="_blank" rel="noopener">来源</a>`).join('、')}）`).join('、')}。</p>`:''}
  ${s.style_profile?`<details><summary>已整理的风格参考（传播效果尚待独立验证）</summary><p>${esc(s.style_profile.summary)}</p><ul>${s.style_profile.observations.map(o=>`<li>${esc(o.text)}</li>`).join('')}</ul></details>`:''}
  <div class="lv-actions"><button class="btn plain" onclick="lvSourceAction(${s.id},'calibrate')">收集样本并校准</button><button class="btn plain" onclick="lvEditSource(${s.id},true)">补充样本 / 搜索词</button>${(s.sample_progress?.comparable_pairs||0)>=Math.ceil((s.sample_progress?.required||20)/2)?`<button class="btn plain" onclick="lvSourceAction(${s.id},'refine')">用现有样本重新验证</button>`:''}${t?.state==='failed'?`<button class="link" onclick="retryTask(${t.id})">重试失败步骤</button>`:''}</div>
  ${t?`<p class="lv-meta">最近任务：${({queued:"排队中",running:"正在处理",done:"已完成",failed:"未完成"})[t.state]||esc(t.state)}${t.error?` · ${esc(t.error)}`:""}</p>`:""}
  ${search?`${search.warning?`<p class="hint">${esc(search.warning)}</p>`:""}<p class="lv-meta">本轮搜索：${search.found} 个不同视频 → ${search.metadata_candidates} 个进入内容核验 → ${search.matched==null?"初筛中":`${search.matched} 个初筛匹配、${search.pending||0} 个待转写核验`}。风格参考范围 ${search.window_days} 天，最长 ${search.max_minutes} 分钟。${search.queries?`搜索词：${esc(search.queries.join("、"))}。`:""}</p>
    <p class="lv-meta">${Object.entries(search.excluded||{}).filter(([,n])=>n>0).map(([why,n])=>`${esc(why)} ${n} 个`).join("；")}</p>
    ${(search.examples||[]).length?`<details><summary>查看部分过滤明细</summary>${search.examples.map(v=>`<p class="lv-meta">${esc(v.title)}：${esc(v.reason)}</p>`).join("")}</details>`:""}`:""}
  ${reason?`<p class="hint">${esc(reason)}</p>`:""}
  ${r?`<p>专属标准：${esc(r.rubric.summary||"")}</p><ul>${(r.rubric.rules||[]).map(rule=>`<li>${esc(rule)}</li>`).join("")}</ul>`:""}
  ${draft&&(!r||a.version!==r.version)?`<details><summary>候选规则 v${a.version}（未启用）</summary><p>${esc(draft.summary||"")}</p><ul>${(draft.rules||[]).map(rule=>`<li>${esc(rule)}</li>`).join("")}</ul></details>`:""}
  ${a?`<p class="lv-meta">${e.sample_count||0} 个效果样本，另有 ${e.style_reference_ids?.length||0} 个风格素材。${e.training_count!=null?`训练 ${e.training_count} 个${e.tuning_count!=null?`、调优 ${e.tuning_count} 个`:''}、最终验证 ${e.validation_count||0} 个。`:''}${e.validation_evaluated===false?'候选未胜过调优基线或证据不足，未进行最终验证。':`${e.metric==='within_publisher_pairs'?'同账号配对命中率':'历史验证区分率'}：${Math.round((e.baseline_accuracy||0)*100)}% → ${Math.round((e.revised_accuracy||0)*100)}%。`}${e.validation_p_value!=null?`检验 p=${Number(e.validation_p_value).toFixed(3)}（要求 ≤0.1）。`:''}${esc(e.limitation||'')}</p>`:''}
  ${e.candidate_trials?.length?`<details><summary>候选方案比较与规则依据</summary>${e.default_tuning_accuracy!=null?`<p class="lv-meta">默认标准在调优集的配对命中率 ${Math.round(e.default_tuning_accuracy*100)}%</p>`:''}${e.candidate_trials.map(trial=>`<p class="lv-meta">方案 ${trial.candidate+1}${trial.candidate===e.selected_candidate?'（选中）':''} · ${trial.tuning_accuracy!=null?'调优配对命中率':'历史训练区分率'} ${Math.round((trial.tuning_accuracy??trial.training_accuracy??0)*100)}%</p><ul>${(trial.rule_evidence||[]).map(rule=>`<li>${esc(rule.rule)}<br><span class="lv-meta">正例 ${rule.support_ids.map(id=>esc(liveData.references.find(v=>v.id===id)?.title||`样本 #${id}`)).join('、')}；对照 ${rule.counter_ids.map(id=>esc(liveData.references.find(v=>v.id===id)?.title||`样本 #${id}`)).join('、')}</span></li>`).join('')}</ul>`).join('')}<p class="lv-meta">新流程只允许 1–2 条局部修正，每条至少有两组正反例。调优不胜过基线就保留默认标准，最终验证不参与选方案。</p></details>`:''}
  <details><summary>查看全部 ${refs.length} 个参考样本及核验原因</summary><div class="lv-rule">${refs.map(v=>`<p class="lv-meta"><a class="link" href="https://www.bilibili.com/video/${esc(v.bvid)}" target="_blank" rel="noopener">${esc(v.title)}</a><br>${esc(v.author)} · 播放 ${v.views} · ${v.role==="evaluation"?"近期效果验证候选":"风格学习素材"} · ${v.excluded?"已排除":({pending:"等待下载",queued:"下载已排队",downloading:"正在下载",transcribing:"正在转写",ready:"转写完成",failed:"处理失败"})[v.state]||v.state}<br>${({pending:"等待转写核验来源",matched:"来源已核验",uncertain:"来源仍不明确",rejected:"来源核验未通过"})[v.source_match]||"等待核验"}${v.match_reason?`：${esc(v.match_reason)}`:""} <button class="link" onclick="lvReferenceAction(${v.id},'${v.excluded?"include":"exclude"}')">${v.excluded?"恢复样本":"排除此样本"}</button>${v.state==="ready"&&["uncertain","rejected"].includes(v.source_match)&&!v.excluded?`<button class="link" onclick="lvReferenceAction(${v.id},'verify')">重新核验来源</button>`:""}${v.state==="failed"&&!v.excluded?`<button class="link" onclick="lvReferenceAction(${v.id},'retry')">重试下载</button>`:""}${v.error?` · ${esc(v.error)}`:""}</p>`).join("")}</div></details></div>`;}).join("")||'<p class="hint">先添加直播 UP，才能按对应受众校准。</p>';}
function lvSheet(title,body) {
  clearTimeout(liveQrPoll);liveQrEpoch++;
  $("#liveModal")?.remove(); const node=document.createElement("div"); node.className="sheet-bg";node.id="liveModal";
  node.innerHTML=`<div class="sheet lv-wide"><div class="lv-bar"><h2>${esc(title)}</h2><button class="link" onclick="lvClose()">关闭</button></div>${body}</div>`;
  node.querySelectorAll('video,audio').forEach(lvRememberVolume);
  node.addEventListener("click",e=>{if(e.target===node)lvClose();});document.body.append(node);
}
function lvSaveVolume(video){try{localStorage.setItem('playerVolume',JSON.stringify({volume:video.volume,muted:video.muted}));}catch{}}
function lvRememberVolume(video){
  try{const saved=JSON.parse(localStorage.getItem('playerVolume')||'null');if(saved){if(typeof saved.volume==='number'&&Number.isFinite(saved.volume))video.volume=Math.max(0,Math.min(1,saved.volume));video.muted=!!saved.muted;}}catch{}
  video.addEventListener('volumechange',()=>lvSaveVolume(video));
}
function lvClose(){ clearTimeout(liveQrPoll);liveQrEpoch++;const modal=$("#liveModal");modal?.querySelectorAll('video,audio').forEach(lvSaveVolume);modal?.remove(); }
function lvSelect(id,checked){checked?liveSelected.add(id):liveSelected.delete(id);renderLive();}
function lvSelectAll(){const ids=lvVisibleClips().filter(lvSelectable).map(c=>c.id);const all=ids.length&&ids.every(id=>liveSelected.has(id));liveSelected.clear();if(!all)ids.forEach(id=>liveSelected.add(id));renderLive();}
function lvManageSource(id){liveSourceFilter=String(id);liveStatus='all';lvTab('highlight');}
async function lvUnfollow(id){try{await lvApi(`/api/live/sources/${id}/delete`,{});await loadLive();toast('已取消追踪，已有切片保留');}catch(e){toast(e.message);}}
async function lvBulk(action){const ids=[...liveSelected];if(!ids.length)return;if(action==='delete'&&!confirm(`删除所选 ${ids.length} 条片段及成片、封面？此操作无法撤销，投稿记录会保留。`))return;
  try{const result=await lvApi('/api/live/clips/batch',{ids,action});liveSelected.clear();await loadLive();toast(`已${action==='delete'?'删除':'忽略'} ${result.changed.length} 条${result.skipped.length?`，保留 ${result.skipped.length} 条`:''}`);if(result.skipped.length)lvSheet('以下片段已保留',result.skipped.map(s=>`<p>${esc(lvClip(s.id)?.title||`片段 #${s.id}`)}：${esc(s.reason)}</p>`).join(''));}catch(e){toast(e.message);}}
function lvEditSource(id, samples=false) {
  const s=lvSource(id),c=s?.settings||{score_min:80,min_seconds:15,max_seconds:180,topic_minutes:20,daily_limit:10,tid:138,auto_highlights:true,auto_topics:false,calibration_enabled:true,calibration_days:7};
  lvSheet(s?"编辑追踪 UP":"新增直播追踪",`<form class="lv-form" onsubmit="lvSaveSource(event,${id||0})">
    ${s?`<p class="lv-meta">${esc(s.name)}</p>`:""}<label>直播 UP 链接<input name="url" type="text" required value="${esc(s?.url||"")}" placeholder="粘贴 UP 主页、直播间或 b23.tv 分享链接" autofocus></label>
    <p class="hint">自动识别主播名字和直播间。也可以直接粘贴 B站 App 的整段分享文字。</p>
    <details class="lv-advanced" ${samples?"open":""}><summary>高级设置（可选）</summary><div class="lv-advanced-body">
    <label>显示名字<input name="name" maxlength="80" value="${esc(s?.name||"")}" placeholder="自动识别，可自行修改"></label>
    <label>标题称呼<input name="title_name" maxlength="20" value="${esc(c.title_name||'')}" placeholder="例如：老谢、冯律师；留空使用主播全名"></label>
    <div class="lv-two"><label>亮点评分门槛<input name="score_min" type="number" min="50" max="100" value="${c.score_min}" required></label><label>每日最多投稿数<input name="daily_limit" type="number" min="1" max="50" value="${c.daily_limit}" required></label><label>亮点最短秒数<input name="min_seconds" type="number" min="5" max="120" value="${c.min_seconds}" required></label><label>亮点最长秒数<input name="max_seconds" type="number" min="15" max="300" value="${c.max_seconds}" required></label><label>长话题最多几分钟<input name="topic_minutes" type="number" min="2" max="60" value="${c.topic_minutes}" required></label><label>B站投稿分区 ID<input name="tid" type="number" min="1" max="9999" value="${c.tid}" required></label></div>
    <label>参考视频（BV 号或完整链接，多个用逗号分隔）<textarea name="reference_bvids" rows="2" placeholder="可补充本人发布、第三方切片或历史代表作">${esc(c.reference_bvids||'')}</textarea></label>
    <label>切片 UP 主（空间链接或 UID，多个用逗号分隔）<input name="reference_mids" value="${esc(c.reference_mids||'')}" placeholder="例如：https://space.bilibili.com/123456"></label>
    <p class="hint">补充样本会结合转写核验来源、去除重复。本人投稿和历史素材用于学习风格，近期第三方样本用于验证效果；保存后点击「收集样本并校准」。</p>
    <label>主播别名（多个用逗号分隔）<input name="source_aliases" value="${esc(c.source_aliases||'')}" placeholder="填写该主播实际使用的简称"></label><p class="hint">搜索会自动补上主播全名，别名与自定义关键词共同使用。</p><label>选题偏好<textarea name="preferences" rows="3" placeholder="例如：关注真实创业反差、实用建议，少选纯吐槽">${esc(c.preferences||"")}</textarea></label><label>切片搜索关键词（多个用英文逗号分隔）<input name="search_keywords" value="${esc(c.search_keywords||"")}" placeholder="例如：勇哥餐饮,勇哥"></label>
    <label class="lv-check"><input name="enabled" type="checkbox" ${!s||s.enabled?"checked":""}>启用直播追踪</label><label class="lv-check"><input name="auto_highlights" type="checkbox" ${c.auto_highlights?"checked":""}>亮点成片后自动投稿</label><label class="lv-check"><input name="auto_topics" type="checkbox" ${c.auto_topics?"checked":""}>完整话题也自动投稿（包含已投稿亮点的话题留待确认）</label><label class="lv-check"><input name="calibration_enabled" type="checkbox" ${c.calibration_enabled?"checked":""}>每周重新校准评分</label></div></details>
    <p class="hint">默认开播后自动录制、分析并生成候选切片；自动投稿先等待评分预校准通过，并使用单独绑定的 B站账号。</p>
    <div class="lv-form-footer"><p class="lv-error" id="lvFormError" role="alert"></p><button class="btn" type="submit">${s?"保存追踪":"开始追踪"}</button></div></form>`);
  if(samples)setTimeout(()=>{const el=$('#liveModal [name=reference_bvids]');el?.focus();el?.scrollIntoView({block:'center'});},50);
}
async function lvSaveSource(event,id) {event.preventDefault();const form=event.target,button=form.querySelector('[type="submit"]'),label=button.textContent;
  try{const values=new FormData(form),body={name:String(values.get("name")||"").trim(),url:String(values.get("url")||"").trim(),enabled:values.has("enabled"),settings:{}};
    for(const key of ["score_min","min_seconds","max_seconds","topic_minutes","daily_limit","tid"])body.settings[key]=Number(values.get(key));
    for(const key of ["auto_highlights","auto_topics","calibration_enabled"])body.settings[key]=values.has(key);
    body.settings.preferences=String(values.get("preferences")||"");body.settings.search_keywords=String(values.get("search_keywords")||"");
    for(const key of ["reference_bvids","reference_mids","source_aliases","title_name"])body.settings[key]=String(values.get(key)||"");
    button.disabled=true;button.textContent=id?"正在保存…":"正在识别并添加…";form.querySelector("#lvFormError").textContent="";
    const result=await lvApi("/api/live/sources"+(id?`/${id}`:""),body);lvClose();await loadLive();toast(result.already_tracked?"该 UP 已在追踪列表中":"追踪设置已保存");
  }catch(e){if(form.isConnected){form.querySelector("#lvFormError").textContent=e.message;button.disabled=false;button.textContent=label;}}}
async function lvPause(id){const s=lvSource(id);try{await lvApi(`/api/live/sources/${id}`,{enabled:!s.enabled});await loadLive();}catch(e){toast(e.message);}}
async function lvSourceAction(id,action){try{await lvApi(`/api/live/sources/${id}/${action}`,{});toast(action==="calibrate"?"校准任务已排队":"已安排检查");await loadLive();}catch(e){toast(e.message);}}
function lvEditClip(id,sessionKey) {
  const c=lvClip(id),session=liveData.sessions.find(s=>s.key===(c?.session_key||sessionKey))||liveData.sessions[0];
  if(!session){toast("还没有直播素材");return;}
  const records=liveData.recordings.filter(r=>r.session_key===session.key),end=records.reduce((max,r)=>Math.max(max,r.offset+r.duration),0);
  lvSheet(c?"编辑切片":"手动新增切片",`<form class="lv-form" onsubmit="lvSaveClip(event,${id||0})">${c?.preview?`<video class="lv-video" src="${esc(c.preview)}" controls playsinline></video>`:""}
    <label>录制分段<select name="session" ${id?"disabled":""}>${liveData.sessions.map(s=>`<option value="${s.key}"${s.key===session.key?" selected":""}>${esc(lvSource(s.source_id)?.name||"直播")} · ${esc(new Date(s.started*1000).toLocaleString())}</option>`).join("")}</select></label>
    <div class="lv-two"><label>开始时间（本段录制时分秒）<input name="start" required value="${lvTime(c?.start||0)}"></label><label>结束时间<input name="end" required value="${lvTime(c?.end||Math.min(60,end))}"></label></div>
    <label>投稿标题<input name="title" maxlength="80" required value="${esc(c?.title||session.topic_title||session.title)}"></label>
    <label>封面文案<textarea name="cover_title" maxlength="48" rows="2" placeholder="用一句短问题或反差提炼看点，可换行；留空从标题排版">${esc(c?.cover_title||'')}</textarea></label>
    <label>简介<textarea name="description" rows="4">${esc(c?.description||"")}</textarea></label><label>标签（英文逗号分隔）<input name="tags" required value="${esc((c?.tags||["直播切片"]).join(","))}"></label><p class="hint">标题会保留主播称呼。确认投稿后才生成封面；现在可先留空封面文案。投稿简介会附上原主播名字和直播间链接。</p><div class="lv-form-footer"><p class="lv-error" id="lvFormError" role="alert"></p><button class="btn" type="submit">保存</button></div></form>`);
}
async function lvSaveClip(event,id){event.preventDefault();const f=event.target,body={session:f.elements.session.value,start:lvSeconds(f.elements.start.value),end:lvSeconds(f.elements.end.value),title:f.elements.title.value,cover_title:f.elements.cover_title.value,description:f.elements.description.value,tags:f.elements.tags.value.split(",").map(t=>t.trim()).filter(Boolean)};
  try{await lvApi("/api/live/clips"+(id?`/${id}`:""),body);lvClose();await loadLive();toast("切片已保存");}catch(e){$("#lvFormError").textContent=e.message;}}
function lvPreview(id){const c=lvClip(id);if(c?.preview)lvSheet(c.title,`<video class="lv-video" src="${esc(c.preview)}" controls autoplay playsinline></video><p style="white-space:pre-wrap">${esc(c.publication_description||c.description)}</p>`);}
function lvRecording(id){const r=liveData.recordings.find(x=>x.id===id);if(r)lvSheet(`直播原始分段 · ${lvTime(r.offset)}`,`<video class="lv-video" src="/api/live/recordings/${id}/media" controls autoplay playsinline></video>`);}
async function lvClipAction(id,action,body={}){try{await lvApi(`/api/live/clips/${id}/${action}`,body);await loadLive();toast(action==="upload"?"已加入投稿队列":"已更新");}catch(e){toast(e.message);}}
function lvRetryUpload(id){if(confirm("已在 B站创作中心确认这条切片没有投稿成功？确认后会重新上传。"))lvClipAction(id,"retry-upload",{confirmed_absent:true});}
function lvReconcile(id){const bvid=prompt("填入已经投稿成功的视频 BV 号");if(bvid)lvClipAction(id,"confirm-upload",{bvid:bvid.trim()});}
async function lvLogin(){const requested=++liveQrEpoch;clearTimeout(liveQrPoll);try{
    const data=await lvApi("/api/live/login/qr",{});if(requested!==liveQrEpoch)return;
    lvSheet("绑定 B站投稿账号",`<p>使用 B站 App 扫码并在手机上确认。这只绑定当前拾光账号的投稿功能。</p><img class="lv-qr" src="${data.qr}" alt="B站登录二维码"><p class="hint" id="lvQrStatus">等待扫码…</p><button class="btn plain" onclick="lvLogin()">刷新二维码</button>`);
    const active=liveQrEpoch;
    async function poll(){if(active!==liveQrEpoch||!$("#lvQrStatus"))return;try{
      const d=await lvApi(`/api/live/login/qr/${data.login}`);if(active!==liveQrEpoch||!$("#lvQrStatus"))return;
      if(d.status==="done"){lvClose();await loadLive();toast("投稿账号已绑定");return;}
      if(d.status==="expired"){$("#lvQrStatus").textContent="二维码已过期，请点击刷新二维码";return;}
      $("#lvQrStatus").textContent=d.status==="scanned"?"已扫码，请在 B站 App 上确认登录…":"等待扫码…";
    }catch(e){if(active!==liveQrEpoch||!$("#lvQrStatus"))return;$("#lvQrStatus").textContent=e.message;}
    liveQrPoll=setTimeout(poll,2500);}liveQrPoll=setTimeout(poll,2000);
  }catch(e){if(requested===liveQrEpoch)toast(e.message);}}
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&chip==="live")loadLive();});

async function lvReferenceAction(id,action){try{await lvApi(`/api/live/references/${id}/${action}`,{});await loadLive();toast(action==='exclude'?'已排除此样本':action==='include'?'已恢复样本':'已加入重试队列');}catch(e){toast(e.message);}}
