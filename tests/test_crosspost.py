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
