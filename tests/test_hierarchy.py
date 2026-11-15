import os
import tempfile
import unittest
from hierarchy import Hierarchy, HierarchyError

class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = Hierarchy(os.path.join(self.temp.name, 'h.db'), node_id='node-b')
        self.secret = 's' * 40

    def tearDown(self):
        self.temp.cleanup()

    def test_role_transitions(self):
        self.assertEqual(self.h.status()['role'], 'standalone')
        self.h.attach('node-c', 'child', 'c'*40)
        self.assertEqual(self.h.status()['role'], 'root')
        self.h.attach('node-a', 'parent', self.secret, ['status.read'])
        self.assertEqual(self.h.status()['role'], 'intermediate')
        self.h.detach('node-c')
        self.assertEqual(self.h.status()['role'], 'member')
        self.h.detach('node-a')
        self.assertEqual(self.h.status()['role'], 'standalone')

    def test_signed_scoped_replay(self):
        self.h.attach('node-a', 'parent', self.secret, ['status.read'])
        msg = Hierarchy.sign(self.secret, 'node-a', 'node-b', 'status.read')
        self.assertEqual(self.h.authorize_command(msg)['action'], 'status.read')
        with self.assertRaisesRegex(HierarchyError, 'replayed'):
            self.h.authorize_command(msg)
        with self.assertRaisesRegex(HierarchyError, 'permission'):
            self.h.authorize_command(Hierarchy.sign(self.secret, 'node-a', 'node-b', 'tasks.cancel'))

    def test_reject_cycles_and_parent_conflicts(self):
        with self.assertRaisesRegex(HierarchyError, 'cycle'):
            self.h.attach('node-a', 'parent', self.secret, ancestors=['node-b'])
        self.h.attach('node-a', 'parent', self.secret)
        with self.assertRaisesRegex(HierarchyError, 'only one'):
            self.h.attach('node-d', 'parent', self.secret)

    def test_signature_and_revocation(self):
        self.h.attach('node-a', 'parent', self.secret, ['status.read'])
        msg = Hierarchy.sign(self.secret, 'node-a', 'node-b', 'status.read')
        msg['payload'] = {'tampered': True}
        with self.assertRaisesRegex(HierarchyError, 'signature'):
            self.h.authorize_command(msg)
        self.h.detach('node-a')
        with self.assertRaisesRegex(HierarchyError, 'authorized parent'):
            self.h.authorize_command(Hierarchy.sign(self.secret, 'node-a', 'node-b', 'status.read'))

if __name__ == '__main__':
    unittest.main()
