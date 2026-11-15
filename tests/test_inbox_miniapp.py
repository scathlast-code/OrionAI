import asyncio
import json
import os
import subprocess
import sys
import types
from pathlib import Path

from plugins import Host
from plugins.inbox import Plugin as Inbox

ROOT = Path(__file__).resolve().parents[1]


class Bot:
    def __init__(self):
        self.sent, self.copied, self.n = [], [], 100

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))

    async def copy_message(self, to, frm, mid, **kw):
        self.n += 1
        self.copied.append((to, frm, mid))
        return types.SimpleNamespace(message_id=self.n)


def msg(chat, text, mid, reply_to=None):
    replies = []

    async def reply_text(t, **kw):
        replies.append(t)
    m = types.SimpleNamespace(chat=types.SimpleNamespace(type="private"), chat_id=chat, text=text, caption=None, message_id=mid,
                              reply_to_message=reply_to, forward_origin=None, reply_text=reply_text)
    for k in ("photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation", "location", "contact", "poll"):
        setattr(m, k, None)
    return m, replies


def upd(uid, m, name="Олена"):
    return types.SimpleNamespace(message=m, effective_user=types.SimpleNamespace(id=uid, full_name=name, username="olena"))


def test_stranger_to_owner_and_reply(tmp_path):
    bot = Bot()
    h = Host(data_dir=tmp_path, admins=lambda: [1], is_admin=lambda u: u == 1, is_allowed=lambda u: u in (1, 7), bot=lambda: bot)
    p = Inbox(h)
    ctx = types.SimpleNamespace(bot=bot)
    m, rep = msg(55, "/start", 1)
    assert asyncio.run(p.on_private(upd(55, m), ctx)) and "Вітаю" in rep[0]
    m, rep = msg(55, "У нас у селі зрізали парк", 2)
    assert asyncio.run(p.on_private(upd(55, m), ctx))
    assert bot.copied[-1] == (1, 55, 2) and "Олена" in bot.sent[-1][1] and "Передав" in rep[0]
    owner_copy = bot.n
    # власник відповідає reply на скопійоване повідомлення
    m2, rep2 = msg(1, "Дякую, перевіримо", 9, reply_to=types.SimpleNamespace(message_id=owner_copy))
    assert asyncio.run(p.on_private(upd(1, m2, "Власник"), ctx))
    assert bot.copied[-1] == (55, 1, 9) and "Надіслано" in rep2[0]
    # звичайне повідомлення власника не перехоплюється
    m3, _ = msg(1, "Привіт, ШІ", 10)
    assert not asyncio.run(p.on_private(upd(1, m3, "Власник"), ctx))
    # учасник: звичайне — до ШІ; після /owner — власнику
    m4, _ = msg(7, "питання до ШІ", 11)
    assert not asyncio.run(p.on_private(upd(7, m4, "Учасник"), ctx))
    p.compose.add(7)
    m5, _ = msg(7, "Особисто вам", 12)
    assert asyncio.run(p.on_private(upd(7, m5, "Учасник"), ctx)) and bot.copied[-1] == (1, 7, 12)
    # блокування
    asyncio.run(p.route(None, None, 1, ["b", "55"]))
    n = len(bot.copied)
    m6, _ = msg(55, "ще", 13)
    assert asyncio.run(p.on_private(upd(55, m6), ctx)) and len(bot.copied) == n
    # запрошення не перехоплюється
    m7, _ = msg(99, "/start inv_abc", 14)
    assert not asyncio.run(p.on_private(upd(99, m7), ctx))


SCRIPT = r'''
import asyncio, json, bot, miniapp
bot.init_db()
async def main():
    out = {}
    r = await miniapp.run(bot, 1, "screen", "m:pl:news")
    out["news"] = [r["text"][:20], r["rows"][0][0][0]]
    r = await miniapp.run(bot, 1, "screen", "m:pl:news:Lnew")
    out["await"] = r["awaiting"]
    r = await miniapp.run(bot, 1, "text", None, text="Мій список")
    out["created"] = "Мій список" in r["text"]
    out["nav"] = any(d.startswith(("m:nav", "imp:")) for row in r["rows"] for _t, d in row)
    out["chat_history"] = bot.L.current(1)
    print(json.dumps(out, ensure_ascii=False))
asyncio.run(main())
'''


def test_miniapp_bridge_same_actions(tmp_path):
    env = dict(os.environ, TELEGRAM_TOKEN="", MCP_SECRET="x" * 20, ALLOWED_USERS="1", DB_PATH=str(tmp_path / "bot.db"))
    r = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["news"][1].startswith("📥 Нові чернетки") and out["await"] and out["created"]
    assert not out["nav"] and out["chat_history"] is None


PARITY = r'''
import asyncio, json, sys
import sandbox_build as SB          # тимчасова база з демо-даними
import bot, miniapp
SB.seed()
screens = asyncio.run(SB.crawl(limit=400))
bad = []
async def check():
    for key in screens:
        r = await miniapp.run(bot, 1, "screen", key)
        if not r["text"] and not r["toast"]:
            bad.append(key)
asyncio.run(check())
print(json.dumps({"n": len(screens), "bad": bad}))
'''


def test_every_chat_screen_exists_in_miniapp(tmp_path):
    """Повна аналогічність: кожен екран, доступний у чаті, відкривається і в застосунку."""
    env = dict(os.environ, SANDBOX_OUT=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", PARITY], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["n"] > 100 and out["bad"] == [], out["bad"][:20]


HUB = r'''
import asyncio, json, types, bot
from starlette.applications import Starlette
from starlette.testclient import TestClient
bot.init_db()
hub = bot.PLUGINS.plugins["hub"]; M = hub.M
out = {}
t, scr = asyncio.run(hub.route(None, None, 1, []))
flat = [d for r in scr[1] for _t, d in r]
out["home"] = scr[0][:20]; out["all_translated"] = all(d.startswith(("m:pl:hub", "m:cm", "url:", "copy:")) for d in flat)
out["has_bots"] = "m:pl:hub:bl" in flat and "m:pl:hub:parent" in flat
asyncio.run(hub.route(None, None, 1, ["badd2", "http"]))
msg = types.SimpleNamespace(chat_id=1, text="Монітор сайту", caption=None, forward_origin=None, document=None, photo=None, reply_to_message=None,
    entities=[], caption_entities=[], message_id=5, from_user=types.SimpleNamespace(id=1, is_bot=False, username="me", full_name="Я"), chat=types.SimpleNamespace(type="private"))
async def nop(*a, **k): pass
hub.host.notify = nop
out["handled"] = asyncio.run(hub.on_private(types.SimpleNamespace(message=msg, effective_user=msg.from_user), None))
b = M.bots_list()[0]
c = TestClient(Starlette(routes=hub.routes("x")))
out["hb"] = c.post(f"/hub/{M.SECRET}/hb", json={"key": b["inbox_key"]}).json()["ok"]
out["in"] = c.post(f"/hub/{M.SECRET}/in", json={"key": b["inbox_key"], "text": "подія"}).json()["ok"]
print(json.dumps(out, ensure_ascii=False))
'''


def test_orion_is_the_hub(tmp_path):
    env = dict(os.environ, TELEGRAM_TOKEN="", MCP_SECRET="x" * 20, ALLOWED_USERS="1", DB_PATH=str(tmp_path / "bot.db"))
    env.pop("MANAGER_SECRET", None)
    r = subprocess.run([sys.executable, "-c", HUB], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"home": out["home"], "all_translated": True, "has_bots": True, "handled": True, "hb": True, "in": True}
