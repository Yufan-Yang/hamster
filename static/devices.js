'use strict';
const tvNode = id => document.getElementById(id);
let tvLoading = false, tvTimer = null, tvScreenURL = null, tvAction = false;
function tvBytes(n) {
  if (!Number.isFinite(n)) return '未知';
  return n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(1) + ' GB' : Math.round(n / 1024 ** 2) + ' MB';
}
function tvFact(label, value) {
  const dt = document.createElement('dt'), dd = document.createElement('dd');
  dt.textContent = label; dd.textContent = value || '未知';
  tvNode('facts').append(dt, dd);
}
function tvPackage(info) {
  if (!info || !info.installed) return '未安装';
  return (info.disabled ? '已停用' : info.stopped ? '未运行' : '已启用') + (info.version ? ' · ' + info.version : '');
}
function renderTV(data) {
  tvNode('deviceName').textContent = data.name;
  tvNode('address').textContent = data.host + ':' + data.port;
  tvNode('online').textContent = data.adb ? '已连接' : data.online ? '连接异常' : '离线';
  tvNode('online').className = 'badge ' + (data.adb ? 'online' : 'offline');
  tvNode('facts').replaceChildren();
  tvFact('系统', data.android ? 'Android ' + data.android + (data.system ? ' / EUI ' + data.system : '') : '无法读取');
  tvFact('ADB 连接', data.adb ? '正常' : '未连接');
  tvFact('乐播投屏', data.receiver_running ? '接收服务运行中' : tvPackage(data.packages['com.hpplay.happyplay.aw']));
  tvFact('内置投屏组件', tvPackage(data.packages['com.stv.smartControl']));
  const projection = data.projection || {};
  tvFact('DLNA 连接', projection.available ? projection.error || '正常 · ' + projection.name : '未发现接收服务');
  const states = {PLAYING:'正在播放', PAUSED_PLAYBACK:'已暂停', STOPPED:'已停止', NO_MEDIA_PRESENT:'未播放', TRANSITIONING:'正在准备播放'};
  tvFact('电视播放', projection.available ? states[projection.state] || '等待状态' : '未连接');
  tvFact('系统提示组件', !data.ads ? '无法读取' : data.ads.package_disabled ? '已被整体停用' : '保留温控及关机功能');
  tvFact('内存', data.memory ? '可用 ' + tvBytes(data.memory.available) + ' / 共 ' + tvBytes(data.memory.total) : '无法读取');
  tvFact('存储', data.storage ? '剩余 ' + tvBytes(data.storage.free) + ' / 共 ' + tvBytes(data.storage.total) : '无法读取');
  tvFact('运行时间', Number.isFinite(data.uptime) ? Math.floor(data.uptime / 3600) + ' 小时 ' + Math.floor(data.uptime % 3600 / 60) + ' 分钟' : '无法读取');
  tvFact('前台应用', data.foreground_app || '无法读取');
  tvNode('updated').textContent = '检查时间：' + new Date(data.checked_at * 1000).toLocaleString('zh-CN', {hour12:false});
  tvNode('screenshot').hidden = !data.admin; tvNode('screenshot').disabled = !data.adb;
  tvNode('restoreReceiver').hidden = !data.admin; tvNode('restoreReceiver').disabled = !data.adb;
  tvNode('receiverHint').hidden = !data.admin;
  tvNode('adminHint').hidden = data.admin;
  const ads = data.ads || {}, items = ads.items || [];
  tvNode('adItems').replaceChildren();
  for (const item of items) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = item.name; dd.textContent = item.blocked ? '已屏蔽' : '未屏蔽'; tvNode('adItems').append(dt, dd);
  }
  tvNode('adSummary').textContent = items.length ? '已屏蔽 ' + items.filter(item => item.blocked).length + ' / ' + items.length + ' 项广告组件' : '暂时无法读取广告设置';
  tvNode('blockAds').hidden = !data.admin; tvNode('restoreAds').hidden = !data.admin || !ads.can_restore;
  tvNode('adPermission').hidden = !ads.reason; tvNode('adPermission').textContent = ads.reason || '';
  tvNode('blockAds').disabled = !data.adb || tvAction || ads.can_manage === false;
  tvNode('restoreAds').disabled = !data.adb || tvAction || ads.can_manage === false;
}
async function restoreTVReceiver() {
  if (tvAction) return; tvAction = true; clearTimeout(tvTimer);
  const button = tvNode('restoreReceiver'); button.disabled = true; button.textContent = '正在恢复…';
  tvNode('message').className = ''; tvNode('message').textContent = '正在重新开启电视投屏，电视上会显示网络设置…';
  try {
    const response = await fetch('/api/tv/receiver/start', {method:'POST'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '恢复失败');
    await refreshTV(true);
    tvNode('message').className = ''; tvNode('message').textContent = '已恢复投屏：' + data.name;
  } catch (error) { tvNode('message').textContent = error.message; tvNode('message').className = 'error'; }
  finally { tvAction = false; button.disabled = false; button.textContent = '恢复投屏'; refreshTV(true, true); }
}
async function changeTVAds(action) {
  if (tvAction) return; tvAction = true; clearTimeout(tvTimer);
  tvNode('blockAds').disabled = tvNode('restoreAds').disabled = true;
  tvNode('message').className = ''; tvNode('message').textContent = action === 'block' ? '正在屏蔽广告组件…' : '正在恢复广告组件…';
  try {
    const response = await fetch('/api/tv/ads', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
    const data = await response.json(); if (!response.ok) throw Error(data.error || '操作失败');
    tvNode('message').textContent = action === 'block' ? '广告专用组件已屏蔽，恢复记录已保存。' : '广告组件已恢复。';
  } catch (error) { tvNode('message').textContent = error.message; tvNode('message').className = 'error'; }
  finally { tvAction = false; refreshTV(true, true); }
}
async function refreshTV(force = false, keepMessage = false) {
  if (tvLoading || tvAction) return;
  tvLoading = true; clearTimeout(tvTimer);
  tvNode('refresh').disabled = true; tvNode('refresh').textContent = '正在刷新…';
  try {
    const response = await fetch('/api/tv/status' + (force ? '?refresh=1' : ''), {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '无法获取设备状态');
    renderTV(data);
    if (data.error || !keepMessage) { tvNode('message').textContent = data.error || ''; tvNode('message').className = data.error ? 'error' : ''; }
  } catch (error) { tvNode('message').textContent = error.message || '连接中断，请重试'; tvNode('message').className = 'error'; }
  finally {
    tvLoading = false; tvNode('refresh').disabled = false; tvNode('refresh').textContent = '刷新状态';
    if (!document.hidden) tvTimer = setTimeout(() => refreshTV(), 15000);
  }
}
async function captureTV() {
  if (tvAction) return; tvAction = true; clearTimeout(tvTimer);
  tvNode('screenshot').disabled = true;
  try {
    const response = await fetch('/api/tv/screenshot', {cache:'no-store'});
    if (!response.ok) { const data = await response.json(); throw Error(data.error || '截图失败'); }
    const blob = await response.blob();
    if (tvScreenURL) URL.revokeObjectURL(tvScreenURL);
    tvScreenURL = URL.createObjectURL(blob); tvNode('screen').src = tvScreenURL;
    tvNode('screenPanel').hidden = false; tvNode('message').textContent = '';
  } catch (error) { tvNode('message').textContent = error.message; tvNode('message').className = 'error'; }
  finally { tvAction = false; tvNode('screenshot').disabled = false; if (!document.hidden) tvTimer = setTimeout(() => refreshTV(), 15000); }
}
tvNode('refresh').addEventListener('click', () => refreshTV(true));
tvNode('screenshot').addEventListener('click', captureTV);
tvNode('restoreReceiver').addEventListener('click', restoreTVReceiver);
tvNode('blockAds').addEventListener('click', () => changeTVAds('block'));
tvNode('restoreAds').addEventListener('click', () => changeTVAds('restore'));
tvNode('closeScreen').addEventListener('click', () => {
  tvNode('screenPanel').hidden = true; tvNode('screen').removeAttribute('src');
  if (tvScreenURL) URL.revokeObjectURL(tvScreenURL); tvScreenURL = null;
});
document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(tvTimer); else refreshTV(); });
refreshTV();
