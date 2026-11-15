import tempfile
import unittest
import sqlite3
from pathlib import Path
import lifetime_stats as st

class LifetimeStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'stats.db')
    def tearDown(self): self.tmp.cleanup()
    def test_all_modes_and_users(self):
        for mode in st.MODES: st.record(self.path,bot='orion',user_id=42,mode=mode,requests=2,duration_ms=150)
        st.record(self.path,bot='manager',user_id=43,requests=3)
        self.assertEqual(st.totals(self.path,bot='orion',user_id=42)['requests'],10)
        self.assertEqual(st.totals(self.path,mode='incognito')['requests'],2)
        self.assertEqual(st.totals(self.path)['requests'],13)
    def test_immutable(self):
        st.record(self.path,bot='orion',user_id=42,requests=1)
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.DatabaseError): db.execute('DELETE FROM lifetime_events')
            with self.assertRaises(sqlite3.DatabaseError): db.execute('UPDATE lifetime_events SET requests=0')
    def test_no_content_columns(self):
        st.record(self.path,bot='orion',user_id=1,mode='incognito',requests=1)
        with sqlite3.connect(self.path) as db:
            cols=[r[1] for r in db.execute('PRAGMA table_info(lifetime_events)')]
        self.assertNotIn('text',cols)
        self.assertNotIn('message_id',cols)
        self.assertNotIn('prompt',cols)
    def test_negative_rejected(self):
        with self.assertRaises(ValueError): st.record(self.path,bot='orion',user_id=1,requests=-1)
    def test_survives_reopen(self):
        st.record(self.path,bot='orion',user_id=1,requests=5)
        self.assertEqual(st.totals(self.path,bot='orion')['requests'],5)
