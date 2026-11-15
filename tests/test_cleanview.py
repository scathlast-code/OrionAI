import tempfile, unittest
from pathlib import Path
import cleanview

class CleanViewTests(unittest.TestCase):
    def test_clean_keeps_claims_and_hides_sources(self):
        text='**Висновок**\n1. Перше твердження.\n2. Друге твердження.\n\nДжерела:\nhttps://example.org'
        result=cleanview.clean(text)
        self.assertIn('Перше твердження.',result)
        self.assertIn('Друге твердження.',result)
        self.assertNotIn('example.org',result)
        self.assertNotIn('**',result)
    def test_toggle_is_reversible(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'a.sqlite')
            original='**Текст**\nДжерела:\nhttps://example.org'
            cleanview.store(db,7,99,original)
            cleaned,on=cleanview.toggle(db,7,99)
            self.assertTrue(on)
            self.assertEqual(cleaned,'Текст')
            restored,on=cleanview.toggle(db,7,99)
            self.assertFalse(on)
            self.assertEqual(restored,original)
    def test_unknown_message(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cleanview.toggle(str(Path(d)/'x.db'),7,1))

if __name__=='__main__': unittest.main()
