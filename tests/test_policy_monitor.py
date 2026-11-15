import tempfile
import unittest
from pathlib import Path
import provider_policy_monitor as p
class MonitorTests(unittest.TestCase):
    def test_baseline_and_change(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'x.db');p.register(db,'openai','https://openai.com/terms')
            self.assertTrue(p.check(db,'openai',lambda u:b'a')['initial_baseline'])
            self.assertFalse(p.check(db,'openai',lambda u:b'a')['changed'])
            self.assertTrue(p.check(db,'openai',lambda u:b'b')['changed'])
            self.assertEqual(len(p.changes(db)),1)
            self.assertTrue(p.acknowledge(db,1))
            self.assertEqual(p.changes(db)[0]['status'],'reviewed')
    def test_invalid_url(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):p.register(str(Path(d)/'x.db'),'test','http://example.com')
    def test_fetch_failure(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'x.db');p.register(db,'test','https://openai.com')
            def fail(u):raise TimeoutError('timeout')
            self.assertIn('error',p.check(db,'test',fail))
            self.assertEqual(len(p.changes(db)),0)
