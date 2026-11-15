"""Обхід усіх кнопок 🧭 головного бота: помилки, кнопки без обробника, задовгі callback, екрани без навігації, дублікати."""
import asyncio, os, sys, json, re, collections, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tcm"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(MANAGER_TOKEN="2:m", MANAGER_SECRET="h" * 20, ALLOWED_USERS="111", MANAGER_DB_PATH=f"{D}/m.db", PUBLIC_URL="https://hub.example")
import httpx
ERRS = []
class H(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.ERROR or "невідома кнопка" in r.getMessage(): ERRS.append(r.getMessage() + (" | " + repr(r.exc_info[1]) + (" @ " + __import__("traceback").format_exception(*r.exc_info)[-2].strip().replace("\n", " ") if r.exc_info else "") if r.exc_info else ""))
logging.getLogger().addHandler(H())
def handler(req):
    u = str(req.url)
    if "api.telegram.org/bot77:" in u:
        m = u.rsplit("/", 1)[1]
        if m == "getMe": return httpx.Response(200, json={"ok": True, "result": {"id": 77, "is_bot": True, "first_name": "Монітор", "username": "mon_bot"}})
        if m == "getWebhookInfo": return httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
        if m.startswith("getMy") and m != "getMyCommands": return httpx.Response(200, json={"ok": True, "result": {"name": "Монітор", "description": "", "short_description": ""}})
        return httpx.Response(200, json={"ok": True, "result": []})
    return httpx.Response(200, json={"ok": True})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install(); tg.handlers["getUpdates"] = lambda p: []
import manager as M
SKIP = re.compile(r"(del|purge|wipe|fryes|frgo|reset|bkgo|bkrs|restore|empty|cancel|stop|nav:|url:|copy:|dis|logout|bgdel|brldel|bkyes|rmset|bwake|clrun|monrun|lkplan|rwsync)")
async def main():
    M.init_db(); await tg.start(M.app); U = 111
    await tg.text(U, "/start")
    M.bot_add("telegram", "Монітор", "mon_bot", None, {"token": "77:abc"}); M.bot_add("http", "ШІ-бот")
    await tg.text(U, "/start")
    seen, q = set(), collections.deque([("m:home", "x"), ("help:main", "x")])
    def btns():
        o = tg.out(U); p = o[-1] if o else {}
        rm = p.get("reply_markup")
        if isinstance(rm, str): rm = json.loads(rm)
        return p, [[(b["text"], b.get("callback_data") or b.get("url") or "copy") for b in row] for row in (rm or {}).get("inline_keyboard", [])]
    rep = {"errors": [], "dups": set(), "nonav": set(), "long": set()}
    n = 0
    while q and n < 5000:
        d, src = q.popleft()
        if d in seen or SKIP.search(d) or not d.startswith(("m:", "help:")): continue
        seen.add(d); n += 1; before = len(ERRS)
        try: await tg.press(U, d)
        except Exception as e: rep["errors"].append((d, repr(e))); continue
        if len(ERRS) > before: rep["errors"].append((d, ERRS[before:]))
        p, b = btns()
        c = collections.Counter(t for row in b for t, _ in row)
        for t, k in c.items():
            if k > 1 and len({dd for row in b for tt, dd in row if tt == t}) > 1: rep["dups"].add((d, t))
        for row in b:
            for t, dd in row:
                if dd.startswith(("m:", "help:")) and len(dd.encode()) > 64: rep["errors"].append((d, "callback >64 байт: " + dd))
                if (len(row) >= 2 and len(t) > 18) or len(t) > 30: rep["long"].add((t, len(row)))
                if dd not in seen: q.append((dd, d))
        if "Надішліть" in p.get("text", "")[:80] or p.get("text", "").startswith("✏️"): M.AWAIT.pop(U, None)
        if not any(dd.startswith("m:nav:") for row in b for _, dd in row) and d != "m:home": rep["nonav"].add(d)
    print("screens:", n)
    print("\nERRORS:", *rep["errors"][:40], sep="\n  ")
    print("\nDUP LABELS:", *sorted(rep["dups"])[:30], sep="\n  ")
    print("\nLONG:", *sorted(rep["long"])[:40], sep="\n  ")
    print("\nNO NAV:", *sorted(rep["nonav"])[:40], sep="\n  ")
asyncio.run(main())
