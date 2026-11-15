"""Кожна команда обох ботів викликається без аргументів і з аргументом: жодна не повинна падати."""
import asyncio, os, sys, shutil, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tcmds"; shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
WHICH = sys.argv[1] if len(sys.argv) > 1 else "bot"
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k",
                  MANAGER_TOKEN="2:m", MANAGER_SECRET="h"*20, MANAGER_DB_PATH=f"{D}/m.db", ORION_PLUGINS_OFF="hub" if WHICH == "manager" else "")
import httpx
ERRS = []
class H(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.ERROR: ERRS.append(r.getMessage()[:200] + (" | " + repr(r.exc_info[1])[:300] if r.exc_info else ""))
logging.getLogger().addHandler(H())
def handler(req): return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "ok": True, "result": []})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
from telegram.ext import CommandHandler
if WHICH == "manager":
    import manager as M; app = M.app; M.init_db()
else:
    import bot as M; app = M.tg_app; M.init_db()
SKIP = {"factory", "reset", "wipe", "restore", "stop", "leave", "sleep"}
async def main():
    await tg.start(app); U = 111
    cmds = sorted({c for hs in app.handlers.values() for h in hs if isinstance(h, CommandHandler) for c in h.commands})
    bad, silent = [], []
    for c in cmds:
        if c in SKIP: continue
        for arg in ("", " тест", " https://example.com/news/1 tag telegram"):
            n, before = len(tg.sent), len(ERRS)
            try:
                await asyncio.wait_for(tg.text(U, f"/{c}{arg}"), 20)
            except Exception as e:  # noqa: BLE001
                bad.append((c, arg, repr(e)[:200])); continue
            if len(ERRS) > before: bad.append((c, arg, ERRS[before:][0]))
            elif arg == "" and len(tg.sent) == n: silent.append(c)
            if hasattr(M, "AWAIT"): M.AWAIT.pop(U, None)
    print(WHICH, "команд:", len(cmds))
    print("ПОМИЛКИ:", *bad, sep="\n  ")
    print("БЕЗ ВІДПОВІДІ:", silent)
asyncio.run(main())
