"""Deletion stays independent of repeated whole-library path checks; synthetic files only."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trash_review as fixtures  # noqa: E402
from shiguang import core, pipeline, trash  # noqa: E402


class TrashProtectionTests(unittest.TestCase):
    setUp = fixtures.TrashReviewTests.setUp
    file = fixtures.TrashReviewTests.file
    job = fixtures.TrashReviewTests.job
    trash_id = fixtures.TrashReviewTests.trash_id

    def test_index_preserves_directory_and_video_sidecar_rules(self):
        protected = [self.file('shared'), self.root / 'Videos' / 'protected-folder',
                     self.file('nested/another')]
        protected = [path.resolve() for path in protected]
        index = trash._FileProtection([str(path) for path in protected])
        candidates = [self.root, self.root / 'Videos', self.root / 'Videos' / 'Other',
                      protected[1] / 'child.mp4', self.root / 'Videos' / 'unrelated.mp4']
        candidates += [self.root / 'Videos' / 'Other' / name for name in
                       ('shared.mp4', 'shared.zh.srt', 'shared.jpg', 'shared2.mp4', 'shared-extra.srt')]
        for path in candidates:
            path = path.resolve()
            with self.subTest(path=path.name):
                overlaps = any(path == value or value in path.parents or path in value.parents
                               for value in protected)
                shared = overlaps or any(path.parent == value.parent and value.suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT
                                         and path.name.startswith(value.stem + '.') for value in protected)
                self.assertEqual(index.overlaps(path), overlaps)
                self.assertEqual(index.shared(path), shared)

    def test_symlink_alias_protects_another_owners_media(self):
        video = self.file('protected')
        alias = self.root / 'alias.mp4'
        alias.symlink_to(video)
        a = self.job(title='first', key='first', files=json.dumps([str(video)]))
        self.job(title='second', key='second', owner='bob', files=json.dumps([str(alias)]))
        pipeline.remove_job(a)
        result = trash.purge(self.trash_id(a, self.owner), self.owner)
        self.assertTrue(result['kept_shared'])
        self.assertEqual(result['files'], 0)
        self.assertTrue(video.exists())

    def test_library_references_are_not_resolved_again_during_file_deletion(self):
        references = [str(self.root / 'Videos' / 'Other' / f'keep-{i}.mp4') for i in range(250)]
        self.job(title='others', key='others', owner='bob', files=json.dumps(references))
        video = self.file('delete')
        a = self.job(title='delete', key='delete', files=json.dumps([str(video)]))
        pipeline.remove_job(a)
        with patch.object(trash, '_safe_path', wraps=trash._safe_path) as resolve:
            result = trash.purge(self.trash_id(a, self.owner), self.owner)
        calls = [str(call.args[0]) for call in resolve.call_args_list]
        # Real purge and unlink must share their safety index, rather than
        # re-reading every referenced filesystem path for each target file.
        self.assertTrue(all(calls.count(value) == 1 for value in references))
        self.assertEqual(result['files'], 1)
        self.assertFalse(video.exists())


if __name__ == '__main__':
    unittest.main()
