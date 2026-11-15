import tempfile
import unittest
from pathlib import Path
from runtime_control import RuntimeControl

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.ctrl=RuntimeControl(Path(self.tmp.name)/'runtime.db')
    def tearDown(self):self.tmp.cleanup()
    def test_default_and_isolation(self):
        self.assertTrue(self.ctrl.can_run('a','news'))
        self.ctrl.set_mode('a','news','deep')
        self.assertFalse(self.ctrl.can_run('a','news','important'))
        self.assertTrue(self.ctrl.can_run('a','news','critical'))
        self.assertTrue(self.ctrl.can_run('a','ai','normal'))
        self.assertTrue(self.ctrl.can_run('b','news','normal'))
    def test_emergency(self):
        self.ctrl.set_mode('a','ai','emergency')
        self.assertFalse(self.ctrl.can_run('a','ai','critical'))
    def test_parent_cannot_override(self):
        with self.assertRaises(PermissionError):self.ctrl.set_mode('a','news','active',actor='parent')
    def test_feedback(self):
        key=self.ctrl.add_feedback('a','news','bug','Duplicate articles')
        self.assertEqual(self.ctrl.list_feedback('a','new')[0]['id'],key)
        with self.assertRaises(PermissionError):self.ctrl.update_feedback(key,'resolved',actor='parent')
        self.ctrl.update_feedback(key,'resolved')
        self.assertEqual(self.ctrl.list_feedback('a','resolved')[0]['body'],'Duplicate articles')
if __name__=='__main__':unittest.main()
