// Search the owned library. AI runs only on an explicit submit.
let mainSearchEpoch = 0, mainLibrarySnapshot = null;
let mainLibraryPage = {key: '', offset: 0, limit: 100, total: null};
const mainSearchLanes = {library: {active: null, pending: null}};
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
  if (!preserveFlight) { refreshController?.abort(); workspaceLoading = false; }
  mainLibrarySnapshot = null;
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
    $('#search').placeholder = mode === 'wildcard' ? '例如 Python*入门、课程??' : mode === 'regex' ? '例如 (教程|入门).*Python' : mode === 'ai' ? '描述想找的内容，按搜索提交' : '搜索标题、简介、标签、字幕…';
    $('#search').setAttribute('aria-label', '搜索自己的片库');
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
  await refresh({explicitSearch: true});
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
  await refresh(); // Only the visible library page changes; AI searches are not rerun.
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
  for (const id of ['grid', 'controw']) { $('#' + id).innerHTML = ''; $('#' + id).dataset.ids = ''; }
  if (typeof closeWatch === 'function' && typeof watching !== 'undefined' && watching) { closeWatch(); $('#watchBody').innerHTML = ''; }
  workspaceError = message || '搜索权限已变化，请重新解锁或登录'; workspaceLoading = false;
  renderMainSearch(); renderWorkspaceStatus();
}
function searchInterpretation(value) {
  return typeof value === 'string' ? value : value ? JSON.stringify(value) : '';
}
function renderMainSearch() {
  const visible = mainSearchAvailable() && !!query;
  $('#mainLibraryHeading').hidden = !visible;
  $('#mainLibraryNote').hidden = !visible;
  const paged = visible && ['exact', 'wildcard', 'regex'].includes(mainSearchMode()) && mainLibraryPage.total != null;
  $('#mainLibraryPages').hidden = !paged || mainLibraryPage.total <= mainLibraryPage.limit;
  $('#mainLibraryPrev').disabled = workspaceLoading || !paged || mainLibraryPage.offset <= 0;
  $('#mainLibraryNext').disabled = workspaceLoading || !paged || mainLibraryPage.offset + mainLibraryPage.limit >= mainLibraryPage.total;
  $('#mainLibraryPage').textContent = paged ? `${Math.floor(mainLibraryPage.offset / mainLibraryPage.limit) + 1} / ${Math.max(1, Math.ceil(mainLibraryPage.total / mainLibraryPage.limit))} 页` : '';
  if (!visible) { $('#mainLibraryNote').textContent = ''; return; }
  $('#scope').textContent = '当前账号的片库';
  $('#mainLibraryNote').textContent = [searchInterpretation(state.search_interpretation), searchInterpretation(state.notice)].filter(Boolean).join('；');
  $('#mainLibraryNote').hidden = !$('#mainLibraryNote').textContent;
  const count = (state.jobs || []).filter(job => !job.resume_only).length;
  $('#mainLibraryCount').textContent = workspaceLoading ? '正在搜索…' : paged ? `共 ${mainLibraryPage.total} 条 · 本页 ${count} 条` : count + ' 条';
}
window.addEventListener('DOMContentLoaded', syncMainSearchControls);
window.addEventListener('popstate', () => resetMainSearch());
