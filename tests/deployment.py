#!/usr/bin/env python3
"""Offline release snapshots, integrity and live-readback checks."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'pi/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release@example.invalid')
        (self.root / 'index.html').write_text('<html>original</html>')
        (self.root / 'grabber.py').write_text('value = 1\n')
        (self.root / '.gitignore').write_text('grabber.env\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.archive = Path(self.temp.name) / 'release.tgz'

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root).decode().strip()

    def unpack(self):
        out = Path(self.temp.name) / 'unpacked'
        out.mkdir(exist_ok=True)
        with tarfile.open(self.archive) as archive:
            archive.extractall(out)
        return out

    def test_dirty_snapshot_keeps_user_head_index_and_excludes_secrets(self):
        original = self.git('rev-parse', 'HEAD')
        (self.root / 'grabber.py').write_text('value = 2\n')
        self.git('add', 'grabber.py')
        staged = self.git('diff', '--cached')
        (self.root / 'grabber.py').write_text('value = 3\n')
        (self.root / 'grabber.env').write_text('SECRET=never-archive')
        (self.root / 'shiguang').mkdir()
        (self.root / 'shiguang/new.py').write_text('new = True\n')
        revision = release.pack(self.archive, root=self.root)
        self.assertNotEqual(revision, original)
        self.assertEqual(self.git('rev-parse', 'HEAD'), original)
        self.assertEqual(self.git('diff', '--cached'), staged)
        self.assertEqual((self.root / 'grabber.py').read_text(), 'value = 3\n')
        out = self.unpack()
        self.assertEqual((out / 'grabber.py').read_text(), 'value = 3\n')
        self.assertTrue((out / 'shiguang/new.py').is_file())
        self.assertGreater((out / 'index.html').stat().st_mtime, 0)
        self.assertFalse((out / 'grabber.env').exists())
        self.assertEqual(release.verify(out)['revision'], revision)
        self.assertEqual(self.git('rev-parse', f'refs/shiguang/releases/{revision}'), revision)

    def test_clean_snapshot_uses_existing_commit_and_detects_tampering(self):
        self.assertEqual(release.pack(self.archive, root=self.root), self.git('rev-parse', 'HEAD'))
        out = self.unpack()
        (out / 'index.html').write_text('wrong version')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            release.verify(out)

    def test_symbolic_links_cannot_enter_release(self):
        (self.root / 'static').mkdir()
        (self.root / 'static/unsafe').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'non-regular'):
            release.pack(self.archive, root=self.root)

    def test_deleted_top_level_path_is_not_resurrected_from_head(self):
        (self.root / 'static').mkdir()
        (self.root / 'static/obsolete.js').write_text('old')
        self.git('add', '.')
        self.git('commit', '-qm', 'old static assets')
        (self.root / 'static/obsolete.js').unlink()
        (self.root / 'static').rmdir()
        release.pack(self.archive, root=self.root)
        with tarfile.open(self.archive) as archive:
            self.assertNotIn('static/obsolete.js', archive.getnames())

    def test_receipt_requires_fresh_worker_not_old_online_row(self):
        release.pack(self.archive, root=self.root)
        out = self.unpack()
        seen = [9]
        def fetch(url, **kwargs):
            content = b'<html>page</html>' if url.endswith('/') else json.dumps({
                'workers': [{'name': 'pi-cpu', 'online': True, 'seen': seen[0]}]}).encode()
            return io.BytesIO(content)
        with patch.object(release.urllib.request, 'urlopen', side_effect=fetch):
            with self.assertRaisesRegex(ValueError, 'fresh heartbeat'):
                release.health(out, 10)
            seen[0] = 11
            result = release.health(out, 10)
        self.assertIn('fresh_pi_cpu', result['checks'])
        self.assertEqual(result['revision'], (out / 'REVISION').read_text().strip())

    def test_empty_http_200_is_not_accepted_as_a_working_page(self):
        release.pack(self.archive, root=self.root)
        out = self.unpack()
        with patch.object(release.urllib.request, 'urlopen', return_value=io.BytesIO(b'')):
            with self.assertRaisesRegex(ValueError, 'did not return HTML'):
                release.health(out, 0)


if __name__ == '__main__':
    unittest.main()
