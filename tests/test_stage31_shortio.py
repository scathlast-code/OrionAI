import tempfile
import unittest
from pathlib import Path
from plugins.newsroom.shortio import ShortIORegistry, normalize_link_rows, import_links, ShortIOError

class TestStage31(unittest.TestCase):
    def test_import_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            registry=ShortIORegistry(Path(d)/'short.sqlite')
            rows=normalize_link_rows({'links':[{'originalURL':'https://greenpost.ua/a?utm_source=facebook&utm_content=autumn','secureShortURL':'https://greenpost.s.gy/abc','idString':'17'}]})
            self.assertEqual(import_links(registry,rows),1)
            self.assertEqual(import_links(registry,rows),0)
            self.assertEqual(registry.recent()[0][3],'facebook')
    def test_reject_unknown_response(self):
        with self.assertRaises(ShortIOError): normalize_link_rows({'unexpected':True})
    def test_invalid_stats_dimension(self):
        import asyncio
        from plugins.newsroom.shortio import ShortIO
        with self.assertRaises(ValueError): asyncio.run(ShortIO('key','greenpost.s.gy','42').statistics('password'))
if __name__=='__main__': unittest.main()
