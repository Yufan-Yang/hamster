"""Offline regression checks for the Pi's Xray config transformation."""
import copy
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'pi' / 'xray-bypass-cn.py'
spec = importlib.util.spec_from_file_location('xray_bypass_cn', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RoutingTests(unittest.TestCase):
    def config(self):
        return {'api': {'tag': 'api'}, 'dns': {'servers': ['223.5.5.5']},
                'inbounds': [{'tag': 'transparent', 'protocol': 'dokodemo-door'}],
                'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                              {'tag': 'upstream', 'protocol': 'vless', 'settings': {'secret': 'unchanged'}},
                              {'tag': 'dns-out', 'protocol': 'dns'}, {'tag': 'block', 'protocol': 'blackhole'}],
                'routing': {'domainStrategy': 'AsIs', 'rules': [
                    {'inboundTag': ['api'], 'outboundTag': 'api'},
                    {'port': '53', 'outboundTag': 'dns-out'},
                    {'domain': ['geosite:category-ads-all'], 'outboundTag': 'block'},
                    {'domain': ['geosite:cn'], 'outboundTag': 'direct'},
                    {'network': 'tcp,udp', 'outboundTag': 'upstream'}]}}

    def test_replaces_domain_routing_and_preserves_services_and_credentials(self):
        config = self.config()
        original = copy.deepcopy(config)
        out = module.bypass_cn(config, 'upstream')
        self.assertEqual(config, original)
        for key in ('dns', 'inbounds', 'outbounds', 'api'):
            self.assertEqual(out[key], original[key])
        self.assertEqual(out['routing']['domainStrategy'], 'IPOnDemand')
        self.assertEqual(out['routing']['rules'][:3], original['routing']['rules'][:3])
        self.assertEqual(out['routing']['rules'][3:], [
            {'type': 'field', 'ip': ['geoip:private', 'geoip:cn'], 'outboundTag': 'direct'},
            {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'upstream'}])
        self.assertEqual(module.bypass_cn(out, 'upstream'), out)

    def test_rejects_invalid_tags_and_unreviewed_routes(self):
        for proxy, direct in [('missing', 'direct'), ('direct', 'direct'), ('upstream', 'block')]:
            with self.subTest(proxy=proxy, direct=direct), self.assertRaises(ValueError):
                module.bypass_cn(self.config(), proxy, direct)
        for rule in [{'outboundTag': 'other-proxy'}, {'balancerTag': 'pool'}]:
            config = self.config()
            config['routing']['rules'].append(rule)
            with self.assertRaises(ValueError):
                module.bypass_cn(config, 'upstream')

    def test_live_dual_node_topology_preserves_balancer_and_explicit_exits(self):
        config = self.config()
        config['outbounds'].append({'tag': 'proxy-b', 'protocol': 'vless'})
        config['routing']['balancers'] = [{'tag': 'proxy', 'selector': ['upstream', 'proxy-b'],
                                          'strategy': {'type': 'leastPing'}, 'fallbackTag': 'upstream'}]
        fixed = {'inboundTag': ['exit-b'], 'outboundTag': 'proxy-b'}
        openai = {'domain': ['geosite:openai'], 'outboundTag': 'proxy-b'}
        config['routing']['rules'] += [fixed, openai, {'network': 'tcp,udp', 'balancerTag': 'proxy'}]
        out = module.bypass_cn(config, balancer_tag='proxy')
        rules = out['routing']['rules']
        bypass = next(i for i, r in enumerate(rules) if r.get('ip') == ['geoip:private', 'geoip:cn'])
        self.assertLess(rules.index(fixed), bypass)
        self.assertGreater(rules.index(openai), bypass)
        self.assertFalse(any(r.get('domain') == ['geosite:cn'] for r in rules))
        self.assertEqual(rules[-1], {'type': 'field', 'network': 'tcp,udp', 'balancerTag': 'proxy'})
        self.assertEqual(out['routing']['balancers'], config['routing']['balancers'])
        self.assertEqual(module.bypass_cn(out, balancer_tag='proxy'), out)

    def test_cli_writes_private_candidate_and_never_overwrites_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, candidate = Path(tmp) / 'config.json', Path(tmp) / 'candidate.json'
            original = json.dumps(self.config())
            source.write_text(original)
            cmd = [sys.executable, str(SCRIPT), str(source), str(candidate), '--proxy-tag', 'upstream']
            subprocess.run(cmd, check=True, capture_output=True)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
            self.assertEqual(json.loads(candidate.read_text()), module.bypass_cn(self.config(), 'upstream'))
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            cmd[3] = str(source)
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual(source.read_text(), original)


if __name__ == '__main__':
    unittest.main()
