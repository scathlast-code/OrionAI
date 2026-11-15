"""Меню-акордеон, навігація Назад/Вперед/Додому, редактор кнопок (перенести, перейменувати, нова група), сортування нотаток."""
import asyncio, os, sys, json, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
shutil.rmtree("/tmp/t1", ignore_errors=True); os.makedirs("/tmp/t1")
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/t1/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import faketg; tg = faketg.install()
import bot
def labels(U): return [t for r in tg.last_buttons(U) for t, d in r]
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start"); print("нижньої клавіатури немає:", not tg.reply_kb(U))
    await tg.press(U, "m:home"); print("головне:", labels(U))
    await tg.press(U, "m:grp:work"); print("розгорнуто «Робота»:", labels(U)[:9])
    assert any("Робота ▲" in t for t in labels(U)) and any("Створення ▼" in t for t in labels(U)), "акордеон має лишати інші категорії"
    await tg.press(U, tg.find(U, "Нотатки")); print("нотатки:", tg.last_text(U)[:30])
    await tg.press(U, tg.find(U, "Назад")); print("back->", tg.last_text(U)[:30])
    await tg.press(U, tg.find(U, "Вперед")); print("fwd->", tg.last_text(U)[:30])
    await tg.press(U, tg.find(U, "Додому")); print("home->", tg.last_text(U)[:30])
    # редактор: перенести «Завдання» в «Створення», перейменувати групу, нова група
    await tg.press(U, "m:bi:tl"); print("редактор tl:", labels(U)[:6])
    await tg.press(U, "m:bmv:tl:g:create"); await tg.press(U, "m:grp:create"); print("Завдання в «Створення»:", any("Завдання" in t for t in labels(U)))
    await tg.press(U, "m:bgnew"); await tg.text(U, "🧪 Лабораторія"); print(tg.last_text(U)[:80])
    await tg.press(U, "m:home"); print("нова група в меню:", any("Лабораторія" in t for t in labels(U)))
    await tg.press(U, "m:brl"); await tg.text(U, "🗑 Кошик = ♻️ Смітник"); print(tg.last_text(U)[-60:])
    for t in ["бета", "альфа", "гама"]:
        await tg.text(U, "/note " + t)
    await tg.press(U, "m:nt"); await tg.press(U, "m:sort:notes:az"); print([l for l in tg.last_text(U).split("\n") if l.strip()][1:4])
    await tg.press(U, "help:main"); await tg.press(U, "help:g:voice"); await tg.press(U, tg.find(U, "Назад")); print("help back:", tg.last_text(U)[:40])
asyncio.run(main())
