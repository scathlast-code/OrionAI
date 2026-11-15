import os
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import notification_modes as n

@pytest.fixture(autouse=True)
def tempdb(tmp_path,monkeypatch):monkeypatch.setenv('NOTIFICATION_DB_PATH',str(tmp_path/'modes.db'))

def test_independent_sleep():
    n.set_sleep('orion',True)
    assert n.is_sleeping('orion') and not n.is_sleeping('manager')
    n.set_sleep('orion',False)
    assert not n.is_sleeping('orion')

def test_quiet_crosses_midnight():
    n.set_quiet('orion',True,'23:00','08:00')
    for hour,expected in [(0,True),(7,True),(12,False),(23,True)]:
        assert n.is_quiet('orion',datetime(2026,11,5,hour,tzinfo=ZoneInfo('Europe/Kyiv')))==expected

def test_user_preferences_separate():
    n.set_user_mode('orion',42,'sound','urgent')
    n.set_user_mode('manager',42,'vibration','sos')
    assert n.user_mode('orion',42)=={'sound':'urgent','vibration':'short'}
    assert n.user_mode('manager',42)=={'sound':'soft','vibration':'sos'}

def test_invalid_options():
    with pytest.raises(ValueError):n.set_user_mode('orion',1,'sound','bad')
    with pytest.raises(ValueError):n.set_quiet('orion',True,'25:00','08:00')

def test_persistence():
    n.set_sleep('orion',True)
    assert n.bot_mode('orion')['deep_sleep']==1
