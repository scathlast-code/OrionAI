import types

import botguide as G


def fake_bot():
    store = {}
    B = types.SimpleNamespace(
        HUBS={"nr": ("📰 Новинарня", "Новини.", [("🔗 UTM-мітки", "m:utm", 0, "open")])},
        PROVIDERS={"gemini": {"title": "Gemini"}}, ERR={"rate_day": ("📅", "Денний ліміт вичерпано")},
        FIX_SHORT={"rate_day": "зачекайте до завтра або іншу модель"},
        prefs=lambda c: {"length": "normal", **store}, set_pref=lambda c, k, v: store.__setitem__(k, v),
        get_choice=lambda c: store.get("_m", "auto"), set_choice=lambda c, v: store.__setitem__("_m", v),
        PLUGINS=types.SimpleNamespace(get=lambda pid: None),
        get_state=lambda pid: {"cooldown_kind": "rate_day", "err_ts": 1}, paused_until=lambda pid: 10 ** 12)
    return B, store


def test_guide_and_settings():
    B, store = fake_bot()
    assert "UTM" in G.guide(B, "UTM-мітки") and "Новинарня" in G.overview(B)
    assert "short" in G.settings_set(B, 1, "length", "short") and store["length"] == "short"
    assert "Недопустиме" in G.settings_set(B, 1, "length", "дуже")
    assert "Невідоме" in G.settings_set(B, 1, "password", "1")
    assert G.settings_set(B, 1, "memory", "9999") and store["memory"] == 200  # межі
    assert "немає" in G.settings_set(B, 1, "model", "xxx")


def test_status_explains_pause():
    B, _ = fake_bot()
    s = G.status(B)
    assert "на паузі" in s and "денний ліміт" in s and "зачекайте" in s
