// Run with node tests/series.js [optional real-jobs JSON]. No browser or dependencies required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
const context = vm.createContext({state: {subs: [{id: 1, platform: "bangumi", name: "动画 第一季"}]}});
vm.runInContext(html.slice(html.indexOf("function titleOf("), html.indexOf("function toggleSeries(")), context);
const job = (title, analysis = {}, source = "sub:10") => ({title, analysis, source, url: "https://example.test/video"});

for (const [title, name] of [
  ["三国杀up锐评新三国290：我挥剑只有一次", "三国杀up锐评新三国"],
  ["三国杀up锐评新三国289: 有天兵天将相助", "三国杀up锐评新三国"],
  ["新三国up锐评老三国２３：打你，便是打那吕布！", "新三国up锐评老三国"],
  ["新三国up锐评神探狄仁杰21：反正不像你那么身轻如燕！", "新三国up锐评神探狄仁杰"],
  ["三国杀up锐评新三国re07：关羽的替身又增加了", "三国杀up锐评新三国re"],
  ["三国杀up锐评新三国RE06：恭喜，新手教程已完结", "三国杀up锐评新三国re"],
  ["动画 第23集 慕兰之战", "动画"],
  ["动画S02E03：开始", "动画"],
  ["动画EP03：开始", "动画"],
  ["动画E03：开始", "动画"],
  ["动画 23", "动画"],
  ["动画23：开始", "动画"],
  ["当折棒在三国杀开启新三国问答", ""],
  ["新三国up锐评三国争洛阳：比星三国英雄十倍", ""],
  ["视频里有290个人", ""],
  ["2026：年度回顾", ""],
]) assert.equal(context.seriesOf(job(title)), name, title);

assert.equal(context.seriesOf(job("AI改写后的标题", {title: "简介", library: "Videos"})), "");
assert.equal(context.seriesOf(job("动画23：开始", {title: "AI改写后的标题", library: "Videos"})), "动画");
assert.equal(context.seriesOf(job("不同标题", {library: "TV", show: "动画 第二季"})), "动画");
assert.equal(context.seriesOf(job("不同标题", {}, "sub:1")), "动画");
assert.equal(context.seriesOf(job("电影23：副标题", {library: "Movies"})), "");

const items = context.seriesItems([
  job("三国杀up锐评新三国290：正文"), job("独立视频"),
  job("三国杀up锐评新三国289：正文"), job("三国杀up锐评新三国re07：正文"),
  job("三国杀up锐评新三国re06：正文"), job("新三国up锐评老三国23：正文"),
]);
assert.equal(items.length, 4);
assert.equal(items[0].group.jobs.length, 2);
assert.equal(items[1].j.title, "独立视频");
assert.equal(items[2].group.name, "三国杀up锐评新三国re");
assert.equal(items[2].group.jobs.length, 2);
assert.equal(items[3].j.title, "新三国up锐评老三国23：正文");

if (process.argv[2]) {
  const jobs = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  for (const j of jobs) if (typeof j.expected_series === "string") {
    assert.equal(context.seriesOf(j), j.expected_series, `server/browser naming mismatch: ${j.id}`);
  }
  const groups = context.seriesItems(jobs).filter(x => x.group).map(x => x.group);
  for (const prefix of ["三国杀up锐评新三国", "三国杀up锐评新三国re", "新三国up锐评老三国", "新三国up锐评神探狄仁杰"]) {
    const expected = jobs.filter(j => j.title.startsWith(prefix) && /^\d+[：:]/.test(j.title.slice(prefix.length)));
    assert.ok(expected.length > 1, `missing real examples for ${prefix}`);
    const group = groups.find(g => g.name === prefix);
    assert.ok(group, `missing group ${prefix}`);
    for (const j of expected) assert.ok(group.jobs.some(g => g.id === j.id), `ungrouped real job ${j.id}`);
    console.log(`${prefix}: ${group.jobs.length} videos grouped`);
  }
}
console.log("Series grouping regression checks passed.");
