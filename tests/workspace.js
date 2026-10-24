// Run with node tests/workspace.js. Uses real page helpers without browser dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
function element() {
  const classes = new Set();
  return {innerHTML: "", textContent: "", hidden: false, disabled: false, value: "", attrs: {},
    classList: {toggle() {}, contains: name => classes.has(name), add: name => classes.add(name), remove: name => classes.delete(name)},
    focus() {}, setAttribute(name, value) { this.attrs[name] = value; }};
}
function pageContext(extra = {}) {
  const nodes = new Map();
  return vm.createContext({
    $: key => { if (!nodes.has(key)) nodes.set(key, element()); return nodes.get(key); },
    URLSearchParams, clearTimeout() {}, setTimeout, ...extra,
  });
}
const calls = [];
const page = pageContext({
  state: {jobs: [{status: "downloading"}], cats: {Movies: 1}, subs: []},
  FOLDERS: {Tutorials: "教程"}, LIBS: {Movies: "电影"}, ACTIVE: ["downloading"],
  category: j => j.category, catName: key => ({Tutorials: "教程", Movies: "电影"}[key] || key),
  esc: s => String(s ?? ""), chip: "all", query: "", deviceFilter: null, videoSort: "added",
  workspaceLoaded: false, workspaceLoading: false, workspaceError: "", searchTimer: null,
  pageLimit: 300, liveData: null, lastBody: "", digestState: {at: Date.now()},
  render() { page.renderChips(); }, saveNavigation() {}, updateSearchOptions() {},
  refresh() { calls.push("refresh"); }, loadLive() { calls.push("live"); }, loadDigests() {},
});
vm.runInContext(html.slice(html.indexOf("function renderChips("), html.indexOf("\nfunction card(")), page);
page.renderChips();
assert.equal((page.$("#workspaceNav").innerHTML.match(/<button/g) || []).length, 6);
assert.ok(page.$("#workspaceNav").innerHTML.includes('onclick="openSources()"'));
assert.ok(!page.$("#workspaceNav").innerHTML.includes("电影"));
assert.ok(page.$("#chips").innerHTML.includes("电影"));
assert.ok(!page.$("#chips").innerHTML.includes("setVideoSort"));
assert.ok(page.$("#viewTools").innerHTML.includes("setVideoSort"));
const categories = page.$("#chips").innerHTML;
page.state.cats = {Tutorials: 999, Movies: 1}; page.renderChips();
assert.equal(categories.replace(/ dim/g, ""), page.$("#chips").innerHTML.replace(/ dim/g, ""));
page.setChip("live");
assert.equal(page.$("#search").disabled, true);
assert.ok(page.$("#search").placeholder.includes("主播"));
assert.equal(page.$("#chips").hidden, true);
assert.equal(page.$("#viewTools").innerHTML, "");
assert.deepEqual(calls, ["refresh"]); // First live load belongs to the bootstrap response.
assert.equal(page.pageLimit, 100);
page.query = "保留的搜索";
page.setChip("books");
assert.equal(page.$("#search").disabled, false);
assert.equal(page.$("#search").value, "保留的搜索");
assert.equal(page.$("#workspaceTitle").textContent, "书架");
page.chip = "failed"; page.state.jobs = []; page.query = ""; page.renderChips();
assert.equal(page.chip, "failed"); // Empty filters remain selected and show a useful empty state.
assert.ok(page.$("#chips").innerHTML.includes('aria-pressed="true"'));
page.workspaceLoading = true; page.renderWorkspaceStatus();
assert.equal(page.$("#workspaceStatus").hidden, false);
assert.equal(page.$("#workspaceRetry").hidden, true);
page.workspaceLoading = false; page.workspaceLoaded = true; page.workspaceError = "offline";
page.renderWorkspaceStatus();
assert.equal(page.$("#workspaceRetry").hidden, false);
assert.ok(page.$("#workspaceStatusText").textContent.includes("当前内容已保留"));
page.workspaceError = ""; page.renderWorkspaceStatus();
assert.equal(page.$("#workspaceStatus").hidden, true);
console.log("PASS stable workspace navigation, context search, bootstrap requests and connection recovery");

const heal = pageContext({
  esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
  ago: value => String(value || "刚刚"), num: value => String(Number(value)),
});
vm.runInContext(html.slice(html.indexOf("const HEAL_STATES ="), html.indexOf("function workerName(")), heal);
assert.ok(heal.healEntryHtml({state: "fixed"}).includes("旧记录未包含上线验收"));
const evidence = heal.healEntryHtml({
  state: "error", label: "<img src=x>", diagnosis: "<script>bad()</script>", evidence: "<img onerror=bad()>",
  verification: {status:"rejected", base:"1234567890abcdef", revision:"fedcba0987654321", verified_at:20,
    checks:["source_hashes","page","task_api","fresh_pi_cpu","<bad>"], regressions:["tests/<img>.py"]},
});
assert.ok(evidence.includes("上线验收未通过"));
assert.ok(evidence.includes("基线 1234567890ab · 发布 fedcba098765"));
for (const label of ["发布文件校验","页面访问","任务接口","Pi CPU 工作进程心跳","&lt;img","&lt;bad&gt;"]) assert.ok(evidence.includes(label));
assert.ok(!evidence.includes("<img") && !evidence.includes("<script>"));
assert.ok(heal.healEntryHtml({verification:{status:"passed"}}).includes("上线验收通过"));
console.log("PASS self-healing evidence, acceptance status, revision labels and HTML escaping");

