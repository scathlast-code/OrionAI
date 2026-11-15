import tempfile, unittest
from pathlib import Path
from reliability import Reliability, diagnose, redact
class TestReliability(unittest.TestCase):
 def test_scopes_and_preferences(self):
  with tempfile.TemporaryDirectory() as d:
   a=Reliability('orion',Path(d)/'r.db'); b=Reliability('manager',Path(d)/'r.db')
   a.record(7,'chat',100); self.assertIsNone(b.preferences(7));a.set_option(7,'tips_enabled',False);self.assertEqual(a.preferences(7)[1],0)
 def test_maturity_and_cap(self):
  with tempfile.TemporaryDirectory() as d:
   a=Reliability('orion',Path(d)/'r.db')
   for i in range(12):a.record(7,'chat',100+i)
   self.assertIsNone(a.suggestion(7,100+13*86400,True))
   self.assertIsNotNone(a.suggestion(7,100+15*86400,True))
   self.assertIsNone(a.suggestion(7,100+15*86400+1,True))
   self.assertIsNotNone(a.suggestion(7,100+19*86400,True))
   self.assertIsNone(a.suggestion(7,100+20*86400,True))
 def test_diagnostics(self):
  d=diagnose(TimeoutError('fail'),'web');self.assertEqual(d['category'],'network')
  self.assertNotIn('ABC',redact('token=ABC'))
if __name__=='__main__':unittest.main()
