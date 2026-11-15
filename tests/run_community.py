import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/tc", exist_ok=True)
import shutil; shutil.rmtree("/tmp/tc", ignore_errors=True); os.makedirs("/tmp/tc", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/tc/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import httpx
def handler(req):
    body = json.loads(req.content); q = str(body["messages"][-1]["content"])
    return httpx.Response(200, json={"choices": [{"message": {"content": "🦬 Зубри — гордість Полісся! (допис від ШІ)"}, "finish_reason": "stop"}]})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
G = -100500
ADMIN = {"status": "administrator", "user": faketg.BOT_USER, "can_be_edited": False, "is_anonymous": False, "can_manage_chat": True,
         "can_delete_messages": True, "can_manage_video_chats": True, "can_restrict_members": True, "can_promote_members": False,
         "can_change_info": True, "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False, "can_delete_stories": False}
tg.handlers["getChatMember"] = lambda p: ADMIN
tg.handlers["getChatAdministrators"] = lambda p: [dict(ADMIN, user={"id": 7, "is_bot": False, "first_name": "Модер"})]
for m in ("restrictChatMember", "banChatMember", "unbanChatMember", "approveChatJoinRequest", "declineChatJoinRequest", "leaveChat"):
    tg.handlers[m] = (lambda name: (lambda p: tg.sent.append((p.get("chat_id"), name, p)) or True))(m)
_orig = tg.handle
def handle(method, p):
    if method == "sendMessage" and int(p.get("chat_id", 0)) == G:
        tg.sent.append((G, method, p)); m = tg._msg(G, p); m["chat"] = {"id": G, "type": "supergroup", "title": "Зубри"}; return m
    return _orig(method, p)
tg.handle = handle
import bot
GC = {"id": G, "type": "supergroup", "title": "Зубри"}
def gmsg(uid, text, name="Петро", **extra):
    m = {"message_id": next(tg.msg_id), "date": int(time.time()), "chat": GC, "from": {"id": uid, "is_bot": False, "first_name": name}, "text": text}
    m.update(extra); return {"update_id": next(tg.upd_id), "message": m}
def g_sent(method="sendMessage"): return [p for c, m, p in tg.sent if str(c) == str(G) and m == method]
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    # 1) чужий додав бота — бот виходить
    await tg.feed({"update_id": next(tg.upd_id), "my_chat_member": {"chat": {"id": -1009, "type": "supergroup", "title": "Чужа"}, "from": {"id": 555, "is_bot": False, "first_name": "Хтось"},
        "date": int(time.time()), "old_chat_member": {"status": "left", "user": faketg.BOT_USER}, "new_chat_member": {"status": "member", "user": faketg.BOT_USER}}})
    print("foreign add:", tg.last_text(U)[:110], "| left:", any(m == "leaveChat" for c, m, p in tg.sent))
    # 2) власник додав адміном
    await tg.feed({"update_id": next(tg.upd_id), "my_chat_member": {"chat": GC, "from": {"id": U, "is_bot": False, "first_name": "Owner"}, "date": int(time.time()),
        "old_chat_member": {"status": "left", "user": faketg.BOT_USER}, "new_chat_member": ADMIN}})
    print("owner add:", tg.last_text(U).replace("\n", " | "), tg.last_buttons(U))
    cid = f"tg:{G}"
    await tg.press(U, f"m:cm:c:{cid}"); print("\n" + tg.last_text(U)); print(tg.last_buttons(U))
    await tg.press(U, f"m:cm:r:{cid}"); await tg.press(U, f"m:cm:rt:{cid}:links"); await tg.press(U, f"m:cm:rt:{cid}:words")
    print("\nprompt:", tg.last_text(U)); await tg.text(U, "дурень, лох")
    print("rules:", tg.last_text(U).split("\n")[-4:])
    # 3) порушення
    await tg.feed(gmsg(42, "гляньте https://spam.example"))
    print("\nviolation:", [p["text"] for p in g_sent()][-1], "| deleted:", len(g_sent("deleteMessage")))
    btn = json.loads(json.dumps(g_sent()[-1]["reply_markup"]))["inline_keyboard"][0][0]["callback_data"]
    await tg.feed(gmsg(42, "ти дурень"))
    await tg.feed(gmsg(7, "адмін може https://ok.example", name="Модер")); print("admin not punished:", "Модер" not in str(g_sent()[-1]["text"]))
    await tg.feed(gmsg(42, "ще http://x.y.com"))
    print("3rd:", g_sent()[-1]["text"][:80], "| restrict:", any(m == "restrictChatMember" for c, m, p in tg.sent))
    # 4) запит дозволу: чужий натискає — відмова, той самий — запит власнику
    async def gpress(uid, data):
        await tg.feed({"update_id": next(tg.upd_id), "callback_query": {"id": "1", "from": {"id": uid, "is_bot": False, "first_name": "Петро"}, "chat_instance": "g", "data": data,
                       "message": {"message_id": 1, "date": 0, "chat": GC}}})
    await gpress(43, btn); await gpress(42, btn)
    print("\nrequest to owner:", tg.last_text(U).replace("\n", " | "), tg.last_buttons(U))
    await tg.press(U, tg.find(U, "1 день")); print("decided:", tg.out(U)[-1]["text"][-30:], "| group:", g_sent()[-1]["text"])
    n = len(g_sent("deleteMessage")); await tg.feed(gmsg(42, "тепер можна https://ok.example")); print("permit works:", len(g_sent("deleteMessage")) == n)
    # 5) заявка на вступ
    await tg.press(U, f"m:cm:jm:{cid}:ask")
    await tg.feed({"update_id": next(tg.upd_id), "chat_join_request": {"chat": GC, "from": {"id": 77, "is_bot": False, "first_name": "Оля", "username": "olya"}, "user_chat_id": 77, "date": int(time.time())}})
    print("\njoin:", tg.last_text(U).replace("\n", " | ")); await tg.press(U, tg.find(U, "Прийняти")); print("approved:", any(m == "approveChatJoinRequest" for c, m, p in tg.sent))
    # 6) публікація від ШІ щодня + текстова зараз
    await tg.press(U, f"m:cm:pe:{cid}:aipost"); await tg.text(U, "про зубрів"); print("\n" + tg.last_text(U)[:80], tg.last_buttons(U))
    await tg.press(U, tg.find(U, "Щодня")); await tg.text(U, "00:00"); print(tg.last_text(U).replace("\n", " | ")[:200])
    await bot.CM.tick(bot.tg_app.bot); print("not yet (today slot passed):", "Зубри —" not in g_sent()[-1]["text"]); await tg.press(U, "m:cm:pgo:1"); print("published AI:", g_sent()[-1]["text"])
    await tg.press(U, f"m:cm:pe:{cid}:post"); await tg.text(U, "Збори в суботу о 10:00"); await tg.press(U, tg.find(U, "Зараз")); print("now:", g_sent()[-1]["text"])
    # 7) /rules для учасника, /mute від адміна
    await tg.feed(gmsg(42, "/rules", entities=[{"type": "bot_command", "offset": 0, "length": 6}])); print("\n/rules:", g_sent()[-1]["text"].replace("\n", " | "))
    # 8) журнал, діагностика, нав
    await tg.press(U, f"m:cm:lg:{cid}"); print("\n" + tg.last_text(U))
    d = bot.diag_collect(U); print("\ndiag:", [x for x in d["ok"] + d["good"] + d["tips"] if "🏘" in x or "🙋" in x])
    await tg.press(U, "m:cm"); print("\nlist:", tg.last_buttons(U))
    # звичайна розмова в приваті не зачеплена
    await tg.text(U, "Привіт"); print("private ok:", tg.last_text(U)[:40])
asyncio.run(main())
