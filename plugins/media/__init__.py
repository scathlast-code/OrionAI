"""📥 Медіа — завантаження відкритих медіа за посиланням і очищення метаданих власних файлів (з MediaBot).

- Посилання з YouTube, Instagram, TikTok, Facebook, Threads, X, Telegram, Reddit, Vimeo, Pinterest та сотень інших
  сайтів (yt-dlp, gallery-dl, прямі файли, відкриті дописи Telegram, Open Graph) → список знайденого → вибір
  елементів, якості, формату (відео / MP3 / GIF / без звуку), озвучки, субтитрів, таймкодів, тексту допису, ZIP.
- 🎵 Музика допису: назва накладеної пісні (з метаданих сайту або 🔎 Shazam) — у підписі; окремим файлом — фрагмент саме
  з допису й/або повна версія; 🗣 чистий звук автора (ШІ Demucs відокремлює голос від музики); каруселі фото під музику
  (TikTok, Instagram), дописи спільноти YouTube з кількома картинками. Усі налаштування — до завантаження.
- 🔴 Ефір — запис перших 1/3/5 хвилин. 🍪 Входи — cookies.txt для сайтів, що вимагають входу.
- 🧽 Метадані — звіт і очищена копія фото, відео, аудіо, PDF, Office, HTML/MHTML, ZIP/TAR.
Усе працює для кожного учасника окремо; одночасно одне завдання на людину.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import contextvars
import logging
import re
import secrets
import shutil
import time
from urllib.parse import quote
import tempfile
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from plugins import Plugin as Base, esc
from . import media as M
from . import related as R
from .common import JobError, TELEGRAM_IN, TELEGRAM_OUT, run, safe_name
from .metadata import REPORT_ONLY, clean, report, suffix
from .bigsend import BIG_LIMIT, Big, Progress

log = logging.getLogger("orion.plugins.media")
PAGE = 12
TOOLS = ("yt-dlp", "gallery-dl", "ffmpeg", "ffprobe", "exiftool", "mat2", "7z", "node", "libreoffice")
MODES = {"video": "🎬 Відео", "audio": "🎵 MP3", "gif": "🎞 GIF", "mute": "🔇 Без звуку"}
HEIGHTS = (0, 360, 480, 720, 1080, 2160)
LIVE = (60, 180, 300)
MEDIA_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch", "threads.net",
               "threads.com", "x.com", "twitter.com", "vimeo.com", "reddit.com", "pinterest.com", "pin.it", "twitch.tv",
               "dailymotion.com", "soundcloud.com", "tumblr.com", "likee.video", "snapchat.com", "rumble.com", "bsky.app")
QUICK_MAX = 10  # стільки елементів (альбом, карусель) завантажуємо одразу, без меню
URL_ONLY = re.compile(r"^\s*(https?://\S+)\s*$")


@dataclass
class Job:
    token: str
    url: str
    items: list
    options: M.Options = field(default_factory=M.Options)
    page: int = 0


@dataclass
class Pending:
    token: str
    file_id: str
    name: str
    size: int | None
    msg_id: int = 0
    chat: int = 0


def mb(n):
    return f"{n / 1048576:.1f} МБ"


def parse_numbers(text, n):
    """«1, 3-5 7» → [1, 3, 4, 5, 7] у межах 1..n."""
    out = []
    for a, b in re.findall(r"(\d+)\s*(?:[-–—]\s*(\d+))?", text or ""):
        lo, hi = int(a), int(b or a)
        for k in range(min(lo, hi), max(lo, hi) + 1):
            if 1 <= k <= n and k not in out:
                out.append(k)
    return out


def bar(done, total, speed=0, eta=0):
    """▓▓▓▓░░░░░░ 42% · 12.3 з 30.1 МБ · 2.1 МБ/с · ще 0:12"""
    if not total:
        return f"{mb(done)}" + (f" · {mb(speed)}/с" if speed else "")
    pct = max(0, min(100, int(done * 100 / total)))
    eta = int(eta or 0)
    return (f"{'▓' * (pct // 10)}{'░' * (10 - pct // 10)} {pct}% · {done / 1048576:.1f} з {mb(total)}"
            + (f" · {mb(speed)}/с" if speed else "") + (f" · ще {eta // 60}:{eta % 60:02d}" if eta else ""))


class Plugin(Base):
    id = "med"
    icon = "⬇️"
    title = "Завантажити"
    version = "1.0"
    admin_only = False
    summary = ("Завантаження відео, фото, аудіо й тексту дописів за посиланням із YouTube, Instagram, TikTok, Facebook, "
               "Threads, X, Telegram, Reddit, Vimeo та сотень інших сайтів: якість, MP3, GIF, без звуку, озвучка, субтитри, "
               "таймкоди, запис ефіру, ZIP. А також звіт і очищення метаданих власних файлів.")
    VIEWS = {"ck", "tools", "help", "arc", "arcl", "rls", "utg"}

    def __init__(self, host):
        super().__init__(host)
        M.COOKIES_DIR = self.dir / "cookies"
        M.COOKIES_DIR.mkdir(exist_ok=True)
        self.jobs: dict[int, Job] = {}
        self.pending: dict[int, Pending] = {}
        self.armed: dict[int, str] = {}       # чат -> "clean" | "cookie:<сайт>": чекаємо файл
        self.tasks: dict[int, asyncio.Task] = {}
        self.offers: dict[str, tuple[int, str]] = {}
        self.live: dict[int, tuple[str, list]] = {}
        self.instr: dict[int, dict] = {}  # ✍️ вказівки про теги до поточного завантаження
        self.got: dict[int, list] = {}  # чат -> файли останнього завантаження (для перегляду в Mini App)  # чат -> останній стан швидкого завантаження (для Mini App)
        self.hist = R.History(self.dir / "history.json")  # 🔗 історія скачаного: автор · тема · місце
        self.big = Big(self.dir, host.token)  # 🚀 файли до 2 ГБ через MTProto
        self.commands = [("media", self.cmd_media, "Медіа: завантажити за посиланням")]

    # ================================================================ особисті налаштування
    DQ = (("ask", "Питати щоразу"), ("360", "до 360p"), ("480", "до 480p"), ("720", "до 720p"), ("1080", "до 1080p"),
          ("1440", "до 1440p"), ("2160", "до 4K"), ("0", "Найкраща, що є"), ("a", "Лише звук (MP3)"))

    def _prefs(self):
        try:
            return json.loads((self.dir / "prefs.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def pref(self, chat, key, default=None):
        return self._prefs().get(str(chat), {}).get(key, default)

    def set_pref(self, chat, key, value):
        data = self._prefs()
        data.setdefault(str(chat), {})[key] = value
        (self.dir / "prefs.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def dq_label(self, chat):
        return dict(self.DQ).get(self.pref(chat, "q", "ask"), "Питати щоразу")

    def dq_screen(self, chat, back=None):
        cur = self.pref(chat, "q", "ask")
        t = ("⭐ <b>Якість за замовчуванням</b>\n<blockquote expandable>ℹ️ Як це працює\n«Питати щоразу» — на кожне відео YouTube "
             "бот показує меню лише з тими якостями, які це відео справді має на сайті (з розмірами), звук із бітрейтами "
             "оригіналу й вибір формату файлу. Якщо обрати межу (напр. «до 720p»), бот одразу бере найкращу якість, що є, "
             "але не вище межі; якщо відео має лише 360p — буде 360p (нічого не «роздуваємо»). Інший формат завжди можна взяти кнопкою під готовим файлом. "
             "Налаштування особисте — лише для вас.</blockquote>")
        rows, line = [], []
        for k, label in self.DQ:
            line.append((("✅ " if k == cur else "") + label, self.cb("dqs", k)))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([("« Медіа", self.cb())])
        return t, rows

    # ================================================================ 🔗 пов'язане скачане
    REL = (("a", "👤 Автор"), ("t", "🏷 Тема (і «без теми»)"), ("g", "📍 Локація (і «без локації»)"), ("l", "🗣 Мова"),
           ("f", "🆕 Вперше"), ("ai", "🤖 Визначати тему, локацію й мову за допомогою ШІ"))

    def rel_on(self, chat, k):
        return (self.pref(chat, "rel", {}) or {}).get(k, True)

    async def user_tags(self, chat, raw):
        """✍️ Теги, які людина написала поруч із посиланням -> [(вид, ключ)]; зберігаються назавжди."""
        found = []
        ans = ""
        if self.rel_on(chat, "ai"):
            try:
                ans, _m = await asyncio.wait_for(self.host.ask_ai(
                    "Теги від користувача: " + "; ".join(raw) + "\nПоверни JSON: "
                    '{"tags":[{"name":"Полісся","phrase":"з Полісся","kind":"place"},{"name":"озеро","phrase":"з озером","kind":"topic"}]} — '
                    "по одному на кожен тег, у тому ж порядку. name — називний відмінок українською (місце з великої, тема з малої), "
                    "phrase — продовження «попереднє …», kind — place (місце, регіон, країна, водойма з назвою) або topic.",
                    label="✍️ Теги користувача"), 30)
            except Exception:  # noqa: BLE001
                ans = ""
        items = []
        mm = re.search(r"\{.*\}", ans or "", re.S)
        if mm:
            try:
                items = [t for t in __import__("json").loads(mm.group(0)).get("tags", []) if isinstance(t, dict) and t.get("name")]
            except ValueError:
                items = []
        for i, tag in enumerate(raw):
            it = items[i] if i < len(items) else None
            seed = next(((kind, k) for kind in ("g", "t") for k, d in R.SEED[kind].items()
                         if R.stem_rx(d["name"]) and R.stem_rx(d["name"]).search(tag)), None)
            if it:
                kind = "g" if str(it.get("kind", "")).startswith("p") else "t"
                name, phrase = str(it["name"])[:40], str(it.get("phrase") or "")[:50] or "· " + str(it["name"])[:40]
            elif seed:
                kind, name, phrase = seed[0], R.SEED[seed[0]][seed[1]]["name"], R.SEED[seed[0]][seed[1]]["phrase"]
            else:  # без ШІ — як написала людина
                kind, name, phrase = "t", tag.strip(), "· " + tag.strip()
            found.append((kind, self.hist.learn_user(kind, name, phrase)))
        return found

    async def detect(self, chat, text, geotag, who, alink, instr=None):
        """Тема, локація, мова: ШІ (без обмежень) + словник + збережені теги людини + її вказівки до посилання;
        мова без тексту — як переважно пише автор. -> (ключі тем, ключі локацій, ключі мов) або None — «без тегів»."""
        instr = instr or {}
        if instr.get("none"):
            return None
        d_t, d_g = R.tags(f"{text}\n{geotag}")
        a_t, a_g, lang = [], [], ""
        body = (text or "").strip()
        if self.rel_on(chat, "ai") and (len(body) >= 12 or geotag):
            try:
                ans, _m = await asyncio.wait_for(self.host.ask_ai(
                    R.ai_prompt(body, geotag, self.hist.known("t"), self.hist.known("g")), system=R.AI_SYSTEM,
                    label="🔗 Тема й локація"), 40)
                a_t, a_g, lang = R.parse_ai(ans)
            except Exception:  # noqa: BLE001 — ШІ недоступний: лишається словник
                log.info("related: ШІ не визначив тему — беру словник")
        topics, geos = R.merge(a_t, d_t), R.merge(a_g, d_g)
        if geotag and not geos:  # геотег є, а ні ШІ, ні словник його не впізнали — беремо як є
            geos = [(geotag.strip()[:40], "· " + geotag.strip()[:40])]
        lang = lang or R.guess_lang(body)
        if not lang:  # без тексту — мова, якою переважно пише автор
            lang = await self.author_lang(chat, who, alink)
        t_k, g_k, l_k = self.hist.learn("t", topics), self.hist.learn("g", geos), self.hist.learn("l", R.lang_pair(lang))
        mine = self.hist.match_user(f"{text}\n{geotag}")  # ✍️ збережені теги людини, згадані в дописі
        t_k += [k for k in mine["t"] if k not in t_k]
        g_k += [k for k in mine["g"] if k not in g_k]
        for kind, k in (await self.user_tags(chat, instr["add"]) if instr.get("add") else []):
            (t_k if kind == "t" else g_k).insert(0, k) if k not in (t_k if kind == "t" else g_k) else None
        for d in instr.get("drop", []):  # «не став тег …»
            rx = R.stem_rx(d)
            for lst, kind in ((t_k, "t"), (g_k, "g"), (l_k, "l")):
                lst[:] = [k for k in lst if not (k == R.key_of(d) or (rx and rx.search(self.hist.name(kind, k))))]
        return t_k, g_k, l_k

    async def author_lang(self, chat, who, alink):
        """Переважна мова автора: з його попередніх скачаних дописів, інакше — з опису його сторінки."""
        a = R.norm_author(who)
        cnt = {}
        for e in self.hist.items(chat):
            if a and e.get("author") == a:
                for k in e.get("langs", []):
                    cnt[k] = cnt.get(k, 0) + 1
        if cnt:
            k = max(cnt, key=cnt.get)
            return next((c for c, (n, _p) in R.LANGS.items() if R.key_of(n) == k), "")
        if alink:
            try:
                _u, page = await asyncio.wait_for(M._grab(alink, 600_000), 12)
                meta = re.findall(r'<meta[^>]+(?:og:description|name="description")[^>]+content="([^"]*)"', page)
                return R.guess_lang(" ".join(meta))
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def mh_link(self, rid):
        try:
            u = self.host.bot().username
        except Exception:  # noqa: BLE001
            u = ""
        return f"https://t.me/{u}?start=mh_{rid}" if u else ""

    def related_parts(self, chat, who, topics, geos, langs):
        """-> (рядок для підпису HTML, його видима довжина, кнопки автора, «вперше»)."""
        r = self.hist.related(chat, who, topics, geos, langs)
        pairs = []
        for kind in ("g", "t", "l"):
            if self.rel_on(chat, kind):
                pairs += [(self.hist.label(kind, k), e) for k, e in r[kind].items()]
        pairs += [(R.EXTRA_LABEL[k], e) for k, e in r["x"].items()
                  if (k == "notopic" and self.rel_on(chat, "t")) or (k == "nogeo" and self.rel_on(chat, "g"))
                  or (k == "first" and self.rel_on(chat, "f"))]
        links, seen = [], set()
        for phrase, e in pairs:
            url = self.mh_link(e["id"])
            if url and e["id"] not in seen:  # одне й те саме повідомлення — одним посиланням
                seen.add(e["id"])
                links.append(("попереднє " + phrase, url))
        line = ("\n↩️ " + " · ".join(f'<a href="{esc(u)}">{esc(lab)}</a>' for lab, u in links)) if links else ""
        vis = len("\n↩️ " + " · ".join(lab for lab, _ in links)) if links else 0
        first = r["first"]
        if first and self.rel_on(chat, "f"):  # 🆕 тема чи локація вперше (автор «вперше» не позначається)
            note = "\n🆕 Вперше: " + self.hist.first_text(first)
            line, vis = esc(note) + line, vis + len(note)
        buttons = []
        if self.rel_on(chat, "a"):
            for st, e in r["site"].items():
                url = self.mh_link(e["id"])
                if url:
                    buttons.append((f"{R.icon(st)} Попереднє відео автора", url))
        return line, vis, buttons, [f"{a}:{b}" for a, b in first]

    async def jump(self, chat, rid):
        """↩️ Перехід до раніше скачаного: відповідь на старе повідомлення (дотик до цитати — і ви там)."""
        bot = self.host.bot()
        e = self.hist.get(chat, rid)
        if not e:
            return await self.host.notify(chat, "Цього запису в історії скачаного вже немає.")
        what = " · ".join(x for x in [e.get("who") and "👤 " + esc(e["who"]), esc(e.get("site") or ""),
                                       esc(R.ts_text(e.get("ts")))] if x)
        try:
            await bot.send_message(chat, f"⬆️ <b>Ось воно</b> — торкніться цитати, щоб перейти\n{what}", parse_mode="HTML",
                                   reply_to_message_id=e["msg"], allow_sending_without_reply=False)
        except Exception:  # noqa: BLE001 — повідомлення видалене з чату
            link = f'\n<a href="{esc(e["url"])}">🔗 Допис</a>' if str(e.get("url", "")).startswith("http") else ""
            await self.host.notify(chat, f"⚠️ Це повідомлення вже видалене з чату.\n{what}{link}")

    def arc_screen(self, chat):
        g = self.hist.groups(chat)
        n = len(self.hist.items(chat))
        t = ("🗂 <b>Архів скачаного</b>\n<blockquote expandable>ℹ️ Як це працює\nКожен скачаний файл бот запам'ятовує: автора, "
             "соцмережу, тему, локацію (з тексту допису й геотегу) і мову (без тексту — якою переважно пише автор). Тему й локацію "
             "визначає ШІ — без обмежень, увесь світ; нові назви запам'ятовуються. Під новим файлом з'являються посилання «попереднє з Карпат», "
             "«попереднє зі снігопадом», «попереднє без локації», «попереднє українською» і кнопки «попереднє відео автора» окремо для "
             "кожної соцмережі (іконкою). Порожніх посилань немає. Дотик — і бот показує те повідомлення. Кнопка «🔗 Допис» — оригінал. "
             "Що показувати — у «⚙️ Що пов'язувати».</blockquote>"
             f"\n\nЗаписів: {n} · авторів: {len(g['a'])} · тем: {len(g['t'])} · локацій: {len(g['g'])} · мов: {len(g['l'])}")
        rows = [[(f"👤 Автори · {len(g['a'])}", self.cb("arcl", "a")), (f"🏷 Теми · {len(g['t'])}", self.cb("arcl", "t"))],
                [(f"📍 Локації · {len(g['g'])}", self.cb("arcl", "g")), (f"🗣 Мови · {len(g['l'])}", self.cb("arcl", "l"))],
                [(f"{R.EXTRA_NAME[k]} · {len(g['x'][k])}", self.cb("arcl", "x", k)) for k in ("notopic", "nogeo", "first") if g["x"].get(k)],
                [("✍️ Мої теги", self.cb("utg")), ("⚙️ Що пов'язувати", self.cb("rls"))], [("« Медіа", self.cb())]]
        return t, [r for r in rows if r]

    def arc_keys(self, chat, kind):
        g = self.hist.groups(chat)[kind]
        return sorted(g, key=lambda k: (-len(g[k]), k)), g

    def arc_list(self, chat, kind, idx=None):
        keys, g = self.arc_keys(chat, kind)
        name = {"a": "👤 Автори", "t": "🏷 Теми", "g": "📍 Локації", "l": "🗣 Мови"}[kind]
        if idx is None or not (0 <= idx < len(keys)):
            t = f"🗂 <b>{name}</b>\n\n" + ("Поки порожньо — з'явиться після завантажень." if not keys else "Оберіть, щоб побачити все скачане:")
            rows, line = [], []
            for i, k in enumerate(keys[:60]):
                es = g[k]
                lab = (es[-1].get("who") or k) if kind == "a" else self.hist.name(kind, k)
                line.append((f"{lab[:24]} · {len(es)}", self.cb("arcl", kind, i)))
                if len(line) == 2:
                    rows.append(line)
                    line = []
            if line:
                rows.append(line)
            rows.append([("« Архів", self.cb("arc"))])
            return t, rows
        k = keys[idx]
        es = list(reversed(g[k]))[:25]
        lab = (es[0].get("who") or k) if kind == "a" else self.hist.name(kind, k)
        lines = [f"🗂 <b>{esc(lab)}</b> · {len(g[k])}", ""]
        for e in es:
            url = self.mh_link(e["id"])
            title = esc((e.get("title") or "файл")[:60])
            head = f'<a href="{esc(url)}">{title}</a>' if url else title
            lines.append(f"• {head} · {R.icon(e.get('site'))} {esc(e.get('who') or '')} · {esc(R.ts_text(e.get('ts')))}")
        return "\n".join(lines)[:4000], [[("« " + name.split(" ", 1)[1], self.cb("arcl", kind))], [("« Архів", self.cb("arc"))]]

    def arc_extra(self, chat, k):
        es = list(reversed(self.hist.groups(chat)["x"].get(k, [])))
        lines = [f"🗂 <b>{R.EXTRA_NAME[k]}</b> · {len(es)}", ""]
        for e in es[:25]:
            url = self.mh_link(e["id"])
            title = esc((e.get("title") or "файл")[:60])
            head = f'<a href="{esc(url)}">{title}</a>' if url else title
            fx = (" · " + esc(self.hist.first_text([tuple(f.split(":", 1)) for f in e["first"]]))) if k == "first" and e.get("first") else ""
            lines.append(f"• {head} · {R.icon(e.get('site'))} {esc(e.get('who') or '')}{fx} · {esc(R.ts_text(e.get('ts')))}")
        return "\n".join(lines)[:4000], [[("« Архів", self.cb("arc"))]]

    @staticmethod
    def instr_text(instr):
        if instr.get("none"):
            return "✍️ Добре — це завантаження без тегів і сортування."
        parts = []
        if instr.get("add"):
            parts.append("додам тег: " + ", ".join(f"«{t}»" for t in instr["add"]) + " (збережу — надалі шукатиму його й сам)")
        if instr.get("drop"):
            parts.append("не ставлю: " + ", ".join(f"«{t}»" for t in instr["drop"]))
        return "✍️ " + "; ".join(parts)

    def mytags_screen(self, chat):
        tags = self.hist.user_tags()
        g = self.hist.groups(chat)
        t = ("✍️ <b>Мої теги</b>\n<blockquote expandable>ℹ️ Як додати\nНадішліть посилання і поруч напишіть, напр.: «сортуй по тегу полісся», "
             "«групуй всі за озером», «тег: Шацькі озера», «#полісся». Бот поставить тег, збереже його й надалі сам шукатиме в нових дописах. "
             "«не став тег снігопад» — без одного тегу; «без тегів» — це завантаження без сортування взагалі.</blockquote>\n\n"
             + ("Поки немає." if not tags else "Дотик — прибрати тег зі збережених (уже скачане лишається в групі)."))
        rows = []
        for i, (kind, k, d) in enumerate(tags[:40]):
            n = len(g[kind].get(k, []))
            rows.append([(f"🗑 {'📍' if kind == 'g' else '🏷'} {d.get('name', k)[:28]} · {n}", self.cb("utd", i))])
        rows.append([("« Архів", self.cb("arc"))])
        return t, rows

    def rel_screen(self, chat):
        t = ("⚙️ <b>Що пов'язувати з новими завантаженнями</b>\n\nУвімкнене з'являється під кожним новим файлом: "
             "посилання на попереднє з тією ж локацією, темою чи мовою, кнопки на попереднє відео автора. «🤖 ШІ» — тема й локація "
             "без обмежень (вимкнено — лише вбудований словник). Налаштування особисте.")
        rows = [[(("✅ " if self.rel_on(chat, k) else "▫️ ") + lab, self.cb("rlt", k))] for k, lab in self.REL]
        rows.append([("« Архів", self.cb("arc"))])
        return t, rows

    # ================================================================ екрани
    def home(self, chat):
        busy = self.busy(chat)
        t = ("📥 <b>Медіа</b>\n<blockquote expandable>ℹ️ Що вміє\n" + esc(self.summary) +
             "\n\nПосилання можна просто надіслати в чат — бот одразу завантажить відео (до 1080p) і надішле файл. "
             "Інший формат (MP3, якість, субтитри, таймкоди) — кнопкою під готовим файлом."
             "\n" + ("🚀 Великі файли — до 2 ГБ (пряме з'єднання з Telegram увімкнено)." if self.big.ready() else
                     "⚠️ Без TELEGRAM_API_ID і TELEGRAM_API_HASH у Railway бот надсилає лише до 50 МБ і приймає до 20 МБ; "
                     "з ними — до 2 ГБ (як інші боти-завантажувачі). Ключі: my.telegram.org → API development tools.") + "</blockquote>")
        if busy:
            t += "\n\n⏳ Зараз виконується ваше завдання."
        rows = [[("🔗 Завантажити за посиланням", self.cb("url"))], [("🧽 Метадані файлу: звіт і очищення", self.cb("clean"))]]
        if busy:
            rows.insert(0, [("⏹ Зупинити завдання", self.cb("stop"))])
        if chat in self.jobs:
            rows.insert(1, [("📋 Останній знайдений набір", self.cb("menu", self.jobs[chat].token))])
        rows += [[(f"⭐ Якість за замовчуванням: {self.dq_label(chat)}", self.cb("dq"))],
                 [("🗂 Архів скачаного: автори · теми · місця", self.cb("arc"))],
                 [("🍪 Входи на сайти (cookies)", self.cb("ck")), ("🛠 Інструменти", self.cb("tools"))],
                 [("🩺 Перевірити, які сайти працюють", self.cb("chk"))]]
        return t, rows

    def job_screen(self, chat):
        job = self.jobs.get(chat)
        if not job:
            return self.home(chat)
        o, t = job.options, job.token
        start, end = PAGE * job.page, min(PAGE * job.page + PAGE, len(job.items))
        lines = [f"📥 <b>Знайдено: {len(job.items)}</b>", f"<code>{esc(job.url[:120])}</code>", ""]
        for i in range(start, end):
            it = job.items[i]
            mark = " · прев'ю" if it.info.get("preview") else (" · 🔴 ефір" if it.info.get("is_live") else "")
            lines.append(f"{i + 1}. {esc(it.title[:90])}{mark}")
        lines.append("")
        lines.append(f"Якість: {o.height or 'найкраща'} · Формат: {MODES[o.mode]} · Текст допису: {'так' if o.post else 'ні'}"
                     + (" · 📦 ZIP" if o.target == "zip" else ""))
        if o.audio != "auto" or o.subs != "off":
            lines.append(f"Озвучка: {o.audio} · Субтитри: {o.subs}")
        if o.cuts:
            lines.append(f"✂️ Таймкоди: {len(o.cuts)} · {'в один файл' if o.join else 'окремо'}")
        rows = [[(f"📥 Завантажити все · {len(job.items)}" if len(job.items) > 1 else "📥 Завантажити", self.cb("g", t, 0))]]
        if len(job.items) > 1:
            nums = [(str(i), self.cb("g", t, i)) for i in range(start + 1, end + 1)]
            rows += [nums[k:k + 6] for k in range(0, len(nums), 6)]
            nav = []
            if job.page:
                nav.append(("‹ Назад", self.cb("pg", t, job.page - 1)))
            if end < len(job.items):
                nav.append(("Далі ›", self.cb("pg", t, job.page + 1)))
            if nav:
                rows.append(nav)
            rows.append([("🔢 Вибрати кілька (1,3,5)", self.cb("pk", t))])
        rows += [[(f"🎞 Якість: {o.height or 'авто'}", self.cb("q", t)), (MODES[o.mode], self.cb("md", t))],
                 [(f"📝 Текст: {'так' if o.post else 'ні'}", self.cb("tx", t)), ("📦 Одним ZIP ✓" if o.target == "zip" else "📦 Одним ZIP", self.cb("zp", t))],
                 [("🗣 Озвучка", self.cb("au", t)), ("💬 Субтитри", self.cb("sb", t)), ("✂️ Таймкоди", self.cb("ct", t))]]
        if any(it.info.get("is_live") or it.info.get("live_status") == "is_live" for it in job.items):
            rows.append([(f"🔴 Запис ефіру: {o.live // 60} хв", self.cb("lv", t))])
        rows.append([("« Медіа", self.cb())])
        return "\n".join(lines)[:4000], rows

    def cookies_screen(self, chat):
        lines = ["🍪 <b>Входи на сайти (cookies)</b>",
                 "<blockquote expandable>ℹ️ Навіщо й як\nInstagram, Facebook, Threads, TikTok, X і YouTube часто не віддають медіа "
                 "серверам без входу. Якщо завантажити сюди файл cookies.txt із вашого браузера, бот відкриватиме ці сайти як ви.\n"
                 "Як отримати: у браузері на комп'ютері встановіть розширення «Get cookies.txt LOCALLY», відкрийте сайт, увійдіть "
                 "і натисніть «Export». Надішліть отриманий .txt сюди.\n⚠️ Cookies — це вхід у ваш акаунт: не діліться ними. "
                 "Видалити можна будь-коли кнопкою 🗑.</blockquote>", ""]
        rows = []
        for site in M.COOKIE_SITES:
            have = (M.COOKIES_DIR / f"{site}.txt").is_file()
            lines.append(f"{'✅' if have else '—'} {site}")
            row = [(f"{'🔄' if have else '➕'} {site}", self.cb("cks", site))]
            if have:
                row.append(("🗑", self.cb("ckd", site)))
            rows.append(row)
        rows.append([("« Медіа", self.cb())])
        return "\n".join(lines), rows

    async def tools_screen(self, chat):
        lines = ["🛠 <b>Інструменти</b>"]
        for tool in TOOLS:
            lines.append(f"{'✅' if shutil.which(tool) else '❌'} {tool}" + (" (старі DOC/XLS/PPT)" if tool == "libreoffice" else ""))
        for tool in ("yt-dlp", "gallery-dl"):
            if shutil.which(tool):
                try:
                    lines.append(f"{tool}: версія {(await run(tool, '--version', timeout=20)).strip()[:20]}")
                except JobError:
                    pass
        rows = [[("🩺 Перевірити сайти", self.cb("chk"))]]
        rows += [[("🔄 Оновити yt-dlp і gallery-dl", self.cb("upd"))]] if self.host.is_admin(chat) else []
        rows.append([("« Медіа", self.cb())])
        import os as _os
        tok = M.apify_token()
        lines.append(f"\n🖥 Працює: {esc(M.server_name())}")
        lines.append("\n<b>Ключі</b>")
        lines.append(f"{'✅' if tok else '❌'} APIFY_TOKEN (сторіс Instagram і Facebook)" + (f" · …{esc(tok[-4:])}" if tok else ""))
        if not tok:
            lines.append("   " + esc(M.apify_hint()))
            rows.insert(0, [("🔑 Вставити ключ Apify тут", self.cb("akey"))])
        elif (self.dir / "apify.key").is_file():
            rows.insert(0, [("🗑 Прибрати ключ Apify", self.cb("akeyd"))])
        lines.append(f"{'✅' if M.pot_ready() else '—'} YouTube PO Token (bgutil, безкоштовно)")
        lines.append(f"{'✅' if _os.environ.get('MEDIA_PROXY') else '—'} MEDIA_PROXY (YouTube)")
        lines.append(f"{'✅' if self.big.ready() else '—'} TELEGRAM_API_ID / HASH (файли до 2 ГБ)")
        lines.append("\nСайти часто змінюються — якщо щось перестало завантажуватись, оновіть завантажувачі.")
        return "\n".join(lines), rows

    # ================================================================ меню
    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else ""
        y = args[2] if len(args) > 2 else ""
        if a == "":
            return None, self.home(chat)
        if a == "url":
            self.ask(chat, "url")
            return None, ("🔗 <b>Надішліть посилання</b> на відео, допис, альбом, плейлист, ефір чи файл.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "clean":
            self.armed[chat] = "clean"
            return None, ("🧽 <b>Надішліть файл</b> — краще як документ (📎 → Файл), щоб Telegram не стискав його. "
                          "Покажу знайдені метадані й запропоную очищену копію.", [[("✖️ Скасувати", self.cb("unarm"))]])
        if a == "unarm":
            self.armed.pop(chat, None)
            return "Скасовано", self.home(chat)
        if a == "dq":
            return None, self.dq_screen(chat)
        if a == "dqs" and x in dict(self.DQ):
            self.set_pref(chat, "q", x)
            return f"⭐ Тепер: {self.dq_label(chat)}", self.dq_screen(chat)
        if a == "arc":
            return None, self.arc_screen(chat)
        if a == "arcl" and x == "x" and y in R.EXTRA_NAME:
            return None, self.arc_extra(chat, y)
        if a == "arcl" and x in ("a", "t", "g", "l"):
            return None, self.arc_list(chat, x, int(y) if y.isdigit() else None)
        if a == "utg":
            return None, self.mytags_screen(chat)
        if a == "utd" and x.isdigit():
            tags = self.hist.user_tags()
            if int(x) < len(tags):
                kind, k, _d = tags[int(x)]
                self.hist.drop_user(kind, k)
            return "🗑 Прибрано", self.mytags_screen(chat)
        if a == "rls":
            return None, self.rel_screen(chat)
        if a == "rlt" and x in dict(self.REL):
            cur = dict(self.pref(chat, "rel", {}) or {})
            cur[x] = not self.rel_on(chat, x)
            self.set_pref(chat, "rel", cur)
            return ("Увімкнено" if cur[x] else "Вимкнено"), self.rel_screen(chat)
        if a == "oth":  # 👤 інші відео автора — список з його каналу
            job = self.job_by(chat, x)
            url = self.author_url(job.items[0].info) if job and job.items else ""
            if not url:
                return "Сторінку автора не знайдено", None
            return self.start_quick(chat, url) or "👤 Відкриваю відео автора…", None
        if a == "ck":
            return None, self.cookies_screen(chat)
        if a == "cks" and x in M.COOKIE_SITES:
            self.armed[chat] = "cookie:" + x
            return None, (f"🍪 Надішліть файл cookies.txt для <b>{x}</b> (документом).", [[("✖️ Скасувати", self.cb("unarm"))]])
        if a == "ckd" and x in M.COOKIE_SITES:
            (M.COOKIES_DIR / f"{x}.txt").unlink(missing_ok=True)
            return "🗑 Видалено", self.cookies_screen(chat)
        if a == "tools":
            return None, await self.tools_screen(chat)
        if a == "akey":
            self.ask(chat, "akey")
            return None, ("🔑 <b>Надішліть ключ Apify</b> (починається з <code>apify_api_</code>).\n"
                          "Бот збереже його на сервері й одразу видалить ваше повідомлення з чату.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "akeyd":
            (self.dir / "apify.key").unlink(missing_ok=True)
            return "🗑 Прибрано", await self.tools_screen(chat)
        if a == "chk":
            if self.busy(chat):
                return "⏳ Попереднє завдання ще триває", None
            self.live[chat] = ("🩺 Перевіряю сайти…", [])
            self._bg(chat, self.check_sites(chat))
            return "🩺 Перевіряю…", (self.live_screen(chat) if self.in_app() else None)
        if a == "upd" and self.host.is_admin(chat):
            self._bg(chat, self.update_tools(chat))
            return "🔄 Оновлюю…", None
        if a == "stop":
            task = self.tasks.get(chat)
            if task and not task.done():
                task.cancel()
                return "⏹ Зупиняю", self.home(chat)
            return "Активного завдання немає", self.home(chat)
        if a == "off" and x in self.offers:  # старі кнопки «Знайти й завантажити» з попередньої версії
            c, url = self.offers.pop(x)
            return self.start_quick(chat, url) or "🔎 Шукаю…", (self.live_screen(chat) if self.in_app() else None)
        if a in ("og", "ox", "oa", "os", "osv", "oso", "osm", "opt", "ofm"):
            job = self.job_by(chat, x)
            if not job:
                return "Посилання застаріло — надішліть його ще раз", None
            if a == "ofm":  # 📦 формат файлу — по колу з тих, що підходять до цих файлів
                vis, _aud, imgs = self.parts(job)
                fm = self.file_formats(job, len(vis) - len(imgs), imgs)
                cur = job.options.fmt if job.options.fmt in fm else ""
                job.options.fmt = fm[(fm.index(cur) + 1) % len(fm)]
                return None, self.options_screen(chat, job)
            if a == "ox":  # перемикач — лише оновлюємо екран налаштувань
                xs = job.__dict__.setdefault("x", set())
                xs.symmetric_difference_update({y})
                return None, self.options_screen(chat, job)
            if a == "os":
                return None, self.sep_screen(job)
            if a == "opt":
                if len(job.items) == 1 and job.items[0].engine == "yt":
                    self._bg(chat, self.show_formats(chat, job, None))
                    return "⚙️", None
                return None, self.options_screen(chat, job)
            if self.busy(chat):
                return "⏳ Ще завантажую попереднє", None
            if a == "og":
                self._bg(chat, self.opt_go(chat, job))
            elif a == "oa":
                self._bg(chat, self.post_audio(chat, job))
            else:
                self._bg(chat, self.sep_go(chat, job, a[-1] if a != "osv" else "v"))
            return "⬇️ Починаю", (self.live_screen(chat) if self.in_app() else None)
        if a in ("fau", "fab", "fvf", "fvs"):
            job = self.job_by(chat, x)
            if not job:
                return "Посилання застаріло — надішліть його ще раз", None
            return await self.fmt_extra(chat, job, a, y)
        if a in ("fq", "fa", "fp", "ff", "fr", "fall", "sp", "spn"):
            job = self.job_by(chat, x)
            if not job:
                return "Посилання застаріло — надішліть його ще раз", None
            if self.busy(chat) and a != "fp":
                return "⏳ Ще завантажую попереднє", None
            if a == "fq":
                self._bg(chat, self.fmt_go(chat, job, [1], mode="video", height=int(y or 0) or 0, cuts=()))
            elif a == "fa":
                self._bg(chat, self.fmt_go(chat, job, [1], mode="audio", cuts=()))
            elif a == "fp":
                thumb = job.items[0].info.get("thumbnail")
                if not thumb:
                    return "Превʼю немає", None
                await self.host.bot().send_photo(chat, thumb, caption="🖼 Превʼю")
                return "🖼", None
            elif a == "ff":
                self._bg(chat, self.full_song(chat, job))
            elif a == "fr":
                self._bg(chat, self.recognize(chat, job))
            elif a == "fall":
                nums = list(range(1, len(job.items) + 1))
                self._bg(chat, self.fmt_go(chat, job, nums, **({"mode": "audio"} if y == "a" else {"mode": "video", "height": 720})))
            elif a == "sp":
                n = len(job.items)
                nums = list(range(1, n + 1)) if y == "all" else [n] if y == "last" else [int(y)] if str(y).isdigit() and 1 <= int(y) <= n else []
                if not nums:
                    return "Немає такого номера", None
                self._bg(chat, self.fmt_go(chat, job, nums))
            elif a == "spn":
                self.ask(chat, "spn", job.token)
                return None, (f"🔢 Напишіть номери сторіс (1–{len(job.items)}), напр. <code>1, 3-5</code>.", [[("✖️ Скасувати", "m:cancel")]])
            return "⬇️ Починаю", (self.live_screen(chat) if self.in_app() else None)
        if a == "st":
            return None, self.live_screen(chat)
        if a == "re" and x in self.offers:  # 🔁 повторити після помилки
            return self.start_quick(chat, self.offers[x][1]) or "🔎 Шукаю…", (self.live_screen(chat) if self.in_app() else None)
        if a == "cl" and chat in self.pending and self.pending[chat].token == x:
            p = self.pending.pop(chat)
            if y != "yes":
                return "Скасовано", None
            self._bg(chat, self.clean_file(chat, p))
            return "🧽 Очищаю копію…", None
        job = self.jobs.get(chat)
        if not job or job.token != x:
            return "Це меню застаріло — надішліть посилання ще раз", self.home(chat)
        o = job.options
        if a == "menu":
            return None, self.job_screen(chat)
        if a == "g":
            if self.busy(chat):
                return "⏳ Попереднє завдання ще триває", None
            n = int(y or 0)
            nums = [n] if n else list(range(1, len(job.items) + 1))
            if any(k < 1 or k > len(job.items) for k in nums):
                return "Такого номера немає", None
            self._bg(chat, self.download(chat, job, nums))
            return f"📥 Завантажую: {len(nums)}", None
        if a == "pg":
            job.page = max(0, min(int(y or 0), (len(job.items) - 1) // PAGE))
        elif a == "q":  # лише ті якості, що справді є на сайті (для одного відео); інакше — загальний ряд
            real = M.video_heights(job.items[0].info) if len(job.items) == 1 else []
            seq = [0] + real if real else list(HEIGHTS)
            o.height = seq[(seq.index(o.height) + 1) % len(seq)] if o.height in seq else 0
        elif a == "md":
            keys = list(MODES)
            o.mode = keys[(keys.index(o.mode) + 1) % len(keys)]
        elif a == "tx":
            o.post = not o.post
        elif a == "zp":
            o.target = "tg" if o.target == "zip" else "zip"
        elif a == "lv":
            o.live = LIVE[(LIVE.index(o.live) + 1) % len(LIVE)] if o.live in LIVE else 180
        elif a in ("au", "sb", "ct", "pk"):
            prompts = {"au": "🗣 Мова озвучки, напр. <code>uk</code>, <code>en</code>; <code>auto</code> — як вирішить сайт.",
                       "sb": "💬 Мова субтитрів, напр. <code>uk</code>, <code>en</code>; <code>off</code> — без субтитрів.",
                       "ct": "✂️ Таймкоди: <code>00:12-00:30, 01:05-01:25</code> — окремі шматки; додайте слово <code>join</code> на початку, "
                             "щоб склеїти в один файл; <code>off</code> — скинути.",
                       "pk": "🔢 Номери через кому, напр. <code>1,3,5</code>."}
            self.ask(chat, a, job.token)
            return None, (prompts[a], [[("✖️ Скасувати", "m:cancel")]])
        return None, self.job_screen(chat)

    async def on_text(self, update, ctx, chat, key, data, text):
        text = (text or "").strip()
        if key == "spn":  # номери сторіс: «1, 3-5»
            job = self.job_by(chat, data)
            if not job:
                return "Посилання застаріло", None
            nums = parse_numbers(text, len(job.items))
            if not nums:
                self.ask(chat, "spn", data)
                return "Не бачу номерів", (f"🔢 Номери від 1 до {len(job.items)}, напр. <code>1, 3-5</code>.", [[("✖️ Скасувати", "m:cancel")]])
            self._bg(chat, self.fmt_go(chat, job, nums))
            return f"⬇️ Завантажую: {', '.join(map(str, nums))}", (self.live_screen(chat) if self.in_app() else None)
        if key == "akey":
            try:
                await update.message.delete()  # ключ — як пароль: у чаті не лишаємо
            except Exception:  # noqa: BLE001
                pass
            token = text.strip().strip('"\'')
            if not re.fullmatch(r"apify_api_[A-Za-z0-9]{20,}", token):
                self.ask(chat, "akey")
                return "Не схоже на ключ", ("⚠️ Ключ Apify починається з <code>apify_api_</code>. Скопіюйте його ще раз.", [[("✖️ Скасувати", "m:cancel")]])
            path = self.dir / "apify.key"
            path.write_text(token, "utf-8")
            path.chmod(0o600)
            return "✅ Ключ збережено", await self.tools_screen(chat)
        if key == "url":
            m = re.search(r"https?://\S+", text)
            if not m:
                self.ask(chat, "url")
                return "Не бачу посилання", ("⚠️ Потрібне посилання, що починається з https://", [[("✖️ Скасувати", "m:cancel")]])
            toast = self.start_quick(chat, m[0].rstrip(").,]»"), R.parse_instr(text))
            return toast, (self.live_screen(chat) if self.in_app() else None)
        job = self.jobs.get(chat)
        if not job or job.token != data:
            return "Меню застаріло", self.home(chat)
        o = job.options
        try:
            if key == "au":
                if not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{2,8})?", text):
                    raise JobError("Приклад: uk, en або auto")
                o.audio = text.lower()
            elif key == "sb":
                if not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{2,8})?", text):
                    raise JobError("Приклад: uk, en або off")
                o.subs = text.lower()
            elif key == "ct":
                if text.lower() == "off":
                    o.cuts, o.join = (), False
                else:
                    join = text.lower().startswith("join")
                    o.cuts = M.parse_cuts(text[4:].strip() if join else text)
                    o.join = join
            elif key == "pk":
                if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", text):
                    raise JobError("Приклад: 1,3,5")
                nums = list(dict.fromkeys(int(v) for v in text.split(",")))
                if any(n < 1 or n > len(job.items) for n in nums):
                    raise JobError("Такого номера немає в списку")
                if self.busy(chat):
                    raise JobError("Попереднє завдання ще триває")
                self._bg(chat, self.download(chat, job, nums))
                return f"📥 Завантажую: {len(nums)}", self.job_screen(chat)
        except JobError as exc:
            self.ask(chat, key, data)
            return str(exc), (f"⚠️ {esc(str(exc))}", [[("✖️ Скасувати", "m:cancel")]])
        return "✅ Збережено", self.job_screen(chat)

    async def cmd_media(self, update, ctx):
        chat = update.effective_chat.id
        arg = (update.message.text or "").partition(" ")[2].strip()
        if arg.startswith("http"):
            toast = self.start_quick(chat, arg)
            if toast:
                await update.message.reply_text(toast)
            return
        t, rows = self.home(chat)
        await self.host.notify(chat, t, rows)

    # ================================================================ посилання в чаті
    async def intercept(self, update, ctx):
        """Повідомлення лише з посиланням на відомий відеосайт — одразу завантажуємо (питання з посиланням іде до ШІ)."""
        msg = update.message
        if not msg or msg.chat.type != "private" or not msg.text:
            return False
        m = URL_ONLY.match(msg.text)
        instr = None
        if not m:  # посилання + вказівки про теги: «сортуй по тегу полісся», «без тегів»
            mm = re.search(r"https?://\S+", msg.text)
            instr = R.parse_instr(msg.text) if mm and len(re.findall(r"https?://", msg.text)) == 1 else None
            m = (None, mm[0].rstrip(").,]»")) if instr else None
        if not m or self.host.extra.get("awaiting", lambda c: False)(msg.chat_id):
            return False
        host = (M.urlsplit(m[1]).hostname or "").lower()
        if not M.domain_matches(host, MEDIA_HOSTS):
            return False
        toast = self.start_quick(msg.chat_id, m[1], instr)
        if instr and not toast:
            await msg.reply_text(self.instr_text(instr))
        if toast or self.in_app():
            await msg.reply_text(toast or "📥 Завантажую. Стан — у розділі «📥 Медіа», файл прийде в чат з ботом.")
        return True

    async def on_file(self, update, ctx):
        """Файл, коли ми на нього чекаємо: 🧽 очищення метаданих або 🍪 cookies.txt."""
        msg = update.message
        chat = msg.chat_id if msg else None
        mode = self.armed.get(chat)
        if not mode:
            return False
        self.armed.pop(chat, None)
        if mode.startswith("cookie:"):
            site = mode.split(":", 1)[1]
            if not msg.document or (msg.document.file_size or 0) > 1_000_000:
                await msg.reply_text("Потрібен текстовий файл cookies.txt (документом, до 1 МБ).")
                return True
            data = bytes(await (await ctx.bot.get_file(msg.document.file_id)).download_as_bytearray())
            text = data.decode("utf-8", "ignore")
            if "\t" not in text or not re.search(r"(?m)^[^#\n]*\t(TRUE|FALSE)\t", text):
                await msg.reply_text("Це не схоже на cookies.txt у форматі Netscape. Експортуйте розширенням «Get cookies.txt LOCALLY».")
                return True
            path = M.COOKIES_DIR / f"{site}.txt"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            try:
                await msg.delete()  # cookies — це вхід в акаунт: не лишаємо файл у чаті
            except Exception:  # noqa: BLE001
                pass
            t, rows = self.cookies_screen(chat)
            await self.host.notify(chat, f"✅ Вхід для {site} збережено (файл із чату прибрав).\n\n" + t, rows)
            return True
        file, default = message_attachment(msg)
        if not file:
            await msg.reply_text("Надішліть файл, фото, відео чи аудіо.")
            return True
        if msg.forward_origin:
            await msg.reply_text("Для очищення надішліть власний файл, а не пересланий.")
            return True
        name = getattr(file, "file_name", None) or default
        p = Pending(secrets.token_hex(4), file.file_id, safe_name(name), getattr(file, "file_size", None), msg.message_id, chat)
        self._bg(chat, self.inspect_file(chat, p))
        return True

    async def on_document(self, update, ctx):
        """Файл із застосунку (там він приходить як документ) — та сама обробка, якщо ми на нього чекаємо."""
        return await self.on_file(update, ctx)

    # ================================================================ робота
    def busy(self, chat):
        t = self.tasks.get(chat)
        return bool(t and not t.done())

    def _bg(self, chat, coro):
        async def wrap():
            try:
                await coro
            except asyncio.CancelledError:
                await self.host.notify(chat, "⏹ Завдання зупинено.")
            except JobError as exc:
                await self.host.notify(chat, f"⚠️ {esc(str(exc))[:3500]}")
            except Exception as exc:  # noqa: BLE001
                log.exception("media job failed")
                await self.host.notify(chat, f"⚠️ Не вдалося: {esc(type(exc).__name__)}")
            finally:
                self._active(chat, done=True)
        # Чистий контекст: якщо завдання почали з Mini App, його повідомлення й файли мають іти в справжній чат,
        # а не в «перехоплювач» запиту застосунку, який уже завершився.
        task = asyncio.get_running_loop().create_task(wrap(), context=contextvars.Context())
        self.tasks[chat] = task

    # ---------------------------------------------------------------- 1 крок: посилання → файл
    def start_quick(self, chat, url, instr=None):
        """Повертає текст лише якщо почати не можна; інакше все показує саме повідомлення-статус.
        instr — вказівки людини про теги («сортуй по тегу полісся», «без тегів»)."""
        if self.busy(chat):
            return "⏳ Ще завантажую попереднє — дочекайтесь або зупиніть: /media"
        self.instr[chat] = instr or {}
        self.live[chat] = (f"🔎 Шукаю: {esc(M.describe_source(url))}…", [])
        self._bg(chat, self.quick(chat, url))
        return None

    def in_app(self):
        return self.host.extra.get("in_app", lambda: False)()

    SERVE_HOURS = 3
    SERVE_MAX = 3 * 1024 ** 3

    def keep(self, path):
        """Файл після надсилання в чат — ще на кілька годин доступний у Mini App (/app/file/<токен>/<назва>)."""
        root = self.dir / "served"
        root.mkdir(exist_ok=True)
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        total = 0
        for d in dirs:
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            if time.time() - d.stat().st_mtime > self.SERVE_HOURS * 3600:
                shutil.rmtree(d, ignore_errors=True)
            else:
                total += size
        while dirs and total + path.stat().st_size > self.SERVE_MAX:  # місце — старші йдуть першими
            d = dirs.pop(0)
            if d.exists():
                total -= sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
        token = secrets.token_urlsafe(12)
        dest = root / token
        dest.mkdir()
        name = safe_name(path.name, "media")
        try:
            shutil.move(str(path), str(dest / name))
        except OSError:
            return None
        ext = Path(name).suffix.lower()
        kind = ("video" if ext in (".mp4", ".mov", ".webm", ".m4v") else "image" if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")
                else "audio" if ext in (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac") else "file")
        return {"name": name, "url": f"/app/file/{token}/{quote(name)}", "size": (dest / name).stat().st_size, "kind": kind}

    def live_screen(self, chat):
        """Стан для Mini App: поки триває — «🔄 Оновити стан» (застосунок натискає її сам кожні 3 с)."""
        text, rows = self.live.get(chat) or ("Активного завантаження немає.", [])
        if self.busy(chat):
            return (text + "\n\n<i>📲 Готові файли приходять у чат з ботом.</i>",
                    [[("🔄 Оновити стан", self.cb("st")), ("⏹ Зупинити", self.cb("stop"))], [("« Медіа", self.cb())]])
        rows = [r for r in (rows or []) if r] or [[("« Медіа", self.cb())]]
        files = self.got.get(chat) or []
        if files:  # застосунок покаже плеєр / фото й кнопку «Зберегти»
            self.host.extra.get("app_media", {})[chat] = files
        tail = ("\n\n<i>📲 Ті самі файли — і в чаті з ботом.</i>" if files else "\n\n<i>📲 Файли — у чаті з ботом.</i>") \
            if text.startswith(("✅", "📝")) else ""
        return text + tail, [[("🔗 Ще посилання", self.cb("url"))]] + rows

    def _markup(self, rows):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]) if rows else None

    async def _status(self, chat, msg, text, rows=None):
        """Одне повідомлення, що оновлюється: 🔎 → ⬇️ → ✅/⚠️."""
        self.live[chat] = (text, [r for r in (rows or []) if not any(d == self.cb("stop") for _, d in r)])
        if msg:
            try:
                await self.host.bot().edit_message_text(text[:4096], chat_id=chat, message_id=msg.message_id, parse_mode="HTML",
                                                        reply_markup=self._markup(rows), disable_web_page_preview=True)
                return msg
            except Exception as exc:  # noqa: BLE001
                why = str(exc).lower()
                # «не змінилось» (той самий текст) чи «забагато запитів» — лишаємо старе повідомлення, а не шлемо нове.
                # Нове — лише якщо старе зникло (видалене).
                if "not found" not in why and "can't be edited" not in why:
                    return msg
        return await self.host.notify(chat, text, rows) or msg

    # ---- статуси, що пережили б перезапуск: бот відмічає, а після старту виправляє «завислі» повідомлення
    def _active_path(self):
        return self.dir / "active.json"

    def _active(self, chat, status=None, url=None, done=False):
        try:
            data = json.loads(self._active_path().read_text("utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        if done:
            data.pop(str(chat), None)
        elif status is not None:
            data[str(chat)] = [getattr(status, "message_id", 0), url or "", int(time.time())]
        try:
            self._active_path().write_text(json.dumps(data), "utf-8")
        except OSError:
            pass

    async def start(self):
        """Після перезапуску: «🔎 Шукаю…» чи «⬇️ Завантажую…», що лишились від перерваного завдання — чесно позначити."""
        try:
            data = json.loads(self._active_path().read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return
        self._active_path().unlink(missing_ok=True)
        await asyncio.sleep(5)
        for chat, (mid, url, _t) in data.items():
            t = secrets.token_hex(4)
            self.offers[t] = (int(chat), url)
            try:
                await self.host.bot().edit_message_text(
                    "⚠️ Завантаження перервалося: бот перезапускався (оновлення). Натисніть, щоб почати знову.",
                    chat_id=int(chat), message_id=int(mid),
                    reply_markup=self._markup([[("🔁 Спробувати ще раз", self.cb("re", t))]]))
            except Exception:  # noqa: BLE001
                pass

    async def quick(self, chat, url):
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        src = M.describe_source(url)
        status = await self._status(chat, None, f"🔎 Шукаю: <b>{esc(src)}</b>…", stop)
        self._active(chat, status, url)
        started, step = time.time(), {"t": "перевіряю посилання"}

        async def ticker():  # кожні 5 с — скільки минуло й що зараз пробуємо: видно, що бот не завис
            while True:
                await asyncio.sleep(5)
                el = int(time.time() - started)
                await self._status(chat, status, f"🔎 Шукаю: <b>{esc(src)}</b>\n⏱ {el // 60}:{el % 60:02d} · {esc(step['t'])}…", stop)

        async def set_note(text):
            step["t"] = text
        tick = asyncio.get_running_loop().create_task(ticker())
        token = M.NOTE.set(set_note)
        try:
            url = await M.resolve_share(url)
            with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
                items = await M.inspect_link(url, Path(work))
        except M.URLDenied as exc:
            await self._status(chat, status, f"⚠️ {esc(str(exc))}")
            self._active(chat, done=True)
            return
        except JobError as exc:
            t = secrets.token_hex(4)
            self.offers[t] = (chat, url)
            rows = [[("🔁 Спробувати ще раз", self.cb("re", t))], [("🍪 Входи на сайти", self.cb("ck")), ("« Медіа", self.cb())]]
            if "APIFY_TOKEN" in str(exc) and "не підходить" not in str(exc):
                rows.insert(0, [("🔑 Вставити ключ Apify тут", self.cb("akey"))])
            await self._status(chat, status, f"⚠️ <b>Не вийшло завантажити</b> · {esc(src)}\n\n{exc}"[:4000], rows)
            self._active(chat, done=True)
            return
        finally:
            tick.cancel()
            M.NOTE.reset(token)
        job = Job(secrets.token_hex(4), url, items[:300])
        job.options.height = 1080  # до 1080p, щоб не впертись у межу Telegram
        job.__dict__["src"] = src
        self.jobs[chat] = job
        self._active(chat, done=True)
        if M.is_story(url) and len(job.items) > 1:  # кілька сторіс за день: вибір
            await self.show_stories(chat, job, status)
            return
        if len(job.items) > 1 and job.items[0].engine == "yt":  # плейлист / канал: список одним повідомленням
            await self.show_playlist(chat, job, status)
            return
        dq = self.pref(chat, "q", "ask")
        if dq not in ("ask", "a") and dq.isdigit():
            job.options.height = int(dq)  # ⭐ особиста якість за замовчуванням — і для інших сайтів
        if len(job.items) == 1 and job.items[0].engine == "yt":  # одне відео: меню форматів із розмірами
            if dq == "ask":
                await self.show_formats(chat, job, status)
            else:  # ⭐ без меню — одразу обрана якість
                kw = {"mode": "audio"} if dq == "a" else {"mode": "video", "height": int(dq)}
                for k, v in dict(kw, cuts=()).items():
                    setattr(job.options, k, v)
                status = await self._status(chat, status, f"⬇️ {esc(src)} · ⭐ {self.dq_label(chat)} (змінити: /media → Якість)",
                                            [[("⏹ Зупинити", self.cb("stop"))]])
                await self.download(chat, job, [1], status=status)
            return
        if len(job.items) > QUICK_MAX:
            t, rows = self.job_screen(chat)
            await self._status(chat, status, f"📋 {esc(src)} · {len(job.items)} елементів — виберіть, що завантажити.\n\n" + t, rows)
            return
        if all(it.engine == "text" for it in job.items):  # лише текст допису — нічого налаштовувати
            await self.download(chat, job, list(range(1, len(job.items) + 1)), status=status)
            return
        text, rows = self.options_screen(chat, job)  # ⚙️ усі налаштування — ДО завантаження, компактно
        self.live[chat] = (text, rows)
        await self._status(chat, status, text, rows)

    # ---------------------------------------------------------------- сторіс: вибір
    async def show_stories(self, chat, job, status):
        n, tok = len(job.items), job.token
        lines = [f"📚 <b>{esc(job.__dict__.get('src') or 'Сторіс')}</b> · сторіс за добу: {n}", ""]
        for k, it in enumerate(job.items[:50], 1):
            kind = "🎬" if str(it.url).split("?")[0].lower().endswith((".mp4", ".mov")) or "video" in str(it.info.get("_fallback") or "") else "🖼"
            mark = " ← з посилання" if it.info.get("_exact") else ""
            lines.append(f"{k}. {kind} {esc(it.info.get('when') or '')}{mark}".rstrip())
        lines += ["", f"⏱ Орієнтовно: ≈{max(1, n * 10 // 60)} хв на всі" if n * 10 >= 60 else f"⏱ Орієнтовно: ≈{n * 10} с на всі",
                  self.limits_line()]
        rows = []
        exact = next((k for k, it in enumerate(job.items, 1) if it.info.get("_exact")), None)
        if exact:
            rows.append([(f"📌 Саме цю (№{exact})", self.cb("sp", tok, exact))])
        rows.append([("🆕 Лише останню", self.cb("sp", tok, "last")), (f"⬇️ Усі ({n})", self.cb("sp", tok, "all"))])
        nums = [(f"{k}", self.cb("sp", tok, k)) for k in range(1, min(n, 10) + 1)]
        rows += [nums[i:i + 5] for i in range(0, len(nums), 5)]
        rows.append([("🔢 Вибрати кілька номерів", self.cb("spn", tok))])
        text = "\n".join(lines)[:4000]
        self.live[chat] = (text, rows)
        await self._status(chat, status, text, rows)

    # ---------------------------------------------------------------- меню форматів
    def tg_limit(self):
        return BIG_LIMIT if self.big.ready() else TELEGRAM_OUT

    @staticmethod
    def sizes(info):
        """[(висота, байтів або None)] для кожної якості + розмір найкращого звуку."""
        fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
        size = lambda f: f.get("filesize") or f.get("filesize_approx")
        audio = [f for f in fmts if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
        best_a = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
        a_size = size(best_a) if best_a else None
        by_h = {}
        for f in fmts:
            h = f.get("height")
            if not h or f.get("vcodec") in (None, "none") or h < 144:
                continue
            if h not in by_h or (f.get("tbr") or 0) > (by_h[h].get("tbr") or 0):
                by_h[h] = f
        out = []
        for h in sorted(by_h):
            f = by_h[h]
            s = size(f)
            if s and f.get("acodec") in (None, "none") and a_size:
                s += a_size
            out.append((h, s))
        if not out and (info.get("filesize") or info.get("filesize_approx")):
            out.append((info.get("height") or 0, info.get("filesize") or info.get("filesize_approx")))
        return out, a_size

    @staticmethod
    def music(info):
        """(виконавець, назва, накладена?) — соцмережі пишуть, яка пісня звучить (TikTok, YouTube, частково Instagram)."""
        track = (info.get("track") or "").strip()
        artist = (info.get("artist") or ", ".join(info.get("artists") or []) or "").strip()
        if not track:
            return None
        original = re.search(r"original sound|оригінальний звук|оригинальный звук|son original|sonido original|orijinal ses|suara asli", track, re.I)
        return artist, track, not original

    def song_of(self, job):
        """(виконавець, назва, накладена?) з метаданих будь-якого елемента допису; кешується в job."""
        if job.__dict__.get("song") is not None:
            return job.__dict__["song"] or None
        m = None
        for it in job.items[:30]:
            m = M.music_of(it.info)
            if m:
                break
        job.__dict__["song_info"] = m or {}
        job.__dict__["song"] = (m["artist"], m["title"], not m["original"]) if m else False
        return job.__dict__["song"] or None

    @staticmethod
    def song_name(song):
        artist, track, _ = song
        return f"{artist} — {track}" if artist and artist.lower() not in track.lower() else track

    def parts(self, job):
        """Номери візуальних елементів (фото/відео) і окремих звукових доріжок допису."""
        vis = [k for k, it in enumerate(job.items, 1) if it.engine != "text" and not M.is_audio_item(it)]
        aud = [k for k, it in enumerate(job.items, 1) if M.is_audio_item(it)]
        imgs = [k for k in vis if M.is_image_item(job.items[k - 1])]
        return vis, aud, imgs

    def options_screen(self, chat, job):
        """⚙️ Що буде завантажено й як — ДО завантаження; лише потрібні перемикачі."""
        vis, aud, imgs = self.parts(job)
        vids = len(vis) - len(imgs)
        song, tok = self.song_of(job), job.token
        x = job.__dict__.setdefault("x", set())
        first = job.items[(vis or [1])[0] - 1]
        what = " · ".join(t for t in [f"🖼 {len(imgs)} фото" if imgs else "", f"🎬 {vids} відео" if vids else "",
                                      "🎵 звукова доріжка" if aud or (job.__dict__.get("song_info") or {}).get("url") else ""] if t)
        lines = [f"📦 <b>{esc(first.title[:150])}</b>", f"📍 {esc(job.__dict__.get('src') or M.site_name(job.url))}" + (f" · {what}" if what else ""), ""]
        if song and song[2]:
            lines.append(f"🎵 Накладена музика: <b>{esc(self.song_name(song)[:120])}</b>")
        elif song:
            lines.append(f"🎙 Звук: оригінальний звук автора ({esc(song[1][:80])})")
        elif vids:
            lines.append("🎵 Яку музику накладено, сайт не вказав — «🔎 Розпізнати» дізнається за звуком.")
        elif imgs:
            lines.append("🎵 Музики до цих фото сайт не віддав.")
        on = lambda k: "☑️" if k in x else "▫️"  # noqa: E731
        rows = [[(f"⬇️ Завантажити{' усе' if len(vis) > 1 else ''}" + (" без звуку" if "mute" in x else ""), self.cb("og", tok))]]
        tog = []
        if vids or aud or (song and song[2]):
            tog.append((f"{on('mus')} 🎵 Музика окремо", self.cb("ox", tok, "mus")))
        if song and song[2]:
            tog.append((f"{on('full')} 🎶 Повна пісня", self.cb("ox", tok, "full")))
        if tog:
            rows.append(tog)
        if vids:
            rows.append([(f"{on('mute')} 🔇 Відео без звуку", self.cb("ox", tok, "mute")), ("🔊 Лише звук", self.cb("oa", tok))])
            rows.append([("🗣 Чистий звук автора ▸", self.cb("os", tok))] + ([] if song else [("🔎 Розпізнати", self.cb("fr", tok))]))
        elif aud:
            rows.append([("🔊 Лише звук", self.cb("oa", tok))])
        fmts = self.file_formats(job, vids, imgs)
        if len(fmts) > 1:
            cur = job.options.fmt if job.options.fmt in fmts else ""
            rows.append([(f"📦 Формат файлу: {(cur or 'як є').upper()} ▸", self.cb("ofm", tok))])
        if len(vis) > 1:
            rows.append([("🔢 Вибрати окремі", self.cb("menu", tok))])
        lines += ["", "<i>☑️ — додати до завантаження. «🎵 Музика окремо» — доріжка саме з цього допису; "
                  "«🎶 Повна пісня» — повна версія з YouTube/SoundCloud.</i>", self.limits_line()]
        return "\n".join(lines)[:4000], rows

    def file_formats(self, job, vids=0, imgs=()):
        """У які формати має сенс перетворити саме ці файли: фото, відео чи документ."""
        if vids:
            return ["", *M.VIDEO_FMTS[1:]]
        if imgs:
            return ["", *M.IMAGE_FMTS]
        ext = Path(M.urlsplit(str(job.items[0].url if job.items else "")).path).suffix.lower() if job.items else ""
        if ext in M.DOC_TO:
            return ["", *M.DOC_TO[ext]]
        return [""]

    def sep_screen(self, job):
        tok = job.token
        return ("🗣 <b>Чистий звук автора</b>\n\n"
                "Соцмережа вже змішала накладену музику з оригінальним звуком в один файл, окремої «чистої» доріжки сайт не віддає. "
                "Бот відокремить голос і живий звук від музики штучним інтелектом (Demucs, безкоштовно, на сервері).\n\n"
                "• Для мови й голосу — добре; шум вулиці чи інструменти в кадрі можуть частково піти разом із музикою.\n"
                "• Час: перший раз після оновлення сервера ≈3–5 хв (встановлення), далі ≈1–2 хв на хвилину відео.",
                [[("🎬 Відео з чистим звуком", self.cb("osv", tok))],
                 [("🗣 Лише чистий звук (MP3)", self.cb("oso", tok)), ("🎼 Лише музика", self.cb("osm", tok))],
                 [("« Налаштування завантаження", self.cb("opt", tok))]])

    async def opt_go(self, chat, job):
        """⬇️ Завантаження з обраними перемикачами: файли допису → (☑️) музика окремо → (☑️) повна пісня."""
        x = job.__dict__.get("x", set())
        vis, aud, _ = self.parts(job)
        nums = vis or aud
        job.options.mode, job.options.cuts = ("mute" if "mute" in x else "video"), ()
        status = await self._status(chat, None, f"⬇️ Готую: {esc(job.__dict__.get('src') or '')}…", [[("⏹ Зупинити", self.cb("stop"))]])
        await self.download(chat, job, nums, status=status)
        if "mus" in x:
            await self.post_audio(chat, job)
        if "full" in x and self.song_of(job):
            await self.full_song(chat, job, keep=True)

    async def post_audio(self, chat, job):
        """🔊 Звук саме з цього допису: окрема доріжка (TikTok/Instagram) → інакше звук із відео (MP3)."""
        vis, aud, imgs = self.parts(job)
        song, si = self.song_of(job), job.__dict__.get("song_info") or {}
        name = self.song_name(song) if song else "Звук допису"
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        if aud:
            sub = Job(secrets.token_hex(4), job.url, [M.Item(job.items[k - 1].engine, job.items[k - 1].url,
                                                             {**job.items[k - 1].info, "title": name}, job.items[k - 1].index) for k in aud])
        elif si.get("url"):
            ext = (Path(si["url"].split("?")[0]).suffix or ".m4a").lstrip(".")
            sub = Job(secrets.token_hex(4), job.url, [M.Item("direct", si["url"], {"title": name, "extension": ext})])
        elif len(vis) > len(imgs):
            sub = Job(secrets.token_hex(4), job.url, [job.items[k - 1] for k in vis if k not in imgs][:1])
            sub.options.mode = "audio"
        else:
            await self._status(chat, None, "🎵 Сайт не віддав окремо музику з цього допису."
                               + (f"\nЦе: <b>{esc(name)}</b> — можна взяти повну версію." if song else ""),
                               [[("🎶 Повна пісня", self.cb("ff", job.token))]] if song else [])
            return
        sub.options.post = False
        sub.__dict__.update(src=job.__dict__.get("src") or "", song=job.__dict__.get("song"), song_info=si)
        status = await self._status(chat, None, f"🎵 Готую звук допису: {esc(name[:80])}…", stop)
        await self.download(chat, sub, list(range(1, len(sub.items) + 1)), status=status)

    async def sep_go(self, chat, job, what):
        """🗣 Відокремлення оригінального звуку від музики: what = v (відео з чистим звуком) | o (лише голос) | m (лише музика)."""
        vis, aud, imgs = self.parts(job)
        vids = [k for k in vis if k not in imgs] or aud
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        if not vids:
            await self._status(chat, None, "У цьому дописі немає відео чи звуку.")
            return
        item = job.items[vids[0] - 1]
        status = await self._status(chat, None, "⬇️ Завантажую відео для розділення звуку…", stop)

        async def say(t):
            await self._status(chat, status, t, stop)
        try:
            with tempfile.TemporaryDirectory(prefix="orion-sep-") as work:
                work = Path(work)
                opts = replace(job.options, mode="video", cuts=(), height=min(job.options.height or 720, 720))
                path, _t, info = await M.fetch(item, job.url, opts, work)
                voice, music = await M.separate(path, work, progress=say)
                stem = Path(safe_name(item.title[:60], "media")).stem
                if what == "v":
                    out = work / f"{stem} · чистий звук.mp4"
                    await say("🎬 Складаю відео з чистим звуком…")
                    await run(*M.FF, "-i", path, "-i", voice, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                              "-b:a", "160k", "-shortest", "-movflags", "+faststart", out, timeout=900)
                    cap = "🗣 Відео з оригінальним звуком (музику прибрано ШІ)"
                else:
                    src_ = voice if what == "o" else music
                    out = work / (f"{stem} · " + ("чистий звук.mp3" if what == "o" else "музика.mp3"))
                    await run(*M.FF, "-i", src_, "-b:a", "192k", out, timeout=600)
                    cap = "🗣 Лише оригінальний звук (без музики)" if what == "o" else "🎼 Лише музика (без голосу)"
                await say(f"⬆️ Надсилаю ({mb(out.stat().st_size)})…")
                await self.send_media(chat, out, esc(cap))
            await self._status(chat, status, "✅ Готово", [[("⚙️ Ще варіант", self.cb("os", job.token))]])
        except JobError as exc:
            await self._status(chat, status, f"⚠️ Не вдалося розділити звук: {esc(str(exc)[:400])}",
                               [[("🔁 Ще раз", self.cb("os", job.token))]])

    def limits_line(self):
        lim = self.tg_limit()
        return f"ℹ️ Ліміт Telegram: файл до {'2 ГБ' if lim >= 1024 ** 3 else mb(lim)}" + ("" if self.big.ready() else " (більше — з TELEGRAM_API_ID)")

    async def fmt_extra(self, chat, job, a, y):
        """🔊 Звук: лише бітрейти до оригінального, формати mp3/m4a/opus/wav/flac/оригінал; 📦 формат відео mp4/mov/mkv/webm."""
        tok, info = job.token, job.items[0].info
        back = [("« Усі формати", self.cb("opt", tok))]
        if a == "fau":
            rates, best = M.audio_choices(info)
            lines = ["🔊 <b>Звук</b>", f"В оригіналі: до {best} кбіт/с" if best else "Бітрейт оригіналу сайт не повідомив.",
                     "Більшого, ніж в оригіналі, не пропоную — якість від цього не зросте.", "",
                     "<b>MP3</b> — грає всюди · <b>M4A</b> — менший файл · <b>OPUS</b> — найменший · <b>WAV/FLAC</b> — без стиснення · "
                     "<b>Оригінал</b> — як на сайті, без перекодування."]
            rows, line = [], []
            for r in rates or [192]:
                line.append((f"MP3 {r}", self.cb("fab", tok, f"mp3-{r}")))
                if len(line) == 4:
                    rows.append(line)
                    line = []
            if line:
                rows.append(line)
            top = rates[-1] if rates else 160
            rows.append([(f"M4A {min(top, 256)}", self.cb("fab", tok, f"m4a-{min(top, 256)}")), (f"OPUS {min(top, 160)}", self.cb("fab", tok, f"opus-{min(top, 160)}"))])
            rows.append([("WAV", self.cb("fab", tok, "wav-0")), ("FLAC", self.cb("fab", tok, "flac-0")), ("Оригінал", self.cb("fab", tok, "orig-0"))])
            rows.append(back)
            return None, ("\n".join(lines), rows)
        if a == "fab":
            fmt, _, abr = (y or "mp3-192").partition("-")
            if fmt not in (*M.AUDIO_FMTS, "orig"):
                return "Невідомий формат", None
            if self.busy(chat):
                return "⏳ Ще завантажую попереднє", None
            self._bg(chat, self.fmt_go(chat, job, [1], mode="audio", cuts=(), fmt=fmt, abr=int(abr or 0)))
            return "⬇️ Починаю", None
        if a == "fvf":
            cur = job.options.fmt if job.options.fmt in M.VIDEO_FMTS else "mp4"
            lines = ["📦 <b>Формат відеофайлу</b>", "<b>MP4</b> — грає всюди, з плеєром у Telegram · <b>MOV</b> — для iPhone і монтажу · "
                     "<b>MKV</b> — усі доріжки й субтитри · <b>WEBM</b> — для сайтів (перекодування, довше).",
                     "Після вибору — оберіть якість (лише ті, що є в цього відео)."]
            rows = [[(("✅ " if f == cur else "") + f.upper(), self.cb("fvs", tok, f)) for f in M.VIDEO_FMTS], back]
            return None, ("\n".join(lines), rows)
        if a == "fvs" and y in M.VIDEO_FMTS:
            job.options.fmt = "" if y == "mp4" else y
            hs = M.video_heights(info)
            rows = [[(f"🎬 {h}p", self.cb("fq", tok, h)) for h in hs[i:i + 4]] for i in range(0, len(hs), 4)] or [[("🎬 Відео", self.cb("fq", tok, 0))]]
            rows.append(back)
            return f"📦 {y.upper()}", (f"📦 Формат: <b>{y.upper()}</b>. Тепер якість — лише наявні в цього відео:", rows)
        return None, None

    @staticmethod
    def author_url(info):
        for k in ("channel_url", "uploader_url"):
            u = (info or {}).get(k) or ""
            if str(u).startswith("http"):
                return u.rstrip("/") + ("/videos" if "youtube.com/" in u and not u.rstrip("/").endswith(("/videos", "/shorts")) else "")
        return ""

    async def show_formats(self, chat, job, status):
        item = job.items[0]
        info = item.info
        sizes, a_size = self.sizes(info)
        lim, tok = self.tg_limit(), job.token
        who = info.get("uploader") or info.get("channel") or info.get("creator") or ""
        dur = int(info.get("duration") or 0)
        lines = [f"🎬 <b>{esc(item.title[:200])}</b>", f"📍 {esc(job.__dict__.get('src') or M.site_name(job.url))}"
                 + (f" · 👤 {esc(who)}" if who else "") + (f" · ⏱ {dur // 60}:{dur % 60:02d}" if dur else "")]
        rows, line = [], []
        if sizes:
            lines.append("")
            for h, s in sizes:
                fits = not s or s <= lim
                label = f"{h}p" if h else "Відео"
                lines.append(f"{'✅' if fits else '⛔'} <code>{label:>6}</code> " + (mb(s) if s else "розмір невідомий"))
                line.append((f"🎬 {label}", self.cb("fq", tok, h or 0)))
                if len(line) == 3:
                    rows.append(line)
                    line = []
            if line:
                rows.append(line)
        else:
            rows.append([("🎬 Відео", self.cb("fq", tok, 0))])
        rows.append([("🔊 Звук: бітрейт і формат ▸" + (f" · {mb(a_size)}" if a_size else ""), self.cb("fau", tok)), ("🖼 Превʼю", self.cb("fp", tok))])
        rows.append([(f"📦 Формат відео: {(job.options.fmt or 'mp4').upper()} ▸", self.cb("fvf", tok))])
        song = self.song_of(job) or self.music(info)
        if song:
            artist, track, added = song
            name = f"{artist} — {track}" if artist else track
            lines.append(f"\n🎵 {'Музика у відео' if added else 'Звук'}: <b>{esc(name[:120])}</b>")
            if added:
                rows.append([("🎵 Фрагмент з відео", self.cb("fa", tok)), ("🎶 Повна пісня", self.cb("ff", tok))])
        else:
            rows.append([("🔎 Розпізнати музику", self.cb("fr", tok))])
        rows.append([("🗣 Чистий звук автора ▸", self.cb("os", tok)), ("✂️ Обрізка, GIF, субтитри", self.cb("menu", tok))])
        extra = [("⭐ Завжди так", self.cb("dq"))]
        if self.author_url(info):
            extra.insert(0, ("👤 Інші відео автора", self.cb("oth", tok)))
        rows.append(extra)
        lines += ["", self.limits_line()]
        text = "\n".join(lines)[:1000]
        self.live[chat] = (text, rows)
        thumb = info.get("thumbnail")
        if thumb:
            try:
                await self.host.bot().send_photo(chat, thumb, caption=text, parse_mode="HTML", reply_markup=self._markup(rows))
                try:
                    await self.host.bot().delete_message(chat, status.message_id)
                except Exception:  # noqa: BLE001
                    pass
                return
            except Exception:  # noqa: BLE001 — превʼю не віддалось: меню текстом
                pass
        await self._status(chat, status, text, rows)

    async def show_playlist(self, chat, job, status):
        """Як у ботів-завантажувачів: усі відео списком (назва-посилання, тривалість) одним повідомленням."""
        def link(it):
            i = it.info
            u = i.get("webpage_url") or i.get("url") or ""
            if not str(u).startswith("http") and i.get("id"):
                u = f"https://youtu.be/{i['id']}"
            return u
        head = f"📋 <b>Плейлист · {len(job.items)}</b> · {esc(job.__dict__.get('src') or '')}"
        chunks, cur = [], head + "\n"
        for n, it in enumerate(job.items[:100], 1):
            d = int(it.info.get("duration") or 0)
            row = f"\n{n}. <a href=\"{esc(link(it))}\">{esc(it.title[:80])}</a>" + (f" · {d // 60}:{d % 60:02d}" if d else "")
            if len(cur) + len(row) > 3800:
                chunks.append(cur)
                cur = ""
            cur += row
        if len(job.items) > 100:
            cur += f"\n\n…і ще {len(job.items) - 100}"
        chunks.append(cur + "\n\n" + self.limits_line())
        tok = job.token
        rows = [[("⬇️ Усі відео (720p)", self.cb("fall", tok, "v")), ("🔊 Усі MP3", self.cb("fall", tok, "a"))],
                [("☑️ Вибрати окремі", self.cb("menu", tok))]]
        for i, c in enumerate(chunks):
            last = i == len(chunks) - 1
            if i == 0:
                status = await self._status(chat, status, c, rows if last else [])
            else:
                await self.host.bot().send_message(chat, c, parse_mode="HTML", disable_web_page_preview=True,
                                                   reply_markup=self._markup(rows) if last else None)
        self.live[chat] = (chunks[0][:3500], rows)

    def job_by(self, chat, tok):
        job = self.jobs.get(chat)
        return job if job and job.token == tok else None

    async def fmt_go(self, chat, job, nums, **opts):
        """Вибраний формат / сторіс: окреме повідомлення-статус і завантаження."""
        for k, v in opts.items():
            setattr(job.options, k, v)
        status = await self._status(chat, None, f"⬇️ Готую: {esc(job.__dict__.get('src') or '')}…", [[("⏹ Зупинити", self.cb("stop"))]])
        await self.download(chat, job, nums, status=status)

    async def full_song(self, chat, job, keep=False):
        artist, track, _ = job.__dict__.get("song") or self.music(job.items[0].info) or ("", "", False)
        query = f"{artist} {track}".strip()
        status = await self._status(chat, None, f"🔎 Шукаю повну пісню: {esc(query[:80])}…", [[("⏹ Зупинити", self.cb("stop"))]])
        engines = (["ytsearch1"] if os.environ.get("MEDIA_PROXY") else []) + ["scsearch1", "ytsearch1"]
        last = ""
        for eng in dict.fromkeys(engines):
            try:
                with tempfile.TemporaryDirectory(prefix="orion-song-") as work:
                    items = await M.video_probe(f"{eng}:{query}", Path(work))
                if items:
                    song = Job(secrets.token_hex(4), f"{eng}:{query}", items[:1])
                    song.options.mode = "audio"
                    song.options.post = False
                    song.__dict__["src"] = "SoundCloud" if eng.startswith("sc") else "YouTube"
                    if not keep:
                        self.jobs[chat] = song
                    await self.download(chat, song, [1], status=status)
                    return
            except JobError as exc:
                last = str(exc)
        await self._status(chat, status, "⚠️ Повну пісню не знайшов на SoundCloud/YouTube." + (f"\n\n{esc(last[:300])}" if last else ""),
                           [[("« Медіа", self.cb())]])

    async def recognize(self, chat, job):
        """🔎 Розпізнати музику (Shazam): 25 секунд звуку з відео → назва й виконавець."""
        status = await self._status(chat, None, "🔎 Слухаю музику у відео…", [[("⏹ Зупинити", self.cb("stop"))]])
        try:
            try:
                from shazamio import Shazam
            except ImportError:
                await self._status(chat, status, "📦 Перший запуск: встановлюю розпізнавач (≈1 хв)…")
                await run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "shazamio", timeout=600)
                import importlib
                importlib.invalidate_caches()
                from shazamio import Shazam
            opts = replace(job.options, mode="audio", cuts=())
            with tempfile.TemporaryDirectory(prefix="orion-rec-") as work:
                work = Path(work)
                path, _t, _i = await M.fetch(job.items[0], job.url, opts, work)
                clip = work / "clip.mp3"
                await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", "25", "-i", path, "-ac", "1", clip, timeout=120)
                sh = Shazam()
                out = await (sh.recognize(str(clip)) if hasattr(sh, "recognize") else sh.recognize_song(str(clip)))
            tr = (out or {}).get("track") or {}
            if not tr.get("title"):
                await self._status(chat, status, "🤷 Не впізнав музику — можливо, це оригінальний звук або голос.", [[("« Медіа", self.cb())]])
                return
            job.__dict__["song"] = (tr.get("subtitle") or "", tr["title"], True)
            await self._status(chat, status, f"🎵 Це: <b>{esc(tr.get('subtitle') or '')} — {esc(tr['title'])}</b>",
                               [[("🎵 Фрагмент з відео", self.cb("fa", job.token)), ("🎶 Повна пісня", self.cb("ff", job.token))]])
        except (JobError, ImportError) as exc:
            await self._status(chat, status, f"⚠️ Розпізнати не вдалося: {esc(str(exc)[:300])}", [[("« Медіа", self.cb())]])

    def start_inspect(self, chat, url):
        if self.busy(chat):
            return "⏳ Попереднє завдання ще триває", None
        self._bg(chat, self.inspect(chat, url))
        return "🔎 Шукаю медіа…", ("🔎 Шукаю відкриті медіа й текст допису — це може тривати до хвилини.", [[("⏹ Зупинити", self.cb("stop"))]])

    async def inspect(self, chat, url):
        url = await M.resolve_share(url)
        with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
            items = await M.inspect_link(url, Path(work))
        self.jobs[chat] = Job(secrets.token_hex(4), url, items[:300])
        t, rows = self.job_screen(chat)
        await self.host.notify(chat, t, rows)

    async def download(self, chat, job, nums, status=None):
        bot = self.host.bot()
        opts = replace(job.options)
        sent, failed, shown, bundle = [], [], set(), []
        self.got[chat] = []
        quick = status is not None
        title = job.items[nums[0] - 1].title if len(nums) == 1 else f"{len(nums)} елементів"
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        src = job.__dict__.get("src") or M.describe_source(job.url)
        status = await self._status(chat, status, f"⬇️ Завантажую: {esc(title[:100])}\n📍 {esc(src)}…", stop)
        self._active(chat, status, job.url)
        last = [0.0]
        t0 = time.time()

        async def stage(text, force=False):
            """Живий стан: застосунок бачить кожне оновлення, повідомлення в чаті — не частіше ніж раз на 3 с."""
            nonlocal status
            self.live[chat] = (text, [])
            now = asyncio.get_running_loop().time()
            if force or now - last[0] >= 3:
                last[0] = now
                status = await self._status(chat, status, text, stop)
        with tempfile.TemporaryDirectory(prefix="orion-media-") as root:
            root = Path(root)
            for k, n in enumerate(nums, 1):
                item = job.items[n - 1]
                work = root / f"{n:03d}"
                work.mkdir()
                left = ""
                if len(nums) > 1 and k > 1:  # скільки ще на всі: за середнім часом уже завантажених
                    per = (time.time() - t0) / (k - 1)
                    rest = int(per * (len(nums) - k + 1))
                    left = f" · усі ще ≈{rest // 60}:{rest % 60:02d}"
                head = (f"⬇️ Завантажую" + (f" {k}/{len(nums)}" if len(nums) > 1 else "") + f": {esc(item.title[:90])}"
                        + f"\n📍 {esc(src)}{left}")

                async def on_dl(done, total, speed, eta, head=head):
                    await stage(f"{head}\n{bar(done, total, speed, eta)}")
                try:
                    await stage(head + "…", force=True)
                    path, tracks, info = await M.fetch(item, job.url, opts, work, progress=on_dl)
                    if path and (opts.mode != "video" or opts.cuts):
                        await stage(f"⚙️ Обробляю: {esc(item.title[:90])}…", force=True)
                    who, link = M.author(info)
                    desc = M.post_text(info)
                    if not job.__dict__.get("song"):  # yt-dlp часто знає пісню лише після завантаження
                        m = M.music_of(info or {})
                        if m:
                            job.__dict__.update(song=(m["artist"], m["title"], not m["original"]), song_info=m)
                    song = job.__dict__.get("song")
                    song_html = f"\n🎵 {esc(self.song_name(song))}" if song and song[2] else ""
                    outs = await M.render(path, opts, work, item.engine == "direct") if path else []
                    if opts.fmt:  # 📦 обраний формат файлу (mov, mkv, webm, png…)
                        outs = [await M.convert_final(o, opts, Path(work)) for o in outs]
                    # Опис і автор — у підписі самого файлу (Telegram дозволяє 1024 символи); довший текст — ще й окремо
                    # Підпис HTML: довгі адреси — короткими посиланнями «Автор» / «Допис»; ліміт Telegram — 1024 видимі символи
                    who_html = (f'<a href="{esc(link)}">{esc(who)}</a>' if link else esc(who)) if who else ""
                    site = M.site_name(job.url) if str(job.url).startswith("http") else (job.__dict__.get("src") or "")
                    geotag = M.geotag(info)
                    rel = await self.detect(chat, desc or "", geotag, who, link, self.instr.get(chat))
                    if rel is None:  # людина попросила без тегів — без сортування й без запису
                        rel_t = rel_g = rel_l = rel_f = rel_btns = []
                        rel_line, rel_len = "", 0
                    else:
                        rel_t, rel_g, rel_l = rel
                        rel_line, rel_len, rel_btns, rel_f = self.related_parts(chat, who, rel_t, rel_g, rel_l)
                    if geotag:
                        rel_line, rel_len = f"\n📍 {esc(geotag[:60])}" + rel_line, rel_len + len(geotag[:60]) + 3
                    tail = (f"\n\n👤 {who_html}" if who else "\n") + (f" · {esc(site)}" if site else "") \
                        + (f' · <a href="{esc(job.url)}">🔗 Допис</a>' if str(job.url).startswith("http") else "") + song_html + rel_line
                    tail_len = len(f"\n\n👤 {who} · {site} · 🔗 Допис") + len(re.sub(r"<[^>]+>|&\w+;", "", song_html)) + rel_len
                    recorded = rel is None
                    first = True
                    for out in outs + tracks:
                        cap = ""
                        if first:
                            if opts.post and desc and desc not in shown and opts.target != "zip":
                                body = desc.strip()
                                if len(body) + tail_len <= 1024:
                                    cap = esc(body) + tail
                                    shown.add(desc)  # уже в підписі — окремим повідомленням не дублюємо
                                else:
                                    cap = esc(body[:1020 - tail_len].rstrip()) + "…" + tail
                            else:
                                cap = tail.strip()
                            first = False
                        if opts.target == "zip":
                            bundle.append(out)
                            continue
                        size_ = out.stat().st_size
                        up = f"⬆️ Надсилаю {esc(out.name[:60])} ({mb(size_)})"
                        await stage(up + "…", force=True)

                        async def on_up(done, total, up=up):
                            await stage(f"{up}\n{bar(done, total)}")
                        msg_ = await self.send_media(chat, out, cap, on_progress=on_up,
                                                     markup=None if recorded else self._url_markup([[b] for b in rel_btns]))
                        if not recorded and getattr(msg_, "message_id", None):  # 🔗 запам'ятовуємо для «попереднє …»
                            recorded = True
                            self.hist.add(chat, msg=msg_.message_id, author=R.norm_author(who), who=who, alink=link, site=site,
                                          url=str(job.url), title=(desc or item.title or "")[:80], topics=rel_t, geos=rel_g, langs=rel_l, first=rel_f, geotag=geotag)
                        sent.append(out.name)
                        kept = self.keep(out)
                        if kept:
                            self.got[chat].append(kept)
                    if opts.post and desc and desc not in shown:
                        body = f"📝 Повний текст допису\n👤 {who or 'автор не вказаний'}" + (f" · {link}" if link else "") + f"\n🔗 {job.url}\n\n{desc}"
                        if opts.target == "zip":
                            note = work / "текст-допису.txt"
                            note.write_text(body, encoding="utf-8")
                            bundle.append(note)
                        elif len(body) < 3800:
                            await bot.send_message(chat, body, disable_web_page_preview=True)
                        else:
                            note = work / "текст-допису.txt"
                            note.write_text(body, encoding="utf-8")
                            await self.send_doc(chat, note, "📝 Текст допису")
                        shown.add(desc)
                    if opts.subs != "off" and item.engine == "yt" and not tracks:
                        failed.append(f"{n} · субтитрів мовою «{opts.subs}» немає")
                    if path is None and not desc:
                        failed.append(f"{n} · лише текст, а його вимкнено")
                except JobError as exc:
                    failed.append(f"{n} · {M.explain(exc)}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("media item failed")
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    failed.append(f"{n} · " + (f"сайт не віддав файл (HTTP {code}) — спробуйте ще раз або інший формат"
                                               if code else esc(type(exc).__name__)))
            if bundle:
                pack = root / (safe_name(job.items[nums[0] - 1].title, "media")[:60] + ".zip")
                with zipfile.ZipFile(pack, "w", zipfile.ZIP_STORED) as z:
                    for f in bundle:
                        z.write(f, f"{f.parent.name}-{f.name}" if len(nums) > 1 else f.name)
                try:
                    await self.send_doc(chat, pack, f"📦 {len(bundle)} файл(ів) · {mb(pack.stat().st_size)}")
                    sent.append(pack.name)
                    kept = self.keep(pack)
                    if kept:
                        self.got[chat].append(kept)
                except JobError as exc:
                    failed.append(f"ZIP · {esc(str(exc))}")
        if sent and not failed:
            msg = "✅ Готово" + (f" · файлів: {len(sent)}" if len(sent) > 1 else "")
        elif sent:
            msg = f"✅ Надіслано файлів: {len(sent)}\n\n⚠️ Не вдалося:\n" + "\n".join(failed[:30])
        elif failed:
            msg = "⚠️ <b>Не вийшло завантажити</b>\n\n" + "\n\n".join(failed[:10])
        elif shown:
            msg = "📝 Тут лише текст допису — надіслав його вище."
        else:
            msg = "⚠️ Файлів не знайшлося. Спробуйте інший формат або якість."
        rows = [[("⚙️ Доналаштувати й завантажити ще" if quick else "📋 До набору", self.cb("opt" if quick else "menu", job.token))], [("« Медіа", self.cb())]]
        if quick and (sent or shown):
            try:  # статус більше не потрібен — лишаємо тільки файли й одну кнопку
                await bot.delete_message(chat, status.message_id)
                status = None
            except Exception:  # noqa: BLE001
                pass
        self._active(chat, done=True)
        await self._status(chat, status, msg[:4000], rows)

    async def inspect_file(self, chat, p):
        if p.size and p.size > TELEGRAM_IN and not self.big.ready():
            raise JobError("Telegram дає боту отримати файл лише до 20 МБ. Щоб до 2 ГБ — додайте TELEGRAM_API_ID і TELEGRAM_API_HASH у Railway.")
        with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
            src = await self._receive(p, Path(work))
            body = await report(src)
            await self._send_report(chat, body, Path(work), "🔎 До очищення")
        if suffix(src) in REPORT_ONLY:
            return await self.host.notify(chat, "Для цього формату доступний лише звіт.")
        self.pending[chat] = p
        await self.host.notify(chat, "🧽 Очистити метадані й надіслати копію?",
                               [[("🧽 Очистити копію", self.cb("cl", p.token, "yes")), ("Скасувати", self.cb("cl", p.token, "no"))]])

    async def clean_file(self, chat, p):
        bot = self.host.bot()
        with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
            src = await self._receive(p, Path(work))
            out = await clean(src)
            await self.send_doc(chat, out, "🧽 Очищена копія")
            await self._send_report(chat, await report(out), Path(work), "🔎 Після очищення")

    async def _receive(self, p, work):
        path = work / p.name
        if p.size and p.size > TELEGRAM_IN:  # 🚀 більший за 20 МБ — через пряме з'єднання
            prog = Progress(self.host.bot(), p.chat, f"⬇️ Отримую файл ({mb(p.size)})")
            await prog.start()
            try:
                return await self.big.download(p.chat, p.msg_id, path, progress=prog)
            finally:
                await prog.done()
        f = await self.host.bot().get_file(p.file_id, read_timeout=40)
        await f.download_to_drive(path, read_timeout=60)
        return path

    def _url_markup(self, rows):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, url=u) for t, u in row] for row in rows]) if rows else None

    async def send_media(self, chat, path, caption_html="", on_progress=None, markup=None):
        """Відео — як відео (з плеєром), фото — як фото, GIF — анімацією, аудіо — треком; решта — файлом."""
        ext = path.suffix.lower()
        size = path.stat().st_size
        bot = self.host.bot()
        kw = dict(caption=caption_html, parse_mode="HTML", read_timeout=300, write_timeout=300)
        if markup:
            kw["reply_markup"] = markup
        meta = {}
        thumb = await M.make_thumb(path, path.parent)  # 🖼 передперегляд — щоб видно, що це за файл, і в чаті, і в завантаженому
        tkw = {"thumbnail": thumb.read_bytes()} if thumb else {}
        if ext in (".mp4", ".mov", ".m4v"):
            try:
                data = await M.probe_media(path)
                v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
                meta = {"width": int(v.get("width") or 0) or None, "height": int(v.get("height") or 0) or None,
                        "duration": int(float((data.get("format") or {}).get("duration") or 0)) or None}
            except Exception:  # noqa: BLE001
                meta = {}
        try:
            if size <= TELEGRAM_OUT - 512 * 1024:
                with path.open("rb") as fh:
                    if ext in (".mp4", ".mov", ".m4v"):
                        return await bot.send_video(chat, fh, filename=path.name, supports_streaming=True, **meta, **tkw, **kw)
                    if ext in (".jpg", ".jpeg", ".png", ".webp") and size <= 10_000_000:
                        return await bot.send_photo(chat, fh, **kw)
                    if ext == ".gif":
                        return await bot.send_animation(chat, fh, filename=path.name, **kw)
                    if ext in (".mp3", ".m4a", ".ogg", ".opus", ".aac"):
                        return await bot.send_audio(chat, fh, filename=path.name, **tkw, **kw)
            elif ext in (".mp4", ".mov", ".m4v") and self.big.ready() and size <= BIG_LIMIT:
                try:
                    return await self.big.send(chat, path, caption_html, progress=on_progress, video=meta or {}, html=True, thumb=thumb)
                except Exception as exc:  # noqa: BLE001
                    raise JobError(f"Не вдалося надіслати велике відео: {type(exc).__name__}: {str(exc)[:200]}") from None
        except JobError:
            raise
        except Exception:  # noqa: BLE001 — Telegram не прийняв як відео/фото: надсилаємо файлом
            log.exception("send as media failed, falling back to document")
        return await self.send_doc(chat, path, re.sub(r"<[^>]+>", "", caption_html).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'), on_progress)

    async def send_doc(self, chat, path, caption="", on_progress=None):
        """До 50 МБ — звичайно; більше (до 2 ГБ) — через пряме з'єднання з Telegram від імені бота."""
        size = path.stat().st_size
        if size <= TELEGRAM_OUT - 512 * 1024:
            thumb = await M.make_thumb(path, path.parent)
            with path.open("rb") as fh:
                return await self.host.bot().send_document(chat, fh, filename=path.name, caption=caption, read_timeout=180, write_timeout=180,
                                                           **({"thumbnail": thumb.read_bytes()} if thumb else {}))
        if not self.big.ready():
            raise JobError(f"{path.name}: {mb(size)}. Звичайний Telegram-бот надсилає лише до 50 МБ. Щоб до 2 ГБ — додайте "
                           "TELEGRAM_API_ID і TELEGRAM_API_HASH у Railway (my.telegram.org → API development tools), "
                           "або оберіть нижчу якість, MP3 чи таймкоди.")
        if size > BIG_LIMIT:
            raise JobError(f"{path.name}: {mb(size)} — більше за 2 ГБ, межу Telegram. Оберіть нижчу якість, MP3 чи таймкоди.")
        prog = None if on_progress else Progress(self.host.bot(), chat, f"⬆️ Надсилаю {path.name} ({mb(size)})")
        if prog:
            await prog.start()
        try:
            await self.big.send(chat, path, caption, progress=on_progress or prog, thumb=await M.make_thumb(path, path.parent))
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"Не вдалося надіслати великий файл: {type(exc).__name__}: {str(exc)[:200]}") from None
        finally:
            if prog:
                await prog.done()

    async def _send_report(self, chat, body, work, title):
        if len(body) <= 3300:
            return await self.host.notify(chat, f"<b>{title}</b>\n<pre>{esc(body)}</pre>")
        await self.host.notify(chat, f"<b>{title}</b>\n<pre>{esc(body[:3000])}</pre>\nПовний звіт — у файлі.")
        rep = work / f"метадані-{secrets.token_hex(2)}.txt"
        rep.write_text(body, encoding="utf-8")
        with rep.open("rb") as fh:
            await self.host.bot().send_document(chat, fh, filename=rep.name)

    SITES = (("YouTube", "Youtube"), ("TikTok", "TikTok"), ("Instagram", "Instagram"), ("Facebook", "Facebook"),
             ("X / Twitter", "Twitter"), ("Reddit", "Reddit"), ("Vimeo", "Vimeo"), ("Dailymotion", "Dailymotion"),
             ("SoundCloud", "Soundcloud"), ("Twitch (кліпи)", "TwitchClips"), ("Pinterest", "Pinterest"), ("Bluesky", "Bluesky"),
             ("Tumblr", "Tumblr"), ("Rumble", "Rumble"), ("VK", "VK"), ("Snapchat", "SnapchatSpotlight"), ("Likee", "Likee"),
             ("Imgur", "Imgur"), ("Telegram (відкриті канали)", "TelegramEmbed"), ("Bilibili", "BiliBili"), ("Coub", "Coub"),
             ("Streamable", "Streamable"), ("LinkedIn", "LinkedIn"))

    async def check_sites(self, chat):
        """🩺 Справжня перевірка з сервера: для кожного сайту — пошук медіа за тестовими посиланнями самого yt-dlp."""
        try:
            import yt_dlp.extractor as E
        except Exception:  # noqa: BLE001
            E = None
        status = await self._status(chat, None, "🩺 Перевіряю сайти з сервера… (1–3 хв)", [[("⏹ Зупинити", self.cb("stop"))]])
        sem = asyncio.Semaphore(4)
        results = {}

        async def one(title, ie):
            cls = getattr(E, ie + "IE", None) if E else None
            urls = [t.get("url") for t in (cls.get_testcases(False) if cls else [])
                    if str(t.get("url") or "").startswith(("http://", "https://"))][:2]
            if not urls:
                results[title] = ("➖", "немає тестового посилання")
                return
            last = ""
            for url in urls:
                async with sem:
                    try:
                        with tempfile.TemporaryDirectory(prefix="orion-chk-") as work:
                            items = await asyncio.wait_for(M.inspect_link(url, Path(work)), 120)
                        real = [i for i in items if i.engine != "text" and not i.info.get("preview")]
                        if real:
                            results[title] = ("✅", f"знайдено: {len(real)}")
                            return
                        last = "лише текст / обкладинка"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        plain = re.sub(r"<[^>]+>", "", str(exc)).replace("&#x27;", "'").replace("&amp;", "&")
                        tech = plain.split("Технічно:", 1)[1].strip() if "Технічно:" in plain else ""
                        last = (plain.split("\n")[0][:70] + (f" · {tech[:110]}" if tech else "")) or type(exc).__name__
            results[title] = ("❌", last)

        await asyncio.gather(*(one(t, ie) for t, ie in self.SITES))
        lines = ["🩺 <b>Перевірка сайтів із сервера</b>", ""]
        for title, _ in self.SITES:
            mark, note = results.get(title, ("❔", ""))
            lines.append(f"{mark} {esc(title)}" + (f" — <i>{esc(note)}</i>" if mark != "✅" else ""))
        lines += ["", "<blockquote expandable>ℹ️ Як читати\nПеревірка — за прикладами з тестів yt-dlp; деякі з них давно видалені, "
                  "тож ❌ «недоступне» ще не означає, що сайт не працює — краще перевірити своїм посиланням. "
                  "Threads і сторіс (Instagram, Facebook, TikTok) тут не перевіряються: сторіс зникають за добу. "
                  "Сторіс Instagram і Facebook потребують APIFY_TOKEN; YouTube блокує сервери — потрібен MEDIA_PROXY.</blockquote>"]
        await self._status(chat, status, "\n".join(lines)[:4000], [[("🔁 Ще раз", self.cb("chk")), ("« Медіа", self.cb())]])

    async def update_tools(self, chat):
        import sys
        out = await run(sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "yt-dlp[default]", "gallery-dl", timeout=300)
        await self.host.notify(chat, "✅ Завантажувачі оновлено (до наступного перезапуску сервера; при оновленні бота ставляться свіжі).\n"
                               f"<pre>{esc(out.strip().splitlines()[-1][:300] if out.strip() else 'готово')}</pre>")

    def status_line(self):
        return f"📥 Медіа: {'готово' if shutil.which('yt-dlp') and shutil.which('ffmpeg') else 'потрібні yt-dlp і ffmpeg'}"


def message_attachment(message):
    if message.document:
        return message.document, "файл"
    if message.animation:
        return message.animation, "анімація.mp4"
    if message.photo:
        return message.photo[-1], "фото.jpg"
    if message.video:
        return message.video, "відео.mp4"
    if message.audio:
        return message.audio, "аудіо.mp3"
    if message.voice:
        return message.voice, "голосове.ogg"
    if message.video_note:
        return message.video_note, "відеоповідомлення.mp4"
    if message.sticker:
        st = message.sticker
        return st, "стікер" + (".webm" if st.is_video else ".tgs" if st.is_animated else ".webp")
    return None, ""
