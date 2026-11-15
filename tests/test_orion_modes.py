import tempfile
from pathlib import Path
import pytest
import orion_modes as m

def test_persistent_separate_users():
    with tempfile.TemporaryDirectory() as d:
        db=str(Path(d)/"modes.db")
        m.set_mode(1,"offline",db)
        m.set_mode(2,"online_required",db)
        assert m.get_mode(1,db)=="offline"
        assert m.get_mode(2,db)=="online_required"
        assert m.get_mode(3,db)=="normal"

def test_incognito_fails_closed():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError): m.set_mode(1,"incognito",str(Path(d)/"m.db"))
    assert m.preflight("incognito")

def test_offline_and_orion_block_without_local_model():
    assert m.preflight("offline")
    assert m.preflight("orion_only")
    assert m.preflight("offline",local_model_ready=True) is None

def test_online_requires_verified_retrieval():
    assert m.preflight("online_required")
    assert m.preflight("online_required",web_verified=True) is None
