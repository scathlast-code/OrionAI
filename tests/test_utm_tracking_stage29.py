import tempfile
import unittest
from pathlib import Path
from plugins.newsroom.utm_tracking import ShortLinks,device_info

class TestShortLinks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=ShortLinks(Path(self.tmp.name)/'utm.sqlite','https://go.example.org')
    def tearDown(self): self.tmp.cleanup()
    def test_two_links_and_clean_tags(self):
        x=self.s.create('https://example.org/news?id=4','ecology','telegram')
        self.assertIn('utm_content=ecology',x['full_url'])
        self.assertNotIn('gpt',x['full_url'].lower())
        self.assertEqual(self.s.resolve(x['code']),x['full_url'])
    def test_stats_and_bot_filter(self):
        x=self.s.create('https://example.org/','agro','facebook')
        self.s.record(x['code'],'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Version/17 Safari/604.1','https://facebook.com/post')
        self.s.record(x['code'],'TelegramBot (like TwitterBot)')
        report=self.s.report()
        self.assertEqual(report['clicks'],1)
        self.assertEqual(report['platforms'][0]['name'],'facebook')
        self.assertEqual(report['devices'][0]['name'],'mobile')
        self.assertEqual(report['cities'][0]['name'],'unknown')
    def test_disable_and_invalid(self):
        x=self.s.create('https://example.org','x','twitter')
        self.assertIsNone(self.s.resolve('../bad'))
        self.assertTrue(self.s.disable(x['code']))
        self.assertIsNone(self.s.resolve(x['code']))
    def test_reject_http_base(self):
        with self.assertRaises(ValueError): ShortLinks(Path(self.tmp.name)/'x.db','http://example.org')

if __name__=='__main__': unittest.main()
