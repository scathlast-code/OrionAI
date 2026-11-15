import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tmenu"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import faketg; tg = faketg.install()
import bot
def labels(): return [t for r in tg.last_buttons(111) for t, d in r]
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    rk = [p for c, m, p in tg.sent if m == "sendMessage" and "remove_keyboard" in json.dumps(p.get("reply_markup") or {})]
    print("RK removed once at start:", len(rk))
    await tg.text(U, "/menu"); await tg.text(U, "/menu")
    print("no '⌨️' junk:", not any(p.get("text") == "⌨️" for c, m, p in tg.sent), "| RK sent total:", len([p for c, m, p in tg.sent if "remove_keyboard" in json.dumps(p.get("reply_markup") or {})]))
    await tg.press(U, "m:grp:settings"); print("\nopen settings:", labels())
    await tg.press(U, "m:grpx:settings"); print("closed:", labels())
    # вкладена група: створюємо «Мої» всередині «Робота» і кладемо туди Нотатки
    gid = bot.L.apply(U, "gnew", value="⭐ Мої")[2:]
    bot.L.apply(U, "mv", f"g:{gid}", "g:work"); bot.L.apply(U, "mv", "nt", f"g:{gid}")
    await tg.press(U, f"m:grp:{gid}"); print("\nnested open:", labels())
    await tg.press(U, f"m:grpx:{gid}"); print("nested close -> parent open:", labels())
    # навігація: відкрили екран, «перезапуск» (пам'ять процесу стерта) — Назад лишився
    await tg.press(U, "m:home"); await tg.press(U, "m:backup"); await tg.press(U, "m:bkfmt")
    bot.L.hist.clear()
    await tg.press(U, "m:bkfmt"); print("\nafter restart nav:", [t for t in labels() if t in ("◀️ Назад", "🏠 Додому", "Вперед ▶️")])
    await tg.press(U, "m:nav:back"); print("back ->", tg.last_text(U).split("\n")[0])
    await tg.press(U, "m:nav:fwd"); print("fwd ->", tg.last_text(U).split("\n")[0])
    # екран, відкритий без історії (наприклад, зі сповіщення): «Назад» веде до батьківського розділу
    bot.set_pref(U, "nav_hist", None); bot.L.hist.clear()
    await tg.press(U, "m:bkscope"); print("no history -> back:", [(t, d) for r in tg.last_buttons(U) for t, d in r if t == "◀️ Назад"])
    # довгий список: «Показати ще ▼» без токенів у пам'яті
    orig = bot.scr_dialogs
    bot.scr_dialogs = lambda c, *a, **k: ("Довгий список", [[(f"Пункт {i}", "m:noop")] for i in range(25)] + [[("« Меню", "m:home")]])
    await tg.press(U, "m:dl"); n1 = len(tg.last_buttons(U)); more = [d for r in tg.last_buttons(U) for t, d in r if t.startswith("Показати ще")]
    print("\nlist rows:", n1, "more:", more)
    await tg.press(U, more[0]); n2 = len(tg.last_buttons(U)); print("expanded rows:", n2, [t for t in labels() if "Згорнути" in t or "Показати" in t])
    bot.L.hist.clear()
    await tg.press(U, "m:more:0"); print("collapse after restart ok:", tg.last_text(U).split("\n")[0], len(tg.last_buttons(U)))
    bot.scr_dialogs = orig
    # стара кнопка з попередньої версії
    await tg.press(U, "ui:fold:deadbeef:10"); print("stale ui button ->", tg.last_text(U).split("\n")[0])
    # команди в синій кнопці
    await bot.tg_app.bot.set_my_commands([bot.BotCommand(c, d) for c, d in bot.COMMANDS]); print("\ncommands:", [c for c, _ in bot.COMMANDS])
    # однакові назви для різних дій у головному меню — немає
    seen = {}
    for g in ("work", "create", "ai", "services", "settings", "start"):
        await tg.press(U, f"m:grp:{g}")
        for t, d in [(t, d) for r in tg.last_buttons(U) for t, d in r]:
            if seen.get(t, d) != d: print("DUP:", t, seen[t], d)
            seen[t] = d
    print("labels checked:", len(seen), "| longest:", max(seen, key=len))
    await tg.press(U, "m:st"); print("\nst title:", tg.last_text(U).split("\n")[0], "| buttons:", labels()[-5:])
asyncio.run(main())
