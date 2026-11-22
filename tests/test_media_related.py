from plugins.media import related as R


def test_tags_snow_regions():
    t, g = R.tags("Сніжить на Волині сьогодні ❄️")
    assert "snow" in t and g == ["volyn"]
    t, g = R.tags("Пів годинки і Карпати солідно забіліли😍❄️🌨")
    assert "snow" in t and g == ["karpaty"]
    assert R.tags("Ужгород, Закарпаття")[1] == ["zakarpattia"]  # не плутаємо з Карпатами
    assert R.tags("Олена гуляє")[0] == []  # ім'я — не олень
    assert "fire" not in R.tags("гори в тумані")[0] and "mountains" in R.tags("гори в тумані")[0]


def test_history_related(tmp_path):
    h = R.History(tmp_path / "h.json")
    h.add(1, msg=10, author=R.norm_author("tvoe_v_v"), who="tvoe_v_v", site="Threads", topics=["snow"], geos=["volyn"])
    h.add(1, msg=11, author=R.norm_author("oleno4ka"), who="oleno4ka", site="Threads", topics=["snow"], geos=["karpaty"])
    h.add(1, msg=12, author=R.norm_author("@oleno4ka"), who="oleno4ka", site="Instagram", topics=["pets"], geos=[])
    by_site, by_geo, by_topic, extra, first = h.related(1, "Oleno4ka", ["snow"], ["karpaty"])
    assert first == [] and "notopic" not in extra
    assert set(by_site) == {"Threads", "Instagram"}
    assert by_geo["karpaty"]["msg"] == 11 and by_topic["snow"]["msg"] == 11  # найновіше
    assert h.related(2, "oleno4ka", ["snow"], [])[0] == {}  # інший чат — своя історія
    assert R.icon("Threads") == "🧵" and R.icon("Instagram") == "📸"


def test_no_topic_and_first(tmp_path):
    h = R.History(tmp_path / "h.json")
    assert h.related(1, "x", [], [])[3] == {}  # нічого ще не було — жодних порожніх посилань
    h.add(1, msg=1, author="a", site="TikTok", topics=[], geos=[])
    h.add(1, msg=2, author="b", site="Threads", topics=["snow"], geos=["volyn"], first=["t:snow", "g:volyn"])
    _s, _g, _t, extra, first = h.related(1, "c", [], [])
    assert extra["notopic"]["msg"] == 1 and extra["nogeo"]["msg"] == 1 and "first" not in extra
    _s, _g, _t, extra, first = h.related(1, "c", ["rain"], ["volyn"])
    assert first == [("t", "rain")] and extra["first"]["msg"] == 2  # автор «вперше» не рахується
    gx = h.groups(1)["x"]
    assert len(gx["notopic"]) == 1 and len(gx["first"]) == 1
    assert R.first_text([("t", "snow"), ("g", "karpaty")]) == "тема «снігопад», місце «Карпати»"
    assert set(R.TOPIC_NAME) == set(R.TOPIC_LABEL) and set(R.GEO_NAME) == set(R.GEO_LABEL)
