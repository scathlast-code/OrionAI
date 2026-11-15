import tempfile,unittest,os
from government_positions import Positions
class Tests(unittest.TestCase):
 def test_events(self):
  with tempfile.TemporaryDirectory() as d:
   p=Positions(os.path.join(d,'p.db'))
   args=dict(sector='медицина',organization='Установа',position='Директор',person='Особа',event='dismissed',source_url='https://example.org/act')
   self.assertTrue(p.add(**args));self.assertFalse(p.add(**args));self.assertEqual(len(p.list()),1)
   self.assertIn('Звільнені (1)',p.report())
   self.assertIn('Призначені (0)',p.report())
 def test_validation(self):
  with tempfile.TemporaryDirectory() as d:
   p=Positions(os.path.join(d,'p.db'))
   with self.assertRaises(ValueError):p.add(sector='медицина',organization='X',position='Y',person='Z',event='appointed',source_url='invalid')
if __name__=='__main__':unittest.main()
