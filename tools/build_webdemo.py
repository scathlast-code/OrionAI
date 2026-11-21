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
MAXN = int(sys.argv[1]) if len(sys.argv) > 1 else 500
D = "/tmp/orion-webdemo"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s" * 20, GROQ_API_KEY="k",
                  ORION_DATA_DIR=D)
import httpx  # noqa: E402

_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):  # жодних справжніх мережевих запитів: ШІ відповідає заглушкою, інше — порожньо
    k["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "(демо: ШІ тут не підключено)"}, "finish_reason": "stop"}]}))
    _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
logging.disable(logging.CRITICAL)
import faketg  # noqa: E402
tg = faketg.install()
import bot  # noqa: E402
import miniapp  # noqa: E402

SKIP = re.compile(r"(del|purge|wipe|fryes|frgo|reset|lv:|leave|bkgo|bkrs|restore|ddel|fdel|tdel|empty|trash:all|cancel|stop|nav:|ui:fold|url:|copy:|"
                  r"dis|logout|bgdel|brldel|utmx|utmd|devbkr|devbkz|jrun|trun|rmset|chk|upd|send|run|go$|now$)")


def rows_of(p):
    rm = p.get("reply_markup")
    if isinstance(rm, str):
        rm = json.loads(rm)
    return [[[b["text"], b.get("callback_data") or b.get("url") or ""] for b in row] for row in (rm or {}).get("inline_keyboard", [])]


async def capture():
    bot.init_db(); bot.migrate_db(); await tg.start(bot.tg_app); U = 111
    with contextlib.redirect_stdout(io.StringIO()):
        await tg.text(U, "/start")
        start = tg.out(U)[-1]
        screens = {"m:home": {"text": start.get("text", ""), "rows": rows_of(start)}}
        q = collections.deque(d for r in screens["m:home"]["rows"] for _t, d in r)
        q.extend(["m:today", "m:hub:zap", "m:hub:nr", "m:hub:cr", "m:grp:settings", "m:sp", "m:dl", "m:pl:red", "m:col", "m:utm"])
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


SHELL = r"""<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OrionAI · веб-версія для тестування</title><style>
body{margin:0;font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;background:#e7ebdf;color:#111}
header{padding:10px 14px;background:#3d6b4f;color:#fff}header b{font-size:17px}header small{opacity:.85;display:block}
.wrap{display:flex;gap:12px;padding:12px;flex-wrap:wrap}.pane{flex:1 1 420px;min-width:320px;background:#fff;border-radius:14px;overflow:hidden;display:flex;flex-direction:column;height:calc(100vh - 110px)}
.pane h3{margin:0;padding:8px 12px;background:#f2f4ef;font-size:14px;border-bottom:1px solid #ddd}
#chat{flex:1;overflow:auto;padding:12px;background:#dfe8d2}.msg{background:#fff;border-radius:12px;padding:9px 12px;margin:6px 0;max-width:92%;white-space:pre-wrap;word-wrap:break-word}
.me{background:#e2fbc8;margin-left:auto}.kb{display:grid;gap:5px;margin:4px 0 10px}.kb .r{display:flex;gap:5px}.kb button{flex:1;border:0;border-radius:9px;padding:9px 6px;background:#7aa36a;color:#fff;font-size:14px;cursor:pointer}
.kb button.miss{opacity:.55}blockquote{margin:4px 0;padding:4px 8px;border-left:3px solid #c33;background:#f8eeee;border-radius:6px}code{background:#eee;padding:0 3px;border-radius:4px}
form{display:flex;border-top:1px solid #ddd}form input{flex:1;border:0;padding:12px;font-size:15px}form button{border:0;background:#3d6b4f;color:#fff;padding:0 16px}
iframe{flex:1;border:0;width:100%}</style></head><body>
<header><b>OrionAI · веб-версія бота й мініапп</b><small>Знімок справжніх екранів і кнопок від __DATE__ (екранів бота: __NB__, мініапп: __NA__). Без ключів і реальних даних: ШІ, погода й інтернет тут не відповідають — це для перевірки меню, кнопок, текстів і переходів.</small></header>
<div class="wrap"><div class="pane"><h3>🤖 Бот у Telegram</h3><div id="chat"></div>
<form onsubmit="say(event)"><input id="inp" placeholder="Повідомлення (у демо ШІ не відповідає)"><button>➤</button></form></div>
<div class="pane"><h3>📱 Мініапп</h3><iframe id="app"></iframe></div></div>
<script>
window.BOT=__BOT__; window.APP=__APP__; const APPHTML=__APPHTML__;
const chat=document.getElementById('chat');let hist=['m:home'];
function add(html,me){const d=document.createElement('div');d.className='msg'+(me?' me':'');d.innerHTML=html;chat.appendChild(d);chat.scrollTop=1e9;return d}
function show(data){let s=BOT[data];
  if(data==='m:nav:home'){hist=['m:home'];s=BOT['m:home']}
  else if(data==='m:nav:back'){if(hist.length>1)hist.pop();s=BOT[hist[hist.length-1]]}
  else if(/^https?:/.test(data)){window.open(data,'_blank');return}
  else if(s)hist.push(data);
  if(!s){add('ℹ️ <i>Цей екран у демо не знято ('+data+') — у справжньому боті він відкриється.</i>');return}
  add(s.text.replace(/\n/g,'<br>'));const kb=document.createElement('div');kb.className='kb';
  for(const row of s.rows){const r=document.createElement('div');r.className='r';
    for(const [t,d] of row){const b=document.createElement('button');b.textContent=t;if(!BOT[d]&&!/^m:nav:|^https?:/.test(d))b.className='miss';b.onclick=()=>{add(t,true);show(d)};r.appendChild(b)}kb.appendChild(r)}
  chat.appendChild(kb);chat.scrollTop=1e9}
function say(e){e.preventDefault();const v=inp.value.trim();if(!v)return;add(v.replace(/</g,'&lt;'),true);inp.value='';add('(демо: ШІ тут не підключено — у справжньому боті тут буде відповідь моделі)')}
show('m:home');hist=['m:home'];
// мініапп: справжній index.html, а відповіді сервера — зі знімка
const stub=`<script>window.Telegram={WebApp:{initData:'demo',initDataUnsafe:{user:{first_name:'Демо'}},ready(){},expand(){},
 HapticFeedback:{impactOccurred(){},selectionChanged(){},notificationOccurred(){}},BackButton:{show(){},hide(){},onClick(){}},MainButton:{show(){},hide(){}},
 themeParams:{},colorScheme:'light',onEvent(){},openLink(u){open(u)},openTelegramLink(u){open(u)},close(){},platform:'web',version:'7.0',isVersionAtLeast(){return true},
 setHeaderColor(){},setBackgroundColor(){},enableClosingConfirmation(){},disableVerticalSwipes(){},CloudStorage:{getItem(k,c){c(null,'')},setItem(k,v,c){c&&c(null,true)}}}};
const APP=parent.APP;const _f=window.fetch;window.fetch=async(u,o)=>{let b={};try{b=JSON.parse(o&&o.body||'{}')}catch(_){}
 const J=x=>new Response(JSON.stringify(x),{status:200,headers:{'Content-Type':'application/json'}});
 if(b.op==='menu')return J(APP.menu);if(b.op==='history')return J(APP.history);
 if(b.op==='screen'){const s=APP.screens[b.data];return J(s||{text:'ℹ️ <i>Цей екран у демо не знято ('+b.data+').</i>',rows:[]})}
 if(b.op==='say')return J({messages:[{role:'assistant',text:'(демо: ШІ тут не підключено)'}]});return J({ok:true})};<\/script>`;
document.getElementById('app').srcdoc=APPHTML.replace('<head>','<head>'+stub);
</script></body></html>"""


CODE_HEAD = """OrionAI — повний код бота й мініапп (для інших нейромереж і розробників)
Зібрано: {date}

ЩО ЦЕ
OrionAI — особистий Telegram-бот (@ai_orion_7bot) з мініапп (Telegram Web App). Бот і мініапп — одне ціле: усе, що вміє бот, уміє й мініапп.
Мова інтерфейсу — українська. Розгорнуто на Railway (Python 3.13, python-telegram-bot, httpx, sqlite; веб-сервер uvicorn для мініапп і вебхука).

ЯК УЛАШТОВАНО
• bot.py — ядро: обробка повідомлень, екрани (функції scr_*), маршрутизатор кнопок route() (дані кнопок виду m:<дія>:<аргументи>),
  розмови з ШІ (кілька безкоштовних моделей з перемиканням: Gemini, OpenRouter…), пам'ять, завдання, нотатки, простори, резервні копії.
• uilayout.py — меню: розкладка кнопок головного екрана, теки, історія «Назад/Вперед».
• miniapp.py + miniapp/index.html — мініапп: той самий маршрутизатор route(), але відповідь повертається JSON-ом (text, rows) у вебсторінку.
• plugins/ — розділи-плагіни: newsroom (Новинарня: дні, «Сьогодні», контент-план, чернетки), editorial (Редакція), weather (погода),
  media (завантаження з YouTube/TikTok/Instagram…), stickers (наліпки), inbox (пошта), hub (мої боти).
• Решта .py — службові модулі (переклад, пошук в інтернеті, збір текстів і зведення, теми, резервні копії, UTM-мітки тощо).
Секретів (ключів, .env) тут немає: ключі задаються змінними середовища на сервері.

ФАЙЛИ
{tree}
"""


def code_text():
    """Абсолютно все текстове з репозиторію (код, вебсторінки, налаштування збирання, довідка), крім секретів і даних."""
    first = [ROOT / "bot.py", ROOT / "uilayout.py", ROOT / "miniapp.py", ROOT / "miniapp" / "index.html"]
    skip_dirs = {".git", "__pycache__", "data", "logs", "dist", "node_modules", ".venv"}
    exts = {".py", ".html", ".js", ".css", ".json", ".md", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".sh", ""}
    allf = sorted(p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip_dirs)
                  and (p.suffix in exts or p.name in ("Dockerfile", "Caddyfile")) and p.stat().st_size < 3_000_000)
    files = [p for p in dict.fromkeys(first + allf) if p.exists() and not re.search(r"(?i)(^|/)\.env|secret|cookies?\.(txt|json)|\.sqlite|token", str(p.relative_to(ROOT)))]
    files = [p for p in files if p.suffix != "" or p.name in ("Dockerfile", "Caddyfile")]
    tree = "\n".join(f"  {p.relative_to(ROOT)}  ({sum(1 for _ in p.open(encoding='utf-8', errors='ignore'))} рядків)" for p in files)
    out = [CODE_HEAD.format(date=datetime.date.today().isoformat(), tree=tree)]
    for p in files:
        out.append(f"\n\n{'=' * 100}\n=== ФАЙЛ: {p.relative_to(ROOT)}\n{'=' * 100}\n")
        out.append(p.read_text("utf-8", errors="replace"))
    return "".join(out)


