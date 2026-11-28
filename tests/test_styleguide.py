from plugins.newsroom import styleguide as S


def test_check_and_fix():
    h, b = "ШОК!!! У Киеве свято", 'Мер прийняв участь на протязі дня - сказав він. "Так" на Україні.'
    msgs = " ".join(n["msg"] for n in S.check(h, b))
    for part in ("взяти участь", "протягом", "клікбейт", "Київ", "лапки", "в Україні"):
        assert part in msgs
    out, n = S.fix(b)
    assert "взяв участь" in out and "протягом" in out and " — " in out and "«Так»" in out and "в Україні" in out and n >= 5
    assert S.fix("У Киеве")[0] == "У Киеве"  # відмінків бот сам не чіпає


def test_custom_dictionary_and_case():
    out, n = S.fix("Зрада! зрада.", "зрада → поворот")
    assert out == "Поворот! поворот." and n == 2
    assert any(x["msg"] == "словник редакції" and not x["fix"] for x in S.check("", "це важливо", "важливо"))
