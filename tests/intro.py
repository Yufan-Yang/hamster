"""Opening evidence, isolation and stale-media guards; no network or models."""
import importlib.util
import json
from pathlib import Path
import random
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('intro_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, download, intro, web  # noqa: E402


class EvidenceTests(unittest.TestCase):
    def test_bilibili_native_pgc_nested_opening_and_ending_are_not_lost(self):
        class Response:
            content = b'{}'
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data
        class Session:
            def __init__(self): self.headers = {}; self.calls = []; self.closed = False
            def get(self, url, params, timeout):
                self.calls.append((url, params))
                payload = [
                    {'code': 0, 'result': {'episodes': [{'id': 3854821, 'aid': 12, 'cid': 34}]}},
                    {'code': 0, 'data': {'view_points': [{'from': 0, 'to': 20, 'content': 'Intro'}]}},
                    {'code': 0, 'result': {'video_info': {'timelength': 1274858, 'clip_info_list': [
                        {'start': 30, 'end': 156, 'clipType': 'CLIP_TYPE_OP'},
                        {'start': 1186, 'end': 1274, 'clipType': 'CLIP_TYPE_ED'}]}}},
                ][len(self.calls) - 1]
                return Response(payload)
            def close(self): self.closed = True
        session = Session()
        with patch.object(intro.requests, 'Session', return_value=session), patch.object(intro.core, 'COOKIES', Path('/nonexistent-fixture-cookies')):
            marks = intro.bilibili_markers('https://www.bilibili.com/bangumi/play/ep3854821')
        self.assertEqual([(mark['t'], mark['end'], mark['kind']) for mark in marks if mark.get('kind')], [(30, 156, 'intro'), (1186, 1274, 'outro')])
        self.assertEqual(intro.chapter_intro({'markers': {'0': marks}}, 0, 1274.858)['end'], 156)
        self.assertEqual(intro.chapter_intro({'markers': {'0': marks}}, 0, 1274.858)['start'], 30)
        self.assertTrue(session.closed)
        self.assertTrue(all(url.startswith('https://api.bilibili.com/') for url, _ in session.calls))
        with patch.object(intro.requests, 'Session', return_value=Session()), patch.object(intro.core, 'COOKIES', Path('/nonexistent-fixture-cookies')):
            self.assertEqual(intro.bilibili_markers('https://www.bilibili.com/bangumi/play/ep3854821', expected_duration=2000), [])

    def test_successful_native_empty_result_avoids_redundant_extractor_requests(self):
        with patch.object(intro, 'bilibili_markers', return_value=[]), patch.object(intro.subprocess, 'run') as run:
            self.assertEqual(intro.source_markers('https://www.bilibili.com/video/BV1Qtp66REKC/?spm_id_from=tracking'), [])
            run.assert_not_called()

    def test_normal_bv_uses_the_selected_part_cid_and_original_chapter_ranges(self):
        def response(data):
            result = Mock(content=b'{}')
            result.json.return_value = data
            return result
        session = Mock()
        session.get.side_effect = [
            response({'code': 0, 'data': {'aid': 12, 'cid': 11, 'pages': [{'page': 1, 'cid': 11}, {'page': 2, 'cid': 22}]}}),
            response({'code': 0, 'data': {'view_points': [{'from': 5, 'to': 65, 'content': 'Intro'}]}}),
            response({'code': 0, 'data': {'timelength': 1500000}}),
        ]
        with patch.object(intro.requests, 'Session', return_value=session), patch.object(intro.core, 'COOKIES', Path('/nonexistent-fixture-cookies')):
            marks = intro.bilibili_markers('https://www.bilibili.com/video/BV1Qtp66REKC/?p=2&spm_id_from=tracking', expected_duration=1500)
        self.assertEqual(marks, [{'t': 5, 'end': 65, 'title': 'Intro'}])
        self.assertEqual(session.get.call_args_list[1].kwargs['params'], {'aid': 12, 'cid': 22})
        session.close.assert_called_once()

    def test_original_site_markers_keep_intro_and_outro_end_times(self):
        marks = download.timeline({'chapters': [{'start_time': 0, 'end_time': 85, 'title': 'Intro'},
                                               {'start_time': 1300, 'end_time': 1390, 'title': 'Outro'},
                                               {'start_time': float('nan'), 'title': 'invalid'}]},
                                  'https://www.bilibili.com/video/BVtest')['markers']
        self.assertEqual(marks, [{'t': 0, 'end': 85, 'title': 'Intro'}, {'t': 1300, 'end': 1390, 'title': 'Outro'}])

    def test_site_marks_use_explicit_end_or_next_marker_and_real_bounds(self):
        a = {'markers': {'0': [{'t': 5, 'title': '片头曲'}, {'t': 85, 'title': '正片'}]}}
        self.assertEqual(intro.chapter_intro(a, 0, 1200), {'start': 5, 'end': 85, 'method': 'chapter', 'automatic': True})
        for marks in ([{'t': 0, 'title': 'Opening'}], [{'t': 0, 'title': 'Introduction to Python', 'end': 60}],
                      [{'t': 500, 'title': 'Intro', 'end': 560}], [{'t': 0, 'title': 'Intro', 'end': float('nan')}],
                      [{'t': 0, 'title': 'Intro', 'end': 1000}]):
            self.assertIsNone(intro.chapter_intro({'markers': {'0': marks}}, 0, 1200))
        self.assertIsNone(intro.chapter_intro({'chapters': a['markers']}, 0, 1200))

    def test_shifted_repeated_audio_survives_small_fingerprint_errors(self):
        rng = random.Random(4)
        words = lambda n: [rng.getrandbits(32) for _ in range(n)]
        music = words(400)
        first = words(100) + music + words(700)
        second = words(240) + [x ^ (1 << (i % 12)) for i, x in enumerate(music)] + words(560)
        hit = intro.match_audio(first, second, .125, 2.5, 1500)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit['start'], 15, delta=.5)
        self.assertAlmostEqual(hit['end'], 62.375, delta=1)
        reverse = intro.match_audio(second, first, .125, 2.5, 1500)
        self.assertAlmostEqual(reverse['start'], 32.5, delta=.5)

    def test_silence_full_duplicates_unrelated_and_short_repeats_do_not_skip(self):
        rng = random.Random(9)
        words = [rng.getrandbits(32) for _ in range(2300)]
        self.assertIsNone(intro.match_audio([0] * 2300, [0] * 2300, .125, 2.5, 1500))
        self.assertIsNone(intro.match_audio(words, words, .125, 2.5, 1500))
        self.assertIsNone(intro.match_audio(words, [rng.getrandbits(32) for _ in words], .125, 2.5, 1500))
        short = [rng.getrandbits(32) for _ in range(1100)]
        short[50:130] = words[100:180]
        self.assertIsNone(intro.match_audio(words, short, .125, 2.5, 1500))

    def test_grouping_requires_tv_show_and_same_season(self):
        self.assertEqual(intro.group_of({'analysis': {'library': 'TV', 'show': '节目 第二季', 'season': 2}}), ('节目', 2))
        self.assertIsNone(intro.group_of({'analysis': {'library': 'Videos', 'show': '节目'}}))
        self.assertIsNone(intro.group_of({'analysis': {'library': 'TV', 'show': ''}}))


