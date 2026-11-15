import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tora"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    print("сервер:", bot.NODE.cost(), "| нагадування увімкнено:", bot.cluster.oracle_on(bot.NODE) if hasattr(bot, "cluster") else __import__("cluster").oracle_on(bot.NODE))
    await bot.worker_pass(); print("перший запуск — лише планує:", time.strftime("%d.%m", time.localtime(bot.NODE.get("oracle_next"))))
    bot.NODE.set("oracle_next", time.time() - 1); await bot.worker_pass()
    print(tg.last_text(U).split("\n")[0], [t for r in tg.last_buttons(U) for t, d in r])
    await tg.press(U, "m:orok"); print("після «Зайшов» — наступне:", time.strftime("%d.%m", time.localtime(bot.NODE.get("oracle_next"))))
    await tg.press(U, "m:rm"); print([l for l in tg.last_text(U).split("\n") if "Oracle" in l], [t for r in tg.last_buttons(U) for t, d in r if "Oracle" in t or t.endswith("🔕")])
    await tg.press(U, "m:orset:7"); print(bot.NODE.get("oracle_every"))
asyncio.run(main())
