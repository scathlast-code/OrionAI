import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tshow"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", GEMINI_API_KEY="g")
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    for d in sys.argv[1:]:
        if d.startswith("/"): await tg.text(U, d)
        else: await tg.press(U, d)
        p = tg.out(U)[-1]; rm = p.get("reply_markup")
        if isinstance(rm, str): rm = json.loads(rm)
        print(f"===== {d}\n{p['text'][:700]}")
        for row in (rm or {}).get("inline_keyboard", []): print("   ", " | ".join(f"[{b['text']}]" for b in row))
asyncio.run(main())