RAILWAY = """Railway (сервер бота) — стан на {date}
• Проєкт: 2877fabf-1981-4a4a-904a-953795ec7955 · сервіс OrionAI (f7e662d4-c802-4935-b8d7-3b4bb7f520cc) · середовище production
• Код: GitHub scathlast-code/OrionAI, гілка main — кожне злиття розгортається само
• Збирання: Railpack (Dockerfile є в репозиторії, але не ввімкнений) · регіон sfo · 1 копія
• Том даних: orionai-volume → /data (500 МБ) — бази, пам'ять, знімки
• Адреса: https://orionai-production-6c93b86a.up.railway.app (порт 8080: вебхук Telegram, мініапп /app, MCP)
• Змінні середовища (лише назви — значення секретні й сюди не потрапляють):
  ALLOWED_USERS, APIFY_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, MCP_SECRET, OPENROUTER_API_KEY, SHORTIO_API_KEY, SHORTIO_DOMAIN,
  TAVILY_API_KEY, TELEGRAM_API_HASH, TELEGRAM_API_ID, TELEGRAM_TOKEN
"""


def services_text():
    import plugins.media.media as MM
    lines = [RAILWAY.format(date=datetime.date.today().isoformat()), "", "Моделі ШІ (каталог у bot.py → CATALOG; підключені ті, для яких є ключ):"]
    for pid, c in bot.CATALOG.items():
        lines.append(f"• {c.get('title', pid)} — модель {c.get('model', '')} · адреса {c.get('base_url', '')} · ключ у змінній {c.get('key_env', '')}"
                     + (f" · безкоштовний ліміт {c['limit'][1]} запитів/{'день' if c['limit'][0] == 'day' else 'місяць'}" if c.get("limit") else ""))
    lines += ["", "На сервері зараз підключені (є ключі): Gemini, OpenRouter. У «Авто» бот пробує їх по черзі й перемикається, якщо модель мовчить.",
              "Gemini: основна модель з GEMINI_MODEL (інакше gemini-flash-latest), запасні — GEMINI_FALLBACK_MODELS.",
              "", "Інші зовнішні сервіси:",
              f"• Apify (APIFY_TOKEN): сторіс Facebook — {getattr(MM, 'FB_STORY_ACTOR', '?')}; сторіс Instagram — {getattr(MM, 'APIFY_ACTOR', '?')}",
              "• Cobalt (запасне завантаження YouTube/TikTok): свій сервер COBALT_API_URL (якщо задано), далі " + ", ".join(MM.COBALT_PUBLIC),
              "• tikwm.com — запасне завантаження TikTok · yt-dlp, gallery-dl — основні завантажувачі",
              "• Tavily (TAVILY_API_KEY) та інші пошукові — пошук в інтернеті (websearch.py)",
              "• Short.io (SHORTIO_API_KEY, SHORTIO_DOMAIN) — короткі посилання для UTM-міток (лише з дозволу на кожен запис)",
              "• Open-Meteo, MET Norway (yr.no) — погода · Google Sheets (GOOGLE_SERVICE_ACCOUNT_JSON, якщо задано) — контент-план",
              "• Telegram API (TELEGRAM_TOKEN; TELEGRAM_API_ID/HASH — для читання каналів)",
              "", "Чого тут немає й чому: самих ключів і паролів (безпека), балансів і журналів акаунтів Apify, Gemini, OpenRouter —",
              "вони доступні лише з ключами в їхніх кабінетах; журнали Railway — у панелі Railway."]
    return "\n".join(lines)


