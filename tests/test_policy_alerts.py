import tempfile
from pathlib import Path
import provider_policy_monitor as pm

def test_persistent_change_alert():
    with tempfile.TemporaryDirectory() as d:
        path=str(Path(d)/'db.sqlite')
        pm.register(path,'github','https://github.com')
        pm.check(path,'github',lambda _:b'old')
        pm.check(path,'github',lambda _:b'new')
        alarms=pm.active_alerts(path)
        assert len(alarms)==1 and alarms[0]['kind']=='changed'
        assert len(pm.active_alerts(path))==1
        assert pm.acknowledge_alert(path,alarms[0]['id'])
        assert pm.active_alerts(path)==[]

def test_error_alert_persists_after_recovery():
    with tempfile.TemporaryDirectory() as d:
        path=str(Path(d)/'db.sqlite')
        pm.register(path,'github','https://github.com')
        def fail(_):raise OSError('offline')
        pm.check(path,'github',fail)
        pm.check(path,'github',lambda _:b'ok')
        assert len(pm.active_alerts(path))==1
