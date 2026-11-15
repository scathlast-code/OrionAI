import access_grants
import security_policy

def test_permanent_scoped_grant(tmp_path,monkeypatch):
    monkeypatch.setenv('SECURITY_GRANTS_DB',str(tmp_path/'access.sqlite'))
    monkeypatch.setenv('SECURITY_REQUIRE_GRANT','1')
    monkeypatch.delenv('SECURITY_ALLOWED_USERS',raising=False)
    access_grants.set_grant('orion',123,True)
    assert access_grants.status('orion',123)=='granted'
    assert access_grants.status('manager',123)=='absent'
    assert security_policy.telegram_access(123,scope='orion')[0]
    assert not security_policy.telegram_access(123,scope='manager')[0]
    assert security_policy.telegram_access(123,country='UA',scope='orion')[0]
    assert not security_policy.telegram_access(123,country='RU',scope='orion')[0]
    access_grants.set_grant('orion',123,False)
    assert not security_policy.telegram_access(123,scope='orion')[0]

def test_expiration_and_audit(tmp_path,monkeypatch):
    monkeypatch.setenv('SECURITY_GRANTS_DB',str(tmp_path/'access.sqlite'))
    access_grants.set_grant('manager',456,True,expires_at=2000000000)
    assert access_grants.status('manager',456,now=1999999999)=='granted'
    assert access_grants.status('manager',456,now=2000000000)=='expired'
    with access_grants.connect() as db:
        assert db.execute('SELECT count(*) FROM audit').fetchone()[0]==1
