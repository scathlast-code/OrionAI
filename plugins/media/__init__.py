"""📥 Медіа — завантаження відкритих медіа за посиланням і очищення метаданих власних файлів (з MediaBot).

- Посилання з YouTube, Instagram, TikTok, Facebook, Threads, X, Telegram, Reddit, Vimeo, Pinterest та сотень інших
  сайтів (yt-dlp, gallery-dl, прямі файли, відкриті дописи Telegram, Open Graph) → список знайденого → вибір
  елементів, якості, формату (відео / MP3 / GIF / без звуку), озвучки, субтитрів, таймкодів, тексту допису, ZIP.
- 🔴 Ефір — запис перших 1/3/5 хвилин. 🍪 Входи — cookies.txt для сайтів, що вимагають входу.
- 🧽 Метадані — звіт і очищена копія фото, відео, аудіо, PDF, Office, HTML/MHTML, ZIP/TAR.
Усе працює для кожного учасника окремо; одночасно одне завдання на людину.
"""
from __future__ import annotations

import asyncio
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
    icon = "📥"
    title = "Медіа"
    version = "1.0"
    admin_only = False
    summary = ("Завантаження відео, фото, аудіо й тексту дописів за посиланням із YouTube, Instagram, TikTok, Facebook, "
               "Threads, X, Telegram, Reddit, Vimeo та сотень інших сайтів: якість, MP3, GIF, без звуку, озвучка, субтитри, "
               "таймкоди, запис ефіру, ZIP. А також звіт і очищення метаданих власних файлів.")
    VIEWS = {"ck", "tools", "help"}

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
        self.got: dict[int, list] = {}  # чат -> файли останнього завантаження (для перегляду в Mini App)  # чат -> останній стан швидкого завантаження (для Mini App)
        self.big = Big(self.dir, host.token)  # 🚀 файли до 2 ГБ через MTProto
        self.commands = [("media", self.cmd_media, "Медіа: завантажити за посиланням")]

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
        rows += [[("🍪 Входи на сайти (cookies)", self.cb("ck")), ("🛠 Інструменти", self.cb("tools"))]]
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
        rows = [[("🔄 Оновити yt-dlp і gallery-dl", self.cb("upd"))]] if self.host.is_admin(chat) else []
        rows.append([("« Медіа", self.cb())])
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
        elif a == "q":
            o.height = HEIGHTS[(HEIGHTS.index(o.height) + 1) % len(HEIGHTS)] if o.height in HEIGHTS else 0
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
        if key == "url":
            m = re.search(r"https?://\S+", text)
            if not m:
                self.ask(chat, "url")
                return "Не бачу посилання", ("⚠️ Потрібне посилання, що починається з https://", [[("✖️ Скасувати", "m:cancel")]])
            toast = self.start_quick(chat, m[0].rstrip(").,]»"))
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
        if not m or self.host.extra.get("awaiting", lambda c: False)(msg.chat_id):
            return False
        host = (M.urlsplit(m[1]).hostname or "").lower()
        if not M.domain_matches(host, MEDIA_HOSTS):
            return False
        toast = self.start_quick(msg.chat_id, m[1])
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
        # Чистий контекст: якщо завдання почали з Mini App, його повідомлення й файли мають іти в справжній чат,
        # а не в «перехоплювач» запиту застосунку, який уже завершився.
        task = asyncio.get_running_loop().create_task(wrap(), context=contextvars.Context())
        self.tasks[chat] = task

    # ---------------------------------------------------------------- 1 крок: посилання → файл
    def start_quick(self, chat, url):
        """Повертає текст лише якщо почати не можна; інакше все показує саме повідомлення-статус."""
        if self.busy(chat):
            return "⏳ Ще завантажую попереднє — дочекайтесь або зупиніть: /media"
        self.live[chat] = ("🔎 Шукаю відео за посиланням…", [])
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

    async def quick(self, chat, url):
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        status = await self._status(chat, None, "🔎 Шукаю відео за посиланням…", stop)
        try:
            url = await M.resolve_share(url)
            with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
                items = await M.inspect_link(url, Path(work))
        except M.URLDenied as exc:
            await self._status(chat, status, f"⚠️ {esc(str(exc))}")
            return
        except JobError as exc:
            t = secrets.token_hex(4)
            self.offers[t] = (chat, url)
            await self._status(chat, status, f"⚠️ <b>Не вийшло завантажити</b>\n\n{exc}"[:4000],
                               [[("🔁 Спробувати ще раз", self.cb("re", t))], [("🍪 Входи на сайти", self.cb("ck")), ("« Медіа", self.cb())]])
            return
        job = Job(secrets.token_hex(4), url, items[:300])
        job.options.height = 1080  # швидкий режим: до 1080p, щоб не впертись у межу Telegram
        self.jobs[chat] = job
        if len(job.items) > QUICK_MAX:  # плейлист чи канал — тут без вибору ніяк
            t, rows = self.job_screen(chat)
            await self._status(chat, status, f"📋 Тут {len(job.items)} елементів — виберіть, що завантажити.\n\n" + t, rows)
            return
        await self.download(chat, job, list(range(1, len(job.items) + 1)), status=status)

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
        status = await self._status(chat, status, f"⬇️ Завантажую: {esc(title[:100])}…", stop)
        last = [0.0]

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
                head = f"⬇️ Завантажую" + (f" {k}/{len(nums)}" if len(nums) > 1 else "") + f": {esc(item.title[:90])}"

                async def on_dl(done, total, speed, eta, head=head):
                    await stage(f"{head}\n{bar(done, total, speed, eta)}")
                try:
                    await stage(head + "…", force=True)
                    path, tracks, info = await M.fetch(item, job.url, opts, work, progress=on_dl)
                    if path and (opts.mode != "video" or opts.cuts):
                        await stage(f"⚙️ Обробляю: {esc(item.title[:90])}…", force=True)
                    who, link = M.author(info)
                    desc = M.post_text(info)
                    outs = await M.render(path, opts, work, item.engine == "direct") if path else []
                    # Опис і автор — у підписі самого файлу (Telegram дозволяє 1024 символи); довший текст — ще й окремо
                    tail = (f"\n\n👤 {who}" + (f" · {link}" if link else "") if who else "\n") + f"\n🔗 {job.url}"
                    first = True
                    for out in outs + tracks:
                        cap = ""
                        if first:
                            if opts.post and desc and desc not in shown and opts.target != "zip":
                                full = desc.strip() + tail
                                if len(full) <= 1024:
                                    cap = full
                                    shown.add(desc)  # уже в підписі — окремим повідомленням не дублюємо
                                else:
                                    cap = desc.strip()[:1020 - len(tail)].rstrip() + "…" + tail
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
                        await self.send_doc(chat, out, cap[:1024], on_progress=on_up)
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
        rows = [[("⚙️ Інший формат (MP3, якість…)" if quick else "📋 До набору", self.cb("menu", job.token))], [("« Медіа", self.cb())]]
        if quick and (sent or shown):
            try:  # статус більше не потрібен — лишаємо тільки файли й одну кнопку
                await bot.delete_message(chat, status.message_id)
                status = None
            except Exception:  # noqa: BLE001
                pass
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

    async def send_doc(self, chat, path, caption="", on_progress=None):
        """До 50 МБ — звичайно; більше (до 2 ГБ) — через пряме з'єднання з Telegram від імені бота."""
        size = path.stat().st_size
        if size <= TELEGRAM_OUT - 512 * 1024:
            with path.open("rb") as fh:
                return await self.host.bot().send_document(chat, fh, filename=path.name, caption=caption, read_timeout=180, write_timeout=180)
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
            await self.big.send(chat, path, caption, progress=on_progress or prog)
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