async function notesPagination() {
  const requests = [];
  const ctx = pageContext({
    notesEpoch: 0, storageOwner: "user:qa", query: "", visualSearch: false, notesOlderRequest: null,
    notesState: {notes: [{id: 3, created: 30}], more: true, q: ""}, notesBody: "", chip: "notes",
    renderNotes() {}, schedule() {}, toast() {},
    fetch(url) { return new Promise(resolve => requests.push({url, resolve})); },
  });
  vm.runInContext(html.slice(html.indexOf("async function loadNotes("), html.indexOf("\nfunction noteMedia(")), ctx);
  const response = data => ({ok: true, text: async () => JSON.stringify(data)});
  const older = ctx.loadNotes(true);
  await ctx.loadNotes(true); await ctx.loadNotes();
  assert.equal(requests.length, 1);
  assert.equal(ctx.$("#noteMore").disabled, true);
  requests[0].resolve(response({notes: [{id: 2, created: 20}], more: true}));
  await older;
  assert.equal(requests.length, 2); // The newest page refresh waits for pagination.
  requests[1].resolve(response({notes: [{id: 3, created: 30}], more: true}));
  await new Promise(setImmediate);
  assert.deepEqual(Array.from(ctx.notesState.notes, n => n.id), [3, 2]);
  assert.equal(ctx.$("#noteMore").disabled, false);
  const stale = ctx.loadNotes(true);
  ctx.query = "new"; const fresh = ctx.loadNotes();
  requests[3].resolve(response({notes: [{id: 8, created: 80}], more: false}));
  await fresh;
  requests[2].resolve(response({notes: [{id: 1, created: 10}], more: false}));
  await stale;
  assert.deepEqual(Array.from(ctx.notesState.notes, n => n.id), [8]);
  console.log("PASS notes pagination coalesces clicks, survives polling and rejects stale searches");
}
notesPagination().catch(error => { console.error(error); process.exitCode = 1; });

async function learningPlans() {
  const requests = [];
  const ctx = pageContext({
    storageOwner: "user:one", privacyToken: null, AbortController,
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
    fetch(url, options) { return new Promise(resolve => requests.push({url, options, resolve})); },
  });
  const begin = html.indexOf("let learningEpoch = ");
  vm.runInContext(html.slice(begin, html.indexOf("/* ---------- 资源使用 ---------- */", begin)), ctx);
  const event = {preventDefault() {}}, response = (body, ok = true) => ({ok, json: async () => body});
  ctx.openLearningPlan(); ctx.learningExample("学习 Python");
  assert.equal(requests.length, 0); // Opening the sheet and selecting examples never invoke AI.
  const first = ctx.generateLearningPlan(event); await ctx.generateLearningPlan(event);
  assert.equal(requests.length, 1);
  assert.equal(ctx.$("#learningGo").disabled, true);
  assert.equal(requests[0].url, "/api/intelligence/learning-plan");
  assert.equal(JSON.parse(requests[0].options.body).goal, "学习 Python");
  requests[0].resolve(response({overview: "先了解语法", candidate_count: 2, steps: [
    {job_id: 7, title: "理解变量", source_title: "<原视频标题>", reason: "入门基础", exercise: "写一个小例子"},
    {job_id: "9);bad()", title: "非法来源"},
  ], limitations: ["仅涵盖现有片库"]}));
  await first;
  assert.ok(ctx.$("#learningResult").innerHTML.includes("openLearningSource(7)"));
  assert.ok(ctx.$("#learningResult").innerHTML.includes("&lt;原视频标题&gt;"));
  assert.ok(!ctx.$("#learningResult").innerHTML.includes("bad()"));
  assert.ok(ctx.$("#learningResult").innerHTML.includes("写一个小例子"));
  assert.equal(ctx.$("#learningGo").disabled, false);
  const failed = ctx.generateLearningPlan(event);
  requests[1].resolve(response({error: "模型暂时不可用"}, false)); await failed;
  assert.equal(ctx.$("#learningGoal").value, "学习 Python");
  assert.equal(ctx.$("#learningError").hidden, false);
  assert.equal(ctx.$("#learningError").textContent, "模型暂时不可用");
  const closed = ctx.generateLearningPlan(event);
  ctx.cancelLearningPlan(); ctx.$("#learningSheet").hidden = true; ctx.openLearningPlan();
  const fresh = ctx.generateLearningPlan(event);
  assert.equal(requests[2].options.signal.aborted, true);
  requests[2].resolve(response({overview: "旧清单不可显示", steps: []})); await closed;
  assert.equal(ctx.$("#learningResult").innerHTML, "");
  assert.equal(ctx.$("#learningGo").disabled, true); // The old request cannot release the new request's button.
  requests[3].resolve(response({overview: "暂无相关素材", steps: [], limitations: ["先添加内容"]})); await fresh;
  assert.ok(ctx.$("#learningResult").innerHTML.includes("添加相关内容"));
  const previousOwner = ctx.generateLearningPlan(event);
  ctx.storageOwner = "user:two";
  requests[4].resolve(response({overview: "账号一的内容", steps: []})); await previousOwner;
  assert.equal(ctx.$("#learningResult").innerHTML, "");
  ctx.privacyToken = "unlocked";
  const privatePlan = ctx.generateLearningPlan(event);
  ctx.privacyToken = null;
  requests[5].resolve(response({overview: "已隐藏的来源", steps: []})); await privatePlan;
  assert.equal(ctx.$("#learningResult").innerHTML, "");
  console.log("PASS learning plan explicit generation, deduplication, source escaping, failures and account/modal/privacy isolation");
}
learningPlans().catch(error => { console.error(error); process.exitCode = 1; });

