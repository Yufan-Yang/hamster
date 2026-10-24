"""No-network regressions for crawl DNS rebinding and browser request handling."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import crawl, crawl_net, web  # noqa: E402
from urllib3.connection import HTTPConnection  # noqa: E402
from urllib3.exceptions import NewConnectionError  # noqa: E402


def address(ip):
    return (2, 1, 6, '', (ip, 443))


class NetworkTests(unittest.TestCase):
    def test_validate_checks_same_hostname_again(self):
        fetcher = crawl.Fetcher()
        self.addCleanup(fetcher.close)
        with patch.object(web, 'internal_link', side_effect=[None, 'private after DNS change']) as check:
            fetcher.validate('https://example.test/first')
            with self.assertRaisesRegex(ValueError, 'DNS change'):
                fetcher.validate('https://example.test/next')
            self.assertEqual(check.call_count, 2)

    def test_socket_pins_checked_ip_and_restores_tls_hostname(self):
        connection = crawl_net.PublicHTTPSConnection('example.test', 443)
        with patch.object(crawl_net.socket, 'getaddrinfo', return_value=[address('93.184.216.34')]), \
             patch.object(HTTPConnection, '_new_conn', autospec=True, side_effect=lambda conn: conn._dns_host):
            self.assertEqual(connection._new_conn(), '93.184.216.34')
        self.assertEqual(connection.host, 'example.test')
        self.assertEqual(connection._dns_host, 'example.test')

    def test_private_connection_and_mixed_dns_never_open_socket(self):
        for addresses in ([address('127.0.0.1')], [address('93.184.216.34'), address('192.168.3.200')],
                          [address('::1')], [address('169.254.169.254')]):
            connection = crawl_net.PublicHTTPSConnection('example.test', 443)
            with patch.object(crawl_net.socket, 'getaddrinfo', return_value=addresses), \
                 patch.object(HTTPConnection, '_new_conn') as connect:
                with self.assertRaises(ValueError):
                    connection._new_conn()
                connect.assert_not_called()

    def test_reconnect_rechecks_dns_and_alternate_addresses_stay_pinned(self):
        connection = crawl_net.PublicHTTPSConnection('example.test', 443)
        calls = []
        def connect(conn):
            calls.append(conn._dns_host)
            if len(calls) == 1:
                raise NewConnectionError(conn, 'fixture unreachable first address')
            return Mock()
        with patch.object(crawl_net.socket, 'getaddrinfo', side_effect=[
                [address('93.184.216.34'), address('1.1.1.1')], [address('127.0.0.1')]]), \
             patch.object(HTTPConnection, '_new_conn', autospec=True, side_effect=connect):
            connection._new_conn()
            with self.assertRaises(ValueError):
                connection._new_conn()
        self.assertEqual(calls, ['93.184.216.34', '1.1.1.1'])
        self.assertEqual(connection.host, 'example.test')

    def test_session_uses_guarded_http_and_https_pools(self):
        fetcher = crawl.Fetcher()
        self.addCleanup(fetcher.close)
        self.assertFalse(fetcher.session.trust_env)
        for scheme, cls in [('http', crawl_net.PublicHTTPConnection), ('https', crawl_net.PublicHTTPSConnection)]:
            adapter = fetcher.session.get_adapter(scheme + '://example.test/')
            pool = adapter.poolmanager.connection_from_url(scheme + '://example.test/')
            self.assertIs(pool.ConnectionCls, cls)

    def test_browser_main_document_http_failure_preserves_retryable_status(self):
        for status in (404, 502):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fetcher = crawl.Fetcher()
                self.addCleanup(fetcher.close)
                response = requests.Response()
                response.status_code = status
                response.url = 'https://example.test/dynamic'
                response._content = b'gateway response'
                response._content_consumed = True
                manager = MagicMock()
                browser = manager.__enter__.return_value.chromium.launch.return_value
                ctx = browser.new_context.return_value
                page = ctx.new_page.return_value
                request = SimpleNamespace(resource_type='document', frame=page.main_frame, url=response.url,
                                          method='GET', post_data_buffer=None, all_headers=lambda: {})
                routed = Mock(request=request)

                def navigate(*args, **kwargs):
                    ctx.route.call_args.args[1](routed)
                    raise RuntimeError('Playwright generic navigation aborted')

                page.goto.side_effect = navigate
                with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                        patch.object(crawl.core, 'STATE', Path(directory)), \
                        patch.object(web, 'internal_link', return_value=None), \
                        patch.object(fetcher.session, 'request', return_value=response):
                    with self.assertRaises(requests.HTTPError) as caught:
                        fetcher.render(response.url)
                self.assertEqual(caught.exception.response.status_code, status)
                self.assertEqual(bool(crawl._transient_failure(caught.exception)), status == 502)
                routed.abort.assert_called_once()
                routed.fulfill.assert_not_called()
                page.content.assert_not_called()
                browser.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
