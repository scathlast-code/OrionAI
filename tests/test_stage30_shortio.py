import asyncio
import json
import tempfile
import unittest
from pathlib import Path
import httpx
from plugins.newsroom.utm import Ledger
from plugins.newsroom.shortio import ShortIO, ShortIORegistry, create_pair, ShortIOError

class TestShortIO(unittest.TestCase):
    def test_create_pair_and_existing_utm(self):
        def handler(req):
            self.assertEqual(req.headers['authorization'],'test-key')
            data=json.loads(req.content)
            self.assertEqual(data['domain'],'greenpost.s.gy')
            self.assertIn('utm_source=telegram',data['originalURL'])
            self.assertNotIn('gpt',data['originalURL'])
            return httpx.Response(200,json={'secureShortURL':'https://greenpost.s.gy/test1','idString':'123'})
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'utm.db');reg=ShortIORegistry(Path(d)/'short.db')
            api=ShortIO('test-key','greenpost.s.gy','42',httpx.MockTransport(handler))
            pair=asyncio.run(create_pair(ledger,reg,api,'https://greenpost.ua/news/item','autumn','telegram'))
            self.assertEqual(pair['short_url'],'https://greenpost.s.gy/test1')
            self.assertEqual(len(reg.recent()),1)
            self.assertEqual(len(ledger.recent()),1)
    def test_stats(self):
        def handler(req):
            self.assertIn('link_clicks',str(req.url))
            return httpx.Response(200,json={'123':17,'456':0})
        api=ShortIO('key','greenpost.s.gy','42',httpx.MockTransport(handler))
        self.assertEqual(asyncio.run(api.link_clicks(['123','456'])),{'123':17,'456':0})
    def test_api_failure_does_not_record(self):
        def handler(req): return httpx.Response(403,json={'message':'denied'})
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'utm.db');reg=ShortIORegistry(Path(d)/'short.db')
            api=ShortIO('key','greenpost.s.gy','42',httpx.MockTransport(handler))
            with self.assertRaises(ShortIOError):
                asyncio.run(create_pair(ledger,reg,api,'https://greenpost.ua/x','autumn','telegram'))
            self.assertEqual(reg.recent(),[])
            self.assertEqual(ledger.recent(),[])
if __name__=='__main__': unittest.main()
