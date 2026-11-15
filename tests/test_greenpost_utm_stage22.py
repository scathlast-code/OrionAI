import tempfile
import unittest
from pathlib import Path
from plugins.newsroom.utm import build_url, Ledger

class UTMTests(unittest.TestCase):
    def test_query_fragment(self):
        u=build_url('https://example.org/a?x=1&utm_source=old#part','energy','telegram')
        self.assertIn('x=1',u)
        self.assertIn('utm_source=telegram',u)
        self.assertNotIn('utm_source=old',u)
        self.assertTrue(u.endswith('#part'))
    def test_restricted(self):
        with self.assertRaises(ValueError):build_url('javascript:alert(1)','tag','telegram')
        with self.assertRaises(ValueError):build_url('https://example.org','tag','youtube_video')
    def test_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'u.sqlite3')
            self.assertFalse(ledger.create('https://example.org/a','tag','telegram')[1])
            self.assertTrue(ledger.create('https://example.org/a','tag','facebook')[1])
            self.assertEqual(len(ledger.recent()),2)
            self.assertEqual(sum(x[1] for x in ledger.statistics()),2)
if __name__=='__main__': unittest.main()
