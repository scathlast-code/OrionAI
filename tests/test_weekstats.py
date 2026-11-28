import datetime as dt

from plugins.newsroom import weekstats as W


def P(i, hour, views, plat="tg"):
    at = dt.datetime(2026, 11, 2 + i % 20, hour).timestamp()
    return {"id": i, "at": at, "views": views, "likes": 0, "reposts": 0, "comments": 0, "platform": plat, "source": "A", "title": "", "text": "t", "url": ""}


def test_best_times_needs_three_posts_per_hour():
    posts = [P(i, 9, 3000) for i in range(3)] + [P(10 + i, 15, 1000) for i in range(5)] + [P(20, 3, 99999)]
    h, _d = W.best_times(posts)
    assert h[0][0] == 9 and all(k != 3 for k, _m, _n in h)


def test_week_summary():
    s = W.week_summary([P(i, 9, 1000 * (i + 1)) for i in range(10)], [P(50, 9, 100)])
    assert s["n"] == 10 and s["views"] == 55000 and s["top"][0][0]["id"] == 9 and len(s["flop"]) == 3
    assert W.delta(120, 100) == " (+20% до мин. тижня)"


def test_eg_parse():
    from plugins.newsroom.weekstats import eg_parse
    r = eg_parse('{"picks":[{"id":3,"why":"сезон","update":"цифри"},{"id":99}]}', {3, 4})
    assert r == [(3, "сезон", "цифри")]
    assert eg_parse("x", {1}) == []
