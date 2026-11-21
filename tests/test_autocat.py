import collect
import postclean
import topicsort as T


def test_topics_and_mixed():
    assert T.main_and_mixed("Вночі ворог атакував Харків дронами, є поранені, вибухи") == ("war", [])
    assert T.main_and_mixed("Нема світла через обстріли енергооб'єктів")[1]
    assert T.main_and_mixed("Синоптики: завтра сніг і мороз")[0] == "weather"
    assert T.find_topic("готуй зведення про погоду") == "weather"
    assert T.kind_of("глянь https://site.ua/x") == "link"


def test_group_and_choices():
    items = [{"id": 1, "text": "Обстріл Харкова, вибухи, поранені"}, {"id": 2, "text": "Як справи?"},
             {"id": 3, "text": "Обстріли: ракети, вибухи"}, {"id": 4, "text": "Синоптики: сніг, мороз"},
             {"id": 5, "text": "Нема світла через обстріли"}, {"id": 6, "text": "Укренерго: графіки відключень світла"}]
    pure, mixed = T.group(items)
    assert [i["id"] for i in pure["war"]] == [1, 3] and pure["weather"][0]["id"] == 4 and pure["energy"][0]["id"] == 6
    pair = next(iter(mixed))
    g = T.apply_choices(pure, mixed, {pair: "join"})
    assert any("+" in k for k in g)
    g = T.apply_choices(pure, mixed, {pair: "both"})
    assert 5 in [i["id"] for i in g["war"]] and 5 in [i["id"] for i in g["energy"]]


def test_clean_channel_junk():
    t = "Вибухи у Києві, працює ППО https://site.ua/a\n\n👉 Підписатися | @kyiv_op\nНаписати адміну — @adm_k\nt.me/kyivop\n#Київ #новини"
    c = postclean.clean(t)
    assert "Підпис" not in c and "@kyiv_op" not in c and "t.me" not in c and "адміну" not in c
    assert "site.ua/a" in c and "ППО" in c


def test_topic_batches_leave_others(tmp_path):
    c = collect.Collect(tmp_path / "c.db")
    b = collect.Batches(c)
    for t in ("Обстріл, вибухи", "Сніг і мороз", "Ракетна атака, поранені"):
        c.add(1, t, True)
    war = b.take(1, only=lambda i: "war" in T.classify(i["text"]))
    assert len(war) == 2
    b.mark(1, war, "зведення", False)
    left = b.pending(1)
    assert [i["text"] for i in left] == ["Сніг і мороз"]
    b.mark(1, left, "погода", False)
    assert not b.pending(1)
