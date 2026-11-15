import security_policy as s

def test_ukraine_unconditionally_allowed_by_geo():
    for sub in ('43','65','23',None,'RU'):
        assert not s.region_blocked('UA', sub)

def test_new_country_blocks():
    for country in 'AF YE PS IL ID RS'.split():
        assert s.region_blocked(country)

def test_parent_countries_remain_accessible(monkeypatch):
    for country in ('BA','MD','GE'):
        assert not s.region_blocked(country)
    monkeypatch.setenv('SECURITY_BLOCKED_MD_SUBDIVISIONS','SN')
    assert s.region_blocked('MD','SN')
    assert not s.region_blocked('MD','CU')
    monkeypatch.setenv('SECURITY_BLOCKED_BA_SUBDIVISIONS','RS')
    assert s.region_blocked('BA','RS')
    assert not s.region_blocked('BA','BIH')

def test_telegram_geo_when_supplied():
    assert not s.telegram_access(123,'AF')[0]
    assert s.telegram_access(123,'UA')[0]
