// Routing receipts load only when explicitly opened; never issue a model request.
let aiActivityEpoch = 0;
let aiActivityView = newAIActivityView();
function newAIActivityView() {
  return {items: [], summary: [], purposes: [], days: 7, scope: 'mine', purpose: '', total: 0,
    more: false, loading: false, error: '', controller: null, canSystem: false};
}
function resetAIActivity() {
  ++aiActivityEpoch; aiActivityView.controller?.abort(); aiActivityView = newAIActivityView();
  $('#aiActivityBody').innerHTML = ''; $('#aiActivitySheet').hidden = true;
}
function closeAIActivity() { resetAIActivity(); }
async function openAIActivity() {
  resetAIActivity(); aiActivityView.canSystem = !!state.admin;
  $('#aiActivitySheet').hidden = false;
  await loadAIActivity();
}
function aiActivityCurrent(epoch, owner, privacy) {
  return epoch === aiActivityEpoch && storageOwner === owner && privateViewEpoch === privacy && !$('#aiActivitySheet').hidden;
}
async function changeAIActivity(key, value) {
  if (!['days', 'scope', 'purpose'].includes(key)) return;
  aiActivityView.controller?.abort(); ++aiActivityEpoch;
  aiActivityView[key] = key === 'days' ? Number(value) : value;
  aiActivityView.items = []; aiActivityView.summary = []; aiActivityView.total = 0;
  aiActivityView.more = false; aiActivityView.loading = false;
  await loadAIActivity();
}
async function loadAIActivity(more = false) {
  const v = aiActivityView;
  if (v.loading || $('#aiActivitySheet').hidden) return;
  const epoch = aiActivityEpoch, owner = storageOwner, privacy = privateViewEpoch;
  const offset = more ? v.items.length : 0;
  const controller = new AbortController(); v.controller = controller; v.loading = true; v.error = '';
  renderAIActivity();
  try {
    const params = new URLSearchParams({days: String(v.days), scope: v.scope, purpose: v.purpose, offset: String(offset), limit: '30'});
    const response = await fetch('/api/ai/activity?' + params, {signal: controller.signal});
    const data = await response.json();
    if (!aiActivityCurrent(epoch, owner, privacy)) return;
    if (data.storage_owner && data.storage_owner !== owner) { resetAIActivity(); return; }
    if (!response.ok) {
      if (response.status === 403) { resetAIActivity(); toast(data.error || '权限已变化，请重新打开。'); return; }
      throw Error(data.error || 'AI 运行记录暂时不可用');
    }
    v.items = more ? [...new Map([...v.items, ...(data.items || [])].map(item => [item.id, item])).values()] : data.items || [];
    v.summary = data.summary || []; v.purposes = data.purposes || [];
    v.total = Number(data.total) || 0; v.more = !!data.more; v.canSystem = !!data.can_system;
  } catch (error) {
    if (aiActivityCurrent(epoch, owner, privacy) && error.name !== 'AbortError') {
      v.error = error.message; v.items = []; v.summary = []; v.total = 0; v.more = false;
    }
  } finally {
    if (aiActivityCurrent(epoch, owner, privacy)) { v.loading = false; v.controller = null; renderAIActivity(); }
  }
}
function aiActivitySeconds(value) {
  return value == null || !Number.isFinite(Number(value)) ? '未观测' : Number(value).toFixed(1) + ' 秒';
}
function renderAIActivity() {
  const v = aiActivityView, engineName = {codex: 'Codex', deepseek: 'DeepSeek', api: '备用 API'};
  $('#aiActivityBody').innerHTML = `<div class="ai-activity-heading"><h2>AI 运行明细</h2><button type="button" class="link" onclick="closeAIActivity()">关闭</button></div>
    <p class="hint">仅记录运行信息，不保存提问或回答。保留最近 30 天、全站最多 20,000 条；隐藏任务沿用隐私权限。系统任务与个人任务分开查看。</p>
    <div class="ai-activity-filters"><label>时间<select aria-label="AI 记录时间" onchange="changeAIActivity('days',this.value)">${[1,7,30].map(days => `<option value="${days}"${days === v.days ? ' selected' : ''}>最近 ${days} 天</option>`).join('')}</select></label>
    <label>用途<select aria-label="AI 记录用途" onchange="changeAIActivity('purpose',this.value)"><option value="">所有用途</option>${v.purposes.map(item => `<option value="${esc(item.value)}"${item.value === v.purpose ? ' selected' : ''}>${esc(item.label)}</option>`).join('')}</select></label>
    ${v.canSystem ? `<label>范围<select aria-label="AI 记录范围" onchange="changeAIActivity('scope',this.value)"><option value="mine"${v.scope === 'mine' ? ' selected' : ''}>我的任务</option><option value="system"${v.scope === 'system' ? ' selected' : ''}>系统任务</option></select></label>` : ''}
    <button type="button" class="btn plain" onclick="loadAIActivity()"${v.loading ? ' disabled' : ''}>刷新</button></div>
    <p role="status" aria-live="polite">${v.loading ? '正在读取…' : `共 ${v.total} 次调用，已显示 ${v.items.length} 次`}</p>
    ${v.error ? `<p class="ai-activity-error" role="alert">${esc(v.error)}</p>` : ''}
    ${v.summary.length ? `<div class="ai-activity-summary">${v.summary.map(row => `<div><strong>${esc(row.label)}</strong><span>${Number(row.total) || 0} 次 · 回退 ${Number(row.fallbacks) || 0} 次（${Math.round((Number(row.fallback_rate) || 0) * 100)}%） · 失败 ${Number(row.failed) || 0} 次</span></div>`).join('')}</div>` : ''}
    <p class="hint">回退率统计触发备用路线的调用；回退也可能失败。排队时间为轮询观察值；执行时间属于最终调用引擎。未上报的数据标为「未观测」。</p>
    <div class="ai-activity-list">${v.items.map(item => `<article class="ai-activity-row"><div class="ai-activity-heading"><strong>${esc(item.label)}</strong><span class="${item.status === 'failed' ? 'ai-activity-error' : ''}">${item.status === 'done' ? '完成' : '失败'}</span></div>
      <p>${esc(new Date(Number(item.ts) * 1000).toLocaleString())} · ${esc(engineName[item.engine] || '未调用成功')} · ${esc(item.model || '模型未观测')}</p>
      ${item.fallback ? `<p class="ai-activity-reason">${esc(item.fallback)}</p>` : ''}
      <div class="ai-activity-metrics"><span>排队 ${esc(aiActivitySeconds(item.queue_seconds))}</span><span>执行 ${esc(aiActivitySeconds(item.run_seconds))}</span><span>全程 ${esc(aiActivitySeconds(item.total_seconds))}</span><span>重试 ${item.retries == null ? '未观测' : `${Math.max(0, Number(item.retries) || 0)} 次`}</span></div></article>`).join('') || (!v.loading && !v.error ? '<p>当前范围还没有运行记录。记录从本次更新后开始收集。</p>' : '')}</div>
    ${v.more ? `<button type="button" class="btn plain wide" onclick="loadAIActivity(true)"${v.loading ? ' disabled' : ''}>加载更多</button>` : ''}`;
}
