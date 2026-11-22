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


def test_pending_limit(tmp_path):
    import types
    from plugins.media import Plugin
    p = Plugin.__new__(Plugin)
    p.dir = tmp_path
    p.pend_seen = {}
    msg = p.pend_add(1, [(f"https://x.com/a/{i}", None) for i in range(48)])
    assert "майже повна" in msg
    msg = p.pend_add(1, [(f"https://x.com/b/{i}", None) for i in range(5)])
    assert "найдавніші 3" in msg and len(p.pend_list(1)) == 50 and p.pend_list(1)[0]["url"] == "https://x.com/a/3"
    p.pend_add(1, [("https://x.com/b/0", None)])  # дубль не додається
    assert len(p.pend_list(1)) == 50
    p.pend_done(1, "https://x.com/b/0")
    assert len(p.pend_list(1)) == 49
