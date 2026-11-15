import json, unittest
from pathlib import Path
from node_registry import load, validate, assign, ConfigurationError
class RegistryTests(unittest.TestCase):
    def setUp(self): self.cfg=load(Path(__file__).with_name('nodes.json'))
    def test_default_all_local(self):
        self.assertEqual(len(self.cfg['nodes']),10)
        self.assertEqual(assign(self.cfg,'greenpost'),'node-01')
        self.assertEqual(sum(n['enabled'] for n in self.cfg['nodes']),1)
    def test_move_role(self):
        self.cfg['default_mode']='distributed'; self.cfg['nodes'][0]['roles']=['telegram','miniapp']; self.cfg['nodes'][1].update(enabled=True,roles=['greenpost'])
        self.assertEqual(assign(self.cfg,'greenpost'),'node-02')
        self.assertIsNone(assign(self.cfg,'llm'))
    def test_reject_duplicate(self):
        self.cfg['nodes'][1]['id']='node-01'
        with self.assertRaises(ConfigurationError): validate(self.cfg)
    def test_reject_insecure_endpoint(self):
        self.cfg['nodes'][1]['endpoint']='http://example.org'
        with self.assertRaises(ConfigurationError): validate(self.cfg)
if __name__=='__main__': unittest.main()
