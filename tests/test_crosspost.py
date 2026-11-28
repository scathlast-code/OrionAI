from plugins.newsroom import crosspost as X


def test_x_len_counts_links_as_23():
    assert X.x_len("Дивіться https://example.com/very/long/path?a=1") == len("Дивіться ") + 23


def test_trim_by_sentence():
    t = "Перше речення тут. Друге речення значно довше за перше і не влізе."
    out = X.trim(t, 30)
    assert out == "Перше речення тут." and len(out) <= 30


def test_parse_limits_tags_and_slides():
    cfg = {**X.DEFAULTS, "tags": 5}
    data = {"th": {"text": "а" * 700, "tags": ["#Київ", "Погода", "погода"]},
            "x": {"text": "Новина https://site.ua/" + "x" * 100 + " " + "б" * 300, "tags": ["Київ"]},
            "ig": {"text": "Підпис #сніг", "slides": ["Гачок", "", "Факт " * 50]}}
    got = X.parse(data, cfg, ["th", "x", "ig"])
    assert len(got["th"]["text"]) <= 500 and got["th"]["tags"] == ["#Київ"]  # у Threads — одна тема
    assert X.x_len(got["x"]["text"]) <= 280 and got["x"]["cut"]
    assert got["ig"]["tags"] == ["#сніг"] and "#сніг" in got["ig"]["text"]
    assert len(got["ig"]["slides"]) == 2 and all(len(s) <= 120 for s in got["ig"]["slides"])


def test_prompt_mentions_only_selected():
    p = X.prompt({"headline": "Заголовок", "body": "Текст", "sources": []}, X.DEFAULTS, ["x", "th"])
    assert '"x"' in p and '"th"' in p and '"ig"' not in p


def test_parse_when():
    import datetime as dt
    from plugins.newsroom.pubqueue import parse_when
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert parse_when("18:30", now).hour == 18 and parse_when("18:30", now).day == 28
    assert parse_when("09:00", now).day == 29  # уже минуло — завтра
    assert parse_when("+45", now) == now + dt.timedelta(minutes=45)
    assert parse_when("+2г", now) == now + dt.timedelta(hours=2)
    w = parse_when("01.12 07:15", now)
    assert (w.month, w.day, w.hour, w.minute) == (12, 1, 7, 15)
    assert parse_when("25:00", now) is None and parse_when("абв", now) is None


def test_new_networks_and_youtube_title():
    assert {"tt", "yt", "tb"} <= set(X.ORDER)
    got = X.parse({"yt": {"text": "Н" * 150 + "\nОпис відео."}}, X.DEFAULTS, ["yt"])
    assert len(got["yt"]["text"].split("\n")[0]) <= 100


def test_editorial_parse_day():
    import datetime as dt
    from plugins.editorial import parse_day
    t = dt.date(2026, 11, 28)
    assert parse_day("+3", t) == "2026-12-01" and parse_day("завтра", t) == "2026-11-29"
    assert parse_day("05.01", t) == "2026-01-05" or parse_day("05.01", t) == "2027-01-05"
    assert parse_day("-", t) is None and parse_day("абв", t) is False


def test_unwrap_instagram_redirect():
    import linktools as L
    u = "https://l.instagram.com/?u=https%3A%2F%2Fgreenpost.s.gy%2Fdl9byv%3Ffbclid%3DPAZXh0&e=AUAMOYog"
    assert L.unwrap(u) == "https://greenpost.s.gy/dl9byv"
    assert L.unwrap("https://l.facebook.com/l.php?u=https%3A%2F%2Fsite.ua%2Fa%3Futm_source%3Dfb&h=x") == "https://site.ua/a"
