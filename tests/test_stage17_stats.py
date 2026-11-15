import tempfile, unittest, sqlite3
from pathlib import Path
import lifetime_stats as st

class Stage17Stats(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db.sqlite')
    def tearDown(self): self.tmp.cleanup()
    def test_grouping_and_incognito(self):
        st.record(self.path,bot='orion',user_id=1,mode='incognito',requests=2,input_tokens=12)
        st.record(self.path,bot='orion',user_id=1,mode='normal',requests=3)
        st.record(self.path,bot='orion',user_id=2,requests=7)
        self.assertEqual(st.by_mode(self.path,bot='orion',user_id=1)['incognito']['input_tokens'],12)
        self.assertEqual(st.by_user(self.path,bot='orion')[0]['requests'],5)
    def test_backup(self):
        st.record(self.path,bot='orion',user_id=1,requests=9)
        copy=str(Path(self.tmp.name)/'copy.db');st.backup(self.path,copy)
        self.assertEqual(st.totals(copy)['requests'],9)
    def test_no_reset_and_reopen(self):
        st.record(self.path,bot='orion',user_id=1,requests=1)
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.DatabaseError): db.execute('DELETE FROM lifetime_events')
        self.assertEqual(st.totals(self.path)['requests'],1)
