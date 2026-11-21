from plugins.media import Plugin as Med


def test_author_url():
    assert Med.author_url({"channel_url": "https://www.youtube.com/@k"}) == "https://www.youtube.com/@k/videos"
    assert Med.author_url({"uploader_url": "https://www.tiktok.com/@k"}) == "https://www.tiktok.com/@k"
    assert Med.author_url({}) == ""


def test_dq_options_cover_ask_and_audio():
    keys = dict(Med.DQ)
    assert "ask" in keys and "a" in keys and "720" in keys
