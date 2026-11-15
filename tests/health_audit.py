"""Bounded diagnostics and browser error privacy; all network/model calls mocked."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('health_fixtures',Path(__file__).with_name('steward.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
from shiguang import core, health_audit as health  # noqa: E402


class HealthTests(unittest.TestCase):
    setUp=fixtures.StewardTests.setUp
    client=fixtures.StewardTests.client
    collection=fixtures.StewardTests.collection

    def test_browser_report_contains_only_position_and_error_type(self):
        with patch.object(health.heal,'report') as report:
            body={'file':'/static/steward.js','line':72,'name':'TypeError'}
            self.assertTrue(self.alice.post('/api/health/client',json=body).json['accepted'])
            self.assertFalse(self.alice.post('/api/health/client',json=body).json['accepted'])
            report.assert_called_once()
            self.assertEqual(report.call_args.kwargs['context'],body)
            for n in range(73,77):self.alice.post('/api/health/client',json={**body,'line':n})
            self.assertEqual(report.call_count,3)

    def test_arbitrary_message_external_scripts_and_unknown_code_are_rejected(self):
        body={'file':'/static/steward.js','line':72,'name':'TypeError'}
        with patch.object(health.heal,'report') as report:
            for bad in [{**body,'message':'private title'},{**body,'file':'https://example.com/a.js'},
                        {**body,'file':'/static/../secret.js'},{**body,'file':'/static/unknown.js'},
                        {**body,'line':True},{**body,'name':'secret'}]:
                self.assertEqual(self.alice.post('/api/health/client',json=bad).status_code,400)
            report.assert_not_called()

    def test_anonymous_reports_cannot_consume_healer_budget(self):
        client=core.app.test_client()
        self.assertEqual(client.post('/api/health/client',json={'file':'/','line':72,'name':'Error'}).status_code,401)

    def test_scheduled_backend_failures_need_recurrence(self):
        with patch.object(health.shutil,'which',return_value=None),patch.object(health.urllib.request,'urlopen',side_effect=OSError('private URL')),patch.object(health.heal,'report') as report:
            first=health.audit();self.assertEqual(first['confirmed'],0);report.assert_not_called()
            second=health.audit();self.assertEqual(second['confirmed'],2)
            self.assertEqual(report.call_count,2)
            self.assertNotIn('private URL',str(report.call_args_list))

    def test_successful_probes_clear_previous_failures(self):
        def response(url,**kwargs):
            return io.BytesIO(b'<html></html>' if url.endswith('/') else json.dumps({'workers':[]}).encode())
        core.kv_set('health-audit:last',{'failures':[['backend','stale failure']]})
        with patch.object(health.shutil,'which',return_value=None),patch.object(health.urllib.request,'urlopen',side_effect=response),patch.object(health.heal,'report') as report:
            self.assertFalse(health.audit()['failures']);report.assert_not_called()

    def test_missing_frontend_asset_is_detected_without_page_contents(self):
        entry=self.path/'grabber.py';entry.write_text('')
        (self.path/'index.html').write_text('<script src="/static/missing.js"></script>')
        def response(url,**kwargs):return io.BytesIO(b'<html></html>' if url.endswith('/') else b'{"workers":[]}')
        with patch.object(core,'ENTRY',str(entry)),patch.object(health.shutil,'which',return_value=None),patch.object(health.urllib.request,'urlopen',side_effect=response),patch.object(health.heal,'report') as report:
            self.assertEqual(len(health.audit()['failures']),1)
            health.audit();report.assert_called_once()


if __name__=='__main__':unittest.main()
