import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/td", exist_ok=True)
import shutil; shutil.rmtree("/tmp/td", ignore_errors=True); os.makedirs("/tmp/td", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/td/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", DROPBOX_APP_KEY="a", DROPBOX_APP_SECRET="b", PUBLIC_URL="https://x/ai")
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start"); await tg.text(U, "/note тест")
    await tg.press(U, "m:bi:g:store"); await tg.press(U, "m:bmv:g:store:off")
    bot.set_pref(U, "voice_reply", True)
    for t in ("sum", "state", "nums", "tips"):
        await tg.press(U, f"m:diag:{t}"); print(f"==== {t}\n" + tg.last_text(U)); 
    print(tg.last_buttons(U))
asyncio.run(main())
