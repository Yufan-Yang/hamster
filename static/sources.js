// A single view of owned channels/catalogues and their newly discovered items.
// The inbox never downloads anything until the user explicitly asks.
function newSourcesView() {
  return {tab: 'inbox', sources: [], sourceTotal: 0, unread: 0, source: '', state: 'unread',
    items: [], total: 0, offset: 0, limit: 50, more: false, selected: new Set(),
    loadingSources: false, loadingInbox: false, busy: false, sourceError: '', inboxError: ''};
}
let sourcesView = newSourcesView(), sourcesEpoch = 0, sourceListEpoch = 0, inboxListEpoch = 0, sourcesPoll = null;
const sourcesRequests = new Set();
let sourceListController = null, inboxListController = null;
const SOURCE_STATUS = {queued: '等待采集', running: '正在采集', stopped: '已停止', done: '已完成', limited: '等待继续',
  failed: '采集失败', updating: '正在检查', pending: '正在检查', active: '追更中', idle: '等待检查', error: '检查失败'};
const INBOX_STATES = {unread: '未读', read: '已读', ignored: '已忽略', all: '全部'};
function sourcesToken() { return {epoch: sourcesEpoch, owner: storageOwner}; }
function currentSources(token) { return token.epoch === sourcesEpoch && token.owner === storageOwner; }
function sourcesVisible() { return !$('#sourcesSheet').hidden && !$('#sourcesSheet').classList.contains('closing'); }
function sourcesNumber(value) { return Math.max(0, Number(value) || 0); }
function sourceHref(value) {
  try { const url = new URL(String(value || '')); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function sourceLink(url, title) {
  const safe = sourceHref(url);
  return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(title || safe)}</a>` : esc(title || '地址不可用');
}
function sourceTime(value) {
  const date = new Date(Number(value) * 1000);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '';
}
function resetSources() {
  ++sourcesEpoch; ++sourceListEpoch; ++inboxListEpoch; clearTimeout(sourcesPoll); sourcesPoll = null;
  sourcesRequests.forEach(controller => controller.abort()); sourcesRequests.clear();
  sourceListController = inboxListController = null; sourcesView = newSourcesView();
  $('#sourcesSheet').hidden = true; $('#sourceCards').innerHTML = $('#inboxItems').innerHTML = '';
  $('#inboxSource').innerHTML = '<option value="">全部来源</option>'; $('#inboxSource').value = ''; $('#inboxState').value = 'unread';
  renderSources();
}
function closeSources() { resetSources(); }
async function sourcesApi(path, {body, controller = new AbortController()} = {}) {
  const token = sourcesToken(); sourcesRequests.add(controller);
  try {
    const response = await fetch(path, {signal: controller.signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
    const data = await response.json();
    if (!currentSources(token) || data.storage_owner && data.storage_owner !== token.owner)
      throw new DOMException('账号或隐私状态已变化', 'AbortError');
    if (!response.ok) {
      if (response.status === 403) { resetSources(); toast(data.error || '验证已失效，请重新打开来源中心。'); }
      throw Error(data.error || '来源中心暂时不可用');
    }
    return data;
  } finally { sourcesRequests.delete(controller); }
}
function scheduleSources() {
  clearTimeout(sourcesPoll); sourcesPoll = null;
  if (sourcesVisible() && !document.hidden) sourcesPoll = setTimeout(() => refreshSources(true), 15000);
}
async function openSources(tab = 'inbox') {
  resetSources(); sourcesView.tab = tab === 'sources' ? 'sources' : 'inbox'; $('#sourcesSheet').hidden = false;
  renderSources(); await refreshSources();
}
async function refreshSources(poll = false) {
  if (!sourcesVisible() || document.hidden || sourcesView.busy || poll && sourcesView.selected.size) { scheduleSources(); return; }
  const token = sourcesToken();
  await Promise.allSettled([loadSourceList(), sourcesView.tab === 'inbox' ? loadInbox({poll}) : Promise.resolve()]);
  if (currentSources(token)) scheduleSources();
}
async function loadSourceList() {
  if (!sourcesVisible() || sourcesView.loadingSources) return;
  const token = sourcesToken(), request = ++sourceListEpoch;
  sourceListController?.abort(); const controller = new AbortController(); sourceListController = controller;
  sourcesView.loadingSources = true; sourcesView.sourceError = ''; renderSources();
  try {
    const data = await sourcesApi('/api/sources', {controller});
    if (!currentSources(token) || request !== sourceListEpoch) return;
    sourcesView.sources = data.items || []; sourcesView.sourceTotal = sourcesNumber(data.total); sourcesView.unread = sourcesNumber(data.unread);
  } catch (e) {
    if (currentSources(token) && request === sourceListEpoch && e.name !== 'AbortError') {
      sourcesView.sourceError = e.message; sourcesView.sources = []; sourcesView.sourceTotal = 0; sourcesView.unread = 0;
    }
  } finally {
    if (currentSources(token) && request === sourceListEpoch) { sourcesView.loadingSources = false; sourceListController = null; renderSources(); }
  }
}
async function loadInbox({offset = sourcesView.offset, poll = false} = {}) {
  if (!sourcesVisible() || sourcesView.tab !== 'inbox') return;
  const token = sourcesToken(), request = ++inboxListEpoch;
  inboxListController?.abort(); const controller = new AbortController(); inboxListController = controller;
  sourcesView.loadingInbox = true; sourcesView.inboxError = ''; if (!poll) sourcesView.selected.clear(); renderSources();
  const params = new URLSearchParams({state: sourcesView.state, offset: String(offset), limit: String(sourcesView.limit)});
  if (sourcesView.source) params.set('source', sourcesView.source);
  try {
    const data = await sourcesApi('/api/inbox?' + params, {controller});
    if (!currentSources(token) || request !== inboxListEpoch) return;
    sourcesView.items = data.items || []; sourcesView.total = sourcesNumber(data.total); sourcesView.unread = sourcesNumber(data.unread);
    sourcesView.more = !!data.more; sourcesView.offset = offset;
    const visible = new Set(sourcesView.items.map(item => String(item.id)));
    sourcesView.selected = new Set([...sourcesView.selected].filter(id => visible.has(id)));
    if (offset && !sourcesView.items.length && sourcesView.total <= offset) {
      await loadInbox({offset: Math.max(0, Math.floor((Math.max(1, sourcesView.total) - 1) / sourcesView.limit) * sourcesView.limit), poll}); return;
    }
  } catch (e) {
    if (currentSources(token) && request === inboxListEpoch && e.name !== 'AbortError') {
      sourcesView.inboxError = e.message; sourcesView.items = []; sourcesView.total = 0; sourcesView.more = false; sourcesView.selected.clear();
    }
  } finally {
    if (currentSources(token) && request === inboxListEpoch) { sourcesView.loadingInbox = false; inboxListController = null; renderSources(); }
  }
}
async function switchSourcesTab(tab) {
  if (sourcesView.busy || !['sources', 'inbox'].includes(tab)) return;
  sourcesView.tab = tab; sourcesView.selected.clear(); ++inboxListEpoch; inboxListController?.abort(); sourcesView.loadingInbox = false;
  renderSources();
  if (tab === 'inbox') await loadInbox({offset: 0}); else if (!sourcesView.sources.length) await loadSourceList();
  scheduleSources();
}
async function filterInbox() {
  if (sourcesView.busy) return;
  sourcesView.state = INBOX_STATES[$('#inboxState').value] ? $('#inboxState').value : 'unread';
  sourcesView.source = $('#inboxSource').value; sourcesView.items = []; sourcesView.selected.clear();
  await loadInbox({offset: 0});
}
function pageInbox(direction) {
  if (sourcesView.loadingInbox || sourcesView.busy) return;
  if (direction > 0 && !sourcesView.more) return;
  return loadInbox({offset: Math.max(0, sourcesView.offset + direction * sourcesView.limit)});
}
function pickInbox(id, checked) {
  if (sourcesView.loadingInbox || sourcesView.busy || !sourcesView.items.some(item => String(item.id) === String(id))) return;
  if (checked) sourcesView.selected.add(String(id)); else sourcesView.selected.delete(String(id)); renderSourcesSelection();
}
function pickInboxPage(checked) {
  if (sourcesView.loadingInbox || sourcesView.busy) return;
  sourcesView.selected = checked ? new Set(sourcesView.items.map(item => String(item.id))) : new Set(); renderSourcesSelection();
}
async function inboxAction(action) {
  if (!['read', 'unread', 'ignore', 'download'].includes(action) || sourcesView.busy || sourcesView.loadingInbox || !sourcesView.selected.size) return;
  const ids = sourcesView.items.filter(item => sourcesView.selected.has(String(item.id)) && (action !== 'download' || item.can_download)).map(item => Number(item.id));
  if (!ids.length) return;
  const token = sourcesToken();
  sourcesView.busy = true; sourcesView.inboxError = ''; ++inboxListEpoch; inboxListController?.abort();
  ++sourceListEpoch; sourceListController?.abort(); sourceListController = null; sourcesView.loadingSources = false; renderSources();
  try {
    const data = await sourcesApi(action === 'download' ? '/api/inbox/import' : '/api/inbox/action', {body: action === 'download' ? {ids} : {action, ids}});
    if (!currentSources(token)) return;
    sourcesView.selected.clear();
    const partial = Array.isArray(data.errors) && data.errors.length ? `；${data.errors.length} 项需刷新后重试` : '';
    toast(action === 'download' ? `已加入下载 ${sourcesNumber(data.added)} 个，已有 ${sourcesNumber(data.duplicates)} 个${partial}`
      : action === 'ignore' ? '已忽略选中内容，视频文件保留' : action === 'read' ? '已标记为已读' : '已标记为未读');
    sourcesView.busy = false;
    await Promise.allSettled([loadInbox(), loadSourceList()]);
  } catch (e) {
    if (currentSources(token) && e.name !== 'AbortError') {
      sourcesView.inboxError = e.message; sourcesView.selected.clear(); sourcesView.items = []; sourcesView.total = 0; sourcesView.more = false;
    }
  } finally {
    if (currentSources(token)) { sourcesView.busy = false; renderSources(); scheduleSources(); }
  }
}
async function manageSource(key) {
  const source = sourcesView.sources.find(item => item.key === key);
  if (!source || sourcesView.busy) return;
  const owner = storageOwner, privacy = privateViewEpoch;
  closeSources();
  if (source.type === 'crawl') {
    await openCrawl(); if (owner === storageOwner && privacy === privateViewEpoch) await selectCrawl(source.id);
  } else {
    setChip('subs'); document.querySelector('[data-source-sub="' + Number(source.id) + '"]')?.scrollIntoView({block: 'center'});
  }
}
async function inboxForSource(key) {
  if (!sourcesView.sources.some(item => item.key === key) || sourcesView.busy) return;
  sourcesView.source = key; sourcesView.state = 'unread'; sourcesView.tab = 'inbox'; sourcesView.items = [];
  sourcesView.selected.clear(); renderSources(); await loadInbox({offset: 0}); scheduleSources();
}
function sourceAdd(kind) { closeSources(); if (kind === 'crawl') openCrawl(); else openAdd(); }
function inboxWatch(id) {
  const item = sourcesView.items.find(item => String(item.id) === String(id));
  if (!item?.job_id || sourcesView.busy) return;
  closeSources(); openWatch(Number(item.job_id));
}
function renderSourcesSelection() {
  const v = sourcesView, count = v.selected.size, busy = v.busy || v.loadingInbox;
  const downloadable = v.items.filter(item => v.selected.has(String(item.id)) && item.can_download).length;
  $('#inboxSelected').textContent = count ? `已选 ${count} 条（本页） · ${downloadable} 条可加入下载` : '选择本页内容后批量处理';
  $('#inboxPickPage').checked = !!v.items.length && count === v.items.length;
  $('#inboxPickPage').indeterminate = count > 0 && count < v.items.length;
  $('#inboxPickPage').disabled = busy || !v.items.length;
  for (const id of ['inboxRead', 'inboxUnread', 'inboxIgnore', 'inboxDownload']) $('#' + id).disabled = busy || !count;
  $('#inboxDownload').disabled = busy || !downloadable;
  $('#inboxRead').hidden = v.state === 'read'; $('#inboxUnread').hidden = v.state === 'unread'; $('#inboxIgnore').hidden = v.state === 'ignored';
  $('#inboxDownload').textContent = v.busy ? '处理中…' : downloadable ? `加入下载（${downloadable}）` : '加入下载';
  $('#inboxItems').querySelectorAll('[data-inbox-pick]').forEach(input => { input.checked = v.selected.has(input.dataset.inboxPick); input.disabled = busy; });
}
function renderSources() {
  const v = sourcesView;
  $('#sourcesOverview').hidden = v.tab !== 'sources'; $('#sourcesInbox').hidden = v.tab !== 'inbox';
  $('#sourcesTabInbox').setAttribute('aria-pressed', String(v.tab === 'inbox')); $('#sourcesTabAll').setAttribute('aria-pressed', String(v.tab === 'sources'));
  $('#sourcesTabInbox').textContent = `新内容 · 未读 ${v.unread}`;
  $('#sourcesTotal').textContent = `当前可见 ${v.sourceTotal} 个来源`;
  $('#sourcesError').textContent = v.sourceError; $('#sourcesError').hidden = !v.sourceError;
  $('#inboxError').textContent = v.inboxError; $('#inboxError').hidden = !v.inboxError;
  $('#sourcesRefresh').disabled = v.busy || v.loadingInbox || v.loadingSources;
  const sourceOptions = '<option value="">全部来源</option>' + v.sources.map(item => `<option value="${esc(item.key)}">${esc(item.title || item.url)} · ${item.type === 'crawl' ? '目录' : '频道'}</option>`).join('');
  if ($('#inboxSource').innerHTML !== sourceOptions) $('#inboxSource').innerHTML = sourceOptions;
  $('#inboxSource').value = v.source; $('#inboxState').value = v.state;
  $('#inboxSource').disabled = $('#inboxState').disabled = v.busy;
  $('#sourceCards').innerHTML = v.sources.map(item => `<article class="source-card"><h3>${esc(item.title || item.url || '未命名来源')}</h3><p class="source-meta">${item.type === 'crawl' ? '目录采集' : '频道追更'} · ${esc(SOURCE_STATUS[item.status] || item.status || '等待检查')} · 未读 ${sourcesNumber(item.unread)} · 已发现 ${sourcesNumber(item.found)}</p><p class="source-url">${sourceLink(item.url)}</p>${item.last_checked || item.next_refresh ? `<p class="source-meta">${item.last_checked ? '上次检查：' + esc(sourceTime(item.last_checked)) : ''}${item.next_refresh ? ' · 下次：' + esc(sourceTime(item.next_refresh)) : ''}</p>` : ''}<div class="source-card-actions"><button type="button" class="link" data-source-action="inbox" data-source-key="${esc(item.key)}">查看新内容</button><button type="button" class="link" data-source-action="manage" data-source-key="${esc(item.key)}">${item.type === 'crawl' ? '管理采集任务' : '管理频道追更'}</button></div></article>`).join('') || `<p class="hint">${v.loadingSources ? '正在读取来源…' : '还没有可见来源，可添加频道追更或目录采集。'}</p>`;
  $('#inboxCount').textContent = v.loadingInbox ? '正在读取…' : `${INBOX_STATES[v.state]} ${v.total} 条`;
  $('#inboxItems').innerHTML = v.items.map(item => `<article class="inbox-item"><label class="inbox-pick"><input type="checkbox" data-inbox-pick="${Number(item.id)}" aria-label="选择 ${esc(item.title || '新内容')}"></label><div class="inbox-item-body"><h3>${sourceLink(item.url, item.title || item.url || '未命名内容')}</h3><p class="source-meta">${esc(INBOX_STATES[item.state] || '未读')}${item.created ? ' · ' + esc(sourceTime(item.created)) : ''}${item.job_id ? ' · 已加入下载' + (item.job_status ? '（' + esc({done:'已完成',queued:'排队中',downloading:'下载中',failed:'失败',cancelled:'已取消'}[item.job_status] || item.job_status) + '）' : '') : ''}</p><p class="source-meta">来源：${(item.sources || []).map(source => esc(source.title || source.url || (source.type === 'crawl' ? '目录采集' : '频道追更'))).join('、') || '来源已移除'}</p>${item.job_id ? `<button type="button" class="link" data-inbox-watch="${Number(item.id)}">查看下载</button>` : ''}</div></article>`).join('') || `<p class="source-empty">${v.loadingInbox ? '正在读取新内容…' : v.inboxError ? '读取失败，可点击刷新重试。' : v.state === 'unread' ? '目前没有未读内容。来源更新后，新发现的内容会出现在这里。' : '当前筛选下没有内容。'}</p>`;
  $('#inboxPages').hidden = !v.total;
  $('#inboxPageLabel').textContent = v.total ? `${Math.floor(v.offset / v.limit) + 1} / ${Math.ceil(v.total / v.limit)} 页` : '';
  $('#inboxPrev').disabled = v.busy || v.loadingInbox || v.offset <= 0; $('#inboxNext').disabled = v.busy || v.loadingInbox || !v.more;
  renderSourcesSelection();
}
$('#sourceCards').addEventListener('click', event => {
  const button = event.target.closest('[data-source-action]'); if (!button) return;
  if (button.dataset.sourceAction === 'manage') manageSource(button.dataset.sourceKey); else inboxForSource(button.dataset.sourceKey);
});
$('#inboxItems').addEventListener('change', event => { if (event.target.matches('[data-inbox-pick]')) pickInbox(event.target.dataset.inboxPick, event.target.checked); });
$('#inboxItems').addEventListener('click', event => { const button = event.target.closest('[data-inbox-watch]'); if (button) inboxWatch(button.dataset.inboxWatch); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(sourcesPoll); sourcesPoll = null; } else if (sourcesVisible()) refreshSources(true);
});
