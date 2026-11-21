import asyncio
from types import SimpleNamespace as N



def test_editorial_collects_silently_and_assigns(tmp_path):
    from plugins import Host as H
    from plugins.editorial import Plugin
    sent = []

    async def notify(uid, text, rows=None):
        sent.append(text)
    h = H(data_dir=str(tmp_path), admins=lambda: [1], is_admin=lambda u: True)
    h.notify = notify
    p = Plugin(h)
    chat = N(id=-1001234567, type="supergroup", title="Редакція", username="")
    user = N(full_name="Лариса", is_bot=False)

    def msg(mid, text, fwd=None):
        return N(message=N(chat=chat, from_user=user, sender_chat=None, text=text, caption=None, message_id=mid, forward_origin=fwd), channel_post=None)
    asyncio.run(p.on_channel_post(msg(1, "привіт"), None))
    assert sent and "редакційний" in sent[0]
    asyncio.run(p.route(None, None, 1, ["chon", str(chat.id)]))
    asyncio.run(p.on_channel_post(msg(2, "Треба написати про ліси до п'ятниці, я візьму"), None))
    asyncio.run(p.on_channel_post(msg(3, "Новина", fwd=object()), None))
    asyncio.run(p.on_channel_post(msg(4, "Домовились: публікуємо зранку"), None))
    c = p.counts()
    assert c["task"] == 1 and c["news"] == 1 and c["decision"] == 1
    t, _r = p.list_screen(1, "task")
    assert "t.me/c/1234567/2" in t and "👤 <code>ЛАРИСА</code>" in t
    # одну тему на сайт двоє не беруть
    from contextlib import closing
    with closing(p.db()) as con, con:
        con.execute("INSERT INTO items(chat,msg,kind,text,who,day,where_,key) VALUES(0,0,'task','Ліси','Валентин','2026-11-25','Сайт','a')")
        con.execute("INSERT INTO items(chat,msg,kind,text,who,day,where_,key) VALUES(0,0,'task','Ліси','','2026-11-25','Сайт','b')")
        iid = con.execute("SELECT id FROM items WHERE key='b'").fetchone()[0]
    assert "уже бере Валентин" in p.set_who(iid, "Лариса")


def test_people_link_to_telegram(tmp_path):
    from plugins import Host as H
    from plugins.editorial import Plugin
    from contextlib import closing
    import time
    p = Plugin(H(data_dir=str(tmp_path), admins=lambda: [1], is_admin=lambda u: True))
    with closing(p.db()) as con, con:
        con.execute("INSERT INTO people(name,src,added) VALUES('Валентин','manual',?)", (time.time(),))
        con.execute("INSERT INTO items(chat,msg,kind,text,who,key) VALUES(0,0,'task','Ліси','Валентин','k')")
    k = str(p.people().index("Валентин"))
    asyncio.run(p.route(None, None, 77, ["pme", k]))
    assert p.my_names(77) == ["Валентин"] and p.my_open(77) == (1, 1)
    t, _r = p.people_screen(77)
    assert "<code>ВАЛЕНТИН</code>" in t and "⭐" not in t


def test_posts_only_on_command(tmp_path):
    from plugins import Host as H
    from plugins.editorial import Plugin
    from contextlib import closing
    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append((chat, text))
    h = H(data_dir=str(tmp_path), admins=lambda: [1], is_admin=lambda u: True)
    h.bot = lambda: Bot()
    p = Plugin(h)
    p._pd = {}
    with closing(p.db()) as con, con:
        con.execute("INSERT INTO chats(id,title,username,on_,seen) VALUES(-100,'Редакція','',1,0)")
    asyncio.run(p.on_text(None, None, 1, "potext", None, "Планерка о 10:00"))
    asyncio.run(p.route(None, None, 1, ["pochat", "-100"]))
    asyncio.run(p.route(None, None, 1, ["powhen", "now"]))
    assert not sent  # ще не підтверджено
    asyncio.run(p.route(None, None, 1, ["pook"]))
    assert sent == [(-100, "Планерка о 10:00")]


def test_parse_pptx():
    import io
    import zipfile
    from plugins.editorial import Plugin
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("ppt/slides/slide2.xml", "<p:sld><a:p><a:r><a:t>Підсумки</a:t></a:r></a:p></p:sld>")
        z.writestr("ppt/slides/slide1.xml", "<p:sld><a:p><a:r><a:t>Звіт </a:t></a:r><a:r><a:t>за листопад</a:t></a:r></a:p><a:p><a:r><a:t>12 публікацій</a:t></a:r></a:p></p:sld>")
    assert Plugin.parse_pptx(b.getvalue()) == [["Звіт за листопад", "12 публікацій"], ["Підсумки"]]


def test_talk_merges_decisions_and_posts_live_in_chats(tmp_path):
    from plugins import Host as H
    from plugins.editorial import Plugin
    h = H(data_dir=str(tmp_path), admins=lambda: [1], is_admin=lambda u: True)
    p = Plugin(h)
    t, rows = p.home(1)
    labels = [b[0] for r in rows for b in r]
    assert not any("Рішення" == l.split(" ·")[0][2:] for l in labels)
    assert any(l.startswith("💬 Обговорення й рішення") for l in labels)
    assert not any("Що й коли" in l for l in labels)
    assert sum("Контент-план" in l for l in labels) == 1
    t, rows = p.chats_screen(1)
    assert any("Що й коли" in b[0] for r in rows for b in r)
    t, rows = p.list_screen(1, "decision")
    assert "Обговорення й рішення" in t


def test_news_screen_has_topics(tmp_path):
    from tests.test_plugins_lists import news
    from plugins.editorial import Plugin
    np_, _ = news(tmp_path)
    np_.host.extra["plugin"] = lambda pid: np_ if pid == "news" else None
    p = Plugin(np_.host)
    t, rows = p.list_screen(1, "news")
    labels = " ".join(b[0] for r in rows for b in r)
    for w in ("Гарячі", "Екологія", "Агро", "Здоров", "Енергетика", "Нові чернетки", "Джерела", "Звіт"):
        assert w in labels, w


def test_personal_settings_are_per_user(tmp_path):
    import asyncio as aio
    from plugins import Host as H
    from plugins.editorial import Plugin
    sent = []

    async def notify(uid, text, rows=None):
        sent.append((uid, text))
    h = H(data_dir=str(tmp_path), admins=lambda: [1], is_admin=lambda u: True)
    h.notify = notify
    p = Plugin(h)
    aio.run(p.route(None, None, 1, ["mef", "only_mine"]))
    assert p.up(1)["only_mine"] and not p.up(2)["only_mine"]
    aio.run(p.route(None, None, 2, ["men", "tasks"]))
    with p.db() as con:
        con.execute("INSERT INTO accounts(name, uid) VALUES('Лариса', 2)")
        con.execute("INSERT INTO items(chat,msg,kind,text,who,at,key) VALUES(0,0,'task','Про ліси','',0,'k1')")
    iid = 1
    aio.run(p.route(None, None, 1, ["set", str(iid), "-"]))
    p.set_who(iid, "Лариса")
    aio.run(p.notify_assigned(iid, "Лариса", by=1))
    assert any(u == 2 and "призначено" in t for u, t in sent)
    aio.run(p.notify_new("task", "Нове завдання", "", "Чат"))
    assert any(u == 2 and "Нове завдання" in t for u, t in sent) and not any(u == 1 and "Нове завдання" in t for u, t in sent)
    t, _ = p.me_screen(2)
    assert "ЛАРИСА" in t and "лише нові завдання" in t
