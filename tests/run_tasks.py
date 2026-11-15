import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/t3/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", TASK_CHECK_EVERY="1")
os.makedirs("/tmp/t3", exist_ok=True)
import faketg; tg = faketg.install()
import bot
async def fake_ask(history, content, preferred=None, fallback=True):
    await asyncio.sleep(0.2); return "Готова відповідь", "groq", [], []
bot.ask_llm = fake_ask
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/later напиши план")
    print(tg.last_text(U)[:300]); print(tg.last_buttons(U)[:2])
    tid = bot.tasks_open(chat_id=U)[0]["id"]
    await tg.press(U, f"m:tsm:{tid}:manual"); print(tg.last_buttons(U)[0])
    await tg.press(U, f"m:tw:{tid}:15m"); print(tg.last_text(U)[:200])
    bot.task_update(tid, run_at=time.time() - 1)
    w = asyncio.create_task(bot.task_worker()); await asyncio.sleep(2.5)
    print("REMIND:", tg.last_text(U)); print(tg.last_buttons(U))
    await tg.press(U, f"m:trun:{tid}"); await asyncio.sleep(0.5); print("DONE:", tg.last_text(U)[:300])
    # auto
    await tg.text(U, "/later друге завдання"); t2 = bot.tasks_open(chat_id=U)[0]["id"]
    await tg.press(U, f"m:tsm:{t2}:auto"); await tg.press(U, f"m:tw:{t2}:15m"); bot.task_update(t2, run_at=time.time() - 1)
    await asyncio.sleep(2.5); print("AUTO:", tg.last_text(U)[:300]); w.cancel()
asyncio.run(main())
