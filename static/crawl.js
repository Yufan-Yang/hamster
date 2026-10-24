// Directory collection stays on the server. This view only polls while open;
// semantic search is an explicit action and never runs from the poll timer.
function newCrawlView() {
  return {crawls: [], id: '', crawl: null, items: [], total: 0, offset: 0, limit: 100, revealed: false, privacyLocked: false,
    filter: {q: '', mode: 'exact'}, selected: new Map(), interpretation: '', notice: '',
    error: '', pollError: '', loading: false, refreshing: false, creating: false,
    mutating: false, exporting: false, editing: false, deleting: false, manageError: '', detailController: null,
    issuesOpen: false, issues: null, issuesOffset: 0, issuesLimit: 20, issuesLoading: false,
    issuesError: '', issuesController: null};
}
let crawlView = newCrawlView(), crawlEpoch = 0, crawlDetailEpoch = 0, crawlIssueEpoch = 0, crawlPoll = null;
const crawlRequests = new Set();
const CRAWL_STATUS = {queued: '等待采集', running: '正在采集', stopped: '已停止', done: '已完成', limited: '已到页数上限', failed: '采集失败'};
function crawlPageLimited(c) { return c?.status === 'limited' && (c.limit_reason === 'pages' || !c.limit_reason && crawlNumber(c.pages_done) >= crawlNumber(c.max_pages)); }
function crawlStatus(c) { return c?.status === 'limited' && !crawlPageLimited(c) ? '可继续采集' : CRAWL_STATUS[c?.status] || c?.status || ''; }
const CRAWL_MODES = {exact: '精准', wildcard: '通配', regex: '正则', ai: 'AI 语义'};
function crawlToken() { return {epoch: crawlEpoch, owner: storageOwner, privacy: typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch}; }
function isCurrentCrawl(token) { return token.epoch === crawlEpoch && token.owner === storageOwner && token.privacy === (typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch); }
function crawlRedacted(c) { return !!(c?.privacy_locked || c?.redacted); }
function crawlTaskVisible(c) { return !!c && !crawlRedacted(c) && (c.source_classification === 'general' || crawlView.revealed); }
function lockCrawlView() { resetCrawl({open: crawlVisible(), locked: true}); scheduleCrawl(); }
function crawlVisible() { return !$('#crawlSheet').hidden && !$('#crawlSheet').classList.contains('closing'); }
function crawlNumber(value) { return Math.max(0, Number(value) || 0); }
function crawlDuration(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? '时长 ' + dur(value) : '时长未知';
}
function crawlURL(value) {
  try { const url = new URL(String(value || '')); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function crawlInputURLs(value) {
  const lines = String(value || '').trim().split(/(?:\s+|[，；]|[,;](?=\s*https?:\/\/))+/).filter(Boolean);
  const urls = [];
  for (let i = 0; i < lines.length; i++) {
    const url = crawlURL(lines[i]);
    if (!url) throw Error('第 ' + (i + 1) + ' 个网址无效，请填写完整的 http(s) 网址。');
    if (!urls.includes(url)) urls.push(url);
  }
  if (!urls.length || urls.length > 20) throw Error('请填写 1–20 个目录网址，每行一个。');
  return urls;
}
function crawlLink(url, title) {
  const safe = crawlURL(url);
  return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(title || safe)}</a>` : esc(title || url || '地址不可用');
}
function crawlAddresses(item) {
  const media = (item.media_urls || []).map(x => crawlURL(typeof x === 'string' ? x : x.url)).filter(Boolean);
  return [...new Set(media.length ? media : [crawlURL(item.url || item.page_url)].filter(Boolean))];
}
function crawlSources(item) {
  const sources = (item.sources || []).map(x => typeof x === 'string' ? {url: x} : x || {});
  if (!sources.length && (item.page_url || item.url)) sources.push({url: item.page_url || item.url});
  return sources.map(x => ({url: crawlURL(x.url || x.page_url), title: x.title || ''})).filter(x => x.url);
}
async function crawlApi(path, {body, text = false, controller = new AbortController()} = {}) {
  const token = crawlToken(); crawlRequests.add(controller);
  try {
    const response = await fetch('/api/crawls' + path, {signal: controller.signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
    if (!isCurrentCrawl(token)) throw new DOMException('账号或采集记录已切换', 'AbortError');
    if (response.status === 403) {
      const message = '采集权限已变化，请重新解锁或登录';
      if (typeof clearMainSearchPermission === 'function') clearMainSearchPermission(message);
      resetCrawl(); toast(message);
      if (typeof schedule === 'function') schedule();
      throw new DOMException(message, 'AbortError');
    }
    const data = text && response.ok ? await response.text() : await response.json();
    if (!isCurrentCrawl(token)) throw new DOMException('账号或采集记录已切换', 'AbortError');
    if (!response.ok) throw Error(data.error || '请求失败，请稍后重试');
    if (data?.storage_owner && data.storage_owner !== storageOwner) throw new DOMException('账号已变化', 'AbortError');
    if (typeof data?.revealed === 'boolean') crawlView.revealed = data.revealed;
    if (data?.privacy_locked || crawlRedacted(data?.crawl) || data?.crawls?.some(crawlRedacted)) {
      lockCrawlView(); throw new DOMException('隐私来源已锁定', 'AbortError');
    }
    return data;
  } finally { crawlRequests.delete(controller); }
}
function suspendCrawl() {
  ++crawlEpoch; ++crawlDetailEpoch; clearTimeout(crawlPoll); crawlPoll = null;
  clearCrawlIssues();
  crawlRequests.forEach(controller => controller.abort()); crawlRequests.clear();
  crawlView.loading = crawlView.refreshing = crawlView.creating = crawlView.mutating = crawlView.exporting = false;
  crawlView.editing = crawlView.deleting = false; crawlView.manageError = '';
  crawlView.detailController = null;
}
function resetCrawl({open = false, locked = false, revealed = false, crawls = []} = {}) {
  suspendCrawl(); crawlView = newCrawlView();
  crawlView.revealed = revealed; crawlView.privacyLocked = locked; crawlView.crawls = crawls.filter(crawlTaskVisible);
  $('#crawlSheet').hidden = !open; $('#crawlURL').value = ''; $('#crawlMaxPages').value = '50'; $('#crawlRefreshHours').value = '6';
  $('#crawlQuery').value = ''; $('#crawlSearchMode').value = 'exact';
  $('#crawlEditURL').value = '';
  for (const id of ['crawlResults','crawlSearchNote','crawlCurrent','crawlLastError','crawlSourceRating','crawlStatus','crawlStats','crawlStatusHint','crawlNextRefresh','crawlHistory','crawlDeleteURL','crawlIssuesList','crawlIssuesSummary','crawlIssuesError','crawlIssuesPageLabel','crawlResultCount','crawlSelectedCount','crawlPageLabel','crawlError','crawlEditError','crawlDeleteError']) {
    $('#' + id).innerHTML = ''; $('#' + id).textContent = '';
  }
  delete $('#crawlResults').dataset.html;
  $('#crawlProgress').value = 0; $('#crawlProgress').max = 1;
  $('#crawlDetail').hidden = true;
  $('#crawlHistoryRow').hidden = locked && !crawlView.crawls.length;
  $('#crawlPrivacyNotice').hidden = !locked;
  $('#crawlPrivacyNotice').textContent = '隐私来源已隐藏。解锁隐私并开启“显示隐藏的内容”后可查看。';
  for (const id of ['crawlCopy','crawlExport','crawlImportAll','crawlImportSelected']) $('#' + id).disabled = true;
  renderCrawl();
}
function scheduleCrawl() {
  clearTimeout(crawlPoll); crawlPoll = null;
  if (crawlVisible() && !document.hidden) crawlPoll = setTimeout(() => refreshCrawl(), 3000);
}
async function openCrawl() {
  hide($('#addSheet')); $('#crawlSheet').hidden = false;
  crawlSearchModeChanged(); renderCrawl();
  await refreshCrawl();
}
async function selectCrawl(id) {
  const crawls = crawlView.crawls.filter(crawlTaskVisible), revealed = crawlView.revealed;
  suspendCrawl(); crawlView = newCrawlView(); crawlView.crawls = crawls; crawlView.revealed = revealed;
  crawlView.id = String(id || ''); crawlView.crawl = crawls.find(c => String(c.id) === crawlView.id) || null;
  $('#crawlQuery').value = ''; $('#crawlSearchMode').value = 'exact';
  if (crawlView.crawl) $('#crawlMaxPages').value = String(crawlView.crawl.max_pages || 50);
  crawlSearchModeChanged(); renderCrawl();
  if (crawlView.id) await loadCrawlItems();
  scheduleCrawl();
}
async function refreshCrawl() {
  if (!crawlVisible() || document.hidden || crawlView.refreshing) return;
  const token = crawlToken(); crawlView.refreshing = true;
  try {
    const data = await crawlApi('');
    if (!isCurrentCrawl(token)) return;
    crawlView.crawls = (data.crawls || []).filter(crawlTaskVisible); crawlView.pollError = '';
    if (crawlView.id && !crawlView.crawls.some(c => String(c.id) === crawlView.id)) {
      const rows = crawlView.crawls, revealed = crawlView.revealed, locked = !revealed && !crawlTaskVisible(crawlView.crawl);
      resetCrawl({open: true, locked, revealed, crawls: rows});
      if (rows.length) await selectCrawl(rows[0].id); else scheduleCrawl();
      return;
    }
    if (!crawlView.id && crawlView.crawls.length) { await selectCrawl(crawlView.crawls[0].id); return; }
    const current = crawlView.crawls.find(c => String(c.id) === crawlView.id);
    if (current) { crawlView.crawl = current; crawlView.privacyLocked = false; }
    renderCrawl();
    // Preserve semantic results until the user searches again. Polling metadata
    // must not send prompts or quietly change the relevance ordering.
    if (crawlView.id && !crawlView.loading && !crawlView.mutating && !(crawlView.filter.mode === 'ai' && crawlView.filter.q))
      await loadCrawlItems({poll: true});
    if (isCurrentCrawl(token) && crawlView.issuesOpen && !crawlView.issuesLoading && !crawlView.mutating)
      await loadCrawlIssues({poll: true});
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.pollError = '连接暂时失败，将自动重试：' + e.message; renderCrawlError(); }
  } finally {
    if (isCurrentCrawl(token)) { crawlView.refreshing = false; scheduleCrawl(); }
  }
}
async function startCrawl(event) {
  event.preventDefault(); if (crawlView.creating) return;
  const maxPages = Number($('#crawlMaxPages').value);
  let urls;
  crawlView.error = '';
  try {
    urls = crawlInputURLs($('#crawlURL').value);
    if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 500) throw Error('目录页数为 1–500 的整数。');
  } catch (e) {
    crawlView.error = e.message; renderCrawlError(); return;
  }
  const token = crawlToken(); crawlView.creating = true; renderCrawlControls();
  try {
    const data = await crawlApi('', {body: {...(urls.length === 1 ? {url: urls[0]} : {urls}), max_pages: maxPages, refresh_hours: Number($('#crawlRefreshHours').value || 6)}});
    if (!isCurrentCrawl(token)) return;
    const created = (data.crawls || (data.crawl ? [data.crawl] : [])).filter(Boolean), errors = data.errors || [];
    if (!created.length && !errors.length) throw Error('未能创建采集任务，请稍后重试');
    const ids = new Set(created.map(c => String(c.id)));
    crawlView.crawls = [...created, ...crawlView.crawls.filter(c => !ids.has(String(c.id)))];
    $('#crawlURL').value = errors.length ? errors.map(error => error.url).join('\n') : '';
    const errorText = errors.map(error => error.url + '：' + error.error).join('\n');
    if (created.length) {
      toast('已添加 ' + created.length + ' 个目录，同时采集 2 个，其余自动排队');
      const selecting = selectCrawl(created[0].id), selectedToken = crawlToken();
      await selecting;
      if (!isCurrentCrawl(selectedToken)) return;
    }
    crawlView.error = errorText; renderCrawlError();
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; renderCrawlError(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.creating = false; renderCrawlControls(); } }
}
async function loadCrawlItems({filter = crawlView.filter, offset = crawlView.offset, poll = false} = {}) {
  if (!crawlView.id) return;
  const token = crawlToken(), request = ++crawlDetailEpoch, id = crawlView.id;
  const nextFilter = {...filter};
  crawlView.detailController?.abort();
  const controller = new AbortController(); crawlView.detailController = controller;
  crawlView.loading = !poll; if (!poll) crawlView.error = '';
  renderCrawlControls(); renderCrawlError();
  const params = new URLSearchParams({q: nextFilter.q, mode: nextFilter.mode, offset: String(offset), limit: String(crawlView.limit)});
  try {
    const data = await crawlApi('/' + encodeURIComponent(id) + '?' + params, {controller});
    if (!isCurrentCrawl(token) || request !== crawlDetailEpoch) return;
    crawlView.crawl = data.crawl; crawlView.items = data.items || []; crawlView.total = crawlNumber(data.total);
    crawlView.filter = nextFilter; crawlView.offset = offset;
    crawlView.interpretation = data.interpretation || ''; crawlView.notice = data.notice || ''; crawlView.pollError = '';
    crawlView.items.forEach(item => { if (crawlView.selected.has(String(item.id))) crawlView.selected.set(String(item.id), item); });
    // A running crawl may merge the last item on a later page.
    if (offset && !crawlView.items.length && crawlView.total <= offset) {
      crawlView.offset = Math.max(0, Math.floor((Math.max(1, crawlView.total) - 1) / crawlView.limit) * crawlView.limit);
      await loadCrawlItems({filter: nextFilter, offset: crawlView.offset, poll}); return;
    }
    renderCrawl();
  } catch (e) {
    if (isCurrentCrawl(token) && request === crawlDetailEpoch && e.name !== 'AbortError') {
      if (poll) crawlView.pollError = '结果更新失败，将自动重试：' + e.message;
      else crawlView.error = e.message;
      renderCrawlError();
    }
  } finally {
    if (isCurrentCrawl(token) && request === crawlDetailEpoch) {
      crawlView.loading = false; crawlView.detailController = null; renderCrawlControls(); scheduleCrawl();
    }
  }
}
function crawlSearchModeChanged() {
  const mode = $('#crawlSearchMode').value;
  $('#crawlQuery').maxLength = mode === 'regex' ? 128 : 256;
  $('#crawlSearchHelp').textContent = {
    exact: '按输入的字词匹配标题、描述和地址。点击搜索后生效。',
    wildcard: '通配（模糊）使用 * 匹配任意长度、? 匹配单个字符，例如 Python*入门、课程??。',
    regex: '使用正则表达式，例如 Python|编程、课程[0-9]+。表达式错误会保留输入供修改。',
    ai: '描述想找的内容，结合近义词、同义词和上下文按关联度排序。点击搜索才会使用 AI。'
  }[mode] || '';
  $('#crawlQuery').placeholder = mode === 'wildcard' ? '例如：Python*入门、课程??' : mode === 'regex' ? '例如：(教程|入门).*Python' : mode === 'ai' ? '例如：适合零基础学习编程的视频' : '搜索标题、描述或地址';
}
function searchCrawl(event) {
  event.preventDefault();
  return loadCrawlItems({filter: {q: $('#crawlQuery').value.trim(), mode: $('#crawlSearchMode').value}, offset: 0});
}
function clearCrawlSearch() {
  $('#crawlQuery').value = '';
  return loadCrawlItems({filter: {q: '', mode: $('#crawlSearchMode').value}, offset: 0});
}
function pageCrawl(direction) {
  if (crawlView.loading) return;
  const offset = Math.max(0, crawlView.offset + direction * crawlView.limit);
  if (direction > 0 && offset >= crawlView.total) return;
  return loadCrawlItems({offset});
}
function clearCrawlIssues() {
  ++crawlIssueEpoch; crawlView.issuesController?.abort();
  crawlView.issuesOpen = crawlView.issuesLoading = false;
  crawlView.issues = null; crawlView.issuesOffset = 0; crawlView.issuesError = ''; crawlView.issuesController = null;
  renderCrawlIssues();
}
async function toggleCrawlIssues() {
  if (crawlView.issuesOpen) { clearCrawlIssues(); return; }
  if (!crawlView.id) return;
  crawlView.issuesOpen = true; renderCrawlIssues();
  await loadCrawlIssues();
}
async function loadCrawlIssues({offset = crawlView.issuesOffset, poll = false} = {}) {
  if (!crawlView.id || !crawlView.issuesOpen) return;
  const token = crawlToken(), request = ++crawlIssueEpoch, id = crawlView.id;
  crawlView.issuesController?.abort();
  const controller = new AbortController(); crawlView.issuesController = controller;
  crawlView.issuesLoading = true; if (!poll) crawlView.issuesError = ''; renderCrawlIssues();
  try {
    const params = new URLSearchParams({offset: String(offset), limit: String(crawlView.issuesLimit)});
    const data = await crawlApi('/' + encodeURIComponent(id) + '/issues?' + params, {controller});
    if (!isCurrentCrawl(token) || request !== crawlIssueEpoch || !crawlView.issuesOpen) return;
    crawlView.issues = data; crawlView.issuesOffset = offset; crawlView.issuesError = '';
    if (offset && !data.items?.length && crawlNumber(data.total) <= offset) {
      await loadCrawlIssues({offset: Math.max(0, Math.floor((Math.max(1, crawlNumber(data.total)) - 1) / crawlView.issuesLimit) * crawlView.issuesLimit), poll});
      return;
    }
  } catch (e) {
    if (isCurrentCrawl(token) && request === crawlIssueEpoch && e.name !== 'AbortError') {
      crawlView.issuesError = e.message;
      // A failed permissions refresh must not leave diagnostic URLs on screen.
      crawlView.issues = null;
    }
  } finally {
    if (isCurrentCrawl(token) && request === crawlIssueEpoch) {
      crawlView.issuesLoading = false; crawlView.issuesController = null; renderCrawlIssues();
    }
  }
}
function pageCrawlIssues(direction) {
  if (crawlView.issuesLoading) return;
  const offset = Math.max(0, crawlView.issuesOffset + direction * crawlView.issuesLimit);
  if (direction > 0 && offset >= crawlNumber(crawlView.issues?.total)) return;
  return loadCrawlIssues({offset});
}
async function retryFailedCrawlPages() {
  const issues = crawlView.issues;
  if (!crawlView.id || crawlView.mutating || crawlView.issuesLoading || !issues?.can_retry || issues.privacy_locked || crawlView.crawl?.status === 'stopped') return;
  const token = crawlToken();
  ++crawlIssueEpoch; crawlView.issuesController?.abort(); crawlView.issuesController = null;
  crawlView.mutating = true; crawlView.issuesError = ''; renderCrawlControls(); renderCrawlIssues();
  try {
    const data = await crawlApi('/' + encodeURIComponent(crawlView.id) + '/retry-failed', {body: {generation: issues.generation}});
    if (!isCurrentCrawl(token)) return;
    if (data.crawl) {
      crawlView.crawl = data.crawl;
      crawlView.crawls = crawlView.crawls.map(c => String(c.id) === crawlView.id ? data.crawl : c);
    }
    crawlView.issues = null;
    renderCrawl(); toast(crawlNumber(data.requeued) ? '已重排 ' + crawlNumber(data.requeued) + ' 个失败页面，成功页面保留' : '没有需要重试的失败页面');
    if (crawlView.issuesOpen) await loadCrawlIssues({offset: 0});
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.issuesError = e.message; crawlView.issues = null; }
  } finally {
    if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); renderCrawlIssues(); scheduleCrawl(); }
  }
}
function crawlIssueTime(value) {
  const date = new Date(Number(value) * 1000);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '';
}
function renderCrawlIssues() {
  const v = crawlView, data = v.issues, items = data?.items || [], locked = !!data?.privacy_locked;
  $('#crawlIssuesToggle').textContent = v.issuesOpen ? '收起问题页面' : '查看问题页面';
  $('#crawlIssuesToggle').setAttribute('aria-expanded', String(v.issuesOpen));
  $('#crawlIssuesPanel').hidden = !v.issuesOpen;
  $('#crawlIssuesError').textContent = v.issuesError; $('#crawlIssuesError').hidden = !v.issuesError;
  const health = data?.health;
  const rate = health?.failure_rate == null ? NaN : Number(health.failure_rate);
  $('#crawlIssuesSummary').textContent = locked ? data.notice || '解锁隐私并显示隐藏内容后可查看问题页面。' : health
    ? `已完成 ${crawlNumber(health.completed)} · 失败 ${crawlNumber(data.failed)} · 自动重试中 ${crawlNumber(data.retrying)}` + (Number.isFinite(rate) ? ` · 本轮页面失败率 ${(Math.max(0, Math.min(1, rate)) * 100).toFixed(2)}%` : '')
    : v.issuesLoading ? '正在检查问题页面…' : '';
  $('#crawlIssuesHint').textContent = locked ? '' : v.crawl?.status === 'stopped' ? '任务已停止，请先继续采集，再重试失败页面。'
    : '失败率按本轮已完成与失败页面计算，不是请求错误率。自动重试中的页面会自行继续；手动重试保留已成功的页面和目录页数设置。';
  const healLabels = {new: '等待自愈诊断', healing: '正在自愈', fixed: '自愈修复已通过', back: '修复后再次失败', error: '自愈未完成', diagnosed: '已诊断，需处理', not_a_bug: '已诊断，非代码故障', needs_review: '等待人工检查', ignored: '已忽略', manual: '需人工处理'};
  const html = locked ? '' : items.map(item => `<article class="crawl-issue"><div class="crawl-issue-meta"><b>${item.state === 'failed' ? '失败' : item.state === 'running' ? '正在自动重试' : '等待自动重试'}</b><span>${item.kind === 'list' ? '目录页' : '详情页'} · 已失败 ${crawlNumber(item.failures)} 次</span>${item.retry_at ? `<span>下次重试：${esc(crawlIssueTime(item.retry_at))}</span>` : ''}${item.heal ? `<span>${esc(healLabels[item.heal.state] || '已提交自愈诊断')}</span>` : ''}</div><p class="crawl-issue-url">${crawlLink(item.url)}</p>${item.error ? `<p class="crawl-issue-error">${esc(item.error)}</p>` : ''}</article>`).join('') || (data ? '<p class="crawl-status-hint">当前没有失败或等待重试的页面。</p>' : '');
  if ($('#crawlIssuesList').innerHTML !== html) $('#crawlIssuesList').innerHTML = html;
  $('#crawlIssuesRetry').disabled = v.mutating || v.issuesLoading || !data?.can_retry || locked || v.crawl?.status === 'stopped';
  $('#crawlIssuesRetry').textContent = v.mutating ? '处理中…' : '仅重试失败页';
  $('#crawlIssuesRefresh').disabled = v.issuesLoading || v.mutating;
  $('#crawlIssuesPrev').disabled = v.issuesLoading || v.mutating || v.issuesOffset <= 0;
  $('#crawlIssuesNext').disabled = v.issuesLoading || v.mutating || v.issuesOffset + items.length >= crawlNumber(data?.total);
  $('#crawlIssuesPages').hidden = locked || !data?.total;
  $('#crawlIssuesPageLabel').textContent = data?.total ? `${Math.floor(v.issuesOffset / v.issuesLimit) + 1} / ${Math.ceil(data.total / v.issuesLimit)} 页` : '';
}
async function controlCrawl() {
  if (!crawlView.crawl || crawlView.mutating) return;
  const current = crawlView.crawl, stopping = ['running', 'queued'].includes(current.status);
  const action = stopping ? 'stop' : 'resume';
  const body = stopping ? {} : {max_pages: Math.min(500, Math.max(crawlNumber($('#crawlMaxPages').value), crawlNumber(current.max_pages) + (crawlPageLimited(current) ? 50 : 0), crawlNumber(current.pages_done), 1))};
  const token = crawlToken();
  ++crawlDetailEpoch; crawlView.detailController?.abort(); crawlView.loading = false;
  crawlView.mutating = true; crawlView.error = ''; renderCrawlControls(); renderCrawlError();
  try {
    const data = await crawlApi('/' + encodeURIComponent(crawlView.id) + '/' + action, {body});
    if (!isCurrentCrawl(token)) return;
    crawlView.crawl = data.crawl;
    crawlView.crawls = crawlView.crawls.map(c => String(c.id) === crawlView.id ? data.crawl : c);
    if (!stopping) $('#crawlMaxPages').value = String(data.crawl.max_pages || body.max_pages);
    renderCrawl(); toast(stopping ? '已停止采集，并关闭自动刷新' : '已继续采集');
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; renderCrawlError(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); scheduleCrawl(); } }
}
async function updateCrawlSettings(action, body) {
  if (!crawlView.id || crawlView.mutating) return;
  const token = crawlToken();
  ++crawlDetailEpoch; crawlView.detailController?.abort(); crawlView.loading = false;
  crawlView.mutating = true; crawlView.error = ''; renderCrawlControls(); renderCrawlError();
  try {
    const data = await crawlApi('/' + encodeURIComponent(crawlView.id) + '/' + action, {body});
    if (!isCurrentCrawl(token)) return;
    if (data.crawl) {
      crawlView.crawl = data.crawl; crawlView.crawls = crawlView.crawls.map(c => String(c.id) === crawlView.id ? data.crawl : c);
    }
    $('#crawlCurrentRefresh').value = String(crawlView.crawl?.refresh_hours || 0);
    renderCrawl(); toast(action === 'refresh' ? '开始增量刷新，只解析新发现的内容' : body.refresh_hours ? '已设置自动增量刷新' : '已关闭自动刷新');
  } catch (e) { if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; $('#crawlCurrentRefresh').value = String(crawlView.crawl?.refresh_hours || 0); renderCrawl(); } }
  finally { if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); scheduleCrawl(); } }
}
function saveCrawlRefresh(value) { return updateCrawlSettings('settings', {refresh_hours: Number(value)}); }
function refreshCrawlNow() { return updateCrawlSettings('refresh', {}); }
function editCrawl() {
  const c = crawlView.crawl;
  if (!c || crawlView.mutating || ['queued', 'running'].includes(c.status)) return;
  crawlView.editing = true; crawlView.deleting = false; crawlView.manageError = '';
  $('#crawlEditURL').value = c.url; $('#crawlEditMaxPages').value = String(c.max_pages || 50);
  $('#crawlEditRefreshHours').value = String(c.refresh_hours || 0);
  renderCrawlManage(); $('#crawlEditURL').focus();
}
function cancelCrawlEdit() {
  if (crawlView.mutating) return;
  crawlView.editing = false; crawlView.manageError = ''; renderCrawlManage(); $('#crawlEdit').focus();
}
async function saveCrawlEdit(event) {
  event.preventDefault();
  const c = crawlView.crawl;
  if (!c || !crawlView.editing || crawlView.mutating) return;
  const url = crawlURL($('#crawlEditURL').value.trim()), maxPages = Number($('#crawlEditMaxPages').value);
  const changed = url !== crawlURL(c.url), minimum = changed ? 1 : Math.max(1, crawlNumber(c.pages_done));
  if (!url || !Number.isInteger(maxPages) || maxPages < minimum || maxPages > 500) {
    crawlView.manageError = '请填写有效网址，目录页数为 ' + minimum + '–500 的整数。'; renderCrawlManage(); return;
  }
  const token = crawlToken();
  ++crawlDetailEpoch; crawlView.detailController?.abort(); crawlView.loading = false;
  crawlView.mutating = true; crawlView.manageError = ''; renderCrawlControls(); renderCrawlManage();
  try {
    const data = await crawlApi('/' + encodeURIComponent(crawlView.id) + '/settings', {body: {url, max_pages: maxPages, refresh_hours: Number($('#crawlEditRefreshHours').value)}});
    if (!isCurrentCrawl(token)) return;
    crawlView.crawl = data.crawl; crawlView.crawls = crawlView.crawls.map(item => String(item.id) === crawlView.id ? data.crawl : item);
    crawlView.editing = false;
    if (changed) {
      clearCrawlIssues();
      crawlView.items = []; crawlView.total = 0; crawlView.offset = 0; crawlView.selected.clear();
      crawlView.filter = {q: '', mode: 'exact'}; crawlView.interpretation = crawlView.notice = '';
      $('#crawlQuery').value = ''; $('#crawlSearchMode').value = 'exact'; crawlSearchModeChanged();
    }
    $('#crawlMaxPages').value = String(data.crawl.max_pages || maxPages);
    renderCrawl(); toast(changed ? '已更换目录，开始重新采集' : '已保存任务设置');
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.manageError = e.message; renderCrawlManage(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); renderCrawlManage(); scheduleCrawl(); } }
}
function askDeleteCrawl() {
  if (!crawlView.crawl || crawlView.mutating) return;
  crawlView.deleting = true; crawlView.editing = false; crawlView.manageError = '';
  renderCrawlManage(); $('#crawlDeleteCancel').focus();
}
function cancelDeleteCrawl() {
  if (crawlView.mutating) return;
  crawlView.deleting = false; crawlView.manageError = ''; renderCrawlManage(); $('#crawlDelete').focus();
}
async function deleteCrawl() {
  if (!crawlView.crawl || !crawlView.deleting || crawlView.mutating) return;
  const token = crawlToken(), id = crawlView.id;
  ++crawlDetailEpoch; crawlView.detailController?.abort(); crawlView.loading = false;
  crawlView.mutating = true; crawlView.manageError = ''; renderCrawlControls(); renderCrawlManage();
  try {
    await crawlApi('/' + encodeURIComponent(id) + '/delete', {body: {}});
    if (!isCurrentCrawl(token)) return;
    crawlView.crawls = crawlView.crawls.filter(c => String(c.id) !== id);
    toast('已删除采集任务，已加入下载的视频保留');
    await selectCrawl(crawlView.crawls[0]?.id || '');
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.manageError = e.message; renderCrawlManage(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); renderCrawlManage(); scheduleCrawl(); } }
}
function renderCrawlManage() {
  const v = crawlView, active = ['queued', 'running'].includes(v.crawl?.status);
  $('#crawlEditForm').hidden = !v.editing; $('#crawlDeleteConfirm').hidden = !v.deleting;
  $('#crawlManageHint').textContent = active ? '编辑任务前请先停止采集。' : '';
  $('#crawlManageHint').hidden = !active;
  $('#crawlDeleteURL').textContent = v.deleting ? v.crawl?.url || '' : '';
  $('#crawlEditError').textContent = $('#crawlDeleteError').textContent = v.manageError;
  $('#crawlEditError').hidden = $('#crawlDeleteError').hidden = !v.manageError;
  $('#crawlEditSave').disabled = v.mutating || active;
  $('#crawlEditCancel').disabled = $('#crawlDeleteCancel').disabled = $('#crawlDeleteGo').disabled = v.mutating;
  $('#crawlEditSave').textContent = v.mutating ? '保存中…' : '保存修改';
  $('#crawlDeleteGo').textContent = v.mutating ? '删除中…' : '确认删除任务';
}
function pickCrawlItem(id, checked) {
  const item = crawlView.items.find(item => String(item.id) === String(id));
  if (checked && item) crawlView.selected.set(String(id), item); else crawlView.selected.delete(String(id));
  renderCrawlSelection(); renderCrawlControls();
}
function pickCrawlPage(checked) {
  crawlView.items.forEach(item => { if (checked) crawlView.selected.set(String(item.id), item); else crawlView.selected.delete(String(item.id)); });
  renderCrawlSelection(); renderCrawlControls();
}
function clearCrawlSelection() { crawlView.selected.clear(); renderCrawlSelection(); renderCrawlControls(); }
function crawlFilterParams() { return new URLSearchParams({q: crawlView.filter.q, mode: crawlView.filter.mode}); }
async function crawlExportText() { return crawlApi('/' + encodeURIComponent(crawlView.id) + '/export?' + crawlFilterParams(), {text: true}); }
async function writeCrawlClipboard(value) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(value); return true; } catch { /* Plain HTTP and denied clipboard permissions use selection below. */ }
  }
  const field = document.createElement('textarea'), previous = document.activeElement;
  field.value = value; field.readOnly = true; field.style.position = 'fixed'; field.style.opacity = '0';
  document.body.appendChild(field); field.select(); field.setSelectionRange(0, value.length);
  const copied = document.execCommand('copy'); field.remove(); previous?.focus(); return copied;
}
async function copyCrawlAddresses(itemId) {
  if (!crawlView.id || crawlView.exporting) return;
  const token = crawlToken(); crawlView.exporting = true; crawlView.error = ''; renderCrawlControls(); renderCrawlError();
  try {
    const items = itemId !== undefined ? crawlView.items.filter(item => String(item.id) === String(itemId)) : [...crawlView.selected.values()];
    if (itemId !== undefined && !items.length) throw Error('这条结果已变化，请刷新后重试');
    const value = items.length ? [...new Set(items.flatMap(crawlAddresses))].join('\n') : await crawlExportText();
    if (!isCurrentCrawl(token)) return;
    if (!value.trim()) throw Error('当前结果没有可复制的地址');
    const ok = await writeCrawlClipboard(value);
    if (isCurrentCrawl(token)) { if (ok) toast('已复制 ' + value.trim().split(/\r?\n/).length + ' 个地址'); else throw Error('复制失败，可以使用导出全部地址'); }
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; renderCrawlError(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.exporting = false; renderCrawlControls(); } }
}
async function exportCrawlAddresses() {
  if (!crawlView.id || crawlView.exporting) return;
  const token = crawlToken(); crawlView.exporting = true; crawlView.error = ''; renderCrawlControls(); renderCrawlError();
  try {
    const value = await crawlExportText(); if (!isCurrentCrawl(token)) return;
    if (!value.trim()) throw Error('当前结果没有可导出的地址');
    const url = URL.createObjectURL(new Blob([value], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = 'video-addresses-' + crawlView.id + '.txt';
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('地址已导出');
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; renderCrawlError(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.exporting = false; renderCrawlControls(); } }
}
async function importCrawl(all) {
  if (!crawlView.id || crawlView.mutating || crawlView.loading || (!all && !crawlView.selected.size)) return;
  const token = crawlToken(), selectedIds = new Set(crawlView.selected.keys());
  const body = all ? {all: true, ...crawlView.filter} : {ids: [...selectedIds].map(Number)};
  crawlView.mutating = true; crawlView.error = ''; renderCrawlControls(); renderCrawlError();
  try {
    const data = await crawlApi('/' + encodeURIComponent(crawlView.id) + '/import', {body});
    if (!isCurrentCrawl(token)) return;
    const count = value => Array.isArray(value) ? value.length : crawlNumber(value);
    const messages = [count(data.added) ? '已添加下载 ' + count(data.added) + ' 个' : '', count(data.duplicates) ? '已存在 ' + count(data.duplicates) + ' 个' : '', count(data.linked) ? '已关联 ' + count(data.linked) + ' 个' : ''].filter(Boolean);
    toast(messages.join('，') || '已处理所选结果');
    crawlView.items.forEach(item => { if (all || selectedIds.has(String(item.id))) item.imported = true; });
    selectedIds.forEach(id => crawlView.selected.delete(id));
    renderCrawl();
    // Download-list refresh does not rerun the user's AI query.
    if (typeof refresh === 'function') Promise.resolve(refresh()).catch(() => {});
  } catch (e) {
    if (isCurrentCrawl(token) && e.name !== 'AbortError') { crawlView.error = e.message; renderCrawlError(); }
  } finally { if (isCurrentCrawl(token)) { crawlView.mutating = false; renderCrawlControls(); scheduleCrawl(); } }
}
function renderCrawlError() {
  const error = crawlView.error || crawlView.pollError;
  $('#crawlError').textContent = error; $('#crawlError').hidden = !error;
}
function renderCrawlHistory() {
  crawlView.crawls = crawlView.crawls.filter(crawlTaskVisible);
  const html = crawlView.crawls.length ? crawlView.crawls.map(c => `<option value="${esc(c.id)}">${esc(crawlStatus(c))} · ${esc(c.url)} · ${crawlNumber(c.found)} 条</option>`).join('') : '<option value="">暂无采集记录</option>';
  if ($('#crawlHistory').innerHTML !== html) $('#crawlHistory').innerHTML = html;
  $('#crawlHistory').value = crawlView.id;
  $('#crawlHistoryRow').hidden = crawlView.privacyLocked && !crawlView.crawls.length;
}
function renderCrawlProgress() {
  if (crawlView.crawl && !crawlTaskVisible(crawlView.crawl)) { lockCrawlView(); return; }
  const c = crawlView.crawl; $('#crawlDetail').hidden = !crawlView.id;
  if (!c) return;
  $('#crawlStatus').textContent = crawlStatus(c);
  $('#crawlStats').innerHTML = `<span>目录页 <b>${crawlNumber(c.pages_done)} / ${crawlNumber(c.max_pages)}</b></span><span>详情页 <b>${crawlNumber(c.details_done)}</b></span><span>视频 <b>${crawlNumber(c.found)}</b></span><span>重复合并 <b>${crawlNumber(c.merged)}</b></span><span>失败页面 <b>${crawlNumber(c.errors)}</b></span>`;
  $('#crawlProgress').max = Math.max(1, crawlNumber(c.max_pages), crawlNumber(c.pages_done));
  $('#crawlProgress').value = crawlNumber(c.pages_done);
  $('#crawlCurrent').innerHTML = (c.current_url ? '当前：' + crawlLink(c.current_url) : '目录：' + crawlLink(c.url));
  $('#crawlStatusHint').textContent = {
    queued: '正在排队，结果将自动更新。', running: '自动翻页、解析详情与合并中。待处理页面：' + crawlNumber(c.pages_pending),
    stopped: '已保留当前结果和进度，可以继续采集。', done: '本次采集完成，可以搜索、导出或批量添加下载。',
    limited: !crawlPageLimited(c) ? '之前因结果数量限制暂停，现可按原页数继续采集。' : crawlNumber(c.max_pages) < 500 ? '已达到目录页上限。继续可增加 50 页，也可以先调高上方页数，最多 500 页。' : '已达到 500 个目录页上限，可使用新的目录网址继续采集。',
    failed: '采集未完成，结果已保留；可以重试继续采集。'
  }[c.status] || '';
  const currentError = c.status === 'limited' && !crawlPageLimited(c) ? '' : c.error;
  $('#crawlLastError').textContent = currentError ? '最近错误记录：' + currentError + '（历史记录不代表仍在失败；当前失败 ' + crawlNumber(c.errors) + ' 页，可在“查看问题页面”核查重试与恢复情况。）' : '';
  $('#crawlLastError').hidden = !currentError;
  const classification = c.source_classification || 'unknown';
  const sourceLabel = classification === 'adult' ? '成人' : classification === 'general' ? '普通' : c.source_checked ? '待确认' : '待判别';
  $('#crawlSourceRating').textContent = '来源分级：' + sourceLabel + (classification === 'adult' ? ' · 结果需解锁隐私并显示隐藏内容' : '') + (c.source_reason ? ' · ' + c.source_reason : '');
  if (document.activeElement !== $('#crawlCurrentRefresh')) $('#crawlCurrentRefresh').value = String(c.refresh_hours || 0);
  const next = c.next_refresh ? new Date(typeof c.next_refresh === 'number' ? c.next_refresh * 1000 : c.next_refresh) : null;
  $('#crawlNextRefresh').textContent = c.refresh_hours ? (next && Number.isFinite(next.getTime()) ? '下次：' + next.toLocaleString('zh-CN') : '完成后按周期刷新') : '自动刷新已关闭';
}
function crawlInterpretation(value) {
  if (!value || typeof value === 'string') return value || '';
  if (Array.isArray(value)) return value.map(crawlInterpretation).filter(Boolean).join('；');
  return Object.entries(value).map(([key, val]) => ({query: '理解', intent: '意图', synonyms: '近义词', terms: '关联词', context: '上下文', explanation: '说明'}[key] || key) + '：' + crawlInterpretation(val)).join('；');
}
function renderCrawlResults() {
  if (crawlView.crawl && !crawlTaskVisible(crawlView.crawl)) { lockCrawlView(); return; }
  const container = $('#crawlResults');
  const expanded = new Set([...container.querySelectorAll('details[open]')].map(el => el.dataset.item));
  const active = document.activeElement;
  const focus = active && container.contains(active) ? {
    item: active.closest('[data-item]')?.dataset.item,
    key: ['pick', 'copy', 'job'].find(key => active.dataset[key] !== undefined),
    summary: active.tagName === 'SUMMARY',
  } : null;
  if (focus?.key) focus.value = active.dataset[focus.key];
  const html = crawlView.items.map(item => {
    const id = String(item.id), media = (item.media_urls || []).map(x => typeof x === 'string' ? x : x.url).filter(crawlURL);
    const sources = crawlSources(item), sourceCount = new Set(sources.map(x => x.url)).size;
    const score = Number(item.score), relevance = item.score === undefined || item.score === null || !Number.isFinite(score) ? '' : '关联度 ' + (score >= 0 && score <= 1 ? Math.round(score * 100) + '%' : Math.round(score));
    return `<article class="crawl-result" data-item="${esc(id)}">
      <label class="crawl-result-pick"><input type="checkbox" data-pick="${esc(id)}" aria-label="选择 ${esc(item.title || '视频')}"></label>
      <div class="crawl-result-content"><h3 class="crawl-result-title">${crawlLink(item.url || item.page_url, item.title || item.url || item.page_url || '未命名视频')}</h3>
      ${item.description ? `<p class="crawl-result-desc">${esc(item.description)}</p>` : ''}
      <div class="crawl-result-tags"><span>${esc(crawlDuration(item.duration))}</span><span>${media.length ? media.length + ' 个视频直链' : '详情页 · 下载时解析'}</span><span>${sourceCount > 1 ? sourceCount + ' 个来源已合并' : '1 个来源'}</span>${item.job_id || item.imported ? '<span class="crawl-imported">已加入下载</span>' : ''}${relevance ? `<span>${esc(relevance)}</span>` : ''}</div>
      ${item.match_reason ? `<p class="crawl-result-reason">匹配原因：${esc(crawlInterpretation(item.match_reason))}</p>` : ''}
      <details data-item="${esc(id)}"><summary>查看视频地址和来源</summary>
      <p>${media.length ? '视频直链' : '尚未解析出直链，保留详情页地址'}</p>${media.length ? `<ul>${media.map(url => '<li>' + crawlLink(url) + '</li>').join('')}</ul>` : ''}
      <p>来源页面</p><ul>${sources.map(source => '<li>' + crawlLink(source.url, source.title || source.url) + '</li>').join('')}</ul>
      <div class="crawl-result-tools"><button type="button" class="link" data-copy="${esc(id)}">复制本条地址</button>${Number.isSafeInteger(Number(item.job_id)) && Number(item.job_id) > 0 ? `<button type="button" class="link" data-job="${Number(item.job_id)}">查看下载</button>` : ''}</div></details></div></article>`;
  }).join('') || `<p class="crawl-empty">${crawlView.loading ? '正在加载结果…' : crawlView.filter.q ? '没有匹配结果，可以修改搜索词或切换搜索模式。' : ['running', 'queued'].includes(crawlView.crawl?.status) ? '正在查找视频，结果会自动出现在这里。' : '暂未找到视频。可以检查目录网址，或查看上方采集状态。'}</p>`;
  // Preserve focus and expanded source lists when a poll has no result changes.
  if (container.dataset.html !== html) {
    container.innerHTML = html; container.dataset.html = html;
    container.querySelectorAll('details').forEach(el => { el.open = expanded.has(el.dataset.item); });
    if (focus?.key) [...container.querySelectorAll('[data-' + focus.key + ']')].find(el => el.dataset[focus.key] === focus.value)?.focus();
    else if (focus?.summary) [...container.querySelectorAll('details')].find(el => el.dataset.item === focus.item)?.querySelector('summary')?.focus();
  }
  const notes = [crawlView.filter.q ? '当前结果：' + CRAWL_MODES[crawlView.filter.mode] + ' · ' + crawlView.filter.q : '', crawlInterpretation(crawlView.interpretation), crawlInterpretation(crawlView.notice)];
  if (crawlView.filter.q && crawlView.filter.mode === 'ai') notes.push('AI 结果保留上次搜索时的内容；采集到新视频后，点击搜索更新。');
  $('#crawlSearchNote').textContent = notes.filter(Boolean).join('\n'); $('#crawlSearchNote').hidden = !notes.some(Boolean);
  const count = crawlView.total, pages = Math.max(1, Math.ceil(count / crawlView.limit));
  $('#crawlResultCount').textContent = (crawlView.filter.q ? CRAWL_MODES[crawlView.filter.mode] + '匹配 ' : '共 ') + count + ' 条';
  $('#crawlPageLabel').textContent = count ? Math.floor(crawlView.offset / crawlView.limit) + 1 + ' / ' + pages + ' 页' : '0 条结果';
  renderCrawlSelection();
}
function renderCrawlSelection() {
  const selected = crawlView.selected.size, current = crawlView.items.filter(item => crawlView.selected.has(String(item.id))).length;
  $('#crawlSelectedCount').textContent = selected ? '已选 ' + selected + ' 条（可跨页）' : '';
  $('#crawlClearSelection').hidden = !selected;
  $('#crawlPickPage').checked = crawlView.items.length > 0 && current === crawlView.items.length;
  $('#crawlPickPage').indeterminate = current > 0 && current < crawlView.items.length;
  $('#crawlResults').querySelectorAll('[data-pick]').forEach(input => {
    input.checked = crawlView.selected.has(input.dataset.pick); input.closest('.crawl-result').classList.toggle('is-selected', input.checked);
  });
}
function renderCrawlControls() {
  const v = crawlView, c = v.crawl, busy = v.loading || v.mutating;
  $('#crawlStart').disabled = v.creating; $('#crawlStart').textContent = v.creating ? '正在创建…' : '开始采集';
  $('#crawlControl').hidden = !c || !['running', 'queued', 'stopped', 'limited', 'failed'].includes(c.status) || crawlPageLimited(c) && crawlNumber(c.max_pages) >= 500;
  $('#crawlControl').disabled = v.mutating;
  $('#crawlCurrentRefresh').disabled = v.mutating;
  $('#crawlRefreshNow').disabled = v.mutating || ['running', 'queued'].includes(c?.status);
  $('#crawlEdit').disabled = v.mutating || ['running', 'queued'].includes(c?.status);
  $('#crawlDelete').disabled = v.mutating;
  $('#crawlControl').textContent = v.mutating ? '处理中…' : ['running', 'queued'].includes(c?.status) ? '停止采集' : crawlPageLimited(c) ? '继续采集 + 50 页' : '继续采集';
  $('#crawlSearchGo').textContent = v.loading ? '搜索中…' : '搜索';
  $('#crawlSearchPending').hidden = !v.loading || !v.items.length;
  $('#crawlClearSearch').hidden = !v.filter.q;
  $('#crawlCopy').disabled = v.exporting || busy || (!v.total && !v.selected.size);
  $('#crawlCopy').textContent = v.exporting ? '处理中…' : v.selected.size ? '复制已选地址' : '复制全部地址';
  $('#crawlExport').disabled = v.exporting || busy || !v.total;
  $('#crawlImportSelected').disabled = busy || !v.selected.size;
  $('#crawlImportAll').disabled = busy || !v.total;
  $('#crawlImportAll').textContent = v.filter.q ? '下载全部匹配结果' : '下载全部结果';
  $('#crawlPickPage').disabled = !v.items.length || v.loading;
  $('#crawlPrev').disabled = v.loading || v.offset <= 0;
  $('#crawlNext').disabled = v.loading || v.offset + v.items.length >= v.total;
}
function renderCrawl() {
  if (crawlView.crawl && !crawlTaskVisible(crawlView.crawl)) { lockCrawlView(); return; }
  $('#crawlPrivacyNotice').hidden = !crawlView.privacyLocked;
  $('#crawlPrivacyNotice').textContent = '隐私来源已隐藏。解锁隐私并开启“显示隐藏的内容”后可查看。';
  renderCrawlHistory(); renderCrawlProgress(); renderCrawlResults(); renderCrawlError(); renderCrawlControls(); renderCrawlManage(); renderCrawlIssues();
}
$('#crawlResults').addEventListener('change', event => { if (event.target.matches('[data-pick]')) pickCrawlItem(event.target.dataset.pick, event.target.checked); });
$('#crawlResults').addEventListener('click', event => {
  const copy = event.target.closest('[data-copy]'), job = event.target.closest('[data-job]');
  if (copy) copyCrawlAddresses(copy.dataset.copy);
  if (job) { hide($('#crawlSheet')); openWatch(Number(job.dataset.job)); }
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(crawlPoll); crawlPoll = null; }
  else if (crawlVisible()) refreshCrawl();
});
crawlSearchModeChanged();
