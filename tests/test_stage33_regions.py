import tempfile, os, unittest, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from government_positions import Positions
from government_regions import RegionSettings, REGIONS

class TestRegions(unittest.TestCase):
    def test_default_all_and_selection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Positions(os.path.join(d,'a.sqlite'))
            self.assertEqual(set(p.regions.get()),set(REGIONS))
            p.regions.set(['UA-30','UA-46'])
            self.assertEqual(p.regions.get(),['UA-46','UA-30'])
            self.assertEqual(len(p.regions.reset()),len(REGIONS))
    def test_region_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p=Positions(os.path.join(d,'a.sqlite'))
            for code in ['UA-30','UA-46']:
                p.add(sector='медицина',organization='Установа',position='Керівник',person='Особа',event='appointed',source_url='https://example.org/'+code,region_code=code)
            self.assertEqual(len(p.list(region_codes=['UA-30'])),1)
            self.assertEqual(len(p.list()),2)
            self.assertEqual(p.list(region_codes=[]),[])
    def test_reject_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p=Positions(os.path.join(d,'a.sqlite'))
            with self.assertRaises(ValueError): p.regions.set(['XX'])
            with self.assertRaises(ValueError): p.add(sector='медицина',organization='Установа',position='Керівник',person='Особа',event='appointed',source_url='https://example.org',region_code='XX')
if __name__=='__main__': unittest.main()
