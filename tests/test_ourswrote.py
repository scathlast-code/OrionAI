from plugins.newsroom import ourswrote as O


def test_topic_score_matches_topic_not_wording():
    a = O.stems("У Карпатах відновили популяцію зубрів. Екологи нарахували понад 50 зубрів у карпатських лісах")
    b = O.stems("У Карпатах побільшало зубрів: екологи нарахували 45 тварин у карпатських лісах")
    c = O.stems("У Києві відкрили новий парк, висадили 300 дерев")
    n, frac = O.topic_score(a, b)
    assert n >= 3 and frac >= 0.25
    assert O.topic_score(a, c)[0] < 2


def test_words_skip_stopwords():
    assert O.words("зубри в Карпатах та у лісах") == ["зубри", "карпатах", "лісах"]