def main():
    screens, app = asyncio.run(capture())
    appjs = json.dumps(app, ensure_ascii=False, default=str)
    html = (SHELL.replace("__DATE__", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))
            .replace("__NB__", str(len(screens))).replace("__NA__", str(len(app["screens"])))
            .replace("__BOT__", json.dumps(screens, ensure_ascii=False)).replace("__APP__", appjs)
            .replace("__APPHTML__", json.dumps(re.sub(r'<script[^>]+telegram-web-app[^>]*></script>', "",  # справжній Telegram підмінив би демо
                                                      (ROOT / "miniapp" / "index.html").read_text("utf-8")).replace(
                "const API = new URL('app/api', location.href.replace(/app\\/?(#.*)?$/, '')).href;", "const API = 'https://demo.local/app/api';"),
                ensure_ascii=False).replace("</", "<\\/")))
    dist = ROOT / "dist"; dist.mkdir(exist_ok=True)
    name = dist / f"orionai-webdemo-{datetime.date.today().isoformat()}.zip"
    with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("orionai-web.html", html)
        svc = services_text()
        z.writestr("orionai-code.txt", svc + "\n\n" + code_text())
        z.writestr("orionai-services.txt", svc)
        z.writestr("ПРОЧИТАЙ.txt", "orionai-web.html — відкрийте в браузері: ліворуч бот, праворуч мініапп (справжні екрани й кнопки).\n"
                                    "orionai-code.txt — повний код бота й мініапп з описом для інших нейромереж.\n"
                                    "orionai-services.txt — сервер Railway, моделі ШІ (Gemini, OpenRouter…), Apify, Cobalt та інші сервіси (без ключів).\n")
    print(name, len(screens), len(app["screens"]))


if __name__ == "__main__":
    main()
