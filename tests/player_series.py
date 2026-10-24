"""Player siblings obey owner/privacy scope and the selected library order."""
import importlib.util
import base64
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('series_player_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, series, web, library  # noqa: E402


class PlayerSeriesTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def test_cover_has_live_owner_and_private_scope_and_no_cache(self):
        public, private = self.episode(1), self.episode(2)
        path = self.path / 'cover.png'
        path.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jS1kAAAAASUVORK5CYII='))
        for jid in (public, private):
            core.q('UPDATE jobs SET thumb=? WHERE id=?', (str(path), jid))
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([private])))
        self.assertEqual(self.alice.get(f'/api/series/{private}/cover').status_code, 404)
        for client in (self.bob, self.admin):
            self.assertEqual(client.get(f'/api/series/{public}/cover').status_code, 404)
        self.assertTrue(self.alice.get(f'/api/series/{public}').json['items'][0]['thumb'])
        token = {'owner':'user:alice', 'reveal':True, 'expires':time.time() + 60}
        with patch.dict(web.privacy_tokens, {'cover':token}):
            response = self.alice.get(f'/api/series/{private}/cover', headers={'X-Privacy-Token':'cover'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.mimetype, 'image/png')
            response.close()
            token['expires'] = time.time() - 1
            self.assertEqual(self.alice.get(f'/api/series/{private}/cover', headers={'X-Privacy-Token':'cover'}).status_code, 403)

    def episode(self, number, owner='user:alice', created=None, published='2026-10-01', tags=()):
        title = f'Drama episode {number}'
        jid = self.job(owner, title, tags=tags)
        a = {'title': title, 'library': 'TV', 'show': 'Drama', 'season': 1, 'episode': number,
             'published': published, 'tags': list(tags)}
        core.q('UPDATE jobs SET analysis=?,files=?,created=? WHERE id=?',
               (json.dumps(a), json.dumps([f'/not-probed/episode-{jid}.mp4']), created if created is not None else number, jid))
        return jid

    def test_complete_owned_series_has_quick_play_and_watch_status_without_media_probes(self):
        first, second = self.episode(1), self.episode(2)
        queued = self.episode(3)
        core.q("UPDATE jobs SET status='queued',files='[]' WHERE id=?", (queued,))
        self.episode(4, owner='user:bob')
        core.q('INSERT INTO watch(owner,job_id,part,pos,dur,done,updated) VALUES(?,?,0,100,100,1,?)', ('user:alice', second, time.time()))
        with patch.object(library, 'media_info') as media, patch.object(library, 'probe_of') as probe:
            r = self.alice.get(f'/api/series/{first}')
            media.assert_not_called(); probe.assert_not_called()
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual([item['id'] for item in r.json['items']], [queued, second, first])
        self.assertEqual([item['playable'] for item in r.json['items']], [False, True, True])
        self.assertTrue(r.json['items'][1]['watched'])
        self.assertEqual(r.json['total'], 3)
        self.assertNotIn('files', r.json['items'][0])
        self.assertEqual(r.headers['Cache-Control'], 'no-store')

    def test_numbered_uploader_collections_also_use_the_selected_time_order(self):
        ids = [self.job('user:alice', f'新三国up锐评老三国{n}：故事') for n in (23, 1, 24)]
        for jid, created in zip(ids, (20, 10, 30)):
            core.q('UPDATE jobs SET created=? WHERE id=?', (created, jid))
        other = self.job('user:alice', '新三国up锐评其他系列1：故事')
        r = self.alice.get(f'/api/series/{ids[0]}?sort=added')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([item['id'] for item in r.json['items']], [ids[2], ids[0], ids[1]])
        self.assertNotIn(other, [item['id'] for item in r.json['items']])

    def test_pagination_filtering_and_added_published_orders_precede_page_cutoff(self):
        first = self.episode(1, created=20, published='2026-10-03')
        second = self.episode(2, created=30, published='2026-10-01')
        third = self.episode(3, created=10, published='2026-10-02')
        for sort, expected in (('added', [second, first, third]), ('published', [first, third, second])):
            for offset, jid in enumerate(expected):
                r = self.alice.get(f'/api/series/{first}', query_string={'sort': sort, 'offset': offset, 'limit': 1})
                self.assertEqual([item['id'] for item in r.json['items']], [jid])
                self.assertEqual(r.json['total'], 3)
        result = self.alice.get(f'/api/series/{first}?q=episode+2').json
        self.assertEqual((result['total'], result['items'][0]['id']), (1, second))

    def test_hidden_members_and_foreign_owners_never_leak_names_or_counts(self):
        first = self.episode(1)
        hidden = self.episode(2)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([hidden])))
        r = self.alice.get(f'/api/series/{first}')
        self.assertEqual((r.json['total'], [item['id'] for item in r.json['items']]), (1, [first]))
        self.assertEqual(self.alice.get(f'/api/series/{hidden}').status_code, 404)
        for client in (self.bob, self.admin):
            self.assertEqual(client.get(f'/api/series/{first}').status_code, 404)
        with patch.dict(web.privacy_tokens, {'expired': {'owner': 'user:alice', 'reveal': True, 'expires': time.time() - 1}}):
            self.assertEqual(self.alice.get(f'/api/series/{first}', headers={'X-Privacy-Token': 'expired'}).status_code, 403)

    def test_late_hidden_change_discards_whole_response(self):
        first, second = self.episode(1), self.episode(2)
        original = series.q
        def changed(sql, args=(), one=False):
            rows = original(sql, args, one=one)
            if sql.startswith('SELECT job_id,done FROM watch'):
                core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([second])))
            return rows
        with patch.object(series, 'q', side_effect=changed):
            r = self.alice.get(f'/api/series/{first}')
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('items', r.json)
        self.assertNotIn('total', r.json)

    def test_membership_validation_detects_replacement_even_when_count_is_unchanged(self):
        first, second = self.episode(1), self.episode(2)
        before = self.alice.get(f'/api/series/{first}').json
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([second])))
        third = self.episode(3)
        after = self.alice.get(f'/api/series/{first}').json
        self.assertEqual((before['total'], after['total']), (2, 2))
        self.assertNotEqual(before['membership'], after['membership'])
        self.assertEqual([item['id'] for item in after['items']], [third, first])


if __name__ == '__main__':
    unittest.main()
