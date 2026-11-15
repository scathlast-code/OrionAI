import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_community.py")).read().split("async def main():")[0].replace("/tmp/tc", "/tmp/tpol"))
G2 = -100600
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    for chat in (GC, {"id": G2, "type": "supergroup", "title": "Бізони"}):
        await tg.feed({"update_id": next(tg.upd_id), "my_chat_member": {"chat": chat, "from": {"id": U, "is_bot": False, "first_name": "O"}, "date": int(time.time()),
            "old_chat_member": {"status": "left", "user": faketg.BOT_USER}, "new_chat_member": ADMIN}})
    cid = f"tg:{G}"; CM = bot.CM
    # загальне правило: посилання заборонені в «Зубрах»
    await tg.press(U, f"m:cm:rt:{cid}:links")
    # учасники пишуть (бот запам'ятовує їх)
    for uid, name in ((42, "Петро"), (43, "Оля"), (44, "Іра")):
        await tg.feed(gmsg(uid, "привіт", name=name))
    # група «Довірені»: Оля й Іра, їм посилання дозволено у всіх спільнотах
    await tg.press(U, "m:cm:grn"); await tg.text(U, "Довірені"); print(tg.last_text(U).split("\n")[0])
    gid = CM.group_id("Довірені")
    await tg.press(U, f"m:cm:gpk:{gid}:0"); print("pick:", [t for r in tg.last_buttons(U) for t, d in r][:4])
    await tg.press(U, f"m:cm:gtg:{gid}:43:0"); await tg.press(U, f"m:cm:gtg:{gid}:44:0")
    await tg.press(U, "m:cm:pl:*"); await tg.press(U, "m:cm:ptg"); await tg.press(U, f"m:cm:ptgp:{gid}")
    await tg.press(U, "m:cm:pv:links"); print("\nmatrix:", tg.last_text(U).split("\n")[0], "|", [t for r in tg.last_buttons(U)[:3] for t, d in r])
    # Іру окремо: у «Зубрах» посилання заборонені (людина важливіша за групу)
    await tg.press(U, f"m:cm:pl:{cid}"); await tg.press(U, "m:cm:ptu:0"); await tg.press(U, "m:cm:ptp:44"); await tg.press(U, "m:cm:pv:links"); await tg.press(U, "m:cm:pv:links")
    # новачкам у всіх: тиша; і Петро (не в групі) не має обмежень окрім загальних
    await tg.press(U, f"m:cm:pl:{cid}"); print("\n" + tg.last_text(U))
    def dels(): return len([1 for c, m, p in tg.sent if m == "deleteMessage"])
    async def check(uid, name, text, chat=GC):
        n = dels(); m = gmsg(uid, text, name=name); m["message"]["chat"] = chat; await tg.feed(m); return "🗑" if dels() > n else "✅"
    print("\nЗубри links: Петро", await check(42, "Петро", "https://a.com"), "| Оля(довірена)", await check(43, "Оля", "https://a.com"), "| Іра(особисто заборонено)", await check(44, "Іра", "https://a.com"))
    # заборона «писати взагалі» для деяких: обрати кількох через список
    await tg.press(U, f"m:cm:pl:tg:{G2}"); await tg.press(U, "m:cm:pti"); await tg.text(U, "42, 43"); print("\nmulti target:", tg.last_text(U).split("\n")[0])
    await tg.press(U, "m:cm:pv:silence"); await tg.press(U, "m:cm:pv:silence")
    B = {"id": G2, "type": "supergroup", "title": "Бізони"}
    print("Бізони silence: Петро", await check(42, "Петро", "тест", B), "| Оля", await check(43, "Оля", "тест", B), "| Іра", await check(44, "Іра", "тест", B))
    # копіювання в інші спільноти
    await tg.press(U, "m:cm:pcp"); print("\ncopy:", [t for r in tg.last_buttons(U) for t, d in r]); await tg.press(U, "m:cm:pck:0"); await tg.press(U, "m:cm:pcgo"); print(tg.out(U)[-1]["text"][:40])
    print("Зубри silence after copy: Петро", await check(42, "Петро", "тест"))
    # команда /group у чаті від адміна
    reply = gmsg(7, "/group VIP", name="Модер", entities=[{"type": "bot_command", "offset": 0, "length": 6}]); reply["message"]["reply_to_message"] = gmsg(44, "x", name="Іра")["message"]
    await tg.feed(reply); print("\n/group:", g_sent()[-1]["text"], "| groups:", [g["name"] for g in CM.q("SELECT name FROM cm_groups")])
    await tg.press(U, "m:cm:grs"); print(tg.last_text(U).split("\n")[0], [t for r in tg.last_buttons(U) for t, d in r])
asyncio.run(main())
