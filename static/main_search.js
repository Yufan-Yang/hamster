// One search input, two owner-scoped sources. AI runs only on an explicit submit.
function newMainSearch() { return {q: '', mode: 'exact', items: [], total: 0, offset: 0, limit: 20, loading: false, error: '', note: '', busy: false}; }
let mainSearchView = newMainSearch(), mainSearchEpoch = 0, mainSearchController = null, mainLibrarySnapshot = null;
let mainLibraryPage = {key: '', offset: 0, limit: 100, total: null};
const mainSearchLanes = {library: {active: null, pending: null}, crawl: {active: null, pending: null}};
function clearSearchLane(lane, release = false) {
  lane.pending?.resolve(); lane.pending = null;
  if (release) { lane.active?.resolve(); lane.active = null; }
}
function coalesceSearch(lane, key, run) {
  if (lane.active?.key === key && !lane.active.invalidated) return lane.active.promise;
  if (lane.pending?.key === key) return lane.pending.promise;
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  const job = {key, run, promise, resolve, reject};
  function start(next) {
    lane.active = next;
    let work;
    try { work = next.run(); } catch (error) { work = Promise.reject(error); }
    Promise.resolve(work).then(next.resolve, next.reject).then(() => {
      if (lane.active !== next) return;
      lane.active = null;
      const pending = lane.pending; lane.pending = null;
      if (pending) start(pending);
    });
  }
  if (lane.active) { clearSearchLane(lane); lane.pending = job; } else start(job);
  return promise;
}
function mainLibraryFlightKey() {
  return JSON.stringify([mainSearchEpoch, storageOwner, query, mainSearchMode(), chip, videoSort,
    typeof deviceFilter === 'undefined' ? '' : deviceFilter, visualSearch]);
}
function queueMainLibraryRefresh(options, run) {
  const lane = mainSearchLanes.library, key = mainLibraryFlightKey();
  if (lane.active && lane.active.key !== key) { ++refreshEpoch; lane.active.invalidated = true; }
  workspaceLoading = true; renderWorkspaceStatus();
  return coalesceSearch(lane, key, () => mainLibraryFlightKey() === key ? run() : undefined);
}
function hasMainLibraryFlight() { return !!(mainSearchLanes.library.active || mainSearchLanes.library.pending); }
function mainSearchAvailable() { return !['notes', 'books', 'live', 'subs'].includes(chip); }
function mainSearchMode() { return $('#mainSearchMode').value || 'exact'; }
function mainSearchToken() { return {epoch: mainSearchEpoch, owner: storageOwner}; }
function currentMainSearch(token) { return token.epoch === mainSearchEpoch && token.owner === storageOwner; }
function resetMainSearch({preserveFlight = false} = {}) {
  ++mainSearchEpoch;
  for (const lane of Object.values(mainSearchLanes)) clearSearchLane(lane, !preserveFlight);
  if (!preserveFlight) { mainSearchController?.abort(); mainSearchController = null; refreshController?.abort(); workspaceLoading = false; }
  mainSearchView = newMainSearch(); mainLibrarySnapshot = null;
  mainLibraryPage = {key: '', offset: 0, limit: 100, total: null};
  if (query && mainSearchMode() === 'ai') { state.jobs = []; cardHtml = []; lastBody = ''; $('#grid').innerHTML = ''; $('#grid').dataset.ids = ''; }
  renderMainSearch();
}
function invalidateMainSearch() {
  clearTimeout(searchTimer); ++refreshEpoch; if (!mainSearchLanes.library.active) refreshController?.abort(); workspaceLoading = false;
  resetMainSearch({preserveFlight: true});
}
function syncMainSearchControls() {
  const available = mainSearchAvailable(), mode = mainSearchMode();
  $('#mainSearchMode').hidden = !available; $('#mainSearchGo').disabled = chip === 'live';
  if (available) {
    $('#search').maxLength = mode === 'regex' ? 128 : 256;
    $('#search').placeholder = mode === 'wildcard' ? '例如 Python*入门、课程??' : mode === 'regex' ? '例如 (教程|入门).*Python' : mode === 'ai' ? '描述想找的内容，按搜索提交' : '搜索片库和采集结果';
    $('#search').setAttribute('aria-label', '搜索自己的片库和全部采集结果');
  } else $('#search').removeAttribute('maxlength');
  $('#mainSearchHelp').hidden = !available || !query;
  $('#mainSearchHelp').textContent = '搜索当前账号视频的标题、简介、标签和地址。' + ({
    exact: '精准按原文匹配，片库同时搜索字幕；点击“定位原文”跳到对应片段。#标签按标签查找，也可补充画面结果。', wildcard: '通配使用 * 匹配任意长度，? 匹配单个字符；同时搜索字幕，命中后可定位原文。',
    regex: '正则按表达式匹配；错误时保留输入供修改。',
    ai: 'AI 结合近义词、同义词和上下文排序；只有点击搜索或按回车才会运行。'
  }[mode] || '');
  if (typeof syncSavedSearchControls === 'function') syncSavedSearchControls();
}
function mainSearchInput() {
  if (!mainSearchAvailable()) { resetMainSearch(); return false; }
  invalidateMainSearch(); query = $('#search').value.trim(); visualSearch = smartSearch = false; exactSearch = true; workspaceError = '';
  state.jobs = []; state.understood = ''; lastBody = ''; cardHtml = []; pageLimit = 100;
  saveNavigation(); render(); updateSearchOptions();
  if (!query || mainSearchMode() !== 'ai') searchTimer = setTimeout(() => submitMainSearch(), query ? 400 : 0);
  return true;
}
function changeMainSearchMode() { mainSearchInput(); }
async function submitMainSearch(event) {
  event?.preventDefault(); clearTimeout(searchTimer);
  if (chip === 'live') return;
  const text = $('#search').value.trim();
  if (!mainSearchAvailable()) { query = text; visualSearch = smartSearch = false; saveNavigation(); await refresh(); return; }
  invalidateMainSearch(); query = text; visualSearch = smartSearch = false; exactSearch = true; pageLimit = 100; workspaceError = '';
  state.jobs = []; lastBody = ''; cardHtml = []; saveNavigation(); render(); updateSearchOptions();
  await Promise.allSettled([refresh({explicitSearch: true}), query ? loadMainCrawlResults(0) : Promise.resolve()]);
}
function mainLibraryRequest(explicit) {
  if (!mainSearchAvailable() || !query) return null;
  const mode = mainSearchMode(), key = storageOwner + '\n' + mode + '\n' + query;
  const request = {key, mode, q: query, owner: storageOwner, epoch: mainSearchEpoch, validate: false, snapshot: null, params: {q: query, mode}};
  if (['all', 'active', 'failed'].includes(chip)) request.params.state = chip;
  if (chip === 'device') { request.params.state = 'device'; request.params.device = deviceFilter || ''; }
  if (['exact', 'wildcard', 'regex'].includes(mode)) {
    const pageKey = key + '\n' + chip + '\n' + videoSort + '\n' + (typeof deviceFilter === 'undefined' ? '' : deviceFilter || '') + '\n' + visualSearch;
    if (mainLibraryPage.key !== pageKey) mainLibraryPage = {key: pageKey, offset: 0, limit: 100, total: null};
    request.pageKey = pageKey;
    request.params.offset = String(mainLibraryPage.offset); request.params.limit = String(mainLibraryPage.limit);
  }
  if (mode === 'ai' && !explicit) {
    const saved = mainLibrarySnapshot?.key === key ? mainLibrarySnapshot : null;
    request.validate = true; request.snapshot = saved;
    // Recheck existing matches against current owner/privacy/deletion state,
    // without asking the model again or adding newly collected matches.
    request.params = saved?.jobs.length ? {mode: 'exact', only: '1', ids: saved.jobs.map(job => job.id).join(',')} : {mode: 'exact', metadata: '1'};
  }
  return request;
}
function applyMainLibraryResult(next, request) {
  if (request.epoch !== mainSearchEpoch || request.owner !== storageOwner || next.storage_owner !== storageOwner) return next;
  if (request.validate) {
    const current = new Map((next.jobs || []).map(job => [job.id, job]));
    const saved = request.snapshot;
    next.jobs = (saved?.jobs || []).filter(job => current.has(job.id)).map(job => ({...job, ...current.get(job.id), score: job.score, match_reason: job.match_reason}));
    next.more = false; next.note_hits = []; next.book_hits = []; next.reading = [];
    next.search_interpretation = saved?.interpretation || ''; next.notice = saved ? 'AI 结果保留上次搜索顺序；点击搜索更新新内容。' : '点击搜索运行 AI 语义检索。';
  }
  if (request.mode === 'ai') mainLibrarySnapshot = {key: request.key, jobs: next.jobs || [], interpretation: next.search_interpretation || ''};
  if (request.pageKey && request.pageKey === mainLibraryPage.key) {
    const total = next.search_total, offset = next.search_offset, limit = next.search_limit;
    if (Number.isSafeInteger(total) && total >= 0 && Number.isSafeInteger(offset) && offset >= 0 && Number.isSafeInteger(limit) && limit > 0)
      mainLibraryPage = {key: request.pageKey, total, offset, limit};
    else mainLibraryPage.total = null;
  }
  return next;
}
async function pageMainLibrary(direction) {
  if (workspaceLoading || !mainSearchAvailable() || !query || !['exact', 'wildcard', 'regex'].includes(mainSearchMode()) || mainLibraryPage.total == null) return;
  const previous = mainLibraryPage.offset, offset = Math.max(0, previous + direction * mainLibraryPage.limit);
  if (offset === previous || offset >= mainLibraryPage.total) return;
  const token = mainSearchToken(); mainLibraryPage.offset = offset;
  await refresh(); // Only the visible library page changes; collection/AI searches are not rerun.
  if (currentMainSearch(token) && workspaceError) mainLibraryPage.offset = previous;
  if (currentMainSearch(token) && !workspaceError && mainLibraryPage.total != null && mainLibraryPage.offset >= mainLibraryPage.total && mainLibraryPage.offset > 0) {
    mainLibraryPage.offset = Math.max(0, Math.floor((Math.max(1, mainLibraryPage.total) - 1) / mainLibraryPage.limit) * mainLibraryPage.limit);
    await refresh();
  }
  renderMainSearch();
}
function clearMainSearchPermission(message) {
  if (typeof privacyToken !== 'undefined') privacyToken = null;
  if (typeof resetPrivateViews === 'function') resetPrivateViews(); else invalidateMainSearch();
  state.jobs = []; state.cats = {}; state.more = false; cardHtml = []; lastBody = '';
  for (const id of ['grid', 'controw', 'mainCrawlResults']) { $('#' + id).innerHTML = ''; $('#' + id).dataset.ids = ''; }
  if (typeof closeWatch === 'function' && typeof watching !== 'undefined' && watching) { closeWatch(); $('#watchBody').innerHTML = ''; }
  workspaceError = message || '搜索权限已变化，请重新解锁或登录'; workspaceLoading = false;
  renderMainSearch(); renderWorkspaceStatus();
}
function loadMainCrawlResults(offset = 0) {
  if (!mainSearchAvailable() || !query) return Promise.resolve();
  const token = mainSearchToken(), text = query, mode = mainSearchMode(), key = JSON.stringify([token, text, mode, offset]);
  mainSearchView.loading = true; mainSearchView.q = text; mainSearchView.mode = mode; renderMainSearch();
  return coalesceSearch(mainSearchLanes.crawl, key, () => currentMainSearch(token) && text === query && mode === mainSearchMode() ? loadMainCrawlResultsNow(offset) : undefined);
}
async function loadMainCrawlResultsNow(offset = 0) {
  if (!mainSearchAvailable() || !query) return;
  const token = mainSearchToken(), requestQuery = query, mode = mainSearchMode();
  mainSearchController?.abort(); const controller = new AbortController(); mainSearchController = controller;
  mainSearchView.loading = true; mainSearchView.error = ''; mainSearchView.q = requestQuery; mainSearchView.mode = mode; renderMainSearch();
  try {
    const params = new URLSearchParams({q: requestQuery, mode, offset: String(offset), limit: String(mainSearchView.limit)});
    const response = await fetch('/api/crawls/search?' + params, {signal: controller.signal});
    const data = await response.json();
    if (!currentMainSearch(token) || mainSearchController !== controller || query !== requestQuery || mainSearchMode() !== mode) return;
    if (data.storage_owner && data.storage_owner !== storageOwner) return;
    if (!response.ok) {
      if (response.status === 403) { clearMainSearchPermission(data.error); return; }
      throw Error(data.error || '采集结果搜索失败');
    }
    mainSearchView.items = data.items || []; mainSearchView.total = Number(data.total) || 0; mainSearchView.offset = offset;
    mainSearchView.note = [crawlInterpretation(data.interpretation), crawlInterpretation(data.notice)].filter(Boolean).join('；');
  } catch (e) {
    if (currentMainSearch(token) && mainSearchController === controller && e.name !== 'AbortError') mainSearchView.error = e.message;
  } finally {
    if (currentMainSearch(token) && mainSearchController === controller) { mainSearchView.loading = false; renderMainSearch(); }
  }
}
function pageMainCrawl(direction) {
  if (mainSearchView.loading) return;
  const offset = Math.max(0, mainSearchView.offset + direction * mainSearchView.limit);
  if (offset >= mainSearchView.total && direction > 0) return;
  return loadMainCrawlResults(offset);
}
async function mainCrawlAction(id, action) {
  const item = mainSearchView.items.find(item => String(item.id) === String(id));
  if (!item || mainSearchView.busy) return;
  const token = mainSearchToken();
  if (action === 'open') { await openCrawl(); if (currentMainSearch(token)) await selectCrawl(item.crawl_id); return; }
  if (action === 'watch') { openWatch(Number(item.job_id)); return; }
  mainSearchView.busy = true; mainSearchView.error = ''; renderMainSearch();
  try {
    if (action === 'copy') {
      if (!await writeCrawlClipboard(crawlAddresses(item).join('\n'))) throw Error('复制失败，请在详情中复制地址');
      if (currentMainSearch(token)) toast('已复制视频地址');
    } else if (action === 'import') {
      const response = await fetch('/api/crawls/' + encodeURIComponent(item.crawl_id) + '/import', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ids:[Number(item.id)]})});
      const data = await response.json(); if (!currentMainSearch(token)) return;
      if (response.status === 403) { clearMainSearchPermission(data.error); return; }
      if (!response.ok) throw Error(data.error || '添加下载失败');
      item.imported = true; if (data.ids?.[0]) item.job_id = data.ids[0]; toast('已加入下载');
    }
  } catch (e) { if (currentMainSearch(token)) mainSearchView.error = e.message; }
  finally { if (currentMainSearch(token)) { mainSearchView.busy = false; renderMainSearch(); } }
}
function renderMainSearch() {
  const v = mainSearchView, visible = mainSearchAvailable() && !!query;
  $('#mainCrawlSearch').hidden = $('#mainLibraryHeading').hidden = !visible;
  $('#mainLibraryNote').hidden = !visible;
  const paged = visible && ['exact', 'wildcard', 'regex'].includes(mainSearchMode()) && mainLibraryPage.total != null;
  $('#mainLibraryPages').hidden = !paged || mainLibraryPage.total <= mainLibraryPage.limit;
  $('#mainLibraryPrev').disabled = workspaceLoading || !paged || mainLibraryPage.offset <= 0;
  $('#mainLibraryNext').disabled = workspaceLoading || !paged || mainLibraryPage.offset + mainLibraryPage.limit >= mainLibraryPage.total;
  $('#mainLibraryPage').textContent = paged ? `${Math.floor(mainLibraryPage.offset / mainLibraryPage.limit) + 1} / ${Math.max(1, Math.ceil(mainLibraryPage.total / mainLibraryPage.limit))} 页` : '';
  if (!visible) { $('#mainCrawlResults').innerHTML = ''; $('#mainCrawlNote').textContent = ''; $('#mainCrawlError').textContent = ''; $('#mainLibraryNote').textContent = ''; return; }
  $('#scope').textContent = '当前账号的片库和采集结果';
  $('#mainLibraryNote').textContent = [crawlInterpretation(state.search_interpretation), crawlInterpretation(state.notice)].filter(Boolean).join('；');
  $('#mainLibraryNote').hidden = !$('#mainLibraryNote').textContent;
  const count = (state.jobs || []).filter(job => !job.resume_only).length;
  $('#mainLibraryCount').textContent = workspaceLoading ? '正在搜索…' : paged ? `共 ${mainLibraryPage.total} 条 · 本页 ${count} 条` : count + ' 条';
  $('#mainCrawlCount').textContent = v.loading ? '正在搜索…' : v.q === query ? '共 ' + v.total + ' 条' : '等待搜索';
  $('#mainCrawlNote').textContent = v.note; $('#mainCrawlNote').hidden = !v.note;
  $('#mainCrawlError').textContent = v.error; $('#mainCrawlError').hidden = !v.error;
  $('#mainCrawlResults').innerHTML = v.items.map(item => {
    const score = Number(item.score), relevance = item.score == null || !Number.isFinite(score) ? '' : '关联度 ' + (score <= 1 ? Math.round(score * 100) : Math.round(score)) + '%';
    return `<article class="main-crawl-card"><h3>${crawlLink(item.url || item.page_url, item.title || item.url || '未命名视频')}</h3>${item.description ? `<p class="crawl-result-desc">${esc(item.description)}</p>` : ''}
      <p class="hint">${esc(crawlDuration(item.duration))}</p>
      ${relevance || item.match_reason ? `<p class="hint">${esc([relevance,crawlInterpretation(item.match_reason)].filter(Boolean).join(' · '))}</p>` : ''}
      <div class="main-crawl-tools"><button type="button" class="link" data-main-action="open" data-main-id="${Number(item.id)}">查看采集任务</button><button type="button" class="link" data-main-action="copy" data-main-id="${Number(item.id)}"${v.busy ? ' disabled' : ''}>复制地址</button>
      <button type="button" class="btn plain" data-main-action="${item.job_id ? 'watch' : 'import'}" data-main-id="${Number(item.id)}"${v.busy || item.imported && !item.job_id ? ' disabled' : ''}>${item.job_id ? '查看下载' : item.imported ? '已加入下载' : '加入下载'}</button></div></article>`;
  }).join('') || `<p class="hint">${v.loading ? '正在查找采集结果…' : v.q !== query ? '点击搜索或按回车，同时查找片库和所有目录的采集结果。' : v.error ? '修改输入后可以重新搜索。' : '采集结果中没有匹配内容。'}</p>`;
  $('#mainCrawlPages').hidden = v.total <= v.limit;
  $('#mainCrawlPage').textContent = Math.floor(v.offset / v.limit) + 1 + ' / ' + Math.max(1, Math.ceil(v.total / v.limit));
  $('#mainCrawlPrev').disabled = v.loading || v.offset === 0; $('#mainCrawlNext').disabled = v.loading || v.offset + v.limit >= v.total;
}
$('#mainCrawlResults').addEventListener('click', event => {
  const button = event.target.closest('[data-main-action]');
  if (button) mainCrawlAction(button.dataset.mainId, button.dataset.mainAction);
});
window.addEventListener('DOMContentLoaded', () => { syncMainSearchControls(); if (mainSearchAvailable() && query && mainSearchMode() !== 'ai') loadMainCrawlResults(); });
window.addEventListener('popstate', () => { resetMainSearch(); if (mainSearchAvailable() && query && mainSearchMode() !== 'ai') loadMainCrawlResults(); });
