from plugins.media import group as G


def test_split_per_link_tags():
    text = ("для всіх: тег зима\nhttps://www.threads.com/share/_p0MF6MLV сортуй по тегу полісся\n"
            "https://www.instagram.com/p/abc/\nбез тегів\nhttps://www.threads.com/share/_qQhIDyVG")
    common, items = G.split(text)
    assert common["add"] == ["зима"]
    assert [u for u, _i, _r in items] == ["https://www.threads.com/share/_p0MF6MLV", "https://www.instagram.com/p/abc/",
                                         "https://www.threads.com/share/_qQhIDyVG"]
    assert items[0][1]["add"] == ["полісся"] and items[1][1]["none"] and items[2][1] is None
    assert G.split("https://a.com/x") is None and G.split("https://a.com/x https://a.com/x") is None  # дубль — не група


def test_merge_and_own():
    assert G.merge_instr({"add": ["зима"], "drop": [], "none": False}, {"add": ["Полісся"], "drop": [], "none": False})["add"] == ["зима", "Полісся"]
    assert G.merge_instr({"none": True}, None)["none"]
    assert G.merge_instr(None, None) is None
    assert G.own_tags("Полісся, озеро")["add"] == ["Полісся", "озеро"]
    assert G.own_tags("без тегів")["none"]
    assert G.instr_short(None) == "теги — сам визначу"
