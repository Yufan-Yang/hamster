"""Synthetic public-page fixtures for catalogue extraction; no network or site data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang.crawl_extract import extract_page  # noqa: E402


def run(check):
    def media_groups():
        page = extract_page('''<title>Two demonstrations</title>
          <base href="https://example.test/assets/">
          <video title="First demo" src="first.mp4?clip=1&amp;token=a">
            <source src="first-1080.mp4?clip=1"><source src="first.webm?clip=1">
          </video>
          <video title="Second demo"><source src="first.mp4?clip=2&amp;token=a"></video>
          <a href="first.mp4?clip=1&amp;token=a">Download first</a>
          <video src="/ads/preroll.mp4"></video>
          <video src="/segments/chunk-001.mp4"></video>''', 'https://example.test/catalogue/', listing=False)
        assert len(page['videos']) == 2, page
        first, second = page['videos']
        assert first['title'] == 'First demo'
        assert first['media_urls'] == ['https://example.test/assets/first.mp4?clip=1&token=a',
                                       'https://example.test/assets/first-1080.mp4?clip=1',
                                       'https://example.test/assets/first.webm?clip=1']
        assert second['media_urls'] == ['https://example.test/assets/first.mp4?clip=2&token=a']
        assert all('https://example.test/catalogue/' not in video['aliases'] for video in page['videos'])

    check('crawl extraction: qualities form one video, query IDs and separate players survive', media_groups)

    def catalogue():
        page = extract_page('''<title>Lectures &amp; experiments</title>
          <link rel="canonical" href="/catalogue/science/">
          <meta name="description" content="Public educational clips">
          <header><a href="/video/unrelated">Site navigation</a></header>
          <main><article><a href="/watch?id=11"><img alt="Pendulum"></a>
            <h2><a href="/watch?id=11">Pendulum experiment</a></h2></article>
          <div class="video-card"><a href="/watch?id=12">Ocean tides</a></div>
          <a href="/category/other">Other category</a>
          <a href="/catalogue/science/">This directory</a>
          <a href="/">Home page</a><a href="?sort=recent">Sort newest</a>
          <a href="javascript:alert(1)">Invalid link</a></main>
          <aside><article><a href="/watch?id=88">Recommended</a></article></aside>
          <nav class="pagination"><a href="?page=1">1</a><a href="?page=2">2</a>
            <a href="?page=2" rel="next">下一页</a><a href="?page=99">尾页</a>
            <a href="/category/other?page=2" rel="next">Next</a></nav>
          <footer><a href="/article/privacy">Privacy policy</a></footer>''', 'https://example.test/catalogue/science/')
        assert page['title'] == 'Lectures & experiments'
        assert page['description'] == 'Public educational clips'
        assert page['links'] == [{'url': 'https://example.test/watch?id=11', 'title': 'Pendulum experiment'},
                                 {'url': 'https://example.test/watch?id=12', 'title': 'Ocean tides'}], page
        assert page['pages'] == ['https://example.test/catalogue/science/?page=2'], page
        assert extract_page('<main><article><a href="/watch?id=99">Related clip</a></article></main>',
                            'https://example.test/watch?id=11', listing=False)['links'] == []

    check('crawl extraction: useful cards, duplicate anchors, navigation and detail recursion', catalogue)

    def pagination():
        cases = [
            ('https://example.test/catalogue/', '/catalogue/page/2/', 'https://example.test/catalogue/page/2/'),
            ('https://example.test/catalogue/page/2/', '/catalogue/page/3/', 'https://example.test/catalogue/page/3/'),
            ('https://example.test/list.html', '/list_2.html', 'https://example.test/list_2.html'),
            ('https://example.test/list.php?category=science&page=2', '?category=science&page=3', 'https://example.test/list.php?category=science&page=3'),
            ('https://example.test/catalogue/?cursor=abc', '?cursor=def', 'https://example.test/catalogue/?cursor=def'),
            ('https://example.test/catalogue/', '/catalogue/2/', 'https://example.test/catalogue/2/'),
            ('https://example.test/catalogue/index.html', '/catalogue/page/2/', 'https://example.test/catalogue/page/2/'),
        ]
        for current, href, expected in cases:
            parsed = extract_page(f'<nav class="pagination"><a rel="next" href="{href}">下一页</a></nav>', current)
            assert parsed['pages'] == [expected], (current, parsed)
        parsed = extract_page('''<nav class="pagination">
          <a href="?category=science&page=1">1</a><a rel="prev" href="?category=science&page=1">上一页</a>
          <a href="?category=arts&page=3" rel="next">Next</a>
          <a href="?category=science&page=99" rel="last">99</a>
          <a href="https://other.test/list.php?category=science&page=3" rel="next">Next</a>
          </nav>''', 'https://example.test/list.php?category=science&page=2')
        assert parsed['pages'] == [], parsed
        parsed = extract_page('<link rel="next" href="?page=2">', 'https://example.test/catalogue/')
        assert parsed['pages'] == ['https://example.test/catalogue/?page=2']
        parsed = extract_page('<a class="next" href="?page=2">Next →</a>', 'https://example.test/catalogue/')
        assert parsed['pages'] == ['https://example.test/catalogue/?page=2']

    check('crawl extraction: forward-only pagination keeps path and category boundaries', pagination)

    def scripts():
        page = extract_page('''<title>Laboratory videos</title>
        <script>const first = new DPlayer({container: document.getElementById('p1'), video: {
          quality: [{name: '720p', url: '/media/one-720.m3u8?id=11&quality=720'},
                    {name: '1080p', url: '/media/one-1080.m3u8?id=11&quality=1080'}]}});
        const second = new DPlayer({video: {url: '/media/two.mp4?id=12'}});
        jwplayer('p3').setup({playlist: [{title:'Three', sources:[{file:'/media/three.mp4'}, {file:'/media/three.webm'}]},
           {title:'Four', file:'/media/four.mp4'}], advertising:{tag:'/ads/ad.m3u8'}});
        const irrelevant = "https://ads.example.test/ads/banner.mp4";
        window.neverExecute = () => { throw new Error('must not execute'); };
        </script>''', 'https://example.test/lesson/1')
        assert len(page['videos']) == 4, page
        assert sorted(len(v['media_urls']) for v in page['videos']) == [1, 1, 2, 2], page
        assert any(v['url'].endswith('two.mp4?id=12') for v in page['videos'])
        assert all('/ads/' not in value for video in page['videos'] for value in video['media_urls'])

    check('crawl extraction: safe JS literals retain player and playlist boundaries', scripts)

    def jsonld_and_platforms():
        page = extract_page('''<title>A lesson</title><link rel="canonical" href="/lesson/canonical">
          <script type="application/ld+json">{"@graph":[{"@type":"VideoObject", "name":"Wave tank",
            "description":"Water waves in a small tank", "contentUrl":"/stream?id=7", "url":"/lesson/canonical"}]}</script>
          <video src="/stream?id=7"></video>''', 'https://example.test/lesson/7', listing=False)
        assert len(page['videos']) == 1, page
        assert page['videos'][0]['media_urls'] == ['https://example.test/stream?id=7']
        assert page['videos'][0]['title'] == 'Wave tank'
        assert 'https://example.test/lesson/canonical' in page['videos'][0]['aliases']
        assert 'https://example.test/lesson/7' in page['videos'][0]['aliases']
        page = extract_page('''<iframe src="https://www.youtube-nocookie.com/embed/abcdef12345"></iframe>
          <a href="https://youtu.be/abcdef12345?t=4">First lesson</a>
          <iframe src="https://player.bilibili.com/player.html?bvid=BV123abc&amp;page=2"></iframe>
          <a href="https://www.bilibili.com/video/BV123abc/?p=2">Part two</a>
          <a href="https://www.bilibili.com/video/BV123abc/?p=3">Part three</a>
          <aside><a href="https://youtu.be/ignored1234">Recommended lesson</a></aside>''', 'https://example.test/catalogue/')
        assert len(page['videos']) == 3, page
        assert {v['url'] for v in page['videos']} == {'https://www.youtube.com/watch?v=abcdef12345',
            'https://www.bilibili.com/video/BV123abc/?p=2', 'https://www.bilibili.com/video/BV123abc/?p=3'}
        assert all(not v['media_urls'] for v in page['videos'])

    check('crawl extraction: JSON-LD, extensionless media, watch aliases and multipart identity', jsonld_and_platforms)

    def lazy_and_malformed():
        page = extract_page('''<div class="dplayer" data-video-id="123" data-player-index="0"></div>
          <div class="dplayer" data-video-id="123" data-player-index="0"></div>
          <div class="dplayer" data-video-id="123" data-player-index="1"></div>
          <div class="dplayer" data-video-id="../secret" data-player-index="1"></div>
          <div class="dplayer" data-video-id="123" data-player-index="../bad"></div>
          <script type="application/ld+json">{broken</script>
          <main><ul><li><a href="/watch?id=1">One lesson<li><a href="/watch?id=2">Two lessons</ul></main>
          <video src="https://user:password@example.test/secret.mp4"></video>
          <a href="https://example.test:bad/video/3">bad port</a>''', 'https://example.test/catalogue/')
        assert page['lazy_players'] == ['https://example.test/play-url/123.json?i=0', 'https://example.test/play-url/123.json?i=1']
        assert [link['url'] for link in page['links']] == ['https://example.test/watch?id=1', 'https://example.test/watch?id=2'], page
        assert page['videos'] == []
        assert extract_page('', 'https://example.test/catalogue/')['title'] == ''

    check('crawl extraction: lazy public player APIs, malformed HTML, invalid credentials and ports', lazy_and_malformed)


if __name__ == '__main__':
    def check(name, fn):
        fn()
        print('ok   ', name)
    run(check)
