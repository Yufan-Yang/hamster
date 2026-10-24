"""Only root metadata is sent for source classification; no media access."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import crawl_source  # noqa: E402


class SourceClassificationTests(unittest.TestCase):
    def test_known_host_and_subdomain_do_not_use_model(self):
        with patch.object(crawl_source.llm, 'llm_json') as model:
            for url in ('https://51cg1.com/list', 'https://www.pornhub.com/videos', 'https://hjvideo.com/'):
                self.assertEqual(crawl_source.classify_source(url)['classification'], 'adult')
            model.assert_not_called()
        self.assertFalse(crawl_source.known_adult('https://pornhub.com.example.test/'))

    def test_only_root_url_title_description_are_classified(self):
        with patch.object(crawl_source.llm, 'llm_json', return_value={'classification': 'general', 'reason': '公开课程目录'}) as model:
            result = crawl_source.classify_source('https://example.test/lessons', '课程目录', 'Python 入门课程')
        self.assertEqual(result['classification'], 'general')
        model.assert_called_once()
        evidence = json.loads(model.call_args.args[1])
        self.assertEqual(set(evidence), {'root_url', 'root_title', 'root_description'})
        self.assertEqual(model.call_args.kwargs['purpose'], 'crawl-source')
        self.assertFalse(model.call_args.kwargs['think'])

    def test_model_can_classify_unknown_adult_source(self):
        with patch.object(crawl_source.llm, 'llm_json', return_value={'classification': 'adult', 'reason': '根页面声明成人影片目录'}):
            self.assertEqual(crawl_source.classify_source('https://example.test/')['classification'], 'adult')

    def test_unavailable_or_invalid_model_is_unknown(self):
        for result in (None, {}, {'classification': 'safe', 'reason': 'bad enum'}, {'classification': 'general', 'reason': ''}):
            with patch.object(crawl_source.llm, 'llm_json', return_value=result):
                self.assertEqual(crawl_source.classify_source('https://example.test/')['classification'], 'unknown')
        with patch.object(crawl_source.llm, 'llm_json', side_effect=RuntimeError('unavailable')):
            self.assertEqual(crawl_source.classify_source('https://example.test/')['classification'], 'unknown')

    def test_metadata_and_model_reason_are_bounded(self):
        with patch.object(crawl_source.llm, 'llm_json', return_value={'classification': 'general', 'reason': 'x' * 1000}) as model:
            result = crawl_source.classify_source('https://example.test/' + 'x' * 9000, 'x' * 9000, 'x' * 9000)
        evidence = json.loads(model.call_args.args[1])
        self.assertEqual([len(evidence[k]) for k in ('root_url', 'root_title', 'root_description')], [8192, 500, 2000])
        self.assertEqual(len(result['reason']), 300)


if __name__ == '__main__':
    unittest.main()
