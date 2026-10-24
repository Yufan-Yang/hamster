"""Original platform membership is owned metadata, independent of downloads."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from flask import g

spec = importlib.util.spec_from_file_location('native_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, native_series as native, web  # noqa: E402


class NativeSeriesTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def subscription(self, owner='user:alice', key='bilibili:123'):
        platform, ident = key.split(':', 1)
        url = f'https://space.bilibili.com/{ident}/upload/video' if platform == 'bilibili' else f'https://www.youtube.com/{ident}/videos'
        core.q('INSERT INTO subs(owner,platform,key,url,name,created,seen) VALUES(?,?,?,?,?,?,?)',
               (owner, platform, key, url, 'Creator', time.time(), '[]'))
        return dict(core.q('SELECT * FROM subs WHERE owner=? AND key=?', (owner, key), one=True))

    def member(self, sub, key='bilibili:BV1234567890:p1'):
        jid = self.job(sub['owner'], 'A title without numbered-series hints')
        core.q('UPDATE jobs SET source=?,key=? WHERE id=?', (f"sub:{sub['id']}", key, jid))
        return {'id': jid}

    def snapshot(self, sub, title='Original playlist', members=('bilibili:BV1234567890:p',), ident='season:4'):
        data = {'owner': sub['owner'], 'sub_key': sub['key'], 'catalogue': [{'id': ident, 'title': title}],
                'groups': {ident: {'title': title, 'members': list(members), 'updated': time.time()}}, 'catalog_due': time.time() + 86400}
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        return data

    def test_original_group_and_part_identity_without_network_or_analysis_changes(self):
        sub = self.subscription()
        first, second = self.member(sub), self.member(sub, 'bilibili:BV1234567890:p3')
        self.snapshot(sub)
        original = core.q('SELECT analysis FROM jobs WHERE id=?', (first['id'],), one=True)['analysis']
        with patch.object(native.requests.Session, 'get', side_effect=AssertionError('request handler went online')):
            native.attach([first, second])
        self.assertEqual((first['series'], second['series']), ('Original playlist', 'Original playlist'))
        self.assertEqual(first['series_key'], second['series_key'])
        self.assertEqual(core.q('SELECT analysis FROM jobs WHERE id=?', (first['id'],), one=True)['analysis'], original)

    def test_sign_in_keeps_native_membership_when_both_owners_follow_the_creator(self):
        old = 'd' * 32
        anonymous = self.subscription(old)
        account = self.subscription()
        job = self.member(anonymous)
        self.snapshot(anonymous)
        with core.app.test_request_context('/'):
            g.device = old
            web.sign_in('alice')
        saved = core.q('SELECT owner,source FROM jobs WHERE id=?', (job['id'],), one=True)
        self.assertEqual((saved['owner'], saved['source']), ('user:alice', f"sub:{account['id']}"))
        native.attach([job])
        self.assertEqual(job['series'], 'Original playlist')

    def test_same_name_from_other_owners_and_creators_never_merges(self):
        alice, bob = self.subscription(), self.subscription('user:bob')
        other = self.subscription(key='bilibili:456')
        self.snapshot(alice); self.snapshot(other)
        a, b, o = self.member(alice), self.member(bob), self.member(other)
        native.attach([a, b, o])
        self.assertNotIn('series', b)
        self.assertNotEqual(a['series_key'], o['series_key'])
        self.snapshot(bob)
        native.attach([b])
        self.assertNotEqual(a['series_key'], b['series_key'])
        core.q('DELETE FROM subs WHERE id=?', (alice['id'],))
        self.assertNotIn('series', native.attach([{'id': a['id']}])[0])

    def test_foreign_subscription_source_is_not_membership(self):
        sub = self.subscription('user:bob')
        self.snapshot(sub)
        job = self.member(sub)
        core.q('UPDATE jobs SET owner=? WHERE id=?', ('user:alice', job['id']))
        self.assertNotIn('series', native.attach([job])[0])

    def test_legacy_subscription_with_missing_or_invalid_key_is_skipped(self):
        sub = self.subscription()
        job = self.member(sub)
        for key in (None, '', 'legacy', 'bilibili:'):
            core.q('UPDATE subs SET key=? WHERE id=?', (key, sub['id']))
            self.assertNotIn('series', native.attach([dict(job)])[0])
            with patch.object(native, '_bili_catalogue') as request:
                native.sync_sub({**sub, 'key': key})
                request.assert_not_called()
        native.move_owner('user:alice', 'user:bob')

    def test_catalogue_seeds_complete_groups_without_per_video_requests(self):
        sub = self.subscription()
        entry = {'id': 'season:4', 'title': 'Original title', 'members': ['bilibili:BV1234567890:p'], 'complete': True}
        with patch.object(native, '_bili_catalogue', return_value=([entry], False)), patch.object(native, '_bili_members') as fetch:
            native.sync_sub(sub)
        fetch.assert_not_called()
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(data['groups']['season:4']['members'], entry['members'])
        self.assertGreater(data['next'], time.time())

    def test_large_playlist_continues_then_atomically_removes_stale_members(self):
        sub = self.subscription()
        data = self.snapshot(sub, members=['bilibili:stale:p'])
        data['groups']['season:4']['updated'] = 0
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        with patch.object(native, '_bili_members', side_effect=[(['bilibili:new1:p'], True), (['bilibili:new2:p'], True)]) as fetch:
            native.sync_sub(sub)
        self.assertEqual(fetch.call_args_list[0].args[2], 1)
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(data['groups']['season:4']['pending']['page'], 3)
        self.assertIn('bilibili:stale:p', data['groups']['season:4']['members'])
        with patch.object(native, '_bili_members', return_value=(['bilibili:new3:p'], False)) as fetch:
            native.sync_sub(sub)
        self.assertEqual(fetch.call_args.args[2], 3)
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(data['groups']['season:4']['members'], ['bilibili:new1:p', 'bilibili:new2:p', 'bilibili:new3:p'])
        self.assertNotIn('pending', data['groups']['season:4'])

    def test_platform_failure_keeps_last_known_members_and_does_not_block_other_playlists(self):
        sub = self.subscription()
        data = self.snapshot(sub)
        data['groups']['season:4']['updated'] = 0
        data['catalogue'].append({'id': 'season:5', 'title': 'Another playlist'})
        data['groups']['season:5'] = {'title': 'Another playlist', 'members': []}
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        with patch.object(native, '_bili_members', side_effect=[TimeoutError('secret'), (['bilibili:BV5555555555:p'], False)]):
            native.sync_sub(sub)
        result = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(result['groups']['season:4']['members'], ['bilibili:BV1234567890:p'])
        self.assertEqual(result['groups']['season:4']['error'], 'TimeoutError')
        self.assertEqual(result['groups']['season:5']['members'], ['bilibili:BV5555555555:p'])
        self.assertNotIn('secret', str(result))

    def test_deleted_or_replaced_subscription_cannot_receive_late_snapshot(self):
        sub = self.subscription()
        def changed(*args):
            core.q('UPDATE subs SET owner=? WHERE id=?', ('user:bob', sub['id']))
            return [], False
        with patch.object(native, '_bili_catalogue', side_effect=changed):
            native.sync_sub(sub)
        self.assertIsNone(core.kv_get(native.cache_key(sub['owner'], sub['key'])))

    def test_account_migration_and_url_normalization(self):
        sub = self.subscription()
        self.snapshot(sub)
        job = self.member(sub)
        native.attach([job])
        previous_identity = job['series_key']
        native.move_owner('user:alice', 'user:bob')
        core.q('UPDATE subs SET owner=? WHERE id=?', ('user:bob', sub['id']))
        core.q('UPDATE jobs SET owner=? WHERE id=?', ('user:bob', job['id']))
        native.attach([job])
        self.assertEqual(job['series'], 'Original playlist')
        self.assertNotEqual(job['series_key'], previous_identity)
        self.assertIsNone(core.kv_get(native.cache_key('user:alice', sub['key'])))
        self.assertEqual(native.video_key('https://www.bilibili.com/video/BV1234567890?p=4'), 'bilibili:BV1234567890:p')
        self.assertEqual(native.video_key('https://youtu.be/abcdefghijk?si=tracking'), 'youtube:abcdefghijk')
        self.assertEqual(native.video_key('https://foreign.test/video/BV1234567890'), '')

    def test_account_merge_retains_newer_snapshots_for_same_followed_creator(self):
        old, new = self.subscription(), self.subscription('user:bob')
        self.snapshot(old, title='Newer original collection')
        data = self.snapshot(new, title='Older cached collection')
        data['groups']['season:4']['updated'] = 0
        core.kv_set(native.cache_key('user:bob', new['key']), data)
        native.move_owner('user:alice', 'user:bob')
        job = self.member(new)
        native.attach([job])
        self.assertEqual(job['series'], 'Newer original collection')


if __name__ == '__main__':
    unittest.main()