class IntroAPITests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def episode(self, owner='user:alice', show='Drama', episode=1, season=1, markers=None):
        jid = self.job(owner, 'Drama episode')
        path = self.path / f'episode-{jid}.mp4'
        path.write_bytes(b'synthetic media')
        a = {'library': 'TV', 'show': show, 'season': season, 'episode': episode}
        if markers:
            a['markers'] = {'0': markers}
        core.q('UPDATE jobs SET files=?,analysis=? WHERE id=?', (json.dumps([str(path)]), json.dumps(a), jid))
        return jid, str(path)

    def test_get_does_not_start_work_post_is_coalesced_and_peers_stay_scoped(self):
        jid, _ = self.episode()
        peer, _ = self.episode(episode=2)
        self.episode(owner='user:bob', episode=3)
        self.episode(season=2, episode=4)
        hidden, _ = self.episode(episode=5)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([hidden])))
        with patch.object(intro._pool, 'submit') as submit:
            self.assertEqual(self.alice.get(f'/api/intro/{jid}').json['status'], 'unknown')
            submit.assert_not_called()
            try:
                for _ in range(2):
                    r = self.alice.post(f'/api/intro/{jid}')
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json['status'], 'pending')
                self.assertEqual(submit.call_count, 1)
                self.assertEqual([row['id'] for row in submit.call_args.args[-1]], [peer])
            finally:
                intro._pending.clear()

    def test_explicit_site_marker_is_cached_and_hidden_foreign_or_expired_never_reads_it(self):
        jid, path = self.episode(markers=[{'t': 0, 'title': 'Opening'}, {'t': 65, 'title': 'Episode'}])
        job = intro._job(jid, 'user:alice')
        signature = intro.file_key(path)
        key = intro._cache_key('user:alice', jid, 0, signature, job['analysis'])
        with patch.object(intro, '_duration', return_value=1400), patch.object(intro, 'fingerprint') as fp:
            intro._analyze(key, 'user:alice', job, 0, path, signature, [])
            fp.assert_not_called()
        r = self.alice.get(f'/api/intro/{jid}')
        self.assertEqual((r.json['start'], r.json['end'], r.json['automatic']), (0, 65, True))
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        for client in (self.bob, self.admin):
            self.assertEqual(client.get(f'/api/intro/{jid}').status_code, 404)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([jid])))
        self.assertEqual(self.alice.get(f'/api/intro/{jid}').status_code, 404)
        with patch.dict(web.privacy_tokens, {'expired': {'owner': 'user:alice', 'reveal': True, 'expires': time.time() - 1}}):
            self.assertEqual(self.alice.get(f'/api/intro/{jid}', headers={'X-Privacy-Token': 'expired'}).status_code, 403)

    def test_one_peer_produces_manual_candidate_two_distinct_episodes_allow_auto(self):
        jid, path = self.episode()
        peers = [intro._job(self.episode(episode=n)[0], 'user:alice') for n in (2, 3)]
        job = intro._job(jid, 'user:alice'); signature = intro.file_key(path)
        key = intro._cache_key('user:alice', jid, 0, signature, job['analysis'])
        with patch.object(intro, '_duration', return_value=1500), patch.object(intro, 'fingerprint_timing', return_value=(.125, 2.5)), \
                patch.object(intro, 'fingerprint', side_effect=lambda path, signature: (signature,)), patch.object(intro, 'match_audio', return_value={'start': 5, 'end': 65}):
            intro._analyze(key, 'user:alice', job, 0, path, signature, peers[:1])
            self.assertFalse(core.kv_get(key)['automatic'])
            intro._analyze(key, 'user:alice', job, 0, path, signature, peers)
            self.assertTrue(core.kv_get(key)['automatic'])
        with patch.object(intro, '_duration', return_value=1500), patch.object(intro, 'fingerprint_timing', return_value=(.125, 2.5)), \
                patch.object(intro, 'fingerprint', side_effect=lambda address, signature: (1,) if address == path else (2,)), \
                patch.object(intro, 'match_audio', return_value={'start': 5, 'end': 65}):
            intro._analyze(key, 'user:alice', job, 0, path, signature, peers)
            self.assertFalse(core.kv_get(key)['automatic'], 'copies of one peer cannot count as independent episodes')

    def test_bilibili_existing_video_refreshes_only_site_marks_before_audio(self):
        jid, path = self.episode(show='节目')
        core.q('UPDATE jobs SET url=?,analysis=? WHERE id=?',
               ('https://www.bilibili.com/video/BVtest', json.dumps({'library': 'TV', 'show': '节目', 'season': 1, 'episode': 1}, ensure_ascii=False), jid))
        job = intro._job(jid, 'user:alice'); signature = intro.file_key(path)
        key = intro._cache_key('user:alice', jid, 0, signature, job['analysis'])
        marks = [{'t': 0, 'end': 70, 'title': '片头'}, {'t': 1300, 'end': 1390, 'title': '片尾'}]
        with patch.object(intro, '_duration', return_value=1500), patch.object(intro, 'source_markers', return_value=marks), \
                patch.object(intro, 'fingerprint') as fp:
            intro._analyze(key, 'user:alice', job, 0, path, signature, [])
            fp.assert_not_called()
        current = intro._job(jid, 'user:alice')
        self.assertEqual(current['analysis']['markers']['0'], marks)
        self.assertEqual(self.alice.get(f'/api/intro/{jid}').json['end'], 70)

    def test_changed_file_and_revoked_privacy_discard_cached_response(self):
        jid, path = self.episode()
        original = core.kv_get
        def changed(key, default=None):
            result = original(key, default)
            Path(path).write_bytes(b'replaced media content')
            return result
        with patch.object(core, 'kv_get', side_effect=changed):
            self.assertEqual(self.alice.get(f'/api/intro/{jid}').status_code, 403)
        def hidden(key, default=None):
            result = original(key, default)
            core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([jid])))
            return result
        with patch.object(core, 'kv_get', side_effect=hidden):
            self.assertEqual(self.alice.get(f'/api/intro/{jid}').status_code, 403)


if __name__ == '__main__':
    unittest.main()