async function learningSourceNavigation() {
  const requests = [], messages = [];
  const ctx = pageContext({
    state: {jobs: []}, query: '', watchOpenEpoch: 0, storageOwner: "user:one", watching: null, casting: null,
    document: {body: {style: {}}}, ACTIVE: [],
    esc: String, hl: String, category: () => null, phone: () => "", ago: () => "刚刚",
    episodeOf: () => "", titleOf: j => j.title, hitList: () => "",
    stopMedia() {}, updateWatchStatus() {}, loadSimilar() {}, loadPointTimes() {}, renderCastNotice() {},
    hide(el) { el.hidden = true; }, toast(message) { messages.push(message); },
    fetch(url) { return new Promise(resolve => requests.push({url, resolve})); },
  });
  const select = ctx.$;
  ctx.$ = key => key === "#watchBody video, #watchBody audio" ? null : select(key);
  vm.runInContext(html.slice(html.indexOf("function waitingDownloadRetry("), html.indexOf("\nfunction card(")), ctx);
  vm.runInContext(html.slice(html.indexOf("async function openWatch("), html.indexOf("async function loadPointTimes(")), ctx);
  vm.runInContext(html.match(/^function openLearningSource\(.*$/m)[0], ctx);
  const job = id => ({id, title: `课程 ${id}`, url: "https://example.invalid/video", created: 1, status: "done", analysis: {}, media: []});
  const response = jobs => ({ok: true, json: async () => ({jobs})});

  ctx.$("#watch").hidden = true;
  ctx.openLearningSource(7);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/jobs?only=1&ids=7");
  // A background refresh may load the same source before its detail request returns.
  ctx.state.jobs = [job(2), {...job(7), compact: true}];
  requests[0].resolve(response([job(7)]));
  await new Promise(setImmediate);
  assert.equal(ctx.$("#learningSheet").hidden, true);
  assert.equal(ctx.$("#watch").hidden, false);
  assert.equal(ctx.watching.id, 7);
  assert.equal(ctx.state.jobs.filter(j => j.id === 7).length, 1);
  assert.equal(ctx.state.jobs.find(j => j.id === 7).compact, undefined);
  assert.ok(ctx.$("#watchBody").innerHTML.includes("课程 7"));

  const missing = ctx.openWatch(8);
  requests[1].resolve(response([])); await missing;
  assert.equal(ctx.watching.id, 7);
  assert.equal(messages.pop(), "这个视频不见了");

  const failed = ctx.openWatch(9);
  requests[2].resolve({ok: false}); await failed;
  assert.equal(messages.pop(), "视频详情加载失败，请重试");

  const stale = ctx.openWatch(10);
  ctx.storageOwner = "user:two"; ctx.state.jobs = []; ctx.watching = null;
  requests[3].resolve(response([job(10)])); await stale;
  assert.equal(ctx.state.jobs.length, 0);
  assert.equal(ctx.watching, null);

  const first = ctx.openWatch(11), second = ctx.openWatch(12);
  requests[5].resolve(response([job(12)])); await second;
  requests[4].resolve(response([job(11)])); await first;
  assert.equal(ctx.watching.id, 12);
  assert.deepEqual(Array.from(ctx.state.jobs, j => j.id), [12]);
  assert.equal(messages.length, 0);
  console.log("PASS unloaded learning sources open by ID, deduplicate refreshes and reject missing/failed/stale responses");
}
learningSourceNavigation().catch(error => { console.error(error); process.exitCode = 1; });
