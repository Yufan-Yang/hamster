"""Original platform membership is owned metadata, independent of downloads."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from flask import g

spec = importlib.util.spec_from_file_location('native_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, native_series as native, series, web  # noqa: E402


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

    def tick(self):
        with patch.object(native.time, 'sleep', side_effect=StopIteration), self.assertRaises(StopIteration):
            native.loop()

    def test_episode_not_added_to_platform_playlist_joins_unique_numbered_family(self):
        sub = self.subscription()
        previous, latest = self.member(sub), self.member(sub, 'bilibili:BV9876543210:p1')
        title = '新三国up锐评神探狄仁杰23：这也有小妈文学可看？'
        core.q('UPDATE jobs SET title=? WHERE id=?', (title, latest['id']))
        entry = {'id': 'season:4', 'title': '合集·神探狄仁杰', 'members': [], 'complete': False}
        archives = {'archives': [{'bvid': 'BV1234567890', 'title': '新三国up锐评神探狄仁杰22：大人真乃神人也！'}],
                    'page': {'total': 1}}
        with patch.object(native, '_bili_catalogue', return_value=([entry], False)), \
                patch.object(native, '_bili_get', return_value=archives):
            native.sync_sub(sub)
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertNotIn('bilibili:BV9876543210:p', data['groups']['season:4']['members'])
        with patch.object(native.requests.Session, 'get', side_effect=AssertionError('request handler went online')):
            native.attach([previous, latest])
            self.assertEqual(latest['series_key'], previous['series_key'])
            player = self.alice.get(f"/api/series/{latest['id']}")
            self.assertEqual(player.status_code, 200, player.json)
            self.assertEqual(player.json['total'], 2)
            page = self.alice.get('/api/jobs?grouped=1&limit=1').json
        self.assertEqual({job['id'] for job in page['jobs']}, {previous['id'], latest['id']})
        self.assertFalse(page['more'])
        # Hidden siblings remain hidden after family matching, including counts.
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([previous['id']])))
        self.assertEqual(self.alice.get(f"/api/series/{latest['id']}").json['total'], 1)
        self.assertEqual(self.bob.get(f"/api/series/{latest['id']}").status_code, 404)

    def test_numbered_family_is_scoped_and_ambiguous_matches_do_not_merge(self):
        sub = self.subscription()
        latest = self.member(sub, 'bilibili:BV9876543210:p1')
        core.q('UPDATE jobs SET title=? WHERE id=?', ('原作者系列23：内容', latest['id']))
        family = series.series_key(series.numbered_name('原作者系列22：内容'))
        data = self.snapshot(sub)
        data['groups']['season:4']['families'] = [family]
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        foreign = []
        for other in (self.subscription('user:bob'), self.subscription(key='bilibili:456')):
            job = self.member(other, 'bilibili:BV9876543210:p1')
            core.q('UPDATE jobs SET title=? WHERE id=?', ('原作者系列23：内容', job['id']))
            foreign.append(job)
        native.attach([latest, *foreign])
        self.assertIn('series_key', latest)
        self.assertTrue(all('series_key' not in job for job in foreign))
        data['catalogue'].append({'id': 'season:5', 'title': 'Second collection'})
        data['groups']['season:5'] = {'title': 'Second collection', 'members': [], 'families': [family]}
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        self.assertNotIn('series_key', native.attach([{'id': latest['id']}])[0])
        data['groups']['season:5']['members'] = ['bilibili:BV9876543210:p']
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        self.assertEqual(native.attach([{'id': latest['id']}])[0]['series'], 'Second collection')

    def test_complete_bilibili_catalogue_preserves_numbered_family_evidence(self):
        sub = self.subscription()
        title = '原作者系列22：内容'
        data = {'items_lists': {'seasons_list': [{'meta': {'season_id': 4, 'name': 'Original playlist', 'total': 1},
                                                'archives': [{'bvid': 'BV1234567890', 'title': title}]}], 'page': {'total': 1}}}
        with patch.object(native, '_bili_get', return_value=data), patch.object(native, '_bili_members') as members:
            native.sync_sub(sub)
        members.assert_not_called()
        snapshot = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(snapshot['groups']['season:4']['families'], [series.series_key(series.numbered_name(title))])

    def test_youtube_playlist_titles_support_new_numbered_episode(self):
        sub = self.subscription(key='youtube:@lessons')
        latest = self.member(sub, 'youtube:abcdefghijk')
        core.q('UPDATE jobs SET title=? WHERE id=?', ('Original lessons EP23: content', latest['id']))
        entry = {'id': 'PLabcdefghijkl', 'title': 'Original playlist', 'members': [], 'complete': False}
        with patch.object(native, '_youtube_catalogue', return_value=([entry], False)), \
                patch.object(native, '_youtube', return_value=([{'id': 'abcdefghij0', 'title': 'Original lessons EP22: content'}], False)):
            native.sync_sub(sub)
        self.assertEqual(native.attach([latest])[0]['series'], 'Original playlist')

    def test_new_automatic_episode_refreshes_fresh_snapshot_and_joins_original_collection(self):
        sub = self.subscription()
        previous = self.member(sub)
        latest = self.member(sub, 'bilibili:BV9876543210:p1')
        core.q('UPDATE jobs SET title=?,analysis=? WHERE id=?',
               ('新三国up锐评神探狄仁杰23：这也有小妈文学可看？',
                json.dumps({'library': 'Videos', 'title': '新三国up锐评神探狄仁杰23：这也有小妈文学可看？'}), latest['id']))
        checked = time.time()
        core.q('UPDATE subs SET checked=? WHERE id=?', (checked, sub['id']))
        data = self.snapshot(sub, title='神探狄仁杰锐评')
        data.update(subscription_checked=checked - 3600, next=checked + 3600)
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        entry = {'id': 'season:4', 'title': '神探狄仁杰锐评',
                 'members': ['bilibili:BV1234567890:p', 'bilibili:BV9876543210:p'], 'complete': True}
        with patch.object(native, '_bili_catalogue', return_value=([entry], False)) as catalogue:
            self.tick()
        catalogue.assert_called_once()
        native.attach([previous, latest])
        self.assertEqual(previous['series_key'], latest['series_key'])
        self.assertEqual(latest['series'], '神探狄仁杰锐评')
        self.assertEqual(self.alice.get(f"/api/series/{latest['id']}").json['total'], 2)
        page = self.alice.get('/api/jobs?grouped=1&limit=1').json
        self.assertEqual({job['id'] for job in page['jobs']}, {previous['id'], latest['id']})
        self.assertFalse(page['more'])
        with patch.object(native, '_bili_catalogue') as catalogue:
            self.tick()
        catalogue.assert_not_called()

    def test_existing_snapshot_catches_up_to_last_automatic_check_after_upgrade(self):
        sub = self.subscription()
        self.member(sub)
        core.q('UPDATE subs SET checked=? WHERE id=?', (time.time(), sub['id']))
        data = self.snapshot(sub)
        data['next'] = time.time() + 3600
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        entry = {'id': 'season:4', 'title': 'Original playlist', 'members': data['groups']['season:4']['members'], 'complete': True}
        with patch.object(native, '_bili_catalogue', return_value=([entry], False)) as catalogue:
            self.tick()
        catalogue.assert_called_once()

    def test_automatic_refresh_failure_keeps_membership_and_respects_retry_delay(self):
        sub = self.subscription()
        job = self.member(sub)
        checked = time.time()
        core.q('UPDATE subs SET checked=? WHERE id=?', (checked, sub['id']))
        data = self.snapshot(sub)
        data.update(subscription_checked=checked - 3600, next=checked + 3600)
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        with patch.object(native, '_bili_catalogue', side_effect=TimeoutError) as catalogue:
            self.tick()
        catalogue.assert_called_once()
        native.attach([job])
        self.assertEqual(job['series'], 'Original playlist')
        # Another uploader check must not bypass a platform failure's backoff.
        core.q('UPDATE subs SET checked=? WHERE id=?', (checked + 1, sub['id']))
        with patch.object(native, '_bili_catalogue') as catalogue:
            self.tick()
        catalogue.assert_not_called()

    def test_new_automatic_check_restarts_incomplete_pages_without_dropping_old_members(self):
        sub = self.subscription()
        checked = time.time()
        core.q('UPDATE subs SET checked=? WHERE id=?', (checked, sub['id']))
        data = self.snapshot(sub)
        data.update(subscription_checked=checked - 3600, next=checked + 3600, catalog_page=2,
                    catalog_work=[{'id': 'season:4', 'title': 'Old title'}])
        data['groups']['season:4']['pending'] = {'page': 3, 'members': ['bilibili:partial:p']}
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        entry = {'id': 'season:4', 'title': 'Original playlist', 'members': [], 'complete': False}
        with patch.object(native, '_bili_catalogue', return_value=([entry], False)) as catalogue, \
                patch.object(native, '_bili_members', side_effect=[(['bilibili:first:p'], True, []), (['bilibili:second:p'], True, [])]) as members:
            self.tick()
        self.assertEqual(catalogue.call_args.args[1], 1)
        self.assertEqual([call.args[2] for call in members.call_args_list], [1, 2])
        updated = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertIn('bilibili:BV1234567890:p', updated['groups']['season:4']['members'])
        self.assertEqual(updated['groups']['season:4']['pending']['page'], 3)

    def test_new_automatic_youtube_check_refreshes_playlist_members(self):
        sub = self.subscription(key='youtube:@lessons')
        job = self.member(sub, 'youtube:abcdefghijk')
        checked = time.time()
        core.q('UPDATE subs SET checked=? WHERE id=?', (checked, sub['id']))
        data = self.snapshot(sub, members=(), ident='PLabcdefghijkl')
        data.update(subscription_checked=checked - 3600, next=checked + 3600)
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        entry = {'id': 'PLabcdefghijkl', 'title': 'Original playlist', 'members': [], 'complete': False}
        with patch.object(native, '_youtube_catalogue', return_value=([entry], False)), \
                patch.object(native, '_youtube_members', return_value=(['youtube:abcdefghijk'], False, [])) as members:
            self.tick()
        members.assert_called_once()
        native.attach([job])
        self.assertEqual(job['series'], 'Original playlist')

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
        with patch.object(native, '_bili_members', side_effect=[(['bilibili:new1:p'], True, ['first']), (['bilibili:new2:p'], True, ['second'])]) as fetch:
            native.sync_sub(sub)
        self.assertEqual(fetch.call_args_list[0].args[2], 1)
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(data['groups']['season:4']['pending']['page'], 3)
        self.assertEqual(data['groups']['season:4']['families'], ['first', 'second'])
        self.assertIn('bilibili:stale:p', data['groups']['season:4']['members'])
        with patch.object(native, '_bili_members', return_value=(['bilibili:new3:p'], False, ['third'])) as fetch:
            native.sync_sub(sub)
        self.assertEqual(fetch.call_args.args[2], 3)
        data = core.kv_get(native.cache_key(sub['owner'], sub['key']))
        self.assertEqual(data['groups']['season:4']['members'], ['bilibili:new1:p', 'bilibili:new2:p', 'bilibili:new3:p'])
        self.assertEqual(data['groups']['season:4']['families'], ['first', 'second', 'third'])
        self.assertNotIn('pending', data['groups']['season:4'])

    def test_platform_failure_keeps_last_known_members_and_does_not_block_other_playlists(self):
        sub = self.subscription()
        data = self.snapshot(sub)
        data['groups']['season:4']['updated'] = 0
        data['catalogue'].append({'id': 'season:5', 'title': 'Another playlist'})
        data['groups']['season:5'] = {'title': 'Another playlist', 'members': []}
        core.kv_set(native.cache_key(sub['owner'], sub['key']), data)
        with patch.object(native, '_bili_members', side_effect=[TimeoutError('secret'), (['bilibili:BV5555555555:p'], False, [])]):
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
