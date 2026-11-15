import tempfile
from pathlib import Path
import pytest
import scenarios

def test_bot_isolation_and_persistence():
    with tempfile.TemporaryDirectory() as d:
        db=Path(d)/'test.sqlite'
        scenarios.update_profile(db,'orion',123,'research',True)
        assert next(x for x in scenarios.list_profiles(db,'orion',123) if x['id']=='research')['enabled']
        assert not next(x for x in scenarios.list_profiles(db,'manager',123) if x['id']=='research')['enabled']
        assert not next(x for x in scenarios.list_profiles(db,'orion',456) if x['id']=='research')['enabled']

def test_unknown_scenario_rejected():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            scenarios.update_profile(Path(d)/'x.sqlite','orion',1,'unknown',True)
