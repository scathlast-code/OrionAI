import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_community.py")).read().split("async def main():")[0].replace("/tmp/tc", "/tmp/ttb"))
CH = {-100500: {"id": -100500, "type": "supergroup", "title": "Зубри"}, -100700: {"id": -100700, "type": "channel", "title": "Новини Полісся", "username": "polissya_news"}}
tg.handlers["getChat"] = lambda p: dict(CH[int(p["chat_id"])], accent_color_id=0, max_reaction_count=11, accepted_gift_types={"unlimited_gifts": False, "limited_gifts": False, "unique_gifts": False, "premium_subscription": False, "gifts_from_channels": False})
tg.handlers["getChatMemberCount"] = lambda p: 128 if int(p["chat_id"]) == -100500 else 2048
tg.handlers["createChatInviteLink"] = lambda p: {"invite_link": "https://t.me/+AbCdEf123", "creator": faketg.BOT_USER, "creates_join_request": False, "is_primary": False, "is_revoked": False}
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    for chat in CH.values():
        await tg.feed({"update_id": next(tg.upd_id), "my_chat_member": {"chat": chat, "from": {"id": U, "is_bot": False, "first_name": "O"}, "date": int(time.time()),
            "old_chat_member": {"status": "left", "user": faketg.BOT_USER}, "new_chat_member": ADMIN}})
    await tg.press(U, "m:cm"); print([t for r in tg.last_buttons(U) for t, d in r][:3])
    await tg.press(U, "m:cm:tb"); print(tg.last_text(U)); print(tg.last_buttons(U))
    await tg.press(U, "m:cm:tbf")
    doc = [p for c, m, p in tg.sent if m == "sendDocument"][-1]; print("\nfile sent:", doc.get("caption", "")[:50])
    print(bot.CM.table_csv().decode("utf-8-sig"))
    await tg.press(U, "m:cm:c:tg:-100500"); print(tg.last_text(U).split("\n")[:2])
asyncio.run(main())
