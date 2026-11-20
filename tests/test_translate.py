import translate as t


def test_parse_request():
    assert t.parse_request("переклади англійською: Привіт") == ("Привіт", "en")
    assert t.parse_request("Translate to Polish:\nДобрий день") == ("Добрий день", "pl")
    assert t.parse_request("як перекласти слово кіт?") is None
    assert t.parse_request("переклади", "Hello there") == ("Hello there", "uk")
    assert t.parse_request("переклади\nЇжак іде") == ("Їжак іде", "en")
    rows = t.lang_rows(lambda c: c)
    assert [c for _b, c in rows[0]] == ["uk", "ru", "en"]
