"""Browser regression: a lazy player below the fold inside an iframe."""
import threading
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from shiguang import download


def run(check):
    def lazy_player():
        requested = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requested.append(self.path)
                if self.path == '/':
                    body = '<iframe src="/frame" width="800" height="600"></iframe>'
                elif self.path == '/api-page':
                    body = '<title>Public &amp; event</title><div class="dplayer" data-video-id="123" data-player-index="0"></div><script src="/usr/plugins/DPlayer/assets/player.js"></script>'
                elif self.path.startswith('/play-url/'):
                    assert self.headers.get('Referer') == f'http://127.0.0.1:{self.server.server_port}/api-page'
                    assert 'fixture=1' in self.headers.get('Cookie', '')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'code': 0, 'data': {'url': '/main/index.m3u8', 'ads': [{'url':'/ad.m3u8'}]}}).encode())
                    return
                elif self.path == '/frame':
                    body = """<div style="height:2400px"></div>
                    <div class="dplayer" data-video-id="123" style="width:600px;height:320px;background:black;cursor:pointer">Play</div>
                    <script>document.querySelector('.dplayer').onclick=()=>fetch('/main/'+'index.'+'m3u8');</script>"""
                elif self.path == '/main/index.m3u8':
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                    self.end_headers()
                    self.wfile.write(b'#EXTM3U\n#EXT-X-ENDLIST\n')
                    return
                else:
                    body = ''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Set-Cookie', 'fixture=1; Path=/')
                self.end_headers()
                self.wfile.write(body.encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        found = None
        try:
            with patch.object(download, 'update'):
                found = download.sniff_page(0, f'http://127.0.0.1:{server.server_port}/')
            assert '/main/index.m3u8' in requested, 'The player must actually be activated'
            assert found and found['media_urls'] == [f'http://127.0.0.1:{server.server_port}/main/index.m3u8']
            Path(found['cookiefile']).unlink(missing_ok=True)
            with patch.object(download, 'update'), patch.object(download, 'heavy_slot', side_effect=AssertionError('Public API should not require a browser')):
                found = download.sniff_page(0, f'http://127.0.0.1:{server.server_port}/api-page')
            assert found['title'] == 'Public & event'
            assert found['media_urls'] == [f'http://127.0.0.1:{server.server_port}/main/index.m3u8']
            assert 'fixture' in Path(found['cookiefile']).read_text()
        finally:
            if found:
                Path(found['cookiefile']).unlink(missing_ok=True)
            server.shutdown();server.server_close();thread.join(timeout=5)

    check('embedded downloads: activate lazy players below the fold inside frames', lazy_player)
