import tempfile,unittest
from pathlib import Path
from government_positions import Positions

class InternationalTests(unittest.TestCase):
 def test_default_and_countries(self):
  with tempfile.TemporaryDirectory() as d:
   p=Positions(str(Path(d)/'db.sqlite'))
   self.assertEqual(p.coverage.get()['countries'],['UA'])
   p.coverage.set_countries(['UA','PL'])
   self.assertEqual(p.coverage.get()['countries'],['UA','PL'])
 def test_international_records(self):
  with tempfile.TemporaryDirectory() as d:
   p=Positions(str(Path(d)/'db.sqlite'))
   p.add(sector='health',organization='Office',position='Director',person='Example',event='appointed',source_url='https://example.org/decision',country_code='PL',territory_code='PL-MZ')
   self.assertEqual(len(p.list(country_codes=['PL'])),1)
   self.assertEqual(len(p.list(country_codes=['UA'])),0)
 def test_region_validation(self):
  with tempfile.TemporaryDirectory() as d:
   p=Positions(str(Path(d)/'db.sqlite'))
   with self.assertRaises(ValueError):p.coverage.set_regions('PL',['PL-MZ'])
   p.coverage.set_countries(['UA','PL'])
   p.coverage.set_regions('PL',['PL-MZ'])
   self.assertEqual(p.coverage.get()['regions']['PL'],['PL-MZ'])
if __name__=='__main__':unittest.main()
