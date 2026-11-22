"""Збирає архів для тестування й для інших нейромереж:
  • orionai-web.html — веб-версія бота й мініапп в одному файлі: справжні екрани й кнопки, зняті з бота
    (без ключів і без реальних даних; ШІ, погода, інтернет у демо не відповідають);
  • orionai-code.txt — повний код бота й мініапп з описом, як усе влаштовано.
Запуск: python3 tools/build_webdemo.py [скільки_екранів]  → dist/orionai-webdemo-<дата>.zip
"""
import asyncio, collections, contextlib, datetime, io, json, logging, os, re, shutil, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
MAXN = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
D = "/tmp/orion-webdemo"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s" * 20, GROQ_API_KEY="k",
                  ORION_DATA_DIR=D)
import httpx  # noqa: E402

def _answer(req):
    """Заглушка ШІ для симулятора: на запити тем/тегів — правдоподібний JSON, на розмову — пояснення демо."""
    body = req.content.decode("utf-8", "ignore") if req.content else ""
    if "Теги від користувача" in body:
        text = '{"tags":[{"name":"Полісся","phrase":"з Полісся","kind":"place"}]}'
    elif "групування скачаних" in body:
        text = '{"topics":[{"name":"снігопад","phrase":"зі снігопадом"}],"places":[{"name":"Карпати","phrase":"з Карпат"}],"lang":"uk"}'
    else:
        text = ("(демо-симулятор: справжній ШІ тут не підключений — у боті відповідає Gemini/OpenRouter. "
                "Усі меню, кнопки й переходи — справжні, зняті з коду бота.)")
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                     "candidates": [{"content": {"parts": [{"text": text}]}}]})


_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):  # жодних справжніх мережевих запитів: ШІ відповідає заглушкою, інше — порожньо
    k["transport"] = httpx.MockTransport(_answer)
    _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
logging.disable(logging.CRITICAL)
import faketg  # noqa: E402
tg = faketg.install()
import bot  # noqa: E402
import miniapp  # noqa: E402

from webdemo import SKIP, build_zip, mask  # noqa: E402


def rows_of(p):
    rm = p.get("reply_markup")
    if isinstance(rm, str):
        rm = json.loads(rm)
    return [[[b["text"], b.get("callback_data") or b.get("url") or ""] for b in row] for row in (rm or {}).get("inline_keyboard", [])]


def seed(U):
    """Сьогоднішні справжні дані (22.11.2026), яких симуляція сама не отримає: скачане, групи, теги, збережені посилання."""
    med = bot.PLUGINS.get("med")
    if not med:
        return
    from plugins.media import related as R
    med.mh_link = lambda rid: f"#mh_{rid}"
    add = med.hist.add
    a = add(U, msg=1001, author=R.norm_author("tvoe_v_v"), who="tvoe_v_v", site="Threads", url="https://www.threads.com/",
            title="Сніжить на Волині сьогодні ❄️", topics=["снігопад"], geos=["волинь"], langs=["українська"], first=["t:снігопад", "g:волинь"])
    add(U, msg=1002, author=R.norm_author("oleno4ka_igorivna1"), who="oleno4ka_igorivna1", site="Threads", url="https://www.threads.com/",
        title="Пів годинки і Карпати солідно забіліли😍❄️🌨", topics=["снігопад"], geos=["карпати"], langs=["українська"], first=["g:карпати"])
    add(U, msg=1003, author=R.norm_author("karpaty_posydok"), who="karpaty_posydok", site="Threads",
        url="https://www.threads.com/share/_qQhIDyVG", title="Будиночки в Карпатах | Відпочинок", topics=["снігопад"], geos=["карпати"],
        langs=["українська"], group=1, gi=0)
    add(U, msg=1004, author=R.norm_author("oleno4ka_igorivna1"), who="oleno4ka_igorivna1", site="Threads",
        url="https://www.threads.com/", title="Пів годинки і Карпати солідно забіліли (повторно)", topics=["снігопад"], geos=["карпати"],
        langs=["українська"], group=1, gi=1)
    med.hist.learn_user("g", "Полісся", "з Полісся")
    med.set_pref(U, "gseq", 1)
    med.pend_add(U, [("https://www.threads.com/share/_p0MF6MLV", {"add": ["полісся"], "drop": [], "none": False})])
    return a


async def capture():
    bot.init_db(); bot.migrate_db(); await tg.start(bot.tg_app); U = 111
    seed(U)
    with contextlib.redirect_stdout(io.StringIO()):
        await tg.text(U, "/start")
        start = tg.out(U)[-1]
        screens = {"m:home": {"text": start.get("text", ""), "rows": rows_of(start)}}
        q = collections.deque(d for r in screens["m:home"]["rows"] for _t, d in r)
        q.extend(["m:today", "m:hub:zap", "m:hub:nr", "m:hub:cr", "m:grp:settings", "m:sp", "m:dl", "m:pl:red", "m:col", "m:utm",
                  "m:pl:med:", "m:pl:med:arc", "m:pl:med:arcl:t", "m:pl:med:arcl:g", "m:pl:med:arcl:a", "m:pl:med:arcl:l",
                  "m:pl:med:grl", "m:pl:med:grl:1", "m:pl:med:utg", "m:pl:med:pend", "m:pl:med:rls", "m:pl:med:ted:3", "m:pl:med:bad"])
        n = 0
        while q and n < MAXN:
            d = q.popleft()
            if d in screens or not d.startswith(("m:", "help:")) or SKIP.search(d):
                continue
            try:
                await asyncio.wait_for(tg.press(U, d), 20)
            except Exception:  # noqa: BLE001
                continue
            p = tg.out(U)[-1] if tg.out(U) else {}
            screens[d] = {"text": p.get("text", ""), "rows": rows_of(p)}
            n += 1
            q.extend(dd for r in screens[d]["rows"] for _t, dd in r if dd not in screens)
            if "Напишіть" in p.get("text", "")[:60]:
                with contextlib.suppress(Exception):
                    await tg.press(U, "m:cancel")
        app = {"menu": miniapp.menu(bot, U), "history": miniapp.history(bot, U), "screens": {}}
        q = collections.deque(i["data"] for s in app["menu"]["sections"] for i in s["items"])
        q.extend(t["data"] for t in app["menu"]["tabs"])
        n = 0
        while q and n < MAXN:
            d = q.popleft()
            if d in app["screens"] or not d.startswith("m:") or SKIP.search(d):
                continue
            try:
                r = await asyncio.wait_for(miniapp.run(bot, U, "screen", data=d), 20)
            except Exception:  # noqa: BLE001
                continue
            app["screens"][d] = r
            n += 1
            for row in r.get("rows") or []:
                q.extend(dd for _t, dd in row)
            for sec in r.get("sections") or []:
                for row in sec.get("rows") or []:
                    q.extend(dd for _t, dd in row)
    return screens, app


def main():
    screens, app = asyncio.run(capture())
    data = build_zip(bot, mask(screens), mask(app), " (симулятор: справжні меню + сьогоднішні дані 22.11.2026)",
                     "Справжні екрани й кнопки бота та мініапп. Додано сьогоднішні дані: скачане (Волинь, Карпати), група №1, "
                     "тег «Полісся», збережене посилання. ШІ, погода й інтернет тут не відповідають наживо — замість них заглушки. "
                     "Ключі й паролі приховано.")
    dist = ROOT / "dist"; dist.mkdir(exist_ok=True)
    name = dist / f"orionai-webdemo-{datetime.date.today().isoformat()}.zip"
    name.write_bytes(data)
    print(name, len(screens), len(app["screens"]))


if __name__ == "__main__":
    main()
