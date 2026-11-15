import os
import tempfile
import unittest
from unittest.mock import patch
import access_grants

class TestStage15(unittest.TestCase):
    def test_grant_and_revoke(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SECURITY_GRANTS_DB': tmp+'/grants.db'}):
            self.assertEqual(access_grants.status('orion', 123456), 'absent')
            access_grants.set_grant('orion',123456,True,actor_id=999)
            self.assertEqual(access_grants.status('orion',123456), 'granted')
            self.assertEqual(access_grants.status('manager',123456), 'absent')
            access_grants.set_grant('orion',123456,False,actor_id=999)
            self.assertEqual(access_grants.status('orion',123456), 'revoked')
    def test_invalid_scope(self):
        with self.assertRaises(ValueError): access_grants.status('other',123)
