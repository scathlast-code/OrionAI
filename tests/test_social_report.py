import asyncio, time
from plugins.newsroom import social, lists, report
from plugins.newsroom.collectors import count_num, parse_preview


def test_parse_social_links():
    P = lambda s: lists.normalize_ref(s)
    assert P("https://x.com/GreenPost") == ("social", "x:greenpost")
    assert P("https://twitter.com/i/status/1") is None
    assert P("https://www.threads.net/@green.post") == ("social", "threads:green.post")
    assert P("https://instagram.com/p/abc") is None
    assert P("instagram.com/greenpost.ua/") == ("social", "ig:greenpost.ua")
    assert P("https://www.facebook.com/profile.php?id=123456") == ("social", "fb:123456")
    assert P("https://green.tumblr.com/post/1") == ("social", "tumblr:green")
    assert P("ig:@name") == ("social", "ig:name")
    assert P("rss:https://site.ua/feed") == ("social", "rss:https://site.ua/feed")
    assert P("@greenpost") == ("public", "greenpost")          # Telegram лишається Telegram
    assert P("https://t.me/greenpost") == ("public", "greenpost")
    assert lists.display_ref("social", "x:name").startswith("𝕏")
    assert lists.link_of("social", "threads:name") == "https://www.threads.net/@name"


def test_feed_and_counts():
    rss = """<rss><channel><title>Блог</title><item><title>Новина</title><link>https://a/1</link><guid>1</guid>
    <pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;Текст новини&lt;/p&gt;</description></item></channel></rss>"""
    t, ps = social.parse_feed(rss)
    assert t == "Блог" and ps[0]["url"] == "https://a/1" and "Текст новини" in ps[0]["text"]
    assert count_num("1.2K") == 1200 and count_num("3,4M") == 3400000 and count_num("512") == 512
    page = ('<div class="tgme_widget_message" data-post="ch/5"><div class="tgme_widget_message_text">Hi</div>'
            '<span class="tgme_widget_message_views">2.5K</span></div>')
    _t, posts = parse_preview(page)
    if posts:
        assert posts[0]["metrics"]["views"] == 2500


def test_social_fetch_without_keys(monkeypatch):
    for k in ("X_BEARER_TOKEN", "RSSHUB_URL"):
        monkeypatch.delenv(k, raising=False)
    c = social.SocialCollector()
    try:
        asyncio.run(c.fetch("x:name"))
        assert False
    except RuntimeError as e:
        assert "RSSHUB_URL" in str(e)


def test_regions():
    assert "Київ" in report.regions_of("Обстріл Києва вночі")
    assert "Київ" not in report.regions_of("У Київській області") and "Київська" in report.regions_of("У Київській області")
    assert "Рівненська" not in report.regions_of("рівно о 9 ранку")
    assert "Львівська" in report.regions_of("у Львові")
    assert "Харківська" in report.regions_of("на Харківщині")


def test_analyze_and_gaps():
    now = time.time()
    topics = [{"key": "war", "title": "⚔️ Війна", "keywords": "обстріл"}, {"key": "animals", "title": "🐾 Права тварин", "keywords": "притулок"},
              {"key": "eco", "title": "🌱 Екологія", "keywords": "сміття"}, {"key": "astro", "title": "✨ Астрологія", "keywords": "гороскоп"}]
    m = lambda t, text: any(k in text.lower() for k in t["keywords"].split(","))
    posts = [{"id": i, "title": "", "text": "обстріл у Харкові", "at": now, "views": 1000, "likes": 5, "reposts": 1, "comments": 0, "platform": "tg"}
             for i in range(10)]
    posts.append({"id": 99, "title": "", "text": "притулок у Львові", "at": now, "views": 9000, "likes": 80, "reposts": 20, "comments": 9, "platform": "tg"})
    r = report.analyze(posts, [{"ours_id": 99, "kind": "quote", "source": "Інший канал"}], topics, m, [])
    v = {t["key"]: t["verdict"] for t in r["topics"]}
    assert v["animals"] == "more" and v["astro"] == "none" and v["war"] in ("ok", "less")
    reg = {g["name"]: g["verdict"] for g in r["regions"]}
    assert reg["Рівненська"] == "none" and reg["Харківська"] == "much"
    g = dict(report.gaps_of(r))
    assert "t:animals" in g and "r:Рівненська" in g


def test_shift():
    assert report.shift("2026-01", -1) == "2025-12" and report.shift("2026-12", 1) == "2027-01"
