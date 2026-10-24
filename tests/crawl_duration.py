"""Duration evidence from already fetched markup; no network, probes or AI."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang.crawl_extract import extract_page, parse_duration  # noqa: E402


class DurationTests(unittest.TestCase):
    def extract(self, markup, listing=False):
        return extract_page(markup, 'https://example.test/catalog/' if listing else 'https://example.test/watch/lesson', listing)

    def test_explicit_duration_formats(self):
        cases = {'PT1H2M3S': 3723, 'PT1M2.5S': 62.5, 'P1DT2H': 93600, 'PT0.5H': 1800,
                 '01:02:03': 3723, '12:34': 754, '125:00': 7500, '00:01.5': 1.5,
                 '时长： 03:20': 200, 'Duration: 00:15': 15, '90.5': 90.5, '.5': .5,
                 12: 12, 23.5: 23.5}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_duration(value), expected)

    def test_invalid_ambiguous_and_nonfinite_values_are_unknown(self):
        for value in (None, '', 0, -1, True, False, float('inf'), float('-inf'), float('nan'),
                      'NaN', 'Infinity', 'PT', 'P', 'P1DT', 'PT0S', 'P1M', '-10', '1:60', '1:60:00', '1:02:60',
                      '2026-10-23T01:23:45', 'Episode 12:34', 'length 2026', '12*60', '90ms', [], {}, 10 ** 400):
            with self.subTest(value=repr(value)[:60]):
                self.assertIsNone(parse_duration(value))

    def test_jsonld_durations_belong_to_each_video_and_merge_without_erasing(self):
        data = {'@graph': [
            {'@type': 'VideoObject', 'name': 'First lesson', 'contentUrl': '/one.mp4', 'duration': 'PT1M30S'},
            {'@type': ['Thing', 'VideoObject'], 'contentUrl': '/two.mp4', 'duration': 'PT2H'},
            {'@type': 'VideoObject', 'contentUrl': '/three.mp4', 'duration': 'unknown'},
        ]}
        page = self.extract('<video src="/one.mp4"></video><script type="application/ld+json">'
                            + json.dumps(data) + '</script><video src="/two.mp4"></video>')
        self.assertEqual({video['url'].rsplit('/', 1)[-1]: video['duration'] for video in page['videos']},
                         {'one.mp4': 90, 'two.mp4': 7200, 'three.mp4': None})

    def test_html_attributes_and_microdata(self):
        page = self.extract('''<video src="/one.mp4" data-duration="12:34"></video>
          <video src="/two.mp4" duration="90.5"></video>
          <div itemscope itemtype="https://schema.org/VideoObject"><meta itemprop="duration" content="PT3M">
            <video src="/three.mp4"></video></div>
          <video><source src="/four.mp4" data-duration-seconds="5"></video>
          <video src="/unknown.mp4" data-duration-ms="5000"></video>''')
        self.assertEqual([video['duration'] for video in page['videos']], [754, 90.5, 180, 5, None])

    def test_catalogue_cards_keep_duration_and_do_not_guess_from_titles_or_dates(self):
        page = self.extract('''<main>
          <article><a href="/watch/one"><img alt="Primary lesson"></a>
            <h2><a href="/watch/one">Primary complete title</a></h2><span class="duration">01:23</span></article>
          <section class="video-card"><a href="/watch/two">Second lesson</a>
            <time itemprop="duration" datetime="PT2M4S">2:04</time></section>
          <div class="video-card" data-duration="125"><a href="/watch/three">Third lesson</a></div>
          <article><a href="/watch/four">Episode 2026 12:34</a>
            <time datetime="2026-10-23T01:23:45">12:34</time><span class="published">01:23</span></article>
          </main>''', listing=True)
        self.assertEqual([(link['url'].rsplit('/', 1)[-1], link.get('duration')) for link in page['links']],
                         [('one', 83), ('two', 124), ('three', 125), ('four', None)])
        self.assertNotIn('duration', page['links'][-1])
        self.assertEqual(page['links'][0]['title'], 'Primary complete title')

    def test_shared_containers_and_multiple_players_do_not_share_one_duration(self):
        page = self.extract('''<div class="video-list"><a href="/watch/one">Primary lesson</a>
          <a href="/watch/two">Second lesson</a><span class="duration">12:34</span></div>
          <article><video src="/first.mp4"></video><video src="/second.mp4"></video>
            <span class="duration">05:00</span></article>''', listing=True)
        self.assertEqual([link.get('duration') for link in page['links']], [None, None])
        self.assertEqual([video['duration'] for video in page['videos']], [None, None])

    def test_all_embedded_player_types_keep_ambiguous_card_duration_unknown(self):
        candidates = [
            ('<iframe src="https://www.youtube.com/embed/abcdefghijk"></iframe>',
             '<iframe src="https://www.youtube.com/embed/lmnopqrstuv"></iframe>'),
            ('<embed src="/one.mp4">', '<object data="/two.mp4"></object>'),
            ('<div class="video-player" data-video-url="/one.mp4"></div>',
             '<div class="video-player" data-video-url="/two.mp4"></div>'),
            ('<video src="/one.mp4"></video>', '<script>player({file:"/two.mp4"})</script>'),
        ]
        for first, second in candidates:
            with self.subTest(player=first):
                page = self.extract('<article class="video-card">' + first + second + '<span class="duration">01:20</span></article>')
                self.assertEqual(len(page['videos']), 2)
                self.assertEqual([video['duration'] for video in page['videos']], [None, None])

    def test_player_configuration_duration_only_from_its_immediate_properties(self):
        page = self.extract('''<script>
          jwplayer('x').setup({playlist:[{file:'/first.mp4',duration:90},
            {file:'/second.mp4',duration:'01:30'}, {file:'/third.mp4',duration:1.2e2},
            {file:'/computed.mp4',duration:12*60}, {file:'/unknown.mp4',description:'duration: 999}'},
            {file:'/fourth.mp4',description:'duration: 999}',duration:40}],
            advertising:{file:'/ads/advert.mp4',duration:888}});
          new DPlayer({video:{quality:[{url:'/quality720.mp4'},{url:'/quality1080.mp4'}],duration:'PT2M'}});
          </script>''')
        actual = {video['url'].rsplit('/', 1)[-1]: video['duration'] for video in page['videos']}
        self.assertEqual(actual, {'first.mp4': 90, 'second.mp4': 90, 'third.mp4': 120, 'computed.mp4': None,
                                  'unknown.mp4': None, 'fourth.mp4': 40, 'quality720.mp4': 120})

    def test_global_metadata_only_applies_to_one_detail_video(self):
        single = self.extract('<meta property="video:duration" content="100"><video src="/single.mp4"></video>')
        self.assertEqual(single['videos'][0]['duration'], 100)
        microdata = self.extract('<head><meta itemprop="duration" content="PT2M"></head><video src="/single.mp4"></video>')
        self.assertEqual(microdata['videos'][0]['duration'], 120)
        multiple = self.extract('<meta name="duration" content="100"><video src="/one.mp4"></video><video src="/two.mp4"></video>')
        self.assertEqual([video['duration'] for video in multiple['videos']], [None, None])
        listing = self.extract('<meta name="duration" content="100"><video src="/one.mp4"></video>', listing=True)
        self.assertIsNone(listing['videos'][0]['duration'])
        identified = self.extract('<meta property="og:video" content="/one.mp4"><meta property="video:duration" content="123">'
                                  '<video src="/one.mp4"></video><video src="/two.mp4"></video>')
        self.assertEqual([video['duration'] for video in identified['videos']], [123, None])

    def test_direct_media_cards_and_platform_links_can_have_duration(self):
        page = self.extract('''<article><a href="/lesson.mp4">Lesson</a><span class="video-duration">02:01</span></article>
          <a href="https://youtu.be/abcdef12345" data-duration="PT5M">Platform lesson</a>
          <video title="Episode 09:55" src="/unknown.mp4"></video>''', listing=True)
        self.assertEqual([video['duration'] for video in page['videos']], [121, 300, None])


if __name__ == '__main__':
    unittest.main()
