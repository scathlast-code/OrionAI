import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/tj", exist_ok=True)
import shutil; shutil.rmtree("/tmp/tj", ignore_errors=True); os.makedirs("/tmp/tj", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/tj/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", MISTRAL_API_KEY="m", SUMMARY_EVERY="4")
import httpx
MODE = {"groq": "ok", "mistral": "ok"}; SEEN = []
def handler(req):
    u = str(req.url); body = json.loads(req.content)
    who = "groq" if "groq.com" in u else "mistral" if "mistral" in u else None
    if not who: return httpx.Response(404)
    SEEN.append((who, body["messages"][0]["content"][-400:], body["messages"][-1]["content"][:80] if isinstance(body["messages"][-1]["content"], str) else "…"))
    m = MODE[who]
    if m == "down": return httpx.Response(429, json={"error": {"message": "rate limit per day"}}, headers={"retry-after": "3600"})
    q = body["messages"][-1]["content"]
    if "підсумок" in str(q)[:60]: return httpx.Response(200, json={"choices": [{"message": {"content": "• Мета: пост про зубрів\n• Зроблено: заголовок"}, "finish_reason": "stop"}]})
    if m == "cut": MODE[who] = "ok"; return httpx.Response(200, json={"choices": [{"message": {"content": "Частина перша тексту про зубрів, яка обірвалась на сере"}, "finish_reason": "length"}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": f"{who}: відповідь на «{str(q)[:40]}»"}, "finish_reason": "stop"}]})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    bot.set_pref(U, "memory", 6)
    await tg.text(U, "/start")
    await tg.text(U, "Привіт, пишемо пост про зубрів")
    MODE["groq"] = "cut"
    await tg.text(U, "Напиши довгий текст"); print("partial btn:", tg.last_text(U), tg.last_buttons(U))
    await tg.press(U, tg.find(U, "Продовжити")); await asyncio.sleep(0.1); print("continued:", tg.out(U)[-1]["text"][:120]); print("  sent to:", SEEN[-1][0], "| last user msg:", SEEN[-1][2])
    MODE["groq"] = "down"; MODE["mistral"] = "down"
    await tg.text(U, "Додай хештеги"); print("\nall down:", tg.last_text(U)[:100])
    MODE["mistral"] = "ok"
    await tg.press(U, "m:jl"); print("\n" + tg.last_text(U)); print(tg.last_buttons(U)[:4])
    bot.update_state("mistral", cooldown_until=0, fails=0)
    await tg.press(U, "m:jl:0:open"); jb = [d for r in tg.last_buttons(U) for t, d in r if d.startswith("m:j:")]
    await tg.press(U, jb[0]); print("\n" + tg.last_text(U)[:500]); print(tg.last_buttons(U))
    bot.update_state("mistral", cooldown_until=0, fails=0)  # Mistral знову доступна
    await tg.press(U, [d for r in tg.last_buttons(U) for t, d in r if d.endswith(":mistral")][0]); await asyncio.sleep(0.1)
    print("\nrun on mistral:", tg.out(U)[-1]["text"][:120])
    for i in range(6): await tg.text(U, f"Ще правка номер {i}")
    await asyncio.sleep(0.5)
    print("\nsummary:", bot.summary_get(bot.current_dialog(U)))
    await tg.text(U, "Що ми вже зробили?"); print("system has summary:", "Стислий підсумок" in SEEN[-1][1], "| tail:", SEEN[-1][1][-160:].replace("\n", " "))
    await tg.press(U, "m:jl:0:all"); print("\n" + tg.last_text(U)[:200])
asyncio.run(main())
