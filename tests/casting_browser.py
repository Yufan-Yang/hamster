#!/usr/bin/env python3
"""Real Chromium DOM regression tests, with ALL network/media/TV requests mocked.

Run: /opt/grabber/venv/bin/python tests/casting_browser.py [app directory]
Requires chromium and websockets; never contacts a real TV or changes the database.
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


MOCKS = r"""
window.requests = []; window.localPlays = 0; window.failStart = false;
window.browserErrors = []; window.addEventListener('error', e => browserErrors.push(e.message));
window.tv = {position: 20, duration: 600, state: 'PLAYING', volume: 50};
window.fixtureJobs = [1,2,3].map(id => ({id, title: '测试视频 ' + id, url: 'https://example.invalid/video',
  created: 1, status: 'done', analysis: {}, media: [0,1].map(part => ({name: '分集 ' + part,
  src: '/mock-media/' + id + '/' + part, duration: 600, audio: id === 3,
  subs: ['en', 'zh'], subsrc: ['/mock-sub/en', '/mock-sub/zh'], bilingual: part === 0 ? '/mock-sub/bi' : null}))}));
HTMLMediaElement.prototype.play = function() { window.localPlays++; return Promise.resolve(); };
window.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({url, body});
  let result = {};
  if (url.startsWith('/api/jobs')) result = {jobs: fixtureJobs, user: 'test', admin: false};
  if (url.startsWith('/api/points')) result = {chapters: {'0': [{t: 123, title: '测试章节'}]}};
  if (url === '/api/cast/start') {
    if (window.holdStart) { const hold = window.holdStart; window.holdStart = null; await hold; }
    if (failStart) result = {error: '模拟电视离线'};
    else { tv.position = body.position; tv.state = 'PLAYING'; result = {name: '客厅电视'}; }
  }
  if (url.startsWith('/api/cast/status')) {
    result = {...tv};
    if (window.holdStatus) { const hold = window.holdStatus; window.holdStatus = null; await hold; }
  }
  if (url === '/api/cast/control') {
    if (body.action === 'seek') tv.position = body.position;
    if (body.action === 'pause') tv.state = 'PAUSED_PLAYBACK';
    if (body.action === 'play') tv.state = 'PLAYING';
    if (body.action === 'stop') tv.state = 'STOPPED';
  }
  return new Response(JSON.stringify(result), {status: result.error ? 500 : 200});
};
"""

TESTS = r"""
(async () => {
  const passed = [];
  const assert = (ok, name) => { if (!ok) throw new Error(name); };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const settle = async () => { await wait(20); await castQueue; await wait(20); };
  const calls = action => requests.filter(r => r.url === '/api/cast/' + action);
  const player = () => $('#watchBody video, #watchBody audio');
  const silent = name => assert(player().paused && !player().autoplay && !player().controls && !localPlays, name);

  await wait(30);
  openWatch(1);
  assert([...player().querySelectorAll('track')].every(t => !t.default) && [...player().textTracks].every(t => t.mode === 'disabled'), 'subtitles off by default');
  player().textTracks[0].mode = 'showing';
  assert(player().textTracks[0].mode === 'showing', 'subtitles can be enabled manually');
  passed.push('subtitles default off and manual selection available');
  await startCast(1, 0, 'tv'); await settle();
  silent('first cast pauses phone');
  assert(!$('#castNotice').hidden && $('#watchCastNotice').textContent.includes('客厅电视'), 'visible device status');
  $('#chapters button').click(); await settle();
  assert(calls('control').length, 'chapter click produced no command: ' + JSON.stringify({browserErrors, casting, requests}));
  assert(calls('control').at(-1).body.action === 'seek' && calls('control').at(-1).body.position === 123, 'chapter seeks TV');
  silent('chapter must not play phone'); passed.push('chapter click controls TV only');

  openWatch(2, 1, 80); await settle();
  assert(calls('start').at(-1).body.id === 2 && calls('start').at(-1).body.part === 1 && calls('start').at(-1).body.position === 80, 'switch retains TV and position');
  silent('switch video stays silent');
  const count = calls('start').length;
  closeWatch(); await wait(210);
  assert(casting && castTimer && !$('#castNotice').hidden, 'closing page preserves casting');
  openWatch(2); await settle();
  assert(calls('start').length === count && watching.part === 1, 'reopen same remote video does not restart');
  openWatch(3); await settle(); silent('audio also stays silent');
  assert(calls('start').at(-1).body.device === 'tv', 'audio retains target');
  passed.push('video/part/audio switching and closing/reopening retain device');

  await castControl('tv', 'pause');
  assert($('#castNotice').textContent.includes('已暂停'), 'pause label');
  tv.position = 145; await castStatus('tv'); saveWatch();
  assert(requests.filter(r => r.url === '/api/watch/3').at(-1).body.pos === 145, 'history uses TV clock');
  passed.push('pause status and remote watch history');

  await settle();
  window.holdStatus = new Promise(resolve => window.releaseStatus = resolve);
  const stale = castStatus('tv'); await wait(10);
  openWatch(1, 0, 70); await settle();
  releaseStatus(); await stale;
  assert(casting.id === 1 && casting.position === 70, 'stale status cannot overwrite new video');
  passed.push('stale status is ignored');

  window.holdStart = new Promise(resolve => window.releaseStart = resolve);
  openWatch(2, 0, 10); await wait(20);
  seekTo(2, 0, 99); releaseStart(); await settle();
  assert(tv.position === 99 && calls('control').at(-1).body.position === 99, 'seek waits for pending start');
  silent('pending seek stays silent');
  window.holdStart = new Promise(resolve => window.releaseStart = resolve);
  openWatch(1, 0, 11); await wait(20);
  openWatch(2, 0, 22); openWatch(3, 0, 33); releaseStart(); await settle();
  assert(calls('start').at(-1).body.id === 3 && tv.position === 33 && casting.id === 3, 'rapid switches end on latest video');
  passed.push('pending seek and rapid switches are serialized');

  failStart = true; openWatch(1, 0, 0); await settle();
  assert($('#castNotice').textContent.includes('异常') && $('#castNotice').textContent.includes('重试'), 'connection failure visible');
  silent('failure cannot start phone');
  failStart = false; await startCast(1, 0, 'tv', 0); await settle();
  assert(!casting.error, 'retry recovers');
  await castControl('tv', 'stop');
  assert(!casting && !castTimer && $('#castNotice').hidden && $('#castOverlay').hidden && player().controls, 'stop clears session and restores controls');
  seekTo(1, 0, 20); assert(localPlays === 1, 'local seek resumes only after stop');
  openWatch(2); assert(player().autoplay, 'next video local after stop');
  passed.push('failure/retry/stop/local playback');
  clearTimeout(pollTimer);
  return passed;
})()
"""


def main():
    app = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    with tempfile.TemporaryDirectory(prefix="shiguang-cast-browser-") as profile:
        process = subprocess.Popen(["chromium", "--headless", "--no-sandbox", "--disable-gpu",
                                    "--disable-background-networking", "--disable-extensions", "--remote-debugging-port=0",
                                    "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = Path(profile) / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                time.sleep(.1)
            port = port_file.read_text().splitlines()[0]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as response:
                target = next(t["webSocketDebuggerUrl"] for t in json.load(response) if t["type"] == "page")
            with connect(target, max_size=8 * 1024 * 1024) as ws:
                sequence = 0

                def call(method, params=None):
                    nonlocal sequence
                    sequence += 1
                    ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                    while True:
                        r = json.loads(ws.recv(timeout=45))
                        if r.get("id") == sequence:
                            if r.get("error") or r.get("result", {}).get("exceptionDetails"):
                                raise AssertionError(r)
                            return r["result"]

                call("Network.enable")
                call("Emulation.setScriptExecutionDisabled", {"value": False})
                call("Network.setBlockedURLs", {"urls": ["http://*", "https://*"]})
                call("Page.enable")
                call("Page.addScriptToEvaluateOnNewDocument", {"source": MOCKS})
                call("Page.navigate", {"url": (app / "index.html").resolve().as_uri()})
                for _ in range(100):
                    loaded = call("Runtime.evaluate", {"expression": "typeof openWatch === 'function' && document.readyState === 'complete'"})
                    if loaded["result"].get("value"):
                        break
                    time.sleep(.1)
                if not loaded["result"].get("value"):
                    raise AssertionError(call("Runtime.evaluate", {"expression": "JSON.stringify({url:location.href,body:document.body.innerText.slice(0,300),errors:window.browserErrors})"}))
                result = call("Runtime.evaluate", {"expression": TESTS, "awaitPromise": True, "returnByValue": True})
                for name in result["result"]["value"]:
                    print("ok   ", name)
                call("Browser.close")
                process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
