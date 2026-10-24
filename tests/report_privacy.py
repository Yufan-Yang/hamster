"""Cached digest / weekly output is revalidated against current privacy and ownership."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy_selection
from shiguang import core, trash, web, weekly


class ReportPrivacyTests(unittest.TestCase):
    job = privacy_selection.PrivacySelectionTests.job
    select = privacy_selection.PrivacySelectionTests.select

    def setUp(self):
        privacy_selection.PrivacySelectionTests.setUp(self)
        patcher = patch.object(trash, 'MEDIA', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        core.q("INSERT INTO subs(id,owner,platform,name) VALUES (1,?,'youtube','Private uploader')", (self.owner,))
        core.q("INSERT INTO subs(id,owner,platform,name) VALUES (2,?,'youtube','Public uploader')", (self.owner,))

    def cache(self, private, public):
        body = {'start': '2026-01-01', 'end': '2026-01-07', 'headline': 'PRIVATE headline', 'uploaders': [
            {'sub': 1, 'name': 'Private uploader', 'overview': 'PRIVATE overview',
             'videos': [{'job': private, 'title': 'PRIVATE title', 'line': 'PRIVATE line'}]},
            {'sub': 2, 'name': 'Public uploader', 'overview': 'PRIVATE cross-reference',
             'videos': [{'job': public, 'title': 'Public title', 'line': 'PRIVATE cross-reference'}]}]}
        core.q('INSERT INTO digests(owner,start,end,body,created) VALUES (?,?,?,?,?)',
               (self.owner, body['start'], body['end'], json.dumps(body), time.time()))

    def digest(self, headers=None):
        response = self.client.get('/api/digests', headers=headers or {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.get_json()['digests'][0]

    def archive(self, jid):
        self.assertEqual(trash.trash_job(jid, self.owner)[0], 'trashed')
        return core.q('SELECT id FROM job_trash WHERE job_id=?', (jid,), one=True)['id']

    def test_manual_hidden_cached_digest_stays_hidden_after_trash_and_purge(self):
        private = self.job(title='PRIVATE title', source='sub:1', files='[]')
        public = self.job(title='Public title', source='sub:2')
        self.cache(private, public)
        self.select([private])
        for operation in ('active', 'trash', 'purge'):
            if operation == 'trash':
                tid = self.archive(private)
            elif operation == 'purge':
                trash.purge(tid, self.owner)
            result = self.digest()
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertEqual([u['sub'] for u in result['uploaders']], [2])
            self.assertEqual(result['uploaders'][0]['videos'][0]['title'], 'Public title')

    def test_tag_hidden_archive_and_expired_reveal_do_not_leak(self):
        private = self.job(title='PRIVATE title', source='sub:1', analysis='{"tags":["hidden"]}', files='[]')
        public = self.job(title='Public title', source='sub:2')
        self.cache(private, public)
        web.privacy_set(self.owner, ['hidden'], [])
        self.tokens[self.token]['reveal'] = True
        self.assertEqual([u['sub'] for u in self.digest(self.headers)['uploaders']], [1])
        self.tokens[self.token]['expires'] = 0
        self.assertNotIn('PRIVATE', json.dumps(self.digest(self.headers)))
        self.archive(private)
        self.assertNotIn('PRIVATE', json.dumps(self.digest()))

    def test_foreign_and_missing_cached_ids_are_removed_and_aggregates_redacted(self):
        foreign = self.job(owner='someone-else', title='PRIVATE foreign')
        public = self.job(title='Public title', source='sub:2')
        self.cache(foreign, public)
        self.assertNotIn('PRIVATE', json.dumps(self.digest()))
        core.q('DELETE FROM jobs WHERE id=?', (foreign,))
        self.assertNotIn('PRIVATE', json.dumps(self.digest()))

    def test_unaffected_digest_keeps_prose_and_updates_current_titles(self):
        private = self.job(title='Renamed title', source='sub:1')
        public = self.job(title='Public title', source='sub:2')
        self.cache(private, public)
        result = self.digest()
        self.assertEqual(result['headline'], 'PRIVATE headline')
        self.assertEqual(result['uploaders'][0]['videos'][0]['title'], 'Renamed title')

    def test_past_and_current_week_exclude_archived_activity_and_cached_backlog(self):
        private = self.job(title='PRIVATE title', source='sub:1', files='[]')
        public = self.job(title='Public title', source='sub:2')
        this_week = weekly.week_start()
        previous = weekly.plus_days(this_week, -7)
        for stamp in (previous + 60, this_week + 60):
            for jid, seconds in ((private, 100), (public, 20)):
                core.q('INSERT INTO activity(owner,day,kind,ref,seconds,pct0,pct1) VALUES (?,?,?,?,?,0,1)',
                       (self.owner, weekly.day_str(stamp), 'video', jid, seconds))
        saved = weekly.report(self.owner, previous, this_week)
        core.q('INSERT INTO weekly(owner,start,end,body,created) VALUES (?,?,?,?,?)',
               (self.owner, weekly.day_str(previous), weekly.day_str(this_week - 1), json.dumps(saved), time.time()))
        self.select([private])
        tid = self.archive(private)
        for operation in ('trash', 'purge'):
            if operation == 'purge':
                trash.purge(tid, self.owner)
            response = self.client.get('/api/weekly')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            result = response.get_json()
            self.assertNotIn('PRIVATE title', json.dumps(result))
            for report in (result['current'], result['past'][0]):
                self.assertEqual(report['videos']['watched'], 1)
                self.assertEqual(report['videos']['seconds'], 20)
                self.assertEqual([v['id'] for v in report['videos']['top']], [public])
            if operation == 'trash':
                self.assertNotIn('Private uploader', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
