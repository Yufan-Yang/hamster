// Personal processing state; opening this panel never starts a scan or a model call.
let stewardEpoch = 0, stewardTimer = null;
let stewardView = {data: null, offset: 0, busy: false, error: '', notice: '', controller: null, draft: null};
function resetSteward() {
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
    } else stewardView.notice = '任务已安排，每轮盘点最多 25 项，结果会自动刷新。';
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
    <p class="hint">核对已登记媒体的文件、字幕、总结和章节，按处理规则补齐缺项。手动补全会在后台继续推进，无需开启自动巡检。下面只展示当前账号与隐私视图中的内容。</p>
    <div class="steward-controls"><button type="button" class="btn plain" onclick="scanSteward(false)"${v.busy ? ' disabled' : ''}>盘点当前范围</button>
    <button type="button" class="btn" onclick="scanSteward(true)"${v.busy ? ' disabled' : ''}>补全当前范围</button>
    <button type="button" class="link" onclick="loadSteward()"${v.busy ? ' disabled' : ''}>刷新</button></div>
    <p role="status" aria-live="polite">${v.busy ? '正在读取…' : d?.busy ? '正在盘点或安排补全，完成后会自动刷新。' : esc(v.notice)}</p>
    ${v.error ? `<p class="steward-error" role="alert">${esc(v.error)}</p>` : ''}
    ${d ? `<div class="steward-counts">${Object.entries(d.counts || {}).map(([key,count]) => `<span>${esc(labels[key] || key)} ${Number(count) || 0}</span>`).join('') || '<span>当前范围没有媒体</span>'}</div>
    <form class="steward-policy" onsubmit="event.preventDefault();saveStewardPolicy()">
      <label>处理档位<select id="stewardProfile" onchange="editStewardPolicy()">${[['essential','基础：核对文件、整理已有字幕'],['standard','标准：补全需要内容分析的媒体'],['deep','深入：分析全部非音乐媒体']].map(([value,label]) => `<option value="${value}"${settings.profile === value ? ' selected' : ''}>${label}</option>`).join('')}</select></label>
      <label><input type="checkbox" id="stewardAutomatic" onchange="editStewardPolicy()"${settings.automatic ? ' checked' : ''}>自动巡检并补全我的整个片库</label>
      <label><input type="checkbox" id="stewardAI" onchange="editStewardPolicy()"${settings.ai_planning ? ' checked' : ''}>按需用 AI 安排补全顺序</label>
      <p class="hint">自动补全包含隐藏媒体；展示仍遵守隐私视图。每轮最多安排 5 项，每天最多 40 项。暂停后停止安排新任务，已开始的处理继续完成。AI 排程每小时最多一次、每天最多 12 次。</p>
      <button type="submit" class="btn plain"${v.busy ? ' disabled' : ''}>保存处理规则</button>
    </form>
    <div>${(d.items || []).map(item => `<article class="steward-item"><div class="steward-heading"><strong>${esc(item.title || '未命名媒体')}</strong><span>${esc(labels[item.state] || item.state)}</span></div>
      ${item.checked ? `<p class="hint">上次盘点 ${esc(new Date(item.checked * 1000).toLocaleString())} · 已读取文件 ${Number(item.observation.files) || 0} · 有字幕 ${Number(item.observation.subtitles) || 0} · 有章节 ${Number(item.observation.chapters) || 0} · ${item.observation.parts?.length > 1 ? '主文件' : ''}${item.observation.summary ? '已有总结' : '尚无总结'}</p>` : '<p class="hint">尚未盘点，可安排当前范围盘点。</p>'}
      ${(item.observation.issues || []).map(text => `<p class="steward-error">${esc(text)}</p>`).join('')}
      ${(item.actions || []).length ? `<ul>${item.actions.map(action => `<li>${esc(kinds[action.kind] || action.kind)} · ${esc(actionStates[action.state] || action.state)}：${esc(action.reason)}</li>`).join('')}</ul>` : ''}</article>`).join('')}</div>
      <div class="steward-controls"><button type="button" class="btn plain" onclick="loadSteward(${Math.max(0,v.offset - 25)})"${v.offset === 0 || v.busy ? ' disabled' : ''}>上一页</button><span>第 ${Math.floor(v.offset / 25) + 1} 页</span><button type="button" class="btn plain" onclick="loadSteward(${v.offset + 25})"${!d.more || v.busy ? ' disabled' : ''}>下一页</button></div>` : ''}`;
}
