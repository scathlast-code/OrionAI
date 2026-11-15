import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/tp", exist_ok=True)
import shutil; shutil.rmtree("/tmp/tp", ignore_errors=True); os.makedirs("/tmp/tp", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/tp/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", MISTRAL_API_KEY="m", SUMMARY_EVERY="100")
import httpx
DELAY = {"groq": 0, "mistral": 0}; REPLY = {}
async def handler(req):
    u = str(req.url); body = json.loads(req.content)
    who = "groq" if "groq.com" in u else "mistral" if "mistral" in u else None
    if not who: return httpx.Response(404)
    q = str(body["messages"][-1]["content"])
    await asyncio.sleep(DELAY[who])
    txt = REPLY.pop(who, None) or f"{who}: відповідь на «{q[:30]}»"
    return httpx.Response(200, json={"choices": [{"message": {"content": txt}, "finish_reason": "stop"}]})
class T(httpx.AsyncBaseTransport):
    async def handle_async_request(self, req):
        await req.aread(); return await handler(req)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", T()); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import bot
bot.Progress.__init__.__defaults__ = (True, 0.2)  # швидше для тесту
_orig_tick = bot.Progress._tick
def sent(U, method): return [p for c, m, p in tg.sent if c == U and m == method]
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    # 1) повільна відповідь → статус з'являється, оновлюється, зникає
    DELAY["groq"] = 7.5
    n0 = len(tg.sent)
    await tg.text(U, "Напиши есе про зубрів")
    news = tg.sent[n0:]
    st = [p for c, m, p in news if m == "sendMessage" and p.get("text", "").startswith("⏳")]
    ed = [p for c, m, p in news if m == "editMessageText" and p.get("text", "").startswith("⏳")]
    dl = [p for c, m, p in news if m == "deleteMessage"]
    print("status:", st[0]["text"].replace("\n", " | ") if st else None)
    print("edits:", len(ed), "| last edit:", ed[-1]["text"].replace("\n", " | ") if ed else None)
    print("status deleted:", bool(dl), "| answer:", tg.out(U)[-1]["text"][:60])
    print("typical remembered:", bot.typical_time("Думаю над відповіддю"))
    # 2) зупинка
    DELAY["groq"] = 30
    t = asyncio.create_task(tg.text(U, "Довге завдання, яке зупиню"))
    await asyncio.sleep(0.6)
    stop = [d for p in sent(U, "sendMessage") for r in (p.get("reply_markup") or {}).get("inline_keyboard", []) for b in r for d in [b.get("callback_data", "")] if d.startswith("m:stop:")][-1]
    await tg.press(U, stop); await asyncio.sleep(0.3)
    print("\nstopped:", tg.out(U)[-1]["text"].replace("\n", " | "), "| task done:", t.done())
    print("journal:", bot.journal_get(max(r["id"] for r in bot.journal_list(U, "all", 0)[0]) if hasattr(bot, "journal_list") else 0) if False else "")
    # 3) паралельні розмови: розмова А думає, переходимо в Б, ставимо інше завдання іншій моделі
    DELAY["groq"] = 3; DELAY["mistral"] = 0.5
    da = bot.current_dialog(U); bot.dialog_update(da, title="Есе про зубрів")
    ta = asyncio.create_task(tg.text(U, "Розширене есе"))
    await asyncio.sleep(0.4)
    db_ = bot.dialog_create(U); bot.dialog_update(db_, title="Рецепт борщу"); bot.set_pref(U, "dialog", db_); bot.set_choice(U, "mistral")
    tb = asyncio.create_task(tg.text(U, "Дай рецепт борщу"))
    await asyncio.sleep(1.2)
    print("\nB done while A thinking:", tb.done(), ta.done(), "|", tg.out(U)[-1]["text"][:70].replace("\n", " "))
    await ta
    outs = [p["text"] for p in tg.out(U)[-3:]]
    for o in outs: print("  >", o[:110].replace("\n", " | "))
    print("A saved to A:", [m["content"][:25] for m in bot.load_history(U, dialog_id=da)][-2:])
    # 4) у тій самій розмові — черга
    bot.set_choice(U, "auto"); DELAY["groq"] = 1.5
    t1 = asyncio.create_task(tg.text(U, "Перше")); await asyncio.sleep(0.3)
    t2 = asyncio.create_task(tg.text(U, "Друге")); await asyncio.sleep(0.2)
    print("\nqueue note:", [p["text"][:60] for p in sent(U, "sendMessage") if p["text"].startswith("🕒")][-1:])
    await t1; await t2
    # 5) відмова → кнопка іншої ШІ, яка не повторює відмовника
    DELAY["groq"] = 0; REPLY["groq"] = "Вибачте, але я не можу допомогти з цим запитом."
    await tg.text(U, "Напиши жорсткий фельєтон"); print("\nrefused:", tg.last_text(U)[:80], tg.last_buttons(U))
    await tg.press(U, tg.find(U, "Іншою ШІ")); await asyncio.sleep(0.2)
    print("retry:", tg.out(U)[-1]["text"][:80].replace("\n", " "))
    # 6) уточнення в розмові, з якої пішли
    DELAY["groq"] = 1.2; REPLY["groq"] = "Уточніть, будь ласка: для скількох людей і чи вегетаріанський?"
    bot.set_pref(U, "dialog", db_)
    tc = asyncio.create_task(tg.text(U, "Зроби меню")); await asyncio.sleep(0.3)
    bot.set_pref(U, "dialog", da); await tc
    print("\nneeds_info note:", tg.out(U)[-1]["text"].replace("\n", " | "), tg.last_buttons(U))
asyncio.run(main())
