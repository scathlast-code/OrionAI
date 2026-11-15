import os
import security_policy as s

def test_country_deny():
    for country in ('RU','IR','CN','KP','US','HU','PL','TR'):
        assert not s.telegram_access(123, country)[0]

def test_allowlist(monkeypatch):
    monkeypatch.setenv('SECURITY_ALLOWED_USERS','123')
    assert s.telegram_access(123)[0]
    assert not s.telegram_access(124)[0]

def test_strict_unknown(monkeypatch):
    monkeypatch.setenv('SECURITY_REQUIRE_VERIFIED_COUNTRY','1')
    assert not s.telegram_access(123)[0]

def test_forwarded_ip_untrusted(monkeypatch):
    monkeypatch.setenv('SECURITY_TRUSTED_PROXIES','')
    assert str(s.client_ip('203.0.113.1','8.8.8.8')) == '203.0.113.1'

def test_unknown_web_geo_denied(monkeypatch):
    monkeypatch.delenv('SECURITY_GEOIP_DB', raising=False)
    monkeypatch.setenv('SECURITY_WEB_FAIL_CLOSED','1')
    assert not s.web_access('203.0.113.1')[0]
