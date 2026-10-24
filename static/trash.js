function newTrashView() { return {items: [], total: 0, hasMore: false, canScan: false, loading: false, busy: false, error: '', confirm: [], progress: ''}; }
let trashView = newTrashView(), trashEpoch = 0, trashLoadEpoch = 0;
const trashRequests = new Set();
function trashToken() { return {epoch: trashEpoch, owner: storageOwner}; }
function currentTrash(token) { return token.epoch === trashEpoch && token.owner === storageOwner; }
async function trashApi(path = '', body) {
  const token = trashToken(), controller = new AbortController(); trashRequests.add(controller);
  try {
    const response = await fetch('/api/trash' + path, {signal: controller.signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
    const data = await response.json();
    if (!currentTrash(token)) throw new DOMException('账号或窗口已切换', 'AbortError');
    if (!response.ok || data.error) throw Error(data.error || '操作失败，请稍后重试');
    return data;
  } finally { trashRequests.delete(controller); }
}
function suspendTrash() {
  ++trashEpoch; ++trashLoadEpoch; trashRequests.forEach(controller => controller.abort()); trashRequests.clear();
  trashView.loading = trashView.busy = false; trashView.confirm = []; trashView.progress = '';
}
function resetTrash() { suspendTrash(); trashView = newTrashView(); $('#trashSheet').hidden = true; renderTrash(); }
async function openTrash() {
  hide($('#accountSheet')); trashView.confirm = []; $('#trashSheet').hidden = false; renderTrash(); await loadTrash();
}
async function loadTrash() {
  if (trashView.busy) return;
  const token = trashToken(), request = ++trashLoadEpoch; trashView.loading = true; trashView.error = ''; renderTrash();
  try {
    const data = await trashApi();
    if (!currentTrash(token) || request !== trashLoadEpoch) return;
    trashView.items = data.items || []; trashView.total = Number(data.total) || trashView.items.length;
    trashView.hasMore = data.has_more === true; trashView.canScan = data.can_scan === true;
  } catch (e) { if (currentTrash(token) && request === trashLoadEpoch && e.name !== 'AbortError') trashView.error = e.message; }
  finally { if (currentTrash(token) && request === trashLoadEpoch) { trashView.loading = false; renderTrash(); } }
}
function trashDate(value) {
  if (!value) return '';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString('zh-CN') : '';
}
function askDeleteTrash(id) {
  if (trashView.busy || trashView.loading) return;
  trashView.confirm = (id === 'all' ? trashView.items : trashView.items.filter(item => String(item.id) === String(id))).map(item => item.id);
  renderTrash(); $('#trashConfirm').scrollIntoView({block: 'nearest'}); $('#trashCancel').focus();
}
function cancelDeleteTrash() { if (!trashView.busy) { trashView.confirm = []; renderTrash(); } }
async function restoreTrash(id) {
  if (trashView.busy || trashView.loading) return;
  const token = trashToken(); trashView.busy = true; trashView.error = ''; renderTrash();
  try {
    await trashApi('/' + encodeURIComponent(id) + '/restore', {}); if (!currentTrash(token)) return;
    trashView.items = trashView.items.filter(item => String(item.id) !== String(id)); trashView.total = Math.max(0, trashView.total - 1); trashView.confirm = [];
    toast('已恢复到片库'); if (typeof refresh === 'function') Promise.resolve(refresh()).catch(() => {});
  } catch (e) { if (currentTrash(token) && e.name !== 'AbortError') trashView.error = e.message; }
  finally { if (currentTrash(token)) { trashView.busy = false; renderTrash(); } }
}
async function deleteTrash() {
  if (trashView.busy || !trashView.confirm.length) return;
  const token = trashToken(), ids = [...trashView.confirm]; let deleted = 0, shared = 0;
  trashView.busy = true; trashView.error = ''; renderTrash();
  try {
    for (const id of ids) {
      if (!currentTrash(token)) return;
      trashView.progress = `正在永久删除 ${deleted + 1} / ${ids.length}`; renderTrash();
      const data = await trashApi('/' + encodeURIComponent(id) + '/delete', {}); if (!currentTrash(token)) return;
      deleted++; if (data.kept_shared) shared++;
      trashView.items = trashView.items.filter(item => String(item.id) !== String(id)); trashView.total = Math.max(0, trashView.total - 1);
      trashView.confirm = trashView.confirm.filter(value => String(value) !== String(id));
    }
    toast(`已永久删除 ${deleted} 个` + (shared ? `，${shared} 个仍在共享使用的文件已保留` : ''));
  } catch (e) {
    if (currentTrash(token) && e.name !== 'AbortError') trashView.error = (deleted ? `已删除 ${deleted} 个；` : '') + e.message;
  } finally { if (currentTrash(token)) { trashView.busy = false; trashView.progress = ''; renderTrash(); } }
}
async function scanTrash() {
  if (trashView.busy || trashView.loading || !trashView.canScan) return;
  const token = trashToken(); trashView.busy = true; trashView.error = ''; trashView.progress = '正在查找遗留文件…'; renderTrash();
  try {
    const data = await trashApi('/scan', {}); if (!currentTrash(token)) return;
    toast(`找回 ${Number(data.found) || 0} 个遗留文件`); trashView.busy = false; await loadTrash();
  } catch (e) { if (currentTrash(token) && e.name !== 'AbortError') trashView.error = e.message; }
  finally { if (currentTrash(token)) { trashView.busy = false; trashView.progress = ''; renderTrash(); } }
}
function renderTrash() {
  const v = trashView, busy = v.busy || v.loading;
  $('#trashError').textContent = v.error; $('#trashError').hidden = !v.error;
  $('#trashCount').textContent = v.progress || (v.loading ? '正在加载…' : v.hasMore ? `显示 ${v.items.length} / ${v.total} 个，处理后刷新查看余下内容` : `共 ${v.items.length} 个内容`);
  $('#trashRefresh').disabled = busy; $('#trashScan').disabled = busy; $('#trashScan').hidden = !v.canScan; $('#trashScanHint').hidden = !v.canScan;
  $('#trashEmpty').disabled = busy || !v.items.length; $('#trashCancel').disabled = v.busy; $('#trashDeleteGo').disabled = v.busy;
  $('#trashConfirm').hidden = !v.confirm.length;
  const single = v.confirm.length === 1 ? v.items.find(item => item.id === v.confirm[0]) : null;
  $('#trashConfirmText').textContent = single ? `永久删除「${single.title || '未命名内容'}」及可删除的文件？` : `永久删除这 ${v.confirm.length} 个内容及可删除的文件？`;
  $('#trashList').innerHTML = v.items.map(item => `<article class="trash-row"><div class="trash-info"><h3>${esc(item.title || item.url || '未命名内容')}</h3>
    <p>${item.orphan && !item.expires_at ? '历史遗留 · 待确认' : '移入时间：' + esc(trashDate(item.deleted_at)) + ' · 自动清理：' + esc(trashDate(item.expires_at))}</p>
    <p>${Math.max(0, Number(item.files_count) || 0)} 个文件${item.orphan ? ' · 从硬盘找回' : ''}</p></div>
    <div class="trash-row-actions"><button type="button" class="btn plain" data-restore="${esc(item.id)}"${busy ? ' disabled' : ''}>恢复</button><button type="button" class="link" data-delete="${esc(item.id)}"${busy ? ' disabled' : ''}>永久删除</button></div></article>`).join('') || `<p class="trash-empty">${v.loading ? '正在加载回收站…' : v.hasMore && v.total ? '当前列表已处理，点击刷新查看余下内容。' : '回收站是空的。移除的视频会在这里保留 30 天。'}</p>`;
}
$('#trashList').addEventListener('click', event => {
  const restore = event.target.closest('[data-restore]'), remove = event.target.closest('[data-delete]');
  if (restore) restoreTrash(restore.dataset.restore); if (remove) askDeleteTrash(remove.dataset.delete);
});
