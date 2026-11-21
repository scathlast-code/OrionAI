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
