"""UTM за специфікацією GreenPost: теги на вибір, 9 міток у блоках коду з ✅, порядок параметрів, повтор, реєстр, статистика, Excel/Word."""
import asyncio, os, sys, shutil, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tutm"; shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k")
import faketg; tg = faketg.install()
import bot, utm_flow as UTM
URL = "https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233?ref=1"
def sent_since(n): return [p for c, m, p in tg.sent[n:] if m in ("sendMessage", "editMessageText", "sendDocument")]
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    assert UTM.clean_url(f"[({URL})]") == URL and UTM.clean_url(f'"{URL}"') == URL
    await tg.text(U, f"/utm [{URL}]"); t = tg.last_text(U); print(t[:160].replace("\n", " | ")); btn = tg.last_buttons(U)
    print("теги:", [x for r in btn for x, d in r])
    assert "1. <code>" in t and any(d == "m:utmown" for r in btn for _, d in r)
    n = len(tg.sent); await tg.press(U, "m:utmt:1")
    out = "\n".join(p.get("text", "") for p in sent_since(n))
    links = [l[5:-6] for l in out.split("\n") if l.startswith("<pre>")]
    print("міток:", len(links), "| ✅:", out.count("✅"))
    assert len(links) == 9 and out.count("✅") == 9
    tag = UTM.suggest_tags(URL)[1]
    assert links[0] == f"https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233?ref=1&amp;utm_content={tag}&amp;utm_medium=social&amp;utm_campaign=news&amp;utm_source=telegram", links[0]
    pairs = [(l.split("utm_source=")[1], l.split("utm_medium=")[1].split("&")[0]) for l in links]
    assert pairs == [("telegram","social"),("telegram","stories"),("tumblr","social"),("facebook","social"),("facebook","stories"),("facebook","reels"),("threads","social"),("instagram","stories"),("twitter","social")], pairs
    assert not any(ch in l for l in links for ch in "[]()\" ")
    # повтор
    await tg.text(U, f"/utm {URL}"); print("повтор:", "вже опрацьоване" in tg.last_text(U)); assert "вже опрацьоване" in tg.last_text(U)
    n = len(tg.sent); await tg.press(U, "m:utmold"); assert "\n".join(p.get("text", "") for p in sent_since(n)).count("<pre>") == 9
    await tg.press(U, "m:utmown"); n = len(tg.sent); await tg.text(U, "Зубр Львів!"); out = "\n".join(p.get("text", "") for p in sent_since(n))
    print("власний тег:", "utm_content=" in out, "| повтор зараховано:", "повтор" in out)
    # резервні — лише на запит
    await tg.press(U, "m:utmres"); assert "7. " in tg.last_text(U) and "Twitter/X (коментарі)" in tg.last_text(U)
    n = len(tg.sent); await tg.press(U, "m:utmrs:0"); out = "\n".join(p.get("text", "") for p in sent_since(n)); assert "utm_medium=video&amp;utm_campaign=news&amp;utm_source=youtube" in out
    n = len(tg.sent); await tg.text(U, f"/utm https://greenpost.ua/eco/lis energy facebook_reels"); out = "\n".join(p.get("text", "") for p in sent_since(n)); assert out.count("<pre>") == 1 and "utm_medium=reels" in out
    # реєстр, статистика, файли
    n = len(tg.sent); await tg.text(U, "/utm реєстр"); out = "\n".join(p.get("text", "") for p in sent_since(n)); print("реєстр: блоків", out.count("<pre>"), "| тегів", out.count("🏷"))
    assert out.count("🏷") == 3 and "📅" in out
    await tg.text(U, "/utm статистика"); s = tg.last_text(U); print(s[:230]); assert "ТИЖНІ" in s and "МІСЯЦІ" in s and "РОКИ" in s and "█" in s
    led = bot.utm_ledger(); st = UTM.stats(led, bot.TZ); print("разом:", st["total"], "посилань:", st["links"], "повторів:", st["years"][0][3]); assert st["years"][0][3] == 1
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(UTM.export_xlsx(led, bot.TZ))); print("Excel:", wb.sheetnames, "| діаграм:", sum(len(ws._charts) for ws in wb))
    assert wb.sheetnames[:3] == ["Тижні", "Місяці", "Роки"] and sum(len(ws._charts) for ws in wb) >= 5
    from docx import Document
    d = Document(io.BytesIO(UTM.export_docx(led, bot.TZ))); print("Word: абзаців", len(d.paragraphs), "| таблиць", len(d.tables)); assert len(d.tables) == 3
    n = len(tg.sent); await tg.press(U, "m:utmx"); await tg.press(U, "m:utmd"); print("файли надіслано:", len([1 for c, m, p in tg.sent[n:] if m == "sendDocument"]))
asyncio.run(main())
