// Incremental discoveries live beside the library, with the same owner/privacy lifetime.
let libraryUpdates = {owner:'', epoch:0, items:[], total:0, at:0, controller:null, busy:false};
function resetLibraryUpdates() {
  libraryUpdates.controller?.abort();
  libraryUpdates = {owner:storageOwner, epoch:libraryUpdates.epoch + 1, items:[], total:0, at:0, controller:null, busy:false};
  const box = $('#libraryUpdates'); if (box) { box.innerHTML = ''; box.hidden = true; }
}
function libraryUpdateFor(id) {
  return libraryUpdates.owner === storageOwner && libraryUpdates.items.some(item => item.job_id === id)
    && !state.jobs.find(job => job.id === id)?.watch?.done;
}
function renderLibraryUpdates() {
  const box = $('#libraryUpdates'), s = libraryUpdates;
  if (!box) return;
  const items = s.items.filter(item => !item.job_id || !state.jobs.find(job => job.id === item.job_id)?.watch?.done);
  box.hidden = chip !== 'all' || !!query || s.owner !== storageOwner || !items.length;
  if (box.hidden) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="library-updates-heading"><b>片库有更新 · ${s.total} 条</b><button class="link" onclick="refreshLibraryUpdates(true)" ${s.busy ? 'disabled' : ''}>刷新</button></div>
    <div class="library-updates-list">${items.map(item => `<article><div><b>${esc(item.title || '新内容')}</b><small>${(item.sources || []).map(source => esc(source.title || '订阅来源')).join(' · ')}</small></div>
      <button class="link" onclick="openLibraryUpdate(${Number(item.id)})" ${s.busy ? 'disabled' : ''}>${item.job_id ? '查看视频' : '加入下载'}</button>
      <button class="link" onclick="dismissLibraryUpdate(${Number(item.id)})" ${s.busy ? 'disabled' : ''}>不再提醒</button></article>`).join('')}</div>${s.total > items.length ? '<p class="hint">处理后继续显示其余更新。</p>' : ''}`;
}
async function refreshLibraryUpdates(force = false) {
  const s = libraryUpdates;
  if (!storageOwner || storageOwner === 'unidentified' || state.login_required || document.hidden || chip !== 'all' || query || s.busy || s.controller || !force && Date.now() - s.at < 60000) return;
  if (s.owner && s.owner !== storageOwner) { resetLibraryUpdates(); return refreshLibraryUpdates(force); }
  s.owner = storageOwner; const epoch = privateViewEpoch, controller = s.controller = new AbortController();
  try {
    const response = await fetch('/api/inbox?state=unread&limit=10', {signal:controller.signal});
    const data = await response.json();
    if (libraryUpdates !== s || s.owner !== storageOwner || epoch !== privateViewEpoch) return;
    if (!response.ok || data.storage_owner && data.storage_owner !== s.owner) { resetLibraryUpdates(); return; }
    s.items = data.items || []; s.total = Number(data.total) || 0; s.at = Date.now();
    render();
  } catch (e) { if (e.name !== 'AbortError' && libraryUpdates === s) s.at = Date.now() - 45000; }
  finally { if (libraryUpdates === s) s.controller = null; }
}
async function dismissLibraryUpdate(id) {
  const s = libraryUpdates; if (s.busy || !s.items.some(item => item.id === id)) return;
  s.busy = true; renderLibraryUpdates();
  try {
    const response = await fetch('/api/inbox/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'read', ids:[id]})});
    if (libraryUpdates !== s) return;
    if (!response.ok) { toast('暂时无法更新，请稍后重试'); return; }
    s.items = s.items.filter(item => item.id !== id); s.total = Math.max(0, s.total - 1); s.at = 0;
  } catch (_) { if (libraryUpdates === s) toast('暂时无法更新，请稍后重试'); }
  finally { if (libraryUpdates === s) { s.busy = false; render(); refreshLibraryUpdates(true); } }
}
async function openLibraryUpdate(id) {
  const s = libraryUpdates, item = s.items.find(item => item.id === id); if (!item || s.busy) return;
  const epoch = privateViewEpoch;
  s.busy = true; renderLibraryUpdates();
  try {
    if (item.job_id) {
      const response = await fetch(`/api/jobs?mode=exact&only=1&ids=${item.job_id}`), data = await response.json();
      if (libraryUpdates !== s || epoch !== privateViewEpoch || data.storage_owner && data.storage_owner !== storageOwner) return;
      const job = data.jobs?.find(job => job.id === item.job_id);
      if (!response.ok || !job) { resetLibraryUpdates(); toast('内容或权限已变化，请刷新'); return; }
      const index = state.jobs.findIndex(job => job.id === item.job_id);
      if (index < 0) state.jobs.push(job); else state.jobs[index] = job;
      await openWatch(item.job_id);
    } else if (item.can_download) {
      const response = await fetch('/api/inbox/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:[id]})});
      if (libraryUpdates !== s || epoch !== privateViewEpoch) return;
      if (!response.ok) throw Error('加入下载失败');
      toast('已加入下载'); s.at = 0; await refresh();
    }
  } catch (_) { if (libraryUpdates === s) toast('操作失败，请稍后重试'); }
  finally { if (libraryUpdates === s) { s.busy = false; renderLibraryUpdates(); } }
}
