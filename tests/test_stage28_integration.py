import tempfile, unittest
from pathlib import Path
from runtime_control import RuntimeControl

class Stage28Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.ctrl=RuntimeControl(Path(self.tmp.name)/'state.db')
    def tearDown(self):self.tmp.cleanup()
    def test_schedule_sleep_and_wake(self):
        self.ctrl.set_schedule('primary','ai',100,200,'deep')
        self.assertEqual(self.ctrl.apply_schedules(99),[])
        self.assertEqual(self.ctrl.apply_schedules(101),[('primary','ai','deep')])
        self.assertFalse(self.ctrl.can_run('primary','ai'))
        self.assertEqual(self.ctrl.apply_schedules(201),[('primary','ai','active')])
        self.assertTrue(self.ctrl.can_run('primary','ai'))
    def test_schedule_owner_only(self):
        with self.assertRaises(PermissionError):
            self.ctrl.set_schedule('primary','ai',100,200,actor='parent')
    def test_emergency_not_overridden(self):
        self.ctrl.set_schedule('primary','ai',100,200,'deep')
        self.ctrl.set_mode('primary','ai','emergency')
        self.ctrl.apply_schedules(101)
        self.ctrl.apply_schedules(201)
        self.assertEqual(self.ctrl.get_mode('primary','ai'),'emergency')
    def test_other_modules_untouched(self):
        self.ctrl.set_mode('primary','greenpost','deep')
        self.ctrl.set_schedule('primary','ai',100,200)
        self.ctrl.apply_schedules(101)
        self.assertEqual(self.ctrl.get_mode('primary','greenpost'),'deep')
