let playerSeries = null, playerSeriesEpoch = 0;
function playerSeriesCurrent(s) {
  return playerSeries === s && s.epoch === playerSeriesEpoch && s.owner === storageOwner
    && s.privacy === (typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch) && !!watching;
}
function stopPlayerSeries() {
  ++playerSeriesEpoch;
  if (playerSeries) {
    playerSeries.controller?.abort(); playerSeries.scopeController?.abort(); clearTimeout(playerSeries.timer); clearInterval(playerSeries.poll);
    playerSeries.coverObserver?.disconnect();
    playerSeries.coverRequests?.forEach(controller => controller.abort());
    playerSeries.covers?.forEach(url => { if (url) URL.revokeObjectURL(url); });
  }
  playerSeries = null;
  const box = $('#playerSeries'); if (box) { box.innerHTML = ''; box.hidden = true; }
  $('#watchBody')?.classList.remove('with-player-series');
  $('#watchLayout')?.classList.remove('with-series');
}
function setupPlayerSeries(job) {
  const name = seriesOf(job), key = job.series_key || name.toLocaleLowerCase('zh-CN').replace(/[\s·:：\-_]/g, '');
  if (!key) { stopPlayerSeries(); return; }
  const previous = playerSeries;
  if (!previous || !playerSeriesCurrent(previous) || previous.key !== key || previous.sort !== videoSort) {
    stopPlayerSeries();
    playerSeries = {epoch:playerSeriesEpoch, owner:storageOwner, privacy:typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch,
      id:job.id, key, name, sort:videoSort, q:'', items:[], total:0, next:0, more:false, loading:true, error:'', queryEpoch:0, focusActive:true,
      covers:new Map(), coverRequests:new Map(), coverQueue:new Set()};
    const fresh = playerSeries; fresh.poll = setInterval(() => validatePlayerSeries(fresh), 30000);
  } else playerSeries.id = job.id;
  const s = playerSeries, box = $('#playerSeries'); if (!box) return;
  box.innerHTML = `<div class="player-series-head"><b id="playerSeriesName"></b><span id="playerSeriesCount" class="hint"></span></div>
    <input id="playerSeriesQuery" type="search" maxlength="128" aria-label="搜索合集分集" placeholder="搜索分集" value="${esc(s.q)}" oninput="filterPlayerSeries(this.value)">
    <div id="playerSeriesError" class="err" role="status"></div><div id="playerSeriesList" class="player-series-list" role="list" aria-label="合集视频列表" onscroll="if(playerSeries)playerSeries.scroll=this.scrollTop"></div>
    <button id="playerSeriesMore" class="link" onclick="loadPlayerSeries(playerSeries.next)">加载更多</button>`;
  renderPlayerSeries();
  if (s !== previous) loadPlayerSeries();
}
function renderPlayerSeries() {
  const s = playerSeries, box = $('#playerSeries'); if (!s || !box || !playerSeriesCurrent(s)) return;
  const show = s.total > 1 || !!s.q || s.loading || !!s.error;
  box.hidden = !show; $('#watchBody')?.classList.toggle('with-player-series', show); $('#watchLayout')?.classList.toggle('with-series', show);
  $('#playerSeriesName').textContent = s.name;
  $('#playerSeriesCount').textContent = s.loading && !s.items.length ? '加载中…' : `${s.total} 个视频`;
  $('#playerSeriesError').textContent = s.error;
  const list = $('#playerSeriesList'), scroll = s.scroll ?? list.scrollTop;
  list.innerHTML = s.items.map(item => {
    const active = watching.id === item.id, pending = s.pending === item.id;
    const episode = item.analysis?.episode, season = item.analysis?.season;
    const label = item.analysis?.library === 'TV' && typeof episode === 'number'
      ? `${season === 0 ? '特别篇 ' : season > 1 ? '第 ' + season + ' 季 · ' : ''}第 ${episode} 集 · ` : '';
    const status = pending ? '加载中…' : active ? '正在播放' : !item.playable ? '尚不可播放' : item.watched ? '已看完' : '播放';
    return `<div role="listitem"><button class="player-series-item${active ? ' active' : ''}" data-series-job="${item.id}" aria-current="${active ? 'true' : 'false'}"
      ${!item.playable || pending ? 'disabled' : ''} onclick="playSeriesVideo(${item.id})"><span class="player-series-cover" data-series-cover="${item.id}"${item.thumb ? ' data-has-cover="1"' : ''}>${s.covers.get(item.id) ? `<img src="${esc(s.covers.get(item.id))}" alt="">` : '<span>暂无封面</span>'}</span><span class="player-series-text"><span>${esc(item.title)}</span><small>${esc(label + status)}</small></span></button></div>`;
  }).join('') || (!s.loading ? '<p class="hint">没有匹配的分集</p>' : '');
  list.scrollTop = scroll;
  if (s.focusActive && s.items.length) {
    const active = list.querySelector('[aria-current="true"]');
    if (active) list.scrollTop = Math.max(0, active.offsetTop - list.clientHeight / 2);
    s.focusActive = false;
  }
  s.scroll = list.scrollTop;
  $('#playerSeriesMore').hidden = !s.more; $('#playerSeriesMore').disabled = s.loading;
  observePlayerSeriesCovers(s, list);
}
function observePlayerSeriesCovers(s, list) {
  s.coverObserver?.disconnect();
  if (typeof IntersectionObserver === 'undefined' || !list.querySelectorAll) return;
  s.coverObserver = new IntersectionObserver(entries => {
    if (!playerSeriesCurrent(s)) return;
    for (const entry of entries) if (entry.isIntersecting) {
      s.coverObserver.unobserve(entry.target);
      const id = Number(entry.target.dataset.seriesCover);
      if (!s.covers.has(id) && !s.coverRequests.has(id)) s.coverQueue.add(id);
    }
    pumpPlayerSeriesCovers(s);
  }, {root:list, rootMargin:'80px'});
  list.querySelectorAll('[data-has-cover]').forEach(node => s.coverObserver.observe(node));
}
function pumpPlayerSeriesCovers(s) {
  if (!playerSeriesCurrent(s)) return;
  while (s.coverRequests.size < 2 && s.coverQueue.size) {
    const id = s.coverQueue.values().next().value; s.coverQueue.delete(id);
    if (s.covers.has(id) || !s.items.some(item => item.id === id)) continue;
    const controller = new AbortController(); s.coverRequests.set(id, controller);
    fetch(`/api/series/${id}/cover`, {signal:controller.signal}).then(async response => {
      if (!playerSeriesCurrent(s)) return;
      if (response.status === 403) { stopPlayerSeries(); return; }
      if (!response.ok || !(response.headers.get('Content-Type') || '').startsWith('image/')) { s.covers.set(id, ''); return; }
      const blob = await response.blob(); if (!playerSeriesCurrent(s)) return;
      const url = URL.createObjectURL(blob); s.covers.set(id, url);
      const node = $('#playerSeriesList')?.querySelector(`[data-series-cover="${id}"]`);
      if (node) node.innerHTML = `<img src="${esc(url)}" alt="">`;
      if (s.covers.size > 200) {
        const oldest = s.covers.keys().next().value, oldURL = s.covers.get(oldest);
        if (oldURL) URL.revokeObjectURL(oldURL); s.covers.delete(oldest);
      }
    }).catch(() => {}).finally(() => { s.coverRequests.delete(id); pumpPlayerSeriesCovers(s); });
  }
}
async function loadPlayerSeries(offset = 0) {
  const s = playerSeries; if (!s || !playerSeriesCurrent(s)) return;
  const queryEpoch = s.queryEpoch;
  s.controller?.abort(); const controller = s.controller = new AbortController();
  s.loading = true; s.error = ''; renderPlayerSeries();
  try {
    const params = new URLSearchParams({sort:s.sort, q:s.q, offset:String(offset), limit:'100'});
    const response = await fetch(`/api/series/${s.id}?${params}`, {signal:controller.signal});
    const data = await response.json();
    if (!playerSeriesCurrent(s) || s.controller !== controller || queryEpoch !== s.queryEpoch) return;
    if (data.storage_owner && data.storage_owner !== s.owner) {
      stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('账号已变化，请刷新后重试'); return;
    }
    if (!response.ok) {
      if ([403,404].includes(response.status)) { stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('合集权限已变化，请重新解锁或登录'); return; }
      throw Error(data.error || '合集加载失败');
    }
    const items = (data.items || []).filter(item => Number.isSafeInteger(item.id) && item.id > 0);
    const previous = offset ? s.items : [], seen = new Set(previous.map(item => item.id));
    s.items = [...previous, ...items.filter(item => !seen.has(item.id))];
    s.total = Number(data.total) || 0; s.more = !!data.more; s.next = (Number(data.offset) || 0) + items.length;
    s.membership = data.membership;
    s.name = data.series || s.name;
  } catch (e) { if (playerSeriesCurrent(s) && queryEpoch === s.queryEpoch && e.name !== 'AbortError') s.error = '合集加载失败，请稍后重试'; }
  finally {
    if (playerSeriesCurrent(s) && s.controller === controller && queryEpoch === s.queryEpoch) { s.loading = false; renderPlayerSeries(); }
  }
}
async function validatePlayerSeries(s) {
  if (!playerSeriesCurrent(s) || s.loading || s.checking || document.hidden) return;
  s.checking = true; const controller = s.scopeController = new AbortController(), queryEpoch = s.queryEpoch;
  try {
    const params = new URLSearchParams({sort:s.sort,q:s.q,offset:'0',limit:'1'});
    const response = await fetch(`/api/series/${s.id}?${params}`, {signal:controller.signal});
    const data = await response.json();
    if (!playerSeriesCurrent(s) || queryEpoch !== s.queryEpoch) return;
    if (data.storage_owner && data.storage_owner !== s.owner) {
      stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('账号已变化，请刷新后重试'); return;
    }
    if ([403,404].includes(response.status)) {
      stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('合集权限已变化，请重新解锁或登录'); return;
    }
    if (response.ok && (!data.storage_owner || data.storage_owner === s.owner) && data.membership !== s.membership) {
      s.items = []; s.total = 0; s.next = 0; s.more = false; s.loading = true; renderPlayerSeries(); await loadPlayerSeries();
    }
  } catch (_) {} finally { if (playerSeries === s) s.checking = false; }
}
function filterPlayerSeries(text) {
  const s = playerSeries; if (!s || !playerSeriesCurrent(s)) return;
  ++s.queryEpoch; s.controller?.abort(); clearTimeout(s.timer);
  s.q = text.trim().slice(0,128); s.items = []; s.total = 0; s.more = false; s.next = 0; s.loading = true; s.error = ''; s.focusActive = true; s.scroll = 0;
  renderPlayerSeries(); s.timer = setTimeout(() => loadPlayerSeries(), 400);
}
async function playSeriesVideo(id) {
  const s = playerSeries; if (!s || !playerSeriesCurrent(s) || s.pending === id) return;
  const item = s.items.find(item => item.id === id); if (!item?.playable) return;
  const epoch = s.playEpoch = (s.playEpoch || 0) + 1;
  s.pending = id; renderPlayerSeries();
  try {
    const response = await fetch(`/api/jobs?mode=exact&only=1&ids=${id}`);
    const data = await response.json();
    if (!playerSeriesCurrent(s) || s.playEpoch !== epoch) return;
    if (data.storage_owner && data.storage_owner !== s.owner) {
      stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('账号已变化，请刷新后重试'); return;
    }
    const job = data.jobs?.find(job => job.id === id);
    if (!response.ok || !job) {
      if ([403,404].includes(response.status) || response.ok) {
        stopPlayerSeries(); if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission('分集权限已变化，请重新打开合集'); return;
      }
      throw Error(data.error || '分集加载失败');
    }
    if (job.status !== 'done' || !job.media?.length) { item.playable = false; s.error = '这集暂时无法播放，请选择其他分集'; renderPlayerSeries(); return; }
    const index = state.jobs.findIndex(job => job.id === id);
    if (index < 0) state.jobs.push(job); else state.jobs[index] = job;
    await openWatch(id);
  } catch (e) { if (playerSeriesCurrent(s) && s.playEpoch === epoch) { s.error = '分集加载失败，请稍后重试'; renderPlayerSeries(); } }
  finally { if (playerSeriesCurrent(s) && s.pending === id) { s.pending = null; renderPlayerSeries(); } }
}
