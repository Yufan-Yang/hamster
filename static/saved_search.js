// Saved searches store only the query and mode; a model runs only on explicit restore.
function newSavedSearchView() {
  return {items: [], total: 0, more: false, limit: 50, limitPerOwner: 100, loading: false, busy: false,
    error: '', form: null, pendingDelete: null, listController: null};
}
let savedSearchView = newSavedSearchView(), savedSearchEpoch = 0, savedSearchListEpoch = 0;
let savedSearchPrivateQuery = false;
const savedSearchRequests = new Set();
const SAVED_SEARCH_MODES = {exact: '精准', wildcard: '通配', regex: '正则', ai: 'AI 语义'};
function savedSearchToken() { return {epoch: savedSearchEpoch, owner: storageOwner}; }
function currentSavedSearch(token) { return token.epoch === savedSearchEpoch && token.owner === storageOwner; }
function savedSearchRevealed() { return !!state.privacy?.revealed && !!privacyToken; }
function closeSavedSearches() {
  ++savedSearchEpoch; ++savedSearchListEpoch;
  savedSearchRequests.forEach(controller => controller.abort()); savedSearchRequests.clear();
  savedSearchView = newSavedSearchView();
  $('#savedSearchName').value = ''; $('#savedSearchList').innerHTML = '';
  $('#savedSearchPreview').textContent = ''; $('#savedSearchError').textContent = '';
  $('#savedSearchSheet').hidden = true; renderSavedSearches();
}
function resetSavedSearches() {
  closeSavedSearches();
  if (savedSearchPrivateQuery) {
    savedSearchPrivateQuery = false; query = ''; $('#search').value = '';
    state = {...state, jobs: [], cats: {}, understood: '', search_interpretation: '', notice: ''};
    cardHtml = []; lastBody = '';
    for (const id of ['grid', 'controw', 'chips']) { $('#' + id).innerHTML = ''; }
    $('#continue').hidden = true;
    if (typeof invalidateMainSearch === 'function') invalidateMainSearch();
    saveNavigation();
  }
  syncSavedSearchControls();
}
function syncSavedSearchControls() {
  const available = mainSearchAvailable(), text = $('#search').value.trim();
  if (!text) savedSearchPrivateQuery = false;
  $('#savedSearchTools').hidden = !available;
  $('#savedSearchSaveCurrent').hidden = !available || !text;
}
async function savedSearchApi(path, {body, controller = new AbortController()} = {}) {
  const token = savedSearchToken(); savedSearchRequests.add(controller);
  try {
    const response = await fetch('/api/searches' + path, {signal: controller.signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
    const data = await response.json();
    if (!currentSavedSearch(token) || data.storage_owner && data.storage_owner !== token.owner)
      throw new DOMException('账号或隐私状态已变化', 'AbortError');
    if (!response.ok) {
      if (response.status === 403) { resetSavedSearches(); toast(data.error || '验证已失效，请重新打开常用搜索。'); }
      throw Error(data.error || '常用搜索暂时不可用');
    }
    return data;
  } finally { savedSearchRequests.delete(controller); }
}
async function openSavedSearches(saveCurrent = false) {
  const text = $('#search').value.trim(), mode = mainSearchMode();
  if (saveCurrent && (!mainSearchAvailable() || !text)) return;
  closeSavedSearches(); $('#savedSearchSheet').hidden = false;
  if (saveCurrent) {
    savedSearchView.form = {kind: 'create', q: text, mode, private: savedSearchRevealed()};
    if (savedSearchView.form.private) { savedSearchPrivateQuery = true; saveNavigation(); }
    $('#savedSearchName').value = text.slice(0, 60);
  }
  renderSavedSearches();
  if (saveCurrent) $('#savedSearchName').focus();
  await loadSavedSearches();
}
async function loadSavedSearches(more = false) {
  if ($('#savedSearchSheet').hidden || savedSearchView.loading || savedSearchView.busy) return;
  const token = savedSearchToken(), request = ++savedSearchListEpoch;
  const offset = more ? savedSearchView.items.length : 0;
  savedSearchView.listController?.abort();
  const controller = new AbortController(); savedSearchView.listController = controller;
  savedSearchView.loading = true; savedSearchView.error = ''; renderSavedSearches();
  try {
    const data = await savedSearchApi('?' + new URLSearchParams({offset: String(offset), limit: String(savedSearchView.limit)}), {controller});
    if (!currentSavedSearch(token) || request !== savedSearchListEpoch) return;
    const items = data.items || [];
    savedSearchView.items = more ? [...new Map([...savedSearchView.items, ...items].map(item => [item.id, item])).values()] : items;
    savedSearchView.total = Math.max(0, Number(data.total) || 0); savedSearchView.more = !!data.more;
    savedSearchView.limitPerOwner = Number(data.limit_per_owner) || 100;
  } catch (e) {
    if (currentSavedSearch(token) && request === savedSearchListEpoch && e.name !== 'AbortError') {
      savedSearchView.error = e.message;
      savedSearchView.items = []; savedSearchView.total = 0; savedSearchView.more = false;
    }
  } finally {
    if (currentSavedSearch(token) && request === savedSearchListEpoch) {
      savedSearchView.loading = false; savedSearchView.listController = null; renderSavedSearches();
    }
  }
}
function renameSavedSearch(id) {
  if (savedSearchView.busy || savedSearchView.loading) return;
  const item = savedSearchView.items.find(item => String(item.id) === String(id));
  if (!item) return;
  savedSearchView.form = {kind: 'rename', ...item}; savedSearchView.pendingDelete = null; savedSearchView.error = '';
  $('#savedSearchName').value = item.name; renderSavedSearches(); $('#savedSearchName').focus();
}
function cancelSavedSearchForm() {
  if (savedSearchView.busy) return;
  savedSearchView.form = null; $('#savedSearchName').value = ''; renderSavedSearches();
}
async function saveNamedSearch(event) {
  event?.preventDefault();
  const form = savedSearchView.form;
  if (!form || savedSearchView.busy || savedSearchView.loading) return;
  const name = $('#savedSearchName').value.trim();
  if (!name || name.length > 60) { savedSearchView.error = '请填写 1–60 个字的名称。'; renderSavedSearches(); return; }
  const token = savedSearchToken(); savedSearchView.busy = true; savedSearchView.error = ''; renderSavedSearches();
  try {
    const data = await savedSearchApi(form.kind === 'rename' ? '/' + form.id + '/rename' : '',
      {body: form.kind === 'rename' ? {name} : {name, q: form.q, mode: form.mode, ...(form.private ? {expected_private: true} : {})}});
    if (!currentSavedSearch(token)) return;
    const item = data.item;
    if (!item) throw Error('未收到保存结果，请刷新后检查。');
    if (form.kind === 'create' && item.private) { savedSearchPrivateQuery = true; saveNavigation(); }
    const existing = savedSearchView.items.some(row => row.id === item.id);
    savedSearchView.items = [item, ...savedSearchView.items.filter(row => row.id !== item.id)];
    if (data.created && !existing) savedSearchView.total++;
    savedSearchView.form = null; $('#savedSearchName').value = '';
    toast(form.kind === 'rename' ? '名称已修改' : item.private ? '已保存为私密搜索，仅显示隐藏内容时可见' : '已保存常用搜索');
  } catch (e) { if (currentSavedSearch(token) && e.name !== 'AbortError') savedSearchView.error = e.message; }
  finally { if (currentSavedSearch(token)) { savedSearchView.busy = false; renderSavedSearches(); } }
}
function askDeleteSavedSearch(id) {
  if (savedSearchView.busy || savedSearchView.loading) return;
  if (!savedSearchView.items.some(item => String(item.id) === String(id))) return;
  savedSearchView.pendingDelete = String(id); savedSearchView.error = ''; renderSavedSearches();
}
function cancelDeleteSavedSearch() {
  if (savedSearchView.busy) return;
  savedSearchView.pendingDelete = null; renderSavedSearches();
}
async function deleteSavedSearch(id) {
  if (savedSearchView.busy || savedSearchView.pendingDelete !== String(id)) return;
  const token = savedSearchToken(); savedSearchView.busy = true; savedSearchView.error = ''; renderSavedSearches();
  try {
    await savedSearchApi('/' + encodeURIComponent(id) + '/delete', {body: {}});
    if (!currentSavedSearch(token)) return;
    savedSearchView.items = savedSearchView.items.filter(item => String(item.id) !== String(id));
    savedSearchView.total = Math.max(0, savedSearchView.total - 1); savedSearchView.pendingDelete = null;
    if (String(savedSearchView.form?.id) === String(id)) { savedSearchView.form = null; $('#savedSearchName').value = ''; }
    toast('已删除常用搜索');
  } catch (e) { if (currentSavedSearch(token) && e.name !== 'AbortError') savedSearchView.error = e.message; }
  finally { if (currentSavedSearch(token)) { savedSearchView.busy = false; renderSavedSearches(); } }
}
async function restoreSavedSearch(id) {
  if (savedSearchView.busy || savedSearchView.loading || !savedSearchView.items.some(item => String(item.id) === String(id))) return;
  const token = savedSearchToken(); savedSearchView.busy = true; savedSearchView.error = ''; renderSavedSearches();
  try {
    const data = await savedSearchApi('/' + encodeURIComponent(id));
    if (!currentSavedSearch(token)) return;
    const item = data.item;
    if (!item || !SAVED_SEARCH_MODES[item.mode] || typeof item.q !== 'string' || !item.q.trim()) throw Error('搜索记录已变化，请刷新后重试。');
    if (item.private && !savedSearchRevealed()) throw Error('先解锁隐私菜单并显示隐藏内容。');
    // Set this before any navigation write; private query text must never enter browser history.
    savedSearchPrivateQuery = !!item.private;
    chip = 'all'; deviceFilter = null; pageLimit = 100;
    $('#search').value = item.q; $('#mainSearchMode').value = item.mode;
    closeSavedSearches(); syncMainSearchControls();
    await submitMainSearch();
  } catch (e) { if (currentSavedSearch(token) && e.name !== 'AbortError') savedSearchView.error = e.message; }
  finally { if (currentSavedSearch(token)) { savedSearchView.busy = false; renderSavedSearches(); } }
}
function renderSavedSearches() {
  const v = savedSearchView, form = v.form, busy = v.busy || v.loading;
  $('#savedSearchForm').hidden = !form;
  $('#savedSearchFormTitle').textContent = form?.kind === 'rename' ? '重命名搜索' : '保存当前搜索';
  $('#savedSearchPreview').textContent = form ? `${SAVED_SEARCH_MODES[form.mode] || ''} · ${form.q}` : '';
  $('#savedSearchPrivacyHint').textContent = form?.private ? '这条搜索仅在解锁隐私并显示隐藏内容时可见。' : '保存名称、搜索词和模式，供当前账号再次使用。';
  $('#savedSearchName').disabled = $('#savedSearchFormSave').disabled = $('#savedSearchFormCancel').disabled = busy;
  $('#savedSearchFormSave').textContent = v.busy ? '保存中…' : form?.kind === 'rename' ? '保存名称' : '保存搜索';
  $('#savedSearchError').textContent = v.error; $('#savedSearchError').hidden = !v.error;
  $('#savedSearchCount').textContent = v.loading ? '正在加载…' : `当前可见 ${v.total} 条 · 每个账号最多保存 ${v.limitPerOwner} 条`;
  $('#savedSearchList').innerHTML = v.items.map(item => `<article class="saved-search-item"><div class="saved-search-item-heading"><button type="button" class="link saved-search-use" data-saved-action="restore" data-saved-id="${Number(item.id)}"${busy ? ' disabled' : ''}>${esc(item.name)}</button><span>${esc(SAVED_SEARCH_MODES[item.mode] || item.mode)}${item.private ? ' · 私密' : ''}</span></div><p class="saved-search-query">${esc(item.q)}</p><div class="saved-search-item-actions">${v.pendingDelete === String(item.id) ? `<span>删除这条搜索？</span><button type="button" class="link" data-saved-action="cancel-delete"${busy ? ' disabled' : ''}>取消</button><button type="button" class="link" data-saved-action="confirm-delete" data-saved-id="${Number(item.id)}"${busy ? ' disabled' : ''}>确认删除</button>` : `<button type="button" class="link" data-saved-action="rename" data-saved-id="${Number(item.id)}"${busy ? ' disabled' : ''}>重命名</button><button type="button" class="link" data-saved-action="delete" data-saved-id="${Number(item.id)}"${busy ? ' disabled' : ''}>删除</button>`}</div></article>`).join('') || `<p class="hint">${v.loading ? '正在读取常用搜索…' : '暂无可见的常用搜索。在首页输入搜索词后，点击“保存搜索”。'}</p>`;
  $('#savedSearchRefresh').disabled = busy;
  $('#savedSearchMore').hidden = !v.more; $('#savedSearchMore').disabled = busy;
}
$('#savedSearchList').addEventListener('click', event => {
  const button = event.target.closest('[data-saved-action]'); if (!button || button.disabled) return;
  const id = button.dataset.savedId;
  ({restore: restoreSavedSearch, rename: renameSavedSearch, delete: askDeleteSavedSearch,
    'confirm-delete': deleteSavedSearch, 'cancel-delete': cancelDeleteSavedSearch}[button.dataset.savedAction])?.(id);
});
window.addEventListener('DOMContentLoaded', syncSavedSearchControls);
