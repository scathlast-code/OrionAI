"""Обходить усі кнопки бота (BFS) і шукає проблеми: помилки, однакові підписи, обрізані/довгі назви, екрани без навігації."""
import asyncio, os, sys, json, time, re, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tcrawl"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", GEMINI_API_KEY="g", MISTRAL_API_KEY="m")
import httpx, logging
ERRS = []
class H(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.ERROR or "невідома кнопка" in r.getMessage(): ERRS.append(r.getMessage() + (" | " + str(r.exc_info[1]) if r.exc_info else ""))
logging.getLogger().addHandler(H())
def handler(req): return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import bot
SKIP = re.compile(r"(del|purge|wipe|fryes|frgo|reset|lv:|leave|bkgo|bkrs|restore|ddel|fdel|tdel|empty|trash:all|cancel|stop|nav:|ui:fold|url:|copy:|dis|bkdis|logout|bgdel|brldel|utmx|utmd)")
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    # трохи даних
    await tg.text(U, "Привіт")
    seen, q = set(), collections.deque()
    def screen():
        o = tg.out(U)
        return o[-1] if o else {}
    def btns(p):
        rm = p.get("reply_markup")
        if isinstance(rm, str): rm = json.loads(rm)
        return [[(b["text"], b.get("callback_data") or b.get("url") or "copy") for b in row] for row in (rm or {}).get("inline_keyboard", [])]
    for g in ("work", "create", "ai", "services", "settings", "start"):
        q.append((f"m:grp:{g}", "home"))
    q.append(("m:home", "x")); q.append(("help:main", "x"))
    for d in ("m:st", "m:md", "m:dl", "m:tl", "m:cr", "m:nt", "m:backup", "m:svc", "m:cm", "m:pr", "m:diag", "m:btn", "m:acc", "m:jl", "m:lib", "m:voice"):
        q.append((d, "x"))
    report = {"dups": set(), "long": set(), "ellipsis": set(), "nonav": set(), "screens": 0, "errors": [], "samename": collections.defaultdict(set)}
    n = 0
    while q and n < int(os.environ.get("CRAWL_MAX", "3000")):
        d, src = q.popleft()
        if d in seen or not d or SKIP.search(d) or not (d.startswith("m:") or d.startswith("help:")): continue
        seen.add(d); n += 1
        before = len(ERRS)
        try:
            await tg.press(U, d)
        except Exception as e:
            report["errors"].append((d, repr(e))); continue
        if len(ERRS) > before: report["errors"].append((d, ERRS[before:]))
        p = screen(); b = btns(p); report["screens"] += 1
        labels = [t for row in b for t, dd in row]
        datas = [dd for row in b for t, dd in row]
        c = collections.Counter(labels)
        for t, k in c.items():
            if k > 1:
                ds = {dd for row in b for tt, dd in row if tt == t}
                if len(ds) > 1: report["dups"].add((d, t))
        for row in b:
            for t, dd in row:
                if dd.startswith(("m:", "help:")) and len(dd.encode()) > 64: report["errors"].append((d, "callback >64 байт: " + dd))
                report["samename"][t].add(dd.split(":")[1] if dd.startswith("m:") and ":" in dd else dd)
                vis = len(t)
                if (len(row) >= 2 and vis > 18) or vis > 30: report["long"].add((t, len(row)))
                if t.endswith("…"): report["ellipsis"].add(t)
        if p.get("text", "").startswith("✍️") or "Напишіть" in p.get("text", "")[:40]:
            await tg.press(U, "m:cancel")
        if not any(dd.startswith("m:nav:") for dd in datas) and d not in ("m:home",): report["nonav"].add(d)
        for row in b:
            for t, dd in row:
                if dd not in seen: q.append((dd, d))
    print("screens:", report["screens"])
    print("\nERRORS:", *report["errors"][:30], sep="\n  ")
    print("\nDUP LABELS (same text, diff action, same screen):", *sorted(report["dups"])[:40], sep="\n  ")
    print("\nSAME LABEL -> DIFFERENT ACTIONS ACROSS BOT:", *[(t, sorted(v)) for t, v in report["samename"].items() if len(v) > 1 and not t.startswith(("◀️", "🏠", "Вперед", "⬆️", "⬇️", "«", "✖️", "▲", "Показати"))][:60], sep="\n  ")
    print("\nLONG:", *sorted(report["long"])[:80], sep="\n  ")
    print("\nELLIPSIS:", *sorted(report["ellipsis"])[:60], sep="\n  ")
    print("\nNO NAV:", *sorted(report["nonav"])[:60], sep="\n  ")
asyncio.run(main())
