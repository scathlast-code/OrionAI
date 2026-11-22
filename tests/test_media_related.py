from plugins.media import related as R


def keys(pairs):
    return [R.key_of(n) for n, _p in pairs]


def test_tags_snow_regions():
    t, g = R.tags("Сніжить на Волині сьогодні ❄️")
    assert "снігопад" in keys(t) and keys(g) == ["волинь"]
    t, g = R.tags("Пів годинки і Карпати солідно забіліли😍❄️🌨")
    assert "снігопад" in keys(t) and keys(g) == ["карпати"]
    assert keys(R.tags("Ужгород, Закарпаття")[1]) == ["закарпаття"]  # не плутаємо з Карпатами
    assert R.tags("Олена гуляє")[0] == []  # ім'я — не олень
    assert "гори" in keys(R.tags("гори в тумані")[0]) and "пожежа" not in keys(R.tags("гори в тумані")[0])
    assert keys(R.tags("Ранок у Закопаному")[1]) == ["польща"]  # не лише Україна


def test_ai_parse_and_lang():
    t, g, lang = R.parse_ai('ок {"topics":[{"name":"вибори","phrase":"про вибори"}],"places":[{"name":"Альпи"}],"lang":"de"}')
    assert t == [("вибори", "про вибори")] and g == [("Альпи", "· Альпи")] and lang == "de"
    assert R.parse_ai("немає json") == ([], [], "")
    assert R.guess_lang("Сніжить на Волині сьогодні, гарно") == "uk"
    assert R.guess_lang("Snow in the mountains today, beautiful") == "en"
    assert R.guess_lang("❄️❄️") == ""  # без тексту — мови немає (категорії «без мови» не буває)


def test_history_related(tmp_path):
    h = R.History(tmp_path / "h.json")
    h.add(1, msg=10, author=R.norm_author("tvoe_v_v"), who="tvoe_v_v", site="Threads", topics=["снігопад"], geos=["волинь"], langs=["українська"])
    h.add(1, msg=11, author=R.norm_author("oleno4ka"), who="oleno4ka", site="Threads", topics=["снігопад"], geos=["карпати"], langs=["українська"])
    h.add(1, msg=12, author=R.norm_author("@oleno4ka"), who="oleno4ka", site="Instagram", topics=["тварини"], geos=[])
    r = h.related(1, "Oleno4ka", ["снігопад"], ["карпати"], ["українська"])
    assert set(r["site"]) == {"Threads", "Instagram"}
    assert r["g"]["карпати"]["msg"] == 11 and r["t"]["снігопад"]["msg"] == 11 and r["l"]["українська"]["msg"] == 11
    assert r["first"] == [] and "notopic" not in r["x"]
    assert h.related(2, "oleno4ka", ["снігопад"], [])["site"] == {}  # інший чат — своя історія
    assert R.icon("Threads") == "🧵" and R.icon("Instagram") == "📸"


def test_no_topic_first_and_vocab(tmp_path):
    h = R.History(tmp_path / "h.json")
    assert h.related(1, "x", [], [])["x"] == {}  # нічого ще не було — жодних порожніх посилань
    h.add(1, msg=1, author="a", site="TikTok", topics=[], geos=[])
    h.add(1, msg=2, author="b", site="Threads", topics=["снігопад"], geos=["волинь"], first=["t:снігопад", "g:волинь"])
    r = h.related(1, "c", [], [])
    assert r["x"]["notopic"]["msg"] == 1 and r["x"]["nogeo"]["msg"] == 1 and "first" not in r["x"]
    keys_ = h.learn("t", [("вибори", "про вибори")])
    r = h.related(1, "c", keys_, ["волинь"])
    assert r["first"] == [("t", "вибори")] and r["x"]["first"]["msg"] == 2  # автор «вперше» не рахується
    assert h.label("t", "вибори") == "про вибори" and h.label("g", "карпати") == "з Карпат"
    assert h.first_text([("t", "снігопад"), ("g", "карпати")]) == "тема «снігопад», локація «Карпати»"
    gx = h.groups(1)["x"]
    assert len(gx["notopic"]) == 1 and len(gx["first"]) == 1 and "nolang" not in gx


def test_old_keys_migrate(tmp_path):
    h = R.History(tmp_path / "h.json")
    h.add(1, msg=1, author="a", topics=["snow"], geos=["volyn"], first=["t:snow"])
    e = h.items(1)[0]
    assert e["topics"] == ["снігопад"] and e["geos"] == ["волинь"] and e["first"] == ["t:снігопад"]


def test_geotag():
    from plugins.media import media as M
    assert M.geotag({"location": {"name": "Synevyr Lake"}}) == "Synevyr Lake"
    assert M.geotag({"location": "Буковель"}) == "Буковель"
    assert M.geotag({}) == ""


def test_parse_instr():
    P = R.parse_instr
    assert P("https://www.threads.com/share/_p0MF6MLV\n\nсортуй по тегу полісся")["add"] == ["полісся"]
    assert P("https://x.y/z групуй всі за озером")["add"] == ["озером"]
    assert P("https://x.y/z без тегів")["none"] and P("не став теги")["none"] and P("не сортуй")["none"]
    r = P("групуй за озером, але без тегу снігопад")
    assert r["add"] == ["озером"] and r["drop"] == ["снігопад"]
    assert P("#полісся #зима")["add"] == ["полісся", "зима"]
    assert P("що це за відео?") is None and P("https://x.y/z") is None


def test_user_tags_saved_and_matched(tmp_path):
    h = R.History(tmp_path / "h.json")
    k = h.learn_user("g", "Полісся", "з Полісся")
    assert h.label("g", k) == "з Полісся" and h.known("g")[0] == "Полісся"  # свої теги — першими для ШІ
    assert h.match_user("Туман на Поліссі зранку")["g"] == [k]
    h.drop_user("g", k)
    assert h.user_tags() == []


def test_emoji_no_false_matches():
    t = keys(R.tags("Пів годинки і Карпати солідно забіліли😍❄️🌨")[0])
    assert t == ["снігопад"]  # не «дощ» і не «спека» через невидимі знаки в емодзі
    assert keys(R.tags("ллє як з відра")[0]) == ["дощ"] and keys(R.tags("все засипало за ніч")[0]) == ["снігопад"]


def test_manual_update(tmp_path):
    h = R.History(tmp_path / "h.json")
    rid = h.add(1, msg=5, author="a", topics=[], geos=[], notag=True)
    e = h.update(1, rid, topics=["снігопад"])
    assert e["topics"] == ["снігопад"] and "notag" not in e and h.by_msg(1, 5)["id"] == rid


def test_search(tmp_path):
    h = R.History(tmp_path / "h.json")
    h.add(1, msg=1, author="karpatyposydok", who="karpaty_posydok", title="Будиночки в Карпатах", topics=["снігопад"], geos=["карпати"])
    h.add(1, msg=2, author="tvoe", who="tvoe_v_v", title="Сніжить на Волині", topics=["снігопад"], geos=["волинь"])
    got, res = h.search(1, "снігопад з Карпат")
    assert [e["msg"] for e in res] == [1] and "📍 Карпати" in got
    assert [e["msg"] for e in h.search(1, "від tvoe_v_v")[1]] == [2]
    assert len(h.search(1, "сніг")[1]) == 2
