// Personal processing state; opening this panel never starts a scan or a model call.
let stewardEpoch = 0, stewardTimer = null;
let stewardSummary = {at:0, epoch:0, controller:null};
let stewardView = {data: null, offset: 0, busy: false, error: '', notice: '', controller: null, draft: null};
function resetSteward() {
  stewardSummary.controller?.abort();
  stewardSummary = {at:0, epoch:stewardSummary.epoch + 1, controller:null};
  const summary = $('#stewardSummary'); if (summary) {summary.hidden = true; summary.innerHTML = '';}
  ++stewardEpoch; clearTimeout(stewardTimer); stewardView.controller?.abort();
  stewardView = {data: null, offset: 0, busy: false, error: '', notice: '', controller: null, draft: null};
  $('#stewardBody').innerHTML = ''; $('#stewardSheet').hidden = true;
}
async function openSteward() {
  resetSteward(); $('#stewardSheet').hidden = false;
  await loadSteward();
}
function stewardCurrent(epoch, owner, privacy) {
  return epoch === stewardEpoch && owner === storageOwner && privacy === privateViewEpoch && !$('#stewardSheet').hidden;
}
async function stewardRequest(path, body) {
  if (stewardView.busy || $('#stewardSheet').hidden) return;
  const epoch = stewardEpoch, owner = storageOwner, privacy = privateViewEpoch;
  const controller = new AbortController(); stewardView.controller = controller;
  clearTimeout(stewardTimer); stewardView.busy = true; stewardView.error = ''; renderSteward();
  try {
    const response = await fetch(path, {signal: controller.signal, ...(body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
    const data = await response.json();
    if (!stewardCurrent(epoch, owner, privacy)) return;
    if (data.storage_owner !== owner || response.status === 403 || response.status === 401) {
      resetSteward(); toast(data.error || '账号或隐私状态已变化，请重新打开。'); return;
    }
    if (!response.ok) throw Error(data.error || '媒体管家暂时不可用');
    if (body === undefined) stewardView.data = data;
    else if (path.endsWith('/policy')) {
      stewardView.data.policy = data.policy; stewardView.draft = null; stewardView.notice = '处理规则已保存。';
    } else stewardView.notice = path.endsWith('/action') && body.action === 'keep_layout' ? '已记住：保留这条媒体的现有归档。' : '任务已安排，处理结果会自动刷新。';
  } catch (error) {
    if (stewardCurrent(epoch, owner, privacy) && error.name !== 'AbortError') {
      stewardView.error = error.message; stewardView.data = null;
    }
  } finally {
    if (stewardCurrent(epoch, owner, privacy)) {
      stewardView.busy = false; stewardView.controller = null; renderSteward();
      if (body !== undefined || stewardView.data?.busy || stewardView.data?.policy.automatic) {
        stewardTimer = setTimeout(loadSteward, stewardView.data?.busy || body !== undefined ? 3000 : 10000);
      }
    }
  }
}
async function loadSteward(offset = stewardView.offset) {
  if (stewardView.busy) return;
  stewardView.offset = Math.max(0, Number(offset) || 0);
  await stewardRequest('/api/steward?offset=' + stewardView.offset);
}
async function scanSteward(apply = false) {
  await stewardRequest('/api/steward/scan', {apply});
}
async function careAction(job, action) {
  const item = stewardView.data?.care?.items.find(item => item.job_id === job);
  if (!item) return;
  await stewardRequest('/api/steward/care/action', {job_id:job, action, revision:item.revision});
}
async function undoCare(operation) {
  await stewardRequest('/api/steward/care/undo', {operation_id:operation});
}
async function recoverUnregistered() {
  if (stewardView.busy || !stewardView.data?.can_recover_files) return;
  resetSteward(); await openTrash(); await scanTrash();
}
async function refreshStewardSummary() {
  const s = stewardSummary;
  if (!storageOwner || storageOwner === 'unidentified' || state.login_required || document.hidden || s.controller || Date.now() - s.at < 60000) return;
  const owner = storageOwner, privacy = privateViewEpoch, controller = s.controller = new AbortController();
  try {
    const response = await fetch('/api/steward', {signal:controller.signal}), data = await response.json();
    if (stewardSummary !== s || storageOwner !== owner || privateViewEpoch !== privacy) return;
    const box = $('#stewardSummary');
    if (!response.ok || data.storage_owner !== owner) {if (box) {box.hidden=true;box.innerHTML='';}return;}
    s.at = Date.now();
    const care = data.care, latest = care?.receipts?.[0];
    const pending = (Number(data.counts?.needs_work)||0) + (Number(data.counts?.blocked)||0);
    if (!box) return;
    box.hidden = false;
    box.innerHTML = `<button class="link" onclick="openSteward()">媒体管家 · ${care?.total ? `${Number(care.total)} 项文件或下载需要处理` : pending ? `${pending} 项内容待补全` : care?.checked ? `已检查 ${Number(care.checked)} 条媒体` : '正在检查文件与下载'}</button>${latest ? `<span class="hint">最近：${esc(latest.outcome)}</span>` : ''}`;
  } catch (_) { if (stewardSummary === s) s.at = Date.now() - 45000; }
  finally { if (stewardSummary === s) s.controller = null; }
}
async function saveStewardPolicy() {
  await stewardRequest('/api/steward/policy', {
    profile: $('#stewardProfile').value,
    automatic: $('#stewardAutomatic').checked,
    ai_planning: $('#stewardAI').checked,
  });
}
function editStewardPolicy() {
  stewardView.draft = {profile: $('#stewardProfile').value,
    automatic: $('#stewardAutomatic').checked, ai_planning: $('#stewardAI').checked};
}
function renderSteward() {
  const v = stewardView, d = v.data;
  const settings = v.draft || d?.policy;
  const labels = {unchecked:'未盘点',ready:'已达标',needs_work:'待补全',waiting:'处理中',blocked:'需要处理',shared:'共享媒体'};
  const kinds = {steward_index:'整理已有字幕',transcribe:'补全转写',summarize:'补全总结',chapters:'补全章节'};
  const actionStates = {pending:'待安排',waiting:'处理中',backoff:'等待复查',blocked:'需要处理'};
  $('#stewardBody').innerHTML = `<div class="steward-heading"><h2 id="stewardTitle">媒体管家</h2><button type="button" class="link" onclick="resetSteward()">关闭</button></div>
    <p class="hint">后台会自动检查文件归档与下载状态。下面列出具体问题、可执行的整理方案和处理结果，只展示当前账号与隐私视图中的内容。</p>
    ${d?.care ? `<section class="steward-care"><h3>文件与下载 · ${Number(d.care.total)} 项待处理</h3><p class="hint">已检查 ${Number(d.care.checked)} 条媒体。归档会同时更新字幕、封面和记录，并保留撤销入口。</p>
      ${(d.care.items || []).map(item => `<article class="steward-item"><strong>${esc(item.title || '未命名媒体')}</strong>
        ${item.stale ? '<p class="hint">内容刚刚更新，正在复查最新文件与处理方案…</p>' : ''}
        ${item.issues.map(issue => `<p>${esc(issue)}</p>`).join('')}
        ${item.actions.map(action => `<div><p>${esc(action.detail)}</p>${action.moves ? `<details><summary>查看 ${action.moves.length} 个文件的改动</summary>${action.moves.map(move => `<p class="hint">${esc(move.from)} → ${esc(move.to)}</p>`).join('')}</details>` : ''}
          <button class="btn plain" onclick="careAction(${Number(item.job_id)},'${esc(action.id)}')"${v.busy ? ' disabled' : ''}>${esc(action.label)}</button>
          ${action.id === 'organize_files' ? `<button class="link" onclick="careAction(${Number(item.job_id)},'keep_layout')"${v.busy ? ' disabled' : ''}>保留现有归档</button>` : ''}</div>`).join('')}</article>`).join('') || '<p>已检查范围内没有待处理的文件或下载问题。</p>'}
      ${d.care.total > d.care.items.length ? '<p class="hint">处理当前事项后继续显示其余问题。</p>' : ''}
      ${d.can_recover_files ? '<button class="link" onclick="recoverUnregistered()">查找未登记文件</button><button class="link" onclick="resetSteward();openTrash()">查看找回的文件</button>' : ''}
      <h3>处理回执</h3>${(d.care.receipts || []).map(item => `<p>${esc(item.title || '媒体')} · ${esc(item.outcome)}${item.state === 'queued' || item.state === 'running' ? '（后台处理中）' : ''}</p>`).join('') || '<p class="hint">执行整理后，这里会记录实际处理结果。</p>'}
      ${(d.care.operations || []).map(op => `<p>${esc(op.title)} · ${op.state === 'undone' ? '已撤销' : op.state === 'done' ? '归档完成' : '归档中断或正在处理'} ${op.error ? esc(op.error) : ''} ${op.state !== 'undone' ? `<button class="link" onclick="undoCare(${Number(op.id)})"${v.busy ? ' disabled' : ''}>撤销归档</button>` : ''}</p>`).join('')}
      </section>` : ''}
    <h3>字幕与内容补全</h3>
    <div class="steward-controls"><button type="button" class="btn plain" onclick="scanSteward(false)"${v.busy ? ' disabled' : ''}>盘点当前范围</button>
    <button type="button" class="btn" onclick="scanSteward(true)"${v.busy ? ' disabled' : ''}>补全当前范围</button>
    <button type="button" class="link" onclick="loadSteward()"${v.busy ? ' disabled' : ''}>刷新</button></div>
    <p role="status" aria-live="polite">${v.busy ? '正在读取…' : d?.busy ? '正在盘点或安排补全，完成后会自动刷新。' : esc(v.notice)}</p>
    ${v.error ? `<p class="steward-error" role="alert">${esc(v.error)}</p>` : ''}
    ${d ? `<div class="steward-counts">${Object.entries(d.counts || {}).map(([key,count]) => `<span>${esc(labels[key] || key)} ${Number(count) || 0}</span>`).join('') || '<span>当前范围没有媒体</span>'}</div>
    <form class="steward-policy" onsubmit="event.preventDefault();saveStewardPolicy()">
      <label>处理档位<select id="stewardProfile" onchange="editStewardPolicy()">${[['essential','基础：核对文件、整理已有字幕'],['standard','标准：补全需要内容分析的媒体'],['deep','深入：分析全部非音乐媒体']].map(([value,label]) => `<option value="${value}"${settings.profile === value ? ' selected' : ''}>${label}</option>`).join('')}</select></label>
      <label><input type="checkbox" id="stewardAutomatic" onchange="editStewardPolicy()"${settings.automatic ? ' checked' : ''}>按视频类型自主整理、恢复下载并补全内容</label>
      <label><input type="checkbox" id="stewardAI" onchange="editStewardPolicy()"${settings.ai_planning ? ' checked' : ''}>按需用 AI 安排补全顺序</label>
      <p class="hint">自主处理包含隐藏媒体；展示仍遵守隐私视图。归档先审查文件、引用和目标冲突，完成后可撤销；取消的下载不恢复。暂停后停止安排新任务。AI 排程每小时最多一次、每天最多 12 次。</p>
      <button type="submit" class="btn plain"${v.busy ? ' disabled' : ''}>保存处理规则</button>
    </form>
    <div>${(d.items || []).map(item => `<article class="steward-item"><div class="steward-heading"><strong>${esc(item.title || '未命名媒体')}</strong><span>${esc(labels[item.state] || item.state)}</span></div>
      ${item.checked ? `<p class="hint">上次盘点 ${esc(new Date(item.checked * 1000).toLocaleString())} · 已读取文件 ${Number(item.observation.files) || 0} · 有字幕 ${Number(item.observation.subtitles) || 0} · 有章节 ${Number(item.observation.chapters) || 0} · ${item.observation.parts?.length > 1 ? '主文件' : ''}${item.observation.summary ? '已有总结' : '尚无总结'}</p>` : '<p class="hint">尚未盘点，可安排当前范围盘点。</p>'}
      ${item.observation.workflow ? `<p>${esc(item.observation.workflow.type)} · ${esc(item.observation.workflow.reason)}</p><p class="hint">方案：${item.observation.workflow.steps.map(esc).join(' → ')} · 审查：${item.observation.review?.approved ? '通过' : '等待处理'}</p>` : ''}
      ${(item.observation.issues || []).map(text => `<p class="steward-error">${esc(text)}</p>`).join('')}
      ${(item.actions || []).length ? `<ul>${item.actions.map(action => `<li>${esc(kinds[action.kind] || action.kind)} · ${esc(actionStates[action.state] || action.state)}：${esc(action.reason)}</li>`).join('')}</ul>` : ''}</article>`).join('')}</div>
      <div class="steward-controls"><button type="button" class="btn plain" onclick="loadSteward(${Math.max(0,v.offset - 25)})"${v.offset === 0 || v.busy ? ' disabled' : ''}>上一页</button><span>第 ${Math.floor(v.offset / 25) + 1} 页</span><button type="button" class="btn plain" onclick="loadSteward(${v.offset + 25})"${!d.more || v.busy ? ' disabled' : ''}>下一页</button></div>` : ''}`;
}
