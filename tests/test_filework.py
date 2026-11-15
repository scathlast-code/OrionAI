import io, tempfile, unittest, zipfile
from pathlib import Path
import filework, delivery, scenarios

class FileTests(unittest.TestCase):
    def test_zip_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); (d/'a.txt').write_text('hello')
            z=d/'a.zip';filework.pack([d/'a.txt'],z)
            files=filework.unpack(z,d/'out')
            self.assertEqual(files[0].read_text(),'hello')
    def test_zip_slip(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);z=d/'bad.zip'
            with zipfile.ZipFile(z,'w') as f:f.writestr('../evil.txt','x')
            with self.assertRaises(filework.FileProblem):filework.unpack(z,d/'out')
    def test_many_files(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);z=d/'many.zip'
            with zipfile.ZipFile(z,'w') as f:
                for i in range(201):f.writestr(f'{i}.txt','x')
            with self.assertRaises(filework.FileProblem):filework.unpack(z,d/'out')
    def test_split(self):
        self.assertEqual(len(delivery.split_paragraphs('a'*12000)),4)
        self.assertTrue(all(len(x)<=3800 for x in delivery.split_paragraphs('a'*12000)))
    def test_scenario(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'s.db');scenarios.set_option(db,123,'ask_delivery',True)
            self.assertTrue(scenarios.get(db,123)['ask_delivery'])

if __name__=='__main__': unittest.main()
