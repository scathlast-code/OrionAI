import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/tr", exist_ok=True)
import shutil; shutil.rmtree("/tmp/tr", ignore_errors=True); os.makedirs("/tmp/tr", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/tr/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    for t in ("перша", "друга"): await tg.text(U, "/note " + t)
    await tg.press(U, "m:set:memory:+"); await tg.press(U, "m:bi:tl"); await tg.press(U, "m:bmv:tl:home")
    bot.set_choice(U, "groq")
    with bot.closing(bot.db()) as con, con:
        con.execute("INSERT INTO custom_providers (title, base_url, api_key, model, created) VALUES ('X','https://x','k','m',0)") if 'custom_providers' in [r[0] for r in con.execute("select name from sqlite_master")] else None
    print("before: notes", len(bot.list_notes_db(chat_id=U)), "memory", bot.prefs(U)["memory"], "tl:", bot.L.where(U, "tl"), "choice", bot.get_choice(U))
    await tg.press(U, "m:fr"); print(tg.last_text(U)[:200]); print(tg.last_buttons(U))
    await tg.press(U, "m:frt:notes"); await tg.press(U, "m:frgo"); print(tg.last_text(U))
    await tg.press(U, "m:fryes")
    print("docs sent:", sum(1 for c, m, p in tg.sent if m == "sendDocument"))
    print([p["text"][:120] for c, m, p in tg.sent if m == "sendMessage"][-2:])
    print("after: notes", len(bot.list_notes_db(chat_id=U)), "memory", bot.prefs(U)["memory"], "tl:", bot.L.where(U, "tl"), "choice", bot.get_choice(U))
    print("dialogs kept:", len(bot.dialogs_list(U)))
asyncio.run(main())
