import datetime as dt

from plugins.newsroom import alerts as A


def test_decide():
    cfg = dict(A.DEFAULTS)
    assert A.decide(cfg, {"urgency": "urgent", "importance": 8, "credibility": "media"}, "Вибух")
    assert not A.decide(cfg, {"urgency": "urgent", "importance": 8, "credibility": "anon"}, "Вибух")
    assert not A.decide(cfg, {"urgency": "urgent", "importance": 5}, "Вибух")
    assert "зубр" in A.decide({**cfg, "words": "зубр, ДСНС"}, {}, "Побачили зубрів")
    assert not A.decide({**cfg, "words": "зубр", "mute": "футбол"}, {}, "зубр на футболі")


def test_quiet_hours_over_midnight():
    t = lambda h, m=0: dt.datetime(2026, 11, 28, h, m)
    assert A.in_quiet("23:00-07:00", t(2)) and A.in_quiet("23:00-07:00", t(23, 30))
    assert not A.in_quiet("23:00-07:00", t(12)) and not A.in_quiet("", t(2))
