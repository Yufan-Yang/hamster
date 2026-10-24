// Opening markers belong to this owner, video, part and privacy epoch.
let introState = null, introEpoch = 0;
function stopIntro() {
  ++introEpoch;
  if (introState) { clearTimeout(introState.timer); clearInterval(introState.tick); introState.controller?.abort(); }
  introState = null;
  const box = $('#introPanel'); if (box) { box.innerHTML = ''; box.hidden = true; }
}
function currentIntro(s) {
  return introState === s && s.epoch === introEpoch && s.owner === storageOwner
    && s.privacy === (typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch)
    && watching?.id === s.id && watching?.part === s.part;
}
function validIntro(marker, duration = 0) {
  return marker?.status === 'ready' && typeof marker.start === 'number' && typeof marker.end === 'number'
    && Number.isFinite(marker.start) && Number.isFinite(marker.end) && marker.start >= 0 && marker.start <= 180
    && marker.end - marker.start >= 10 && marker.end - marker.start <= 180 && marker.end <= 300
    && (!(Number.isFinite(duration) && duration > 0) || marker.end < duration - 1);
}
function renderIntro() {
  const box = $('#introPanel'), s = introState;
  if (!box || !s || !currentIntro(s)) return;
  const hit = validIntro(s.marker, s.duration), label = s.skipped ? '已跳过片头' : hit ?
    (s.marker.automatic ? '已识别片头' : '发现疑似片头，可手动跳过') : ({pending:'正在识别片头…',busy:'等待识别片头…',
      not_found:'暂未识别到片头',unavailable:'暂时无法识别片头',unknown:'尚未识别片头'})[s.status] || '尚未识别片头';
  box.innerHTML = `<label><input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleIntroAuto(this.checked)"> 自动跳过片头</label>
    <span class="hint" role="status">${esc(label)}${hit ? ' · ' + dur(s.marker.start) + '–' + dur(s.marker.end) : ''}</span>
    ${hit ? `<button id="introSkip" class="link" onclick="skipIntro(false)">跳过片头</button><button class="link" onclick="replayIntro()">回看片头</button>`
      : !s.enabled && s.status === 'unknown' ? '<button class="link" onclick="requestIntro(true)">识别片头</button>' : ''}`;
}
function setupIntro(job, part, media, {explicit = false} = {}) {
  stopIntro();
  if (!media || media.audio || job.analysis?.library !== 'TV' || !job.analysis?.show) return;
  const box = $('#introPanel'); if (!box) return;
  let enabled = true;
  try { enabled = localStorage.getItem('introAuto:' + storageOwner) !== '0'; } catch (_) {}
  const s = introState = {epoch:introEpoch, owner:storageOwner, privacy:typeof privateViewEpoch === 'undefined' ? 0 : privateViewEpoch,
    id:job.id, part, duration:Number(media.duration) || 0, enabled, consumed:explicit, marker:null, status:'unknown', attempts:0};
  box.hidden = false;
  const el = $('#watchBody video');
  if (el) {
    el.addEventListener('play', () => { if (currentIntro(s)) { s.played = true; tickIntro(); } });
    el.addEventListener('timeupdate', tickIntro);
    el.addEventListener('loadedmetadata', () => { if (currentIntro(s)) { s.duration = el.duration; tickIntro(); } });
    el.addEventListener('seeking', () => { if (currentIntro(s) && s.played && !s.skipped) s.consumed = true; });
  }
  s.tick = setInterval(tickIntro, 1000);
  renderIntro();
  requestIntro(enabled);
}
async function requestIntro(start = false) {
  const s = introState;
  if (!s || !currentIntro(s) || s.loading) return;
  if (start) s.requested = true;
  s.loading = true; const controller = s.controller = new AbortController();
  try {
    const response = await fetch(`/api/intro/${s.id}?part=${s.part}`, {method:start ? 'POST' : 'GET', signal:controller.signal});
    const data = await response.json();
    if (!currentIntro(s)) return;
    if (!response.ok) { s.status = 'unavailable'; s.marker = null; renderIntro(); return; }
    s.status = data.status;
    s.marker = validIntro(data, s.duration) ? data : null;
    if (s.marker) {
      renderIntro(); tickIntro();
      if (data.method === 'chapter' && typeof loadPointTimes === 'function') loadPointTimes(s.id);
    }
    else {
      renderIntro();
      if ((s.enabled || s.requested) && ['pending','busy','unknown'].includes(data.status) && ++s.attempts < 40)
        s.timer = setTimeout(() => requestIntro(data.status !== 'pending'), 3000);
    }
  } catch (e) { if (currentIntro(s) && e.name !== 'AbortError') { s.status = 'unavailable'; renderIntro(); } }
  finally { if (introState === s) s.loading = false; }
}
function toggleIntroAuto(value) {
  const s = introState; if (!s || !currentIntro(s)) return;
  s.enabled = !!value;
  try { localStorage.setItem('introAuto:' + s.owner, value ? '1' : '0'); } catch (_) {}
  if (value) { if (!s.marker) requestIntro(true); else tickIntro(); }
  else { s.requested = false; clearTimeout(s.timer); }
  renderIntro();
}
function tickIntro() {
  const s = introState;
  if (!s || !currentIntro(s)) return;
  const c = typeof casting === 'undefined' ? null : casting;
  const el = $('#watchBody video');
  const remote = c && c.id === s.id && c.part === s.part;
  const at = remote ? c.position : !c ? el?.currentTime : null;
  const button = $('#introSkip'); if (button && s.marker && Number.isFinite(at)) button.hidden = at >= s.marker.end;
  if (!s.enabled || s.consumed || s.marker?.automatic !== true) return;
  if (c && (!remote || !c.ready || c.pending || c.seeking || c.state !== 'PLAYING')) return;
  if (!c && (!el || el.paused || el.readyState < 1)) return;
  const position = remote ? c.position : el.currentTime;
  const duration = remote ? c.duration : el.duration;
  if (validIntro(s.marker, duration) && Number.isFinite(position) && position >= s.marker.start && position < s.marker.end - .5) skipIntro(true);
}
async function skipIntro(automatic = false) {
  const s = introState; if (!s || !currentIntro(s) || !validIntro(s.marker, s.duration)) return;
  if (automatic && (!s.enabled || s.consumed || s.marker.automatic !== true)) return;
  s.consumed = true; s.skipped = true;
  const c = typeof casting === 'undefined' ? null : casting;
  const el = $('#watchBody video');
  const position = c ? c.position : el?.currentTime;
  const duration = c ? c.duration : el?.duration;
  if (!validIntro(s.marker, duration) || Number.isFinite(position) && position >= s.marker.end) return;
  if (c) {
    if (c.id !== s.id || c.part !== s.part || !c.ready) { s.skipped = false; return; }
    try { await castSeek(c.device, s.marker.end); if (c.error) s.skipped = false; }
    catch (_) { s.skipped = false; }
  } else {
    if (!el || el.readyState < 1) { s.skipped = false; return; }
    el.currentTime = s.marker.end;
  }
  if (currentIntro(s)) renderIntro();
}
async function replayIntro() {
  const s = introState; if (!s || !currentIntro(s) || !validIntro(s.marker, s.duration)) return;
  s.consumed = true; s.skipped = false;
  const position = s.marker.method === 'audio' ? Math.max(0, s.marker.start - 3) : s.marker.start;
  const c = typeof casting === 'undefined' ? null : casting;
  if (c) { if (c.id === s.id && c.part === s.part && c.ready) await castSeek(c.device, position); }
  else { const el = $('#watchBody video'); if (el) el.currentTime = position; }
  if (currentIntro(s)) renderIntro();
}
