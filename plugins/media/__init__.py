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
from . import group as G
from .common import JobError, TELEGRAM_IN, TELEGRAM_OUT, run, safe_name
from .metadata import REPORT_ONLY, clean, report, suffix
from .bigsend import BIG_LIMIT, Big, Progress
from .studio import StudioUI

log = logging.getLogger("orion.plugins.media")
PAGE = 12
TOOLS = ("yt-dlp", "gallery-dl", "ffmpeg", "ffprobe", "exiftool", "mat2", "7z", "node", "libreoffice")
MODES = {"video": "🎬 Відео", "audio": "🎵 MP3", "gif": "🎞 GIF", "mute": "🔇 Без звуку"}
HEIGHTS = (0, 360, 480, 720, 1080, 2160)
LIVE = (60, 180, 300)
MEDIA_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch", "threads.net",
               "threads.com", "x.com", "twitter.com", "vimeo.com", "reddit.com", "pinterest.com", "pin.it", "twitch.tv",
               "dailymotion.com", "soundcloud.com", "tumblr.com", "likee.video", "snapchat.com", "rumble.com", "bsky.app")
# Соцмережі (і їхні веб- та мобільні версії — m., mobile., web. теж підходять): посилання сюди — одразу завантажуємо.
# Посилання на будь-які інші сайти (зокрема відеосайти й файлообмінники) — бот питає, що з ними робити.
SOCIAL_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch", "fb.com", "threads.net",
                "threads.com", "x.com", "twitter.com", "reddit.com", "redd.it", "pinterest.com", "pin.it", "tumblr.com",
                "snapchat.com", "bsky.app", "likee.video", "linkedin.com", "t.me", "telegram.me")
TG_POST = re.compile(r"^https?://(?:www\.)?(?:t|telegram)\.me/(?:s/|c/)?[\w]+/\d+", re.I)


def is_social(url):
    host = (M.urlsplit(url).hostname or "").lower()
    if not M.domain_matches(host, SOCIAL_HOSTS):
        return False
    if M.domain_matches(host, ("t.me", "telegram.me")):  # Telegram — лише конкретний допис каналу, не запрошення чи профіль
        return bool(TG_POST.match(url))
    return True


QUICK_MAX = 10
ALBUM_EXT = (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v")
ALBUM_MAX = 49 * 1024 * 1024  # ліміт Bot API на файл в альбомі  # стільки елементів (альбом, карусель) завантажуємо одразу, без меню
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


def blocks(frac, fill="🟩"):
    """Кольоровий бігунок із 10 квадратиків: 🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜"""
    n = max(0, min(10, int(frac * 10 + 1e-9)))
    return fill * n + "⬜" * (10 - n)


def bar(done, total, speed=0, eta=0):
    """🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 42% · 12.3 з 30.1 МБ · 2.1 МБ/с · ще 0:12"""
    if not total:
        return f"{mb(done)}" + (f" · {mb(speed)}/с" if speed else "")
    pct = max(0, min(100, int(done * 100 / total)))
    eta = int(eta or 0)
    return (f"{blocks(pct / 100)} {pct}% · {done / 1048576:.1f} з {mb(total)}"
            + (f" · {mb(speed)}/с" if speed else "") + (f" · ще {eta // 60}:{eta % 60:02d}" if eta else ""))


def pbar(frac, fill="🟦"):
    """Бігунок без розміру: 🟦🟦🟦🟦⬜⬜⬜⬜⬜⬜ 42% (🟩 — файл, 🟦 — усе в дописі, 🟪 — уся група)"""
    pct = max(0, min(100, int(frac * 100)))
    return f"{blocks(pct / 100, fill)} {pct}%"


class SkipItem(Exception):
    """Людина натиснула «⏭ Пропустити» — цей файл/допис пропускаємо, решта йде далі."""


class Plugin(StudioUI, Base):
    id = "med"
    icon = "⬇️"
    title = "Завантажити"
    version = "1.0"
    admin_only = False
    summary = ("Завантаження відео, фото, аудіо й тексту дописів за посиланням із YouTube, Instagram, TikTok, Facebook, "
               "Threads, X, Telegram, Reddit, Vimeo та сотень інших сайтів: якість, MP3, GIF, без звуку, озвучка, субтитри, "
               "таймкоди, запис ефіру, ZIP. А також звіт і очищення метаданих власних файлів.")
    VIEWS = {"ck", "tools", "help", "arc", "arcl", "rls", "utg", "gra", "grl", "pend", "bad", "ted", "fnd"}

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
        self.instr: dict[int, dict] = {}
        self.gctx: dict[int, dict] = {}       # 📦 поточне групове завантаження чату
        self.gpend: dict[str, dict] = {}      # 📦 групи, що чекають вибору
        self.user_stop: set = set()
        self.ctl: dict[int, dict] = {}        # ⏸ керування: pause, act (pause|skip), held — відкладені дописи групи
        self.fetching: dict[int, asyncio.Future] = {}  # поточне завантаження файлу (щоб зупинити лише його)
        self.gmsg: dict = {}  # чат -> повідомлення-статус групи (одне на всю групу)
        self.just_done: set = set()
        self.force_now: dict[int, bool] = {}
        self.last_result: dict[int, tuple] = {}
        self.cur_url: dict[int, str] = {}     # посилання поточного завантаження (як надіслала людина)
        self.pend_seen: dict[int, float] = {} # коли востаннє нагадували про збережені посилання
        self.ticks: dict[int, asyncio.Task] = {}  # ✍️ вказівки про теги до поточного завантаження
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
           ("f", "🆕 Вперше"), ("ai", "🤖 Визначати тему, локацію й мову за допомогою ШІ"),
           ("see", "👁 ШІ дивиться на фото й кадри відео всього допису"),
           ("hear", "👂 ШІ слухає, що говорять у відео (початок, для змісту)"),
           ("about", "🧠 Рядок «що це» в підписі (зміст, місце, мета допису)"),
           ("risk", "⚠️ Попереджати: російська мова, матюки, музика, створене ШІ, чужі авторські права"),
           ("risk_up", "⚠️ Перевіряти так само й файли, які я надсилаю в бот"),
           ("qual", "📉 Перевіряти якість фото й відео (роздільність, стиснення, різкість, світло)"),
           ("shz", "🎧 Сайт не вказав музику — шукати її в самому звуці (Shazam, і змішану з голосом)"),
           ("geo_author", "🧭 Немає місця в дописі — брати з інших дописів автора чи його сторінки"))

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

    VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
    IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic")
    AUD_EXT = (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac")

    @classmethod
    async def _frames(cls, path, n=3):
        """Кадри з відео (на 0, 4, 8… с) як байти JPEG — щоб ШІ бачив, що відбувається, а не лише перший кадр."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "fps=1/4,scale=512:-2", "-frames:v", str(n),
                f"{tmp}/f%02d.jpg")
            try:
                await asyncio.wait_for(proc.wait(), 40)
            except asyncio.TimeoutError:
                proc.kill()
            return [f.read_bytes() for f in sorted(Path(tmp).glob("f*.jpg"))]

    @staticmethod
    async def _fetch_small(url, limit=4_000_000):
        """Інше фото чи обкладинка з допису за адресою (ще не скачане) — для аналізу всього допису."""
        try:
            async with M.open_url(url, agent=M.BROWSER) as r:
                buf = bytearray()
                async for chunk in r.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > limit:
                        return None
                return bytes(buf)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _collage(blobs):
        """До 4 зображень — одна картинка-сітка 2×2 (до 1024 пікс.): будь-яка модель із зором бачить увесь допис одним запитом."""
        import base64
        import io
        from PIL import Image
        ims = []
        for b in blobs:
            try:
                im = Image.open(io.BytesIO(b)).convert("RGB")
                im.thumbnail((512, 512))
                ims.append(im)
            except Exception:  # noqa: BLE001
                continue
        if not ims:
            return None
        ims = ims[:4]
        if len(ims) == 1:
            canvas = ims[0]
        else:
            cols = 2
            rows = (len(ims) + 1) // 2
            canvas = Image.new("RGB", (cols * 512, rows * 512), (255, 255, 255))
            for i, im in enumerate(ims):
                x, y = (i % cols) * 512, (i // cols) * 512
                canvas.paste(im, (x + (512 - im.width) // 2, y + (512 - im.height) // 2))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    async def _hear(self, path, seconds=45):
        """Що говорять у відео чи аудіо (перші ~45 с) — для розуміння змісту; без сервісу розшифровки — порожньо."""
        import tempfile
        try:
            import media as core
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "a.mp3"
                proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-t", str(seconds),
                                                            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out))
                await asyncio.wait_for(proc.wait(), 40)
                if not out.exists() or out.stat().st_size < 2000:
                    return ""
                text, _p, _e = await asyncio.wait_for(core.transcribe(out.read_bytes(), "a.mp3"), 45)
                return (text or "").strip()[:800]
        except asyncio.TimeoutError:
            log.info("related: розшифровка для аналізу — сервіс не відповів вчасно")
            return ""
        except Exception as exc:  # noqa: BLE001 — немає звуку чи сервісу: аналіз без мови
            log.info("related: розшифровка для аналізу не вийшла: %s", str(exc)[:160])
            return ""

    async def understand(self, chat, ctx):
        """🧠 Що саме скачують: кадри фото й відео всього допису (сітка), мова у відео, музика, типи файлів.
        ctx: media — уже скачаний файл; others — адреси інших фото/обкладинок допису; song; files. -> (картинка base64, текст)."""
        ctx = ctx or {}
        blobs, notes = [], []
        media = ctx.get("media")
        suf = Path(media).suffix.lower() if media else ""
        see, hear = self.rel_on(chat, "see"), self.rel_on(chat, "hear")
        if media and see:
            if suf in self.VIDEO_EXT:
                blobs += await self._frames(media, 3 if not ctx.get("others") else 1)
            elif suf in self.IMG_EXT:
                try:
                    blobs.append(Path(media).read_bytes())
                except OSError:
                    pass
        if see and ctx.get("others"):
            got = await asyncio.gather(*(self._fetch_small(u) for u in ctx["others"][: max(0, 4 - len(blobs))]))
            blobs += [g for g in got if g]
        if media and hear and suf in self.VIDEO_EXT + self.AUD_EXT:
            said = await self._hear(media)
            if said:
                notes.append(f"Що говорять/звучить у відео (розшифровка початку): {said}")
        if ctx.get("song"):
            notes.append(f"Музика в дописі: {ctx['song']}")
        if ctx.get("files"):
            notes.append(f"Файли допису: {ctx['files']}")
        img = await asyncio.to_thread(self._collage, blobs) if blobs else None
        ctx["_said"] = next((n for n in notes if n.startswith("Що говорять")), "")
        return img, "\n".join(notes), len(blobs)

    async def scan_path(self, chat, path, caption=""):
        """⚠️ Перевірка файлу, який людина сама надіслала: російська мова, матюки, музика (авторські права).
        Ті самі способи, що й для скачаного: кадри, мова у відео, текст підпису, написи на зображенні. -> множина небезпек."""
        risks = set()
        ctx = {"media": str(path)}
        img, notes, nimg = None, "", 0
        try:
            img, notes, nimg = await self.understand(chat, ctx)
        except Exception:  # noqa: BLE001
            log.info("scan: аналіз файлу не вийшов", exc_info=True)
        said = ctx.get("_said", "")
        if R.swear_hits(f"{caption}\n{said}"):
            risks.add("swear")
        if "ru" in (R.guess_lang(caption), R.guess_lang(said)):
            risks.add("ru")
        if re.search(r"(?i)\[(?:music|музика|музыка)\]|♪", said):
            risks.add("music")
        if R.ai_marks_in_file(path):
            risks.add("ai")
        rm = R.rights_marks_in_file(path)
        if rm:
            risks |= {"rights", "why:" + rm}
        if self.rel_on(chat, "qual"):
            q = await self.quality(path)
            if q:
                risks.add("q:" + ", ".join(q))
        if Path(path).suffix.lower() in self.VIDEO_EXT + self.AUD_EXT and self.rel_on(chat, "shz") and await self._has_audio(path):
            got = await self._shazam(path)  # 🎧 відома пісня у звуці файлу
            if got:
                risks.add("music")
        if self.rel_on(chat, "ai") and (img or notes or len((caption or "").strip()) >= 2):
            for pic in ((img, None) if img else (None,)):
                try:
                    ans, _m = await asyncio.wait_for(self.host.ask_ai(
                        R.ai_prompt(caption or "", "", [], [], seen=bool(pic), notes=notes, nimg=nimg),
                        system=R.AI_SYSTEM, label="⚠️ Перевірка файлу", image=pic), 70 if pic else 40)
                    if re.search(r"\{.*\}", ans or "", re.S):
                        risks |= R.parse_risks(ans) - {"music"}  # ШІ звуку не чує — про музику судить лише Shazam
                        seen_t = R.parse_seen(ans)
                        if R.swear_hits(seen_t):
                            risks.add("swear")
                        if seen_t and R.guess_lang(seen_t) == "ru":
                            risks.add("ru")
                        break
                except Exception:  # noqa: BLE001
                    log.info("scan: ШІ не перевірив файл", exc_info=True)
        return risks

    async def scan_upload(self, update, ctx):
        """Будь-який файл, надісланий у бот людиною (фото, відео, аудіо, голосове, документ-медіа): перевірка у фоні,
        попередження відповіддю на нього; основна обробка (ШІ, розшифровка, інструменти) іде як завжди."""
        msg = update.message
        if not msg or not self.rel_on(msg.chat_id, "risk") or not self.rel_on(msg.chat_id, "risk_up"):
            return
        file, default = message_attachment(msg)
        if not file or msg.forward_origin:  # лише власні файли людини; репости не перевіряємо
            return
        mime = getattr(file, "mime_type", "") or ""
        if msg.document and not mime.startswith(("image/", "video/", "audio/")):
            return
        chat, mid, cap = msg.chat_id, msg.message_id, msg.caption or ""
        size = getattr(file, "file_size", 0) or 0

        async def job():
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                name = safe_name(getattr(file, "file_name", None) or default)
                dest = Path(tmp) / name
                try:
                    if size > 20 * 1024 * 1024:
                        if not self.big.ready():
                            return
                        await self.big.download(chat, mid, dest)
                    else:
                        await (await file.get_file()).download_to_drive(str(dest))
                except Exception:  # noqa: BLE001
                    log.info("scan: файл не отримано", exc_info=True)
                    return
                risks = await self.scan_path(chat, dest, cap)
            if risks:
                await self.host.bot().send_message(chat, esc(R.risks_line(risks)) + "\n<i>Перевірте перед публікацією.</i>",
                                                   parse_mode="HTML", reply_to_message_id=mid, disable_notification=True)
        asyncio.get_running_loop().create_task(job())

    @staticmethod
    async def _has_audio(path):
        try:
            proc = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                                        "-of", "csv=p=0", str(path), stdout=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
            return bool(out.strip())
        except Exception:  # noqa: BLE001
            return True  # не змогли перевірити — вважаємо, що звук є

    @staticmethod
    async def _duration(path):
        try:
            proc = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                                        str(path), stdout=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
            return float(out.strip() or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def shazam_windows(dur, clip=20, most=45):
        """Звідки брати шматки звуку: усе відео суцільно, шматками по 20 с без проміжків (останній — впритул до кінця).
        Дуже довге (понад 15 хв) — 45 шматків рівномірно по всій довжині, щоб перевірка не тривала безкінечно."""
        if dur <= clip + 2:
            return [0.0]
        last = max(0.0, dur - clip)
        n = int(-(-dur // clip))  # скільки шматків покриють усе
        if n <= most:
            return sorted({*(float(i * clip) for i in range(n - 1)), round(last, 1)})
        return sorted({round(last * i / (most - 1), 1) for i in range(most)})

    async def _shazam(self, path, all_hits=False):
        """Shazam по кількох шматках усього відео (початок, середина, кінець) -> (виконавець, назва) першої знайденої пісні,
        або список усіх різних пісень (all_hits=True); None — нічого."""
        try:
            from shazamio import Shazam
        except ImportError:
            return None
        hits = []
        sh = None
        checked = 0
        try:
            sh = Shazam()
            dur = await self._duration(path)
            with tempfile.TemporaryDirectory(prefix="orion-shz-") as work:
                for i, start in enumerate(self.shazam_windows(dur)):
                    clip = Path(work) / f"c{i}.mp3"
                    await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-t", "20", "-i", str(path),
                              "-vn", "-ac", "1", clip, timeout=120)
                    if not clip.is_file() or clip.stat().st_size < 4000:
                        continue
                    try:
                        out = await asyncio.wait_for(sh.recognize(str(clip)) if hasattr(sh, "recognize") else sh.recognize_song(str(clip)), 40)
                        checked += 1
                    except Exception:  # noqa: BLE001
                        continue
                    tr = (out or {}).get("track") or {}
                    if tr.get("title"):
                        hit = (tr.get("subtitle") or "", tr["title"])
                        if not all_hits:
                            hits = [hit]
                            break
                        if hit not in hits:
                            hits.append(hit)
        except Exception:  # noqa: BLE001
            log.info("shazam не вдався", exc_info=True)
        finally:
            try:  # закриваємо з'єднання клієнта — без «Unclosed client session» у журналі
                await sh.close()
            except Exception:  # noqa: BLE001
                pass
        self._shz_checked = checked  # скільки шматків Shazam реально перевірив (0 — перевірити не вдалося)
        try:
            import health
            health.ok("shazam") if checked else health.fail("shazam", "жоден шматок не перевірено")
        except Exception:  # noqa: BLE001
            pass
        if all_hits:
            return hits
        return hits[0] if hits else None

    @staticmethod
    def _sharp_ratio(im):
        """Різкість: наскільки слабшають контури після легкого розмиття (різке — у рази, розмите — майже ні)."""
        from PIL import ImageFilter, ImageStat
        g = im.convert("L")
        g.thumbnail((640, 640))
        a = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]
        b = ImageStat.Stat(g.filter(ImageFilter.GaussianBlur(1.2)).filter(ImageFilter.FIND_EDGES)).var[0]
        return a / max(b, 1e-6), ImageStat.Stat(g).mean[0]

    async def quality(self, path):
        """📉 Якість фото чи відео для публікації: роздільність, стиснення, кадри за секунду, різкість, світло -> [проблеми]."""
        import io
        import json as _json
        suf = Path(path).suffix.lower()
        if suf not in self.IMG_EXT + self.VIDEO_EXT:
            return []
        issues = []
        try:
            proc = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
                                                        stdout=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
            data = _json.loads(out or b"{}")
        except Exception:  # noqa: BLE001
            data = {}
        v = next((x for x in data.get("streams", []) if x.get("codec_type") == "video"), {})
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if w and h and min(w, h) < 720:
            issues.append(f"низька роздільність {w}×{h} (для мереж краще {'1080×1920' if h > w else '1920×1080' if w > h else '1080×1080'})")
        if suf in self.VIDEO_EXT and v:
            try:
                br = float(v.get("bit_rate") or (data.get("format") or {}).get("bit_rate") or 0)
            except ValueError:
                br = 0
            if br and min(w, h) >= 720 and br < 1_200_000:
                issues.append(f"сильно стиснуте ({br / 1e6:.1f} Мбіт/с)")
            try:
                num, den = (v.get("avg_frame_rate") or "0/1").split("/")
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = 0
            if 0 < fps < 20:
                issues.append(f"мало кадрів ({fps:.0f}/с)")
        try:
            from PIL import Image
            if suf in self.VIDEO_EXT:
                blobs = await self._frames(path, 3)
                ims = [Image.open(io.BytesIO(b)) for b in blobs]
            else:
                ims = [Image.open(path)]
            if ims:
                vals = await asyncio.to_thread(lambda: [self._sharp_ratio(i) for i in ims])
                sharp = sorted(x[0] for x in vals)[len(vals) // 2]
                light = sum(x[1] for x in vals) / len(vals)
                if sharp < 1.3:
                    issues.append("розмите")
                if light < 35:
                    issues.append("дуже темне")
                elif light > 235:
                    issues.append("пересвічене")
        except Exception:  # noqa: BLE001
            log.info("quality: кадри не перевірено", exc_info=True)
        return issues

    async def post_music_in_file(self, media, song):
        """Threads: музика прикріплена до допису. Чи є вона в самому скачаному файлі?
        -> True (є) | False (немає: фото, відео без звуку або звучить інше) | None (не вдалося перевірити)."""
        if not media or Path(media).suffix.lower() not in self.VIDEO_EXT + self.AUD_EXT:
            return False  # фото — музики в них немає
        if not await self._has_audio(media):
            return False
        got = await self._shazam(media)
        if got is None:
            return False if getattr(self, "_shz_checked", 0) else None  # прослухав і нічого не знайшов — у файлі цієї пісні немає
        title = (song or "").lower()
        return any(w and w.lower() in title for w in (got[1], got[1].split("(")[0].strip()))

    async def detect(self, chat, text, geotag, who, alink, instr=None, media=None, ctx=None):
        """Тема, локація, мова: ШІ (без обмежень) + словник + збережені теги людини + її вказівки до посилання;
        мова без тексту — як переважно пише автор. -> (ключі тем, ключі локацій, ключі мов) або None — «без тегів»."""
        instr = instr or {}
        if instr.get("none"):
            return None
        d_t, d_g = R.tags(f"{text}\n{geotag}")
        a_t, a_g, lang = [], [], ""
        body = (text or "").strip()
        self.__dict__.setdefault("_about", {}).pop(chat, None)
        self.__dict__.setdefault("_seen", {}).pop(chat, None)
        img, notes, nimg = (None, "", 0)
        risks = set()
        if self.rel_on(chat, "ai"):
            ctx = dict(ctx or {}, media=media or (ctx or {}).get("media"))
            try:
                img, notes, nimg = await self.understand(chat, ctx)
            except Exception:  # noqa: BLE001
                log.info("related: аналіз допису не вийшов", exc_info=True)
        if self.rel_on(chat, "ai") and (len(body) >= 2 or geotag or img or notes):  # навіть «Саржин яр» — це вже місце
            for pic in ((img, None) if img else (None,)):  # модель без зору — повтор без картинки
                try:
                    ans, _m = await asyncio.wait_for(self.host.ask_ai(
                        R.ai_prompt(body, geotag, self.hist.known("t"), self.hist.known("g"), seen=bool(pic), notes=notes,
                                    nimg=nimg, site=(ctx or {}).get("site", ""), who=who or ""),
                        system=R.AI_SYSTEM, label="🔗 Що в дописі", image=pic), 70 if pic else 40)
                    a_t, a_g, lang = R.parse_ai(ans)
                    about = R.parse_about(ans)
                    if about:
                        self._about[chat] = about
                    risks |= R.parse_risks(ans) - {"music"}  # ШІ звуку не чує — про музику судять лише Shazam і дані сайту
                    seen = R.parse_seen(ans)
                    if seen:
                        self.__dict__.setdefault("_seen", {})[chat] = seen
                    if a_t or a_g or about:
                        break
                except Exception:  # noqa: BLE001 — ШІ недоступний: лишається словник
                    log.info("related: ШІ не визначив тему%s", " за кадрами" if pic else " — беру словник")
        # ⚠️ небезпеки: російська, матюки, музика (авторські права) — ШІ + перевірка без ШІ
        said = (ctx or {}).get("_said", "") if isinstance(ctx, dict) else ""
        if R.swear_hits(f"{body}\n{said}"):
            risks.add("swear")
        if "ru" in (lang, R.guess_lang(body), R.guess_lang(said)):
            risks.add("ru")
        on_post = (ctx or {}).get("music_on_post")  # лише Threads: музика на дописі, а не обов'язково у відео
        if (ctx or {}).get("song_overlay") and on_post:
            inside = await self.post_music_in_file((ctx or {}).get("media") or media, (ctx or {}).get("song", ""))
            # Threads вказав музику допису: почута у файлі — повне попередження; інакше — що вона прикріплена, але у файлі її немає
            risks.add("music" if inside else "music_post")
            log.info("related: музика допису у файлі: %s", inside)
        elif (ctx or {}).get("song_overlay") or re.search(r"(?i)\[(?:music|музика|музыка)\]|♪", said):
            risks.add("music")
        seen_t = self.__dict__.get("_seen", {}).get(chat, "")
        if seen_t:  # ⚠️ і в написах на кадрах
            if R.swear_hits(seen_t):
                risks.add("swear")
            if R.guess_lang(seen_t) == "ru":
                risks.add("ru")
        med_ = (ctx or {}).get("media") or media
        if (med_ and R.ai_marks_in_file(med_)) or R.ai_marks_in_info((ctx or {}).get("info") or {}):
            risks.add("ai")  # позначка ШІ у файлі (C2PA) чи в даних сайту — надійніше за здогад
        if ((ctx or {}).get("info") or {}).get("paid_partnership"):
            risks.add("paid")  # 💼 сам сайт позначив допис як рекламу
        rm = R.rights_marks_in_file(med_) if med_ else ""
        if rm:  # ©️ фотобанк, агенція чи поле авторських прав у метаданих
            risks |= {"rights", "why:" + rm}
        self.__dict__.setdefault("_said", {})[chat] = said.split(": ", 1)[-1] if said else ""
        self.__dict__.setdefault("_risk", {})[chat] = risks if self.rel_on(chat, "risk") else set()
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
        self.__dict__.setdefault("_geo_guess", {}).pop(chat, None)
        if not g_k and self.rel_on(chat, "geo_author"):  # 📍 у дописі місця немає — шукаємо в автора (є — не шукаємо)
            try:
                more, why = await self.author_geo(chat, who, alink)
            except Exception:  # noqa: BLE001
                more, why = [], ""
            if more:
                g_k += more
                self._geo_guess[chat] = (more, why)  # у підписі — «імовірна локація», не як факт допису
                log.info("related: імовірна локація з «%s»: %s", why, more)
        for kind, k in (await self.user_tags(chat, instr["add"]) if instr.get("add") else []):
            (t_k if kind == "t" else g_k).insert(0, k) if k not in (t_k if kind == "t" else g_k) else None
        for d in instr.get("drop", []):  # «не став тег …»
            rx = R.stem_rx(d)
            for lst, kind in ((t_k, "t"), (g_k, "g"), (l_k, "l")):
                lst[:] = [k for k in lst if not (k == R.key_of(d) or (rx and rx.search(self.hist.name(kind, k))))]
        return t_k, g_k, l_k

    async def author_geo(self, chat, who, alink):
        """Допис без місця: локація, яку автор найчастіше ставить в інших скачаних дописах, інакше — з опису його сторінки."""
        a = R.norm_author(who)
        cnt = {}
        for e in self.hist.items(chat):
            if a and e.get("author") == a:
                for k in e.get("geos", []):
                    cnt[k] = cnt.get(k, 0) + 1
        if cnt:
            best = max(cnt.values())
            return [k for k, v in cnt.items() if v == best][:2], "дописи автора"
        if not alink:
            return [], ""
        try:
            _u, page = await asyncio.wait_for(M._grab(alink, 600_000), 12)
        except Exception:  # noqa: BLE001
            return [], ""
        bio = " ".join(re.findall(r'<meta[^>]+(?:og:description|name="description")[^>]+content="([^"]*)"', page))
        bio = __import__("html").unescape(bio)[:600]
        if not bio.strip():
            return [], ""
        _t, geos = R.tags(bio)
        if not geos and self.rel_on(chat, "ai"):
            try:
                ans, _m = await asyncio.wait_for(self.host.ask_ai(
                    "Це опис сторінки автора, не допис. Якщо в ньому вказано, де автор живе чи працює, — це місце; теми не потрібні.\n\n"
                    + R.ai_prompt(bio, "", [], self.hist.known("g")), system=R.AI_SYSTEM, label="🔗 Локація автора"), 30)
                _t, geos, _l = R.parse_ai(ans)
            except Exception:  # noqa: BLE001
                geos = []
        return (self.hist.learn("g", geos[:2]), "сторінка автора") if geos else ([], "")

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

    def mh_act(self, *args):
        """Посилання-дія для підпису альбому: t.me/<бот>?start=mx_<дія>_<параметр> → той самий екран, що й кнопка."""
        try:
            u = self.host.bot().username
        except Exception:  # noqa: BLE001
            u = ""
        tail = "_".join(str(a) for a in args if str(a))
        return f"https://t.me/{u}?start=mx_{tail}"[:80] if u else ""

    def mh_link(self, rid):
        try:
            u = self.host.bot().username
        except Exception:  # noqa: BLE001
            u = ""
        return f"https://t.me/{u}?start=mh_{rid}" if u else ""

    ROW_CHARS = 34  # приблизно стільки знаків уміщає рядок підпису на телефоні

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
        by_id = {}
        for phrase, e in pairs:  # одне й те саме попереднє повідомлення — одним посиланням з усіма збігами
            url = self.mh_link(e["id"])
            if url:
                by_id.setdefault(e["id"], [url, []])
                if phrase not in by_id[e["id"]][1]:
                    by_id[e["id"]][1].append(phrase)
        links = [("попереднє " + ", ".join(ph), url) for url, ph in by_id.values()]
        line = ("\n↩️ " + " · ".join(f'<a href="{esc(u)}">{esc(lab)}</a>' for lab, u in links)) if links else ""
        vis = len("\n↩️ " + " · ".join(lab for lab, _ in links)) if links else 0
        first = r["first"]
        if first and self.rel_on(chat, "f"):  # 🆕 тема чи локація вперше (автор «вперше» не позначається)
            note = "\n🆕 Вперше: " + self.hist.first_text(first)
            line, vis = esc(note) + line, vis + len(note)
        if vis > self.ROW_CHARS * 3:  # більше ніж три рядки на екрані телефона — згортаємо
            n = len(links) + (1 if first and self.rel_on(chat, "f") else 0)
            line = f"\n<blockquote expandable>🔗 Пов'язане · {n}" + line + "</blockquote>"
            vis += len(f"🔗 Пов'язане · {n}") + 1
        buttons = []
        if self.rel_on(chat, "a"):
            for st, e in r["site"].items():
                url = self.mh_link(e["id"])
                if url:
                    buttons.append((f"{R.icon(st)} Попереднє відео автора", url))
        return line, vis, buttons, [f"{a}:{b}" for a, b in first]

    @staticmethod
    def when_size(e):
        """«22.11.2026 14:07 · 8.0 МБ» — точний час скачування й розмір файлу."""
        return R.ts_text(e.get("ts")) + (f" · {mb(e['size'])}" if e.get("size") else "")

    def related_all(self, chat, e, limit=15):
        """Усе скачане, пов'язане з цим записом: за кожною темою, локацією й автором -> [(заголовок, [записи])]."""
        seen, es = {R.norm_url(e.get("url", ""))}, []
        for x in reversed(self.hist.items(chat)):  # той самий допис, скачаний кілька разів, — один раз (найновіший)
            k = R.norm_url(x.get("url", "")) or str(x.get("id"))
            if x.get("id") != e.get("id") and not x.get("notag") and k not in seen:
                seen.add(k)
                es.append(x)
        es.reverse()
        out = []
        if e.get("author"):
            same = [x for x in es if x.get("author") == e["author"]]
            if same:
                out.append((f"👤 {e.get('who') or e['author']}", same))
        for kind, key, icon_ in (("g", "geos", "📍"), ("t", "topics", "🏷")):
            for k in e.get(key, []):
                same = [x for x in es if k in x.get(key, [])]
                if same:
                    out.append((f"{icon_} {self.hist.name(kind, k)}", same))
        if e.get("group"):
            same = [x for x in self.hist.items(chat) if x.get("group") == e["group"] and x.get("id") != e.get("id")]
            if same:
                out.append((f"📦 Група №{e['group']}", same))
        return [(h, list(reversed(lst))[:limit], len(lst)) for h, lst in out]

    async def jump(self, chat, rid, head=None, extra_rows=None):
        """↩️ «попереднє …»: відповідь на той допис (дотик до цитати — і ви там) зі списком УСЬОГО пов'язаного:
        та сама тема, локація, автор, група. Список згорнутий — розгортається й гортається."""
        bot = self.host.bot()
        e = self.hist.get(chat, rid)
        if not e:
            return await self.host.notify(chat, "Цього запису в історії скачаного вже немає.")
        parts = []
        for sec, lst, total in self.related_all(chat, e):
            lines = []
            for x in lst:
                url = self.mh_link(x["id"])
                title = esc((x.get("title") or "файл").replace("\n", " ")[:50])
                lines.append(("• " + (f'<a href="{esc(url)}">{title}</a>' if url else title)
                              + f" · {R.icon(x.get('site'))} {esc(self.when_size(x))}"))
            more = f"\n…ще {total - len(lst)} — в «🗂 Архів скачаного»" if total > len(lst) else ""
            parts.append(f"<b>{esc(sec)}</b> · {total}\n" + "\n".join(lines) + more)
        head = head or f"⬆️ <b>Цей допис</b> — торкніться цитати, щоб перейти до нього\n🕓 Скачано {esc(self.when_size(e))}"
        body = (head + "\n\n🔗 <b>Пов'язане</b>\n<blockquote expandable>" + "\n\n".join(parts) + "</blockquote>") if parts \
            else head + "\n\nІнших пов'язаних завантажень поки немає."
        from footnotes import html_split
        kb = self._markup((extra_rows or []) + [[("🏷 Категорії", self.cb("ted", rid))]])
        chunks = html_split(body, 3900)
        try:
            for i, ch in enumerate(chunks):
                await bot.send_message(chat, ch, parse_mode="HTML", disable_web_page_preview=True,
                                       reply_markup=kb if i == len(chunks) - 1 else None,
                                       **({"reply_to_message_id": e["msg"], "allow_sending_without_reply": False} if i == 0 else {}))
        except Exception:  # noqa: BLE001 — сам допис видалено з чату: список усе одно показуємо
            await self.host.notify(chat, "⚠️ Цей файл уже видалено з чату.\n\n" + body.split("\n\n", 1)[-1], [[("🏷 Категорії", self.cb("ted", rid))]])

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
        rows = [[("🔎 Знайти в скачаному", self.cb("fnd"))],
                [(f"👤 Автори · {len(g['a'])}", self.cb("arcl", "a")), (f"🏷 Теми · {len(g['t'])}", self.cb("arcl", "t"))],
                [(f"📍 Локації · {len(g['g'])}", self.cb("arcl", "g")), (f"🗣 Мови · {len(g['l'])}", self.cb("arcl", "l"))],
                [(f"{R.EXTRA_NAME[k]} · {len(g['x'][k])}", self.cb("arcl", "x", k)) for k in ("notopic", "nogeo", "first") if g["x"].get(k)],
                [("📦 Групи", self.cb("grl")), ("✍️ Мої теги", self.cb("utg"))], [("⚙️ Що пов'язувати", self.cb("rls"))], [("« Медіа", self.cb())]]
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
            lines.append(f"• {head} · {R.icon(e.get('site'))} {esc(e.get('who') or '')} · {esc(self.when_size(e))}")
        return "\n".join(lines)[:4000], [[("« " + name.split(" ", 1)[1], self.cb("arcl", kind))], [("« Архів", self.cb("arc"))]]

    def arc_extra(self, chat, k):
        es = list(reversed(self.hist.groups(chat)["x"].get(k, [])))
        lines = [f"🗂 <b>{R.EXTRA_NAME[k]}</b> · {len(es)}", ""]
        for e in es[:25]:
            url = self.mh_link(e["id"])
            title = esc((e.get("title") or "файл")[:60])
            head = f'<a href="{esc(url)}">{title}</a>' if url else title
            fx = (" · " + esc(self.hist.first_text([tuple(f.split(":", 1)) for f in e["first"]]))) if k == "first" and e.get("first") else ""
            lines.append(f"• {head} · {R.icon(e.get('site'))} {esc(e.get('who') or '')}{fx} · {esc(self.when_size(e))}")
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

    def next_group(self, chat):
        n = int(self.pref(chat, "gseq", 0) or 0) + 1
        self.set_pref(chat, "gseq", n)
        return n

    def group_list(self, chat, gid=None):
        es = [e for e in self.hist.items(chat) if e.get("group")]
        if gid is None:
            by = {}
            for e in es:
                by.setdefault(e["group"], []).append(e)
            t = "📦 <b>Групові завантаження</b>\n\n" + ("Поки немає. Надішліть кілька посилань одним повідомленням." if not by else "Оберіть групу:")
            rows = [[(f"№{k} · {len(v)} · {R.ts_text(v[0].get('ts'))[:10]}", self.cb("grl", k))] for k, v in sorted(by.items(), reverse=True)[:30]]
            return t, rows + [[("« Архів", self.cb("arc"))]]
        lst = [e for e in es if e.get("group") == gid]
        lines = [f"📦 <b>Група №{gid}</b> · {len(lst)}", ""]
        for e in lst:
            url = self.mh_link(e["id"])
            title = esc((e.get("title") or "файл")[:60])
            lines.append(f"{e.get('gi', 0) + 1}. " + (f'<a href="{esc(url)}">{title}</a>' if url else title)
                         + f" · {R.icon(e.get('site'))} {esc(e.get('who') or '')} · {esc(self.when_size(e))}")
        return "\n".join(lines)[:4000], [[("« Групи", self.cb("grl"))], [("« Архів", self.cb("arc"))]]

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
        bl = [e for e in self.bad_list(chat) if not e.get("ok")]
        if bl:
            rows.insert(1 if len(rows) > 1 else 0, [(f"🧯 Биті посилання · {len(bl)}", self.cb("bad"))])
        pl = self.pend_list(chat)
        if pl:
            rows.insert(0, [(f"📌 Збережені посилання · {len(pl)}", self.cb("pend"))])
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
        cache = self.__dict__.setdefault("_ver", {})  # версії — раз на 10 хв (запуск програм гальмував екран)
        for tool in ("yt-dlp", "gallery-dl"):
            if shutil.which(tool):
                v, t0 = cache.get(tool, ("", 0))
                if time.time() - t0 > 600:
                    try:
                        v = (await run(tool, '--version', timeout=20)).strip()[:20]
                    except JobError:
                        v = ""
                    cache[tool] = (v, time.time())
                if v:
                    lines.append(f"{tool}: версія {v}")
        rows = [[("🩺 Перевірити сайти", self.cb("chk")), ("🧪 Перевірити розпізнавання", self.cb("selft"))]]
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
        if a == "gra" and x in self.gpend:
            return None, self.group_screen(chat, x)
        if a == "grm" and x in self.gpend and y in G.MODE_ICON:
            self.gpend[x]["mode"] = y
            return "Вигляд для всіх змінено", self.group_screen(chat, x)
        if a == "gri" and x in self.gpend and y.isdigit():
            it = self.gpend[x]["items"][int(y) - 1]
            order = [""] + [k for k, _l in G.MODES]
            it["mode"] = order[(order.index(it["mode"]) + 1) % len(order)]
            return f"№{y}: " + (dict(G.MODES)[it["mode"]] if it["mode"] else "як для всіх"), self.group_screen(chat, x)
        if a == "grt" and x in self.gpend and y.isdigit():
            self.ask(chat, "grt", f"{x}:{y}")
            return None, (f"✍️ <b>Теги для №{y}</b>\n\nНапишіть теги через кому (напр. «Полісся, озеро»), або «без тегів», "
                          "або «не став тег снігопад». Порожнє «-» — повернути як було (теги визначу сам).", [[("« Група", self.cb("gra", x))]])
        if a == "grx" and x in self.gpend:
            self.gpend.pop(x, None)
            return "Скасовано", self.home(chat)
        if a == "grgo" and x in self.gpend:
            if self.busy(chat):
                g = self.gpend.pop(x)
                return None, (self.pend_add(chat, [(it["url"], G.merge_instr(g["common"], it["own"])) for it in g["items"]]),
                              [[("📌 Збережені", self.cb("pend"))]])
            g = self.gpend.pop(x)
            gid = self.next_group(chat)
            items = [{"url": it["url"], "mode": it["mode"] or g["mode"], "instr": G.merge_instr(g["common"], it["own"])}
                     for it in g["items"]]
            self.gctx[chat] = {"id": gid, "items": items, "left": list(range(len(items))), "n": len(items)}
            self._bg(chat, self.group_run(chat))
            return f"📦 Група №{gid}: починаю", None
        if a == "fnd":
            self.ask(chat, "fnd")
            return None, ("🔎 <b>Що знайти в скачаному?</b>\n\nНапишіть тему, місце, автора — можна разом, як зручно:\n"
                          "«снігопад з Карпат», «усе від oleno4ka», «Полісся озеро», «англійською про спорт».", [[("« Архів", self.cb("arc"))]])
        if a == "ted" and x.isdigit():
            return None, self.ted_screen(chat, int(x))
        if a == "tdel" and x.isdigit() and y[:1] in ("t", "g", "l") and y[1:].isdigit():
            e = self.hist.get(chat, int(x))
            if e:
                key = R.KINDS[y[0]]
                lst = list(e.get(key, []))
                if int(y[1:]) < len(lst):
                    lst.pop(int(y[1:]))
                    self.hist.update(chat, int(x), **{key: lst})
            return "🗑 Прибрано", self.ted_screen(chat, int(x))
        if a == "tadd" and x.isdigit():
            self.ask(chat, "tadd", int(x))
            return None, ("➕ <b>Нові категорії</b>\n\nНапишіть через кому: теми й місця (напр. «снігопад, Полісся, озеро»).",
                          [[("« Категорії", self.cb("ted", x))]])
        if a == "bad":
            return None, self.bad_screen(chat, int(x) if x.isdigit() else 0)
        if a == "badre" and x.isdigit():
            lst = list(reversed(self.bad_list(chat)))
            if 0 < int(x) <= len(lst):
                return self.start_quick(chat, lst[int(x) - 1]["url"]) or "🔎 Пробую ще раз…", None
            return "Запис застарів", self.bad_screen(chat)
        if a == "badclr":
            data = self._bad()
            data.pop(str(chat), None)
            (self.dir / "failed.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            return "🗑 Очищено", self.bad_screen(chat)
        if a == "pend":
            return None, self.pend_screen(chat)
        if a == "pgo":
            lst = self.pend_list(chat)
            if not lst:
                return "Порожньо", self.pend_screen(chat)
            if self.busy(chat):
                return "⏳ Ще завантажую — запропоную, щойно закінчу", None
            if len(lst) == 1:
                return self.start_quick(chat, lst[0]["url"], lst[0].get("instr")) or "🔎 Шукаю…", None
            tok = self.group_offer_pairs(chat, [(e["url"], e.get("instr"), "") for e in lst])
            return None, self.group_screen(chat, tok)
        if a == "pdel" and x.isdigit():
            lst = self.pend_list(chat)
            if 0 < int(x) <= len(lst):
                del lst[int(x) - 1]
                self._pend_save(chat, lst)
            return "🗑 Прибрано", self.pend_screen(chat)
        if a == "pclr":
            self._pend_save(chat, [])
            return "🗑 Пам'ять очищено", self.pend_screen(chat)
        if a == "grl":
            return None, self.group_list(chat, int(x) if x.isdigit() else None)
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
        if a == "selft":  # 🧪 чи працюють розшифровка мови, зір ШІ й Shazam — те, від чого залежать аналіз, субтитри й попередження
            self._bg(chat, self.self_test(chat))
            return "🧪 Перевіряю…", (self.live_screen(chat) if self.in_app() else None)
        if a == "chk":
            if self.busy(chat):
                return "⏳ Попереднє завдання ще триває", None
            self.live[chat] = ("🩺 Перевіряю сайти…", [])
            self._bg(chat, self.check_sites(chat))
            return "🩺 Перевіряю…", (self.live_screen(chat) if self.in_app() else None)
        if a == "upd" and self.host.is_admin(chat):
            self.__dict__.get("_ver", {}).clear()  # після оновлення — свіжі версії
            self._bg(chat, self.update_tools(chat))
            return "🔄 Оновлюю…", None
        if a == "stop":
            task = self.tasks.get(chat)
            if task and not task.done():
                self.user_stop.add(chat)
                task.cancel()
                return "⏹ Зупиняю", self.home(chat)
            return "Активного завдання немає", self.home(chat)
        if a == "off" and x in self.offers:  # старі кнопки «Знайти й завантажити» з попередньої версії
            c, url = self.offers.pop(x)
            return self.start_quick(chat, url) or "🔎 Шукаю…", (self.live_screen(chat) if self.in_app() else None)
        if a in ("og", "ox", "oa", "os", "osv", "oso", "osm", "opt", "ofm"):
            job = self.job_by(chat, x)
            if not job:
                return self.revive(chat, x)
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
        if a == "stu" and y in ("safe", "subs", "clips"):  # 🎬 студія для скачаного відео
            job = self.job_by(chat, x)
            if not job:
                return self.revive(chat, x)
            if self.busy(chat):
                return "⏳ Ще завантажую попереднє", None
            self._bg(chat, self.studio_go(chat, job, y))
            return "🎬 Починаю", (self.live_screen(chat) if self.in_app() else None)
        if a in ("pil", "pi", "pim", "pif", "pia", "pir"):  # 🔢 окремо для кожного файлу
            job = self.job_by(chat, x)
            if not job:
                return self.revive(chat, x)
            if a == "pil":
                return None, self.per_screen(chat, job)
            if a == "pir":
                job.__dict__["per"] = {}
                return "♻️ Скинуто", self.per_screen(chat, job)
            n = int(y) if str(y).isdigit() else 0
            if not 1 <= n <= len(job.items):
                return "Такого файлу немає", None
            if a == "pi":
                return None, self.per_item_screen(chat, job, n)
            if a == "pim":
                m = args[3] if len(args) > 3 else "-"
                p = self.per(job, n)
                if (p.get("mode") == "audio") != (m == "audio"):
                    p.pop("fmt", None)  # звук ↔ відео: формат попереднього виду вже не підходить
                if m == "-":
                    p.pop("mode", None)
                else:
                    p["mode"] = m
                if not p:
                    self.per(job).pop(n, None)
                return None, self.per_item_screen(chat, job, n)
            if a == "pif":
                kind = self.item_kind(job, n)
                fm = self.file_formats(job, 1 if kind == "vid" else 0, [n] if kind == "img" else [])
                p = self.per(job, n)
                if kind == "aud" or p.get("mode") == "audio":
                    fm = ["", *M.AUDIO_FMTS, "orig"]
                cur = p.get("fmt", job.options.fmt if job.options.fmt in fm else "")
                p["fmt"] = fm[(fm.index(cur) + 1) % len(fm)] if cur in fm else fm[0]
                return None, self.per_item_screen(chat, job, n)
            if a == "pia":
                what = args[3] if len(args) > 3 else ""
                if what == "sep":
                    sub = self.sub_job(job, n)
                    return None, self.sep_screen(sub)
                if self.busy(chat):
                    return "⏳ Ще завантажую попереднє", None
                sub = self.sub_job(job, n)
                if what == "mus":
                    self._bg(chat, self.post_audio(chat, sub))
                elif what == "rec":
                    self._bg(chat, self.recognize(chat, sub))
                return "⬇️ Починаю", (self.live_screen(chat) if self.in_app() else None)
        if a in ("fau", "fab", "fvf", "fvs"):
            job = self.job_by(chat, x)
            if not job:
                return self.revive(chat, x)
            return await self.fmt_extra(chat, job, a, y)
        if a in ("fq", "fa", "fp", "ff", "fr", "fall", "sp", "spn"):
            job = self.job_by(chat, x)
            if not job:
                return self.revive(chat, x)
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
        if a == "cr" and x.isdigit() and y.isdigit():  # 🎠 ◀️ ▶️
            car = self._car_load().get(f"{chat}:{x}")
            if not car:
                return "Це повідомлення вже не гортається", None
            i = int(y) % len(car["files"])
            try:
                await self._car_show(chat, int(x), car["files"], i, car["cap"], car.get("kb"))
            except Exception as exc:  # noqa: BLE001
                if "not modified" in str(exc).lower():
                    return f"{i + 1}/{len(car['files'])}", None
                log.info("гортання не вдалося", exc_info=True)
                return "Не вдалося перегорнути — спробуйте ще раз", None
            return f"{i + 1}/{len(car['files'])}", None
        if a == "pz":  # ⏸ / ▶️
            c = self.ctl.setdefault(chat, {})
            c["pause"] = not c.get("pause")
            ft = self.fetching.get(chat)
            if c["pause"] and ft and not ft.done():
                c["act"] = "pause"
                ft.cancel()  # поточний файл зупиняємо; після «▶️» він завантажиться наново
            return ("⏸ Пауза — інше не зачеплено" if c["pause"] else "▶️ Продовжую"), None
        if a == "skp":  # ⏭ лише поточний файл / допис
            c = self.ctl.setdefault(chat, {})
            c["pause"] = False
            ft = self.fetching.get(chat)
            if ft and not ft.done():
                c["act"] = "skip"
                ft.cancel()
                return "⏭ Пропускаю цей — решта йде далі", None
            return "Зараз нічого не завантажується", None
        if a == "grq":
            return None, self.group_queue(chat)
        if a == "grh" and x.isdigit():  # ⏸/▶️ окремий допис групи
            held = self.ctl.setdefault(chat, {}).setdefault("held", set())
            i = int(x)
            if i in held:
                held.discard(i)
                toast = f"▶️ №{i + 1} повернуто в чергу"
            else:
                held.add(i)
                toast = f"⏸ №{i + 1} відкладено — решта качається далі"
                g = self.gctx.get(chat)
                ft = self.fetching.get(chat)
                if g and g.get("i") == i and ft and not ft.done():  # саме він зараз качається — зупиняємо лише його
                    self.ctl[chat]["act"] = "skip"
                    self.ctl[chat]["requeue"] = i
                    ft.cancel()
            return toast, self.group_queue(chat)
        if a == "grd" and x.isdigit():  # ✖️ окремий допис групи
            g = self.gctx.get(chat)
            i = int(x)
            if g and i in g.get("left", []):
                if g.get("i") == i:
                    ft = self.fetching.get(chat)
                    if ft and not ft.done():
                        self.ctl.setdefault(chat, {})["act"] = "skip"
                        ft.cancel()
                else:
                    g["left"].remove(i)
                g.setdefault("cancelled", []).append(i)
            return f"✖️ №{i + 1} скасовано — решта йде далі", self.group_queue(chat)
        if a == "snd":  # 🔁 людина погодилась повторити завантаження зі звуком
            job = self.jobs.get(chat)
            if not job or job.token != x:
                return "Це завантаження вже застаріло — надішліть посилання ще раз", None
            if self.busy(chat):
                return "⏳ Ще завантажую — дочекайтесь", None
            job.options.__dict__["need_audio"] = True
            self._bg(chat, self.download(chat, job, list(range(1, len(job.items) + 1))))
            return "🔁 Завантажую ще раз — лише формати зі звуком", None
        if a == "force" and x in self.offers:  # ⬇️ людина свідомо хоче повторно (напр. інший формат)
            return self.start_quick(chat, self.offers[x][1], self.instr.get(chat) or None, force=True) or "🔎 Шукаю…", None
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
        if key == "fnd":
            return None, self.find_screen(chat, text)
        if key == "tadd":
            toast = await self.ted_apply(chat, int(data), text)
            return toast, self.ted_screen(chat, int(data))
        if key == "grt":
            tok, num = str(data).split(":")
            g = self.gpend.get(tok)
            if not g:
                return "Група застаріла", self.home(chat)
            g["items"][int(num) - 1]["own"] = None if text.strip() in ("-", "—") else G.own_tags(text)
            return "✍️ Записано", self.group_screen(chat, tok)
        return await self._on_text(update, ctx, chat, key, data, text)

    async def _on_text(self, update, ctx, chat, key, data, text):
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
        m_find = re.match(r"(?is)\s*(?:знайди|пошукай|покажи|шукай)\s+(?:мені\s+)?(?:(?:усе|все|всі|усі)\s+)?"
                          r"(?:скачан\w*|завантажен\w*)\s*(?:відео|дописи|файли)?\s*(.*)", msg.text)
        if m_find and not G.URL.search(msg.text):  # 🔎 «знайди скачане про снігопад з Карпат»
            await self.host.notify(msg.chat_id, *self.find_screen(msg.chat_id, m_find.group(1) or ""))
            return True
        r0 = msg.reply_to_message
        if r0 and not G.URL.search(msg.text) and len(msg.text) < 200:  # ✏️ відповідь на скачаний файл: «тег снігопад»
            e = self.hist.by_msg(msg.chat_id, r0.message_id)
            if e and re.search(r"(?i)тег|категор|сортуй|групуй|прибери|видали|зніми|#", msg.text):
                toast = await self.ted_apply(msg.chat_id, e["id"], msg.text)
                await self.host.notify(msg.chat_id, *(lambda t, r: (f"{toast}\n\n{t}", r))(*self.ted_screen(msg.chat_id, e["id"])))
                return True
        if len(G.URL.findall(msg.text)) >= 2 and not self.host.extra.get("awaiting", lambda c: False)(msg.chat_id) \
                and not self.host.extra.get("utm_screen", lambda c: False)(msg.chat_id):  # 📦 кілька посилань — один вибір на всі
            tok = self.group_offer(msg.chat_id, msg.text)
            if tok:
                await self.host.notify(msg.chat_id, *self.group_screen(msg.chat_id, tok))
                return True
        m = URL_ONLY.match(msg.text)
        instr = None
        if not m:  # посилання + вказівки про теги: «сортуй по тегу полісся», «без тегів»
            mm = re.search(r"https?://\S+", msg.text)
            instr = R.parse_instr(msg.text) if mm and len(re.findall(r"https?://", msg.text)) == 1 else None
            m = (None, mm[0].rstrip(").,]»")) if instr else None
        if not m or self.host.extra.get("awaiting", lambda c: False)(msg.chat_id):
            return False
        if not is_social(m[1]):  # інші сайти — бот питає, що робити (🧭 пропозиції)
            return False
        if self.host.extra.get("utm_screen", lambda c: False)(msg.chat_id):  # у меню «UTM-мітки» посилання — для мітки, не для завантаження
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
            data = bytes(await (await msg.document.get_file()).download_as_bytearray())  # і в чаті, і в Mini App
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
            shutdown = False
            try:
                await coro
            except asyncio.CancelledError:
                if chat not in self.user_stop:  # бот вимикається (оновлення) — не стираємо, після старту продовжимо
                    shutdown = True
                    raise
                self.user_stop.discard(chat)
                g = self.gctx.pop(chat, None)
                self.gmsg.pop(chat, None)
                if chat not in self.just_done and self.cur_url.get(chat):  # зупинили до кінця — посилання збережемо
                    self.pend_keep(chat, self.cur_url[chat], "завантаження зупинено")
                for i in (g or {}).get("left", [])[1:]:  # решта групи — теж у збережені
                    self.pend_keep(chat, g["items"][i]["url"], "група зупинена")
                await self.host.notify(chat, "⏹ Завдання зупинено." + (" Посилання збережено в «📌 Збережені» — нагадаю."
                                                                        if self.pend_list(chat) else ""))
            except JobError as exc:
                await self.host.notify(chat, f"⚠️ {esc(str(exc))[:3500]}")
            except Exception as exc:  # noqa: BLE001
                log.exception("media job failed")
                if self.cur_url.get(chat) and chat not in self.just_done:  # збій бота чи мережі — не губимо
                    self.pend_keep(chat, self.cur_url[chat], f"збій: {type(exc).__name__}")
                await self.host.notify(chat, f"⚠️ Не вдалося: {esc(type(exc).__name__)}. Посилання збережено в «📌 Збережені» — нагадаю.")
            finally:
                t_ = self.ticks.pop(chat, None)
                if t_:
                    t_.cancel()
                if not shutdown:
                    self._active(chat, done=True)
                    if not self.gctx.get(chat):
                        asyncio.get_running_loop().create_task(self._remind_later(chat))
        # Чистий контекст: якщо завдання почали з Mini App, його повідомлення й файли мають іти в справжній чат,
        # а не в «перехоплювач» запиту застосунку, який уже завершився.
        task = asyncio.get_running_loop().create_task(wrap(), context=contextvars.Context())
        self.tasks[chat] = task

    # ---------------------------------------------------------------- 📌 посилання, надіслані під час іншого завантаження
    PEND_MAX = 50

    def _pend(self):
        try:
            return json.loads((self.dir / "pending.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def pend_list(self, chat):
        return self._pend().get(str(chat), [])

    def _pend_save(self, chat, lst):
        data = self._pend()
        if lst:
            data[str(chat)] = lst
        else:
            data.pop(str(chat), None)
        (self.dir / "pending.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    TRANSIENT = re.compile(r"(?i)timeout|timed out|тайм|час очікування|не відповіда|мереж|network|connect|з'єднан|502|503|504|429|"
                           r"temporar|тимчасов|reset by peer|ssl|dns|unreachable|забагато запитів")

    def pend_keep(self, chat, url, why):
        """Збій мережі, бота чи випадкова зупинка — посилання не губиться: у збережені, з причиною."""
        if not url:
            return
        lst = [e for e in self.pend_list(chat) if R.norm_url(e["url"]) != R.norm_url(url)]
        lst.append({"url": url, "instr": self.instr.get(chat) or None, "ts": int(time.time()), "why": why[:120]})
        self._pend_save(chat, lst[-self.PEND_MAX:])
        self.pend_seen.pop(chat, None)

    def pend_add(self, chat, pairs):
        """Зберегти посилання (до 50; понад ліміт — найдавніші видаляються). -> текст для людини."""
        lst = self.pend_list(chat)
        have = {e["url"] for e in lst}
        added = 0
        skipped = []
        for url, instr in pairs:
            if self.same_running(chat, url):
                skipped.append("⏳ " + url[:60] + " — уже завантажується")
                continue
            if self.hist.by_url(chat, url):
                skipped.append("♻️ " + url[:60] + " — уже скачувалось")
                continue
            if url not in have:
                lst.append({"url": url, "instr": instr or None, "ts": int(time.time())})
                have.add(url)
                added += 1
        dropped = max(0, len(lst) - self.PEND_MAX)
        lst = lst[dropped:]
        self._pend_save(chat, lst)
        self.pend_seen.pop(chat, None)  # нове — нагадаємо одразу після поточного
        msg = (f"⏳ Ще завантажую попереднє. 📌 Посилання збережено" + (f" ({added})" if added > 1 else "")
               + f" — у пам'яті {len(lst)} з {self.PEND_MAX}. Щойно закінчу — запропоную завантажити"
               + (" всі разом груповим завантаженням." if len(lst) > 1 else "."))
        if skipped:
            msg += "\nНе додано:\n" + "\n".join(esc(x) for x in skipped)
            if not added:
                msg = "Нічого нового не додано:\n" + "\n".join(esc(x) for x in skipped)
        if dropped:
            msg += f"\n⚠️ Перевищено ліміт {self.PEND_MAX}: найдавніші {dropped} посилань видалено."
        elif len(lst) >= self.PEND_MAX - 5:
            msg += f"\n⚠️ Пам'ять майже повна: понад {self.PEND_MAX} найдавніші посилання видалятимуться."
        return msg

    def pend_done(self, chat, url):
        lst = self.pend_list(chat)
        if any(e["url"] == url for e in lst):
            self._pend_save(chat, [e for e in lst if e["url"] != url])

    def pend_screen(self, chat):
        lst = self.pend_list(chat)
        if not lst:
            return "📌 <b>Збережені посилання</b>\n\nПорожньо — усе завантажено.", [[("« Медіа", self.cb())]]
        lines = [f"📌 <b>Збережені посилання · {len(lst)} з {self.PEND_MAX}</b>",
                 "Надіслані, поки бот завантажував інше, а також ті, що не почались через збій мережі чи бота або зупинку (⚠️ причина). "
                 "Лишаються в пам'яті, доки не завантажите (бот нагадуватиме) або не приберете 🗑. "
                 f"Понад {self.PEND_MAX} — найдавніші видаляються.", ""]
        for i, e in enumerate(lst, 1):
            lines.append(f"{i}. {R.icon(M.site_name(e['url']))} <code>{esc(e['url'][:70])}</code> · {esc(R.ts_text(e['ts'])[:16])}"
                         + (f"\n    ⚠️ {esc(e['why'])}" if e.get("why") else ""))
        rows = [[(f"⬇️ Завантажити" + (f" усі · {len(lst)} (груповим)" if len(lst) > 1 else ""), self.cb("pgo"))]]
        dels = [(f"🗑 {i}", self.cb("pdel", i)) for i in range(1, min(len(lst), 20) + 1)]
        rows += [dels[k:k + 5] for k in range(0, len(dels), 5)]
        rows += [[("🗑 Очистити все", self.cb("pclr"))], [("« Медіа", self.cb())]]
        return "\n".join(lines)[:4000], rows

    async def pend_remind(self, chat):
        """Після завершення завантаження: є збережені посилання — пропонуємо (повторно — не частіше ніж раз на 20 хв)."""
        if chat not in self.just_done:  # лише після справжнього завершеного завантаження, не посеред вибору
            return
        self.just_done.discard(chat)
        lst = self.pend_list(chat)
        if not lst or self.busy(chat):
            return
        last = self.pend_seen.get(chat)
        if last and time.time() - last < 1200:
            return
        self.pend_seen[chat] = time.time()
        again = " (нагадую)" if last else ""
        if len(lst) == 1:
            t = (f"📌 У пам'яті посилання, яке ви надіслали під час завантаження{again}:\n<code>{esc(lst[0]['url'][:200])}</code>\n"
                 f"Воно лишиться, доки не завантажите. Пам'ять — до {self.PEND_MAX} посилань, понад ліміт найдавніші видаляються.")
            rows = [[("⬇️ Завантажити", self.cb("pgo"))], [("📌 Збережені", self.cb("pend")), ("🗑 Прибрати", self.cb("pclr"))]]
            await self.host.notify(chat, t, rows)
        else:
            tok = self.group_offer_pairs(chat, [(e["url"], e.get("instr"), "") for e in lst])
            t, rows = self.group_screen(chat, tok)
            head = (f"📌 У пам'яті {len(lst)} з {self.PEND_MAX} посилань, надісланих під час завантаження{again} — завантажити груповим? "
                    f"Вони лишаться, доки не завантажите; понад {self.PEND_MAX} найдавніші видаляються.\n\n")
            await self.host.notify(chat, head + t, rows[:-1] + [[("📌 Збережені", self.cb("pend")), ("✖️ Пізніше", self.cb("grx", tok))]])

    # ---------------------------------------------------------------- 🧯 биті посилання (окремо від збережених)
    BAD_MAX = 200

    def _bad(self):
        try:
            return json.loads((self.dir / "failed.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def bad_list(self, chat):
        return self._bad().get(str(chat), [])

    def bad_add(self, chat, url, why):
        """Посилання, з якого нічого не вийшло, — з причиною. До 200; найстаріші видаляються самі."""
        if not url:
            return
        data = self._bad()
        lst = [e for e in data.get(str(chat), []) if e["url"] != url]
        lst.append({"url": url, "why": re.sub(r"<[^>]+>", "", str(why))[:300], "ts": int(time.time()), "site": M.site_name(url)})
        data[str(chat)] = lst[-self.BAD_MAX:]
        try:
            (self.dir / "failed.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        except OSError:
            pass

    def bad_ok(self, chat, url):
        """Згодом вдалося — позначаємо ✅ (запис лишається для історії)."""
        data = self._bad()
        changed = False
        for e in data.get(str(chat), []):
            if e["url"] == url and not e.get("ok"):
                e["ok"] = int(time.time())
                changed = True
        if changed:
            (self.dir / "failed.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def bad_screen(self, chat, page=0):
        lst = list(reversed(self.bad_list(chat)))
        if not lst:
            return "🧯 <b>Биті посилання</b>\n\nПорожньо — усе завантажувалось.", [[("« Медіа", self.cb())]]
        per = 8
        page = max(0, min(page, (len(lst) - 1) // per))
        part = lst[page * per:(page + 1) * per]
        lines = [f"🧯 <b>Биті посилання · {len(lst)} з {self.BAD_MAX}</b>",
                 f"<blockquote expandable>ℹ️ Що це\nПосилання, з яких бот нічого не зміг завантажити, і чому. Окремо від «📌 Збережених» "
                 f"(тих, що ще не пробували). Понад {self.BAD_MAX} — найстаріші видаляються самі. «✅» — згодом вдалося.</blockquote>", ""]
        rows = []
        for k, e in enumerate(part, page * per + 1):
            lines.append(f"{k}. {'✅ ' if e.get('ok') else ''}{R.icon(e.get('site'))} {esc(R.ts_text(e['ts'])[:16])}\n"
                         f"🔗 {esc(e['url'][:200])}\n❓ {esc(e['why'][:200])}")
            if not e.get("ok"):
                rows.append([(f"🔁 №{k} ще раз", self.cb("badre", k))])
        nav = []
        if page:
            nav.append(("◀️", self.cb("bad", page - 1)))
        if (page + 1) * per < len(lst):
            nav.append(("▶️", self.cb("bad", page + 1)))
        if nav:
            rows.append(nav)
        rows += [[("🗑 Очистити", self.cb("badclr"))], [("« Медіа", self.cb())]]
        return "\n\n".join(["\n".join(lines[:3])] + lines[3:])[:4000], rows

    # ---------------------------------------------------------------- 📦 групове завантаження
    def group_offer(self, chat, text):
        """Кілька посилань одним повідомленням -> токен групи, що чекає вибору (або None)."""
        sp = G.split(text)
        if not sp:
            return None
        common, items = sp
        return self.group_offer_pairs(chat, items, common)

    def group_offer_pairs(self, chat, items, common=None):
        tok = secrets.token_hex(3)
        self.gpend[tok] = {"chat": chat, "common": common, "mode": "auto",
                           "items": [{"url": u, "own": ins, "raw": raw, "mode": ""} for u, ins, raw in items[:30]]}
        while len(self.gpend) > 50:
            self.gpend.pop(next(iter(self.gpend)))
        return tok

    def group_screen(self, chat, tok):
        g = self.gpend.get(tok)
        if not g:
            return "📦 Ця група вже запущена або застаріла — надішліть посилання ще раз.", [[("« Медіа", self.cb())]]
        n = len(g["items"])
        word = "допис" if n % 10 == 1 and n % 100 != 11 else ("дописи" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "дописів")
        lines = [f"📦 <b>Групове завантаження · {n} {word}</b>",
                 "<blockquote expandable>ℹ️ Як це працює\nОбираєте один раз — бот завантажує всі по черзі, кожен окремим дописом із "
                 "власним підписом. Усі позначаються «📦 Група №…» і мають посилання на попередній із групи; в архіві — «📦 Групи». "
                 "Вигляд: «Як є» — відео як відео, фото як фото. Свій вигляд для допису — кнопка з його номером (по колу). "
                 "Теги: свої — кнопкою «✍️ №…» або текстом біля посилання («сортуй по тегу полісся», «без тегів»); "
                 "без них — бот сам призначить теги кожному за тим, що знайде в дописі.</blockquote>", ""]
        if g["common"]:
            lines.append(f"Для всіх: {esc(G.instr_short(g['common']))}\n")
        for i, it in enumerate(g["items"], 1):
            mode = it["mode"] or g["mode"]
            ins = G.merge_instr(g["common"], it["own"])
            dup = " · ♻️ уже скачувалось — пропущу" if self.hist.by_url(chat, it["url"]) else ""
            lines.append(f"{i}. {R.icon(M.site_name(it['url']))} {G.MODE_ICON[mode]} <code>{esc(it['url'][:60])}</code>{dup}\n    {esc(G.instr_short(ins))}")
        rows = [[(("✅ " if g["mode"] == k else "") + lab, self.cb("grm", tok, k)) for k, lab in G.MODES[:3]],
                [(("✅ " if g["mode"] == k else "") + lab, self.cb("grm", tok, k)) for k, lab in G.MODES[3:]]]
        per = [(f"{i} {G.MODE_ICON[it['mode'] or g['mode']]}", self.cb("gri", tok, i)) for i, it in enumerate(g["items"], 1)]
        rows += [per[k:k + 5] for k in range(0, len(per), 5)]
        tg = [(f"✍️ №{i}", self.cb("grt", tok, i)) for i in range(1, n + 1)]
        rows += [tg[k:k + 5] for k in range(0, len(tg), 5)]
        rows.append([(f"▶️ Завантажити всі · {n}", self.cb("grgo", tok))])
        rows.append([("✖️ Скасувати", self.cb("grx", tok))])
        return "\n".join(lines)[:4000], rows

    async def group_run(self, chat):
        """По черзі: кожне посилання — самостійне завантаження з позначкою групи."""
        g = self.gctx[chat]
        n = len(g["items"])
        gst = None
        while g.get("left"):
            c = self.ctl.setdefault(chat, {})
            while c.get("pause"):  # ⏸ пауза всієї групи — між дописами
                await asyncio.sleep(1.5)
            held = c.setdefault("held", set())
            ready = [x for x in g["left"] if x not in held]
            if not ready:  # лишились тільки відкладені — у «📌 Збережені», група завершується
                for x in list(g["left"]):
                    self.pend_keep(chat, g["items"][x]["url"], "відкладено в груповому завантаженні")
                    g.setdefault("held_saved", []).append(x)
                g["left"] = []
                break
            i = ready[0]
            g["i"] = i
            it = g["items"][i]
            done_n = n - len(g["left"])
            gst = await self._status(chat, gst, f"📦 <b>Група №{g['id']}</b> · {done_n} з {n} готово · зараз №{i + 1}\n"
                                     f"📦 Уся група:\n{pbar(done_n / n, '🟪')}", self.ctl_rows(chat))
            g["mid"] = getattr(gst, "message_id", 0)
            self.gmsg[chat] = gst  # одне повідомлення на всю групу: статус кожного допису — в ньому ж
            self.instr[chat] = it.get("instr") or {}
            self.last_result.pop(chat, None)
            why = ""
            old = self.hist.by_url(chat, it["url"])
            if old and not it.get("force"):  # ♻️ уже скачували — не дублюємо
                g.setdefault("dups", []).append({"n": i + 1, "id": old["id"], "when": R.ts_text(old.get("ts"))})
                g["left"].remove(i)
                self._active(chat, done=True)
                continue
            try:
                await self.group_one(chat, it["url"], it.get("mode") or "auto")
                n_sent, fl = self.last_result.get(chat, (1, []))
                if not n_sent:
                    why = "; ".join(re.sub(r"<[^>]+>", "", f) for f in fl[:3]) or "файлів не знайшлося"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — одна невдача не зупиняє групу
                log.exception("group item failed")
                if isinstance(exc, JobError):
                    nice, raw = M.explain(exc), re.sub(r"<[^>]+>", "", str(exc))[:200]
                    why = nice if raw in nice else f"{nice} ({raw})"
                else:
                    why = f"{type(exc).__name__}: {str(exc)[:150]}"
                if self.TRANSIENT.search(why) or not isinstance(exc, JobError):  # збій мережі чи бота — у збережені
                    self.pend_keep(chat, it["url"], "збій: " + why[:80])
                else:
                    self.bad_add(chat, it["url"], why)
            if i in g.get("cancelled", []) or i in held:  # ✖️ скасували чи ⏸ відклали саме його — це не помилка
                why = ""
            if why:
                g.setdefault("failed", []).append({"n": i + 1, "url": it["url"], "why": re.sub(r"<[^>]+>", "", why)[:300]})
            if i in g["left"] and i not in held:
                g["left"].remove(i)
            self._active(chat, done=True)  # черга збережена — після перезапуску продовжимо
        fails = [f if isinstance(f, dict) else {"n": "?", "url": "", "why": str(f)} for f in g.get("failed", [])]
        ok_n = n - len(fails) - len(g.get("cancelled", [])) - len(g.get("held_saved", []))
        text = f"{'✅' if not fails else '⚠️'} <b>Група №{g['id']}</b> · готово {ok_n} з {n}"
        if fails:
            text += "\n\n<b>Не вийшло:</b>"
            for f in fails:
                text += f"\n\n№{f['n']} · 🔗 {esc(f['url'][:200])}\n❓ {esc(f['why'])}"
            text += "\n\nУсі такі посилання — у «🧯 Биті посилання» (там можна спробувати ще раз)."
        if g.get("cancelled"):
            text += "\n\n✖️ Скасовано: " + ", ".join(f"№{x + 1}" for x in g["cancelled"])
        if g.get("held_saved"):
            text += "\n⏸ Відкладені — у «📌 Збережені»: " + ", ".join(f"№{x + 1}" for x in g["held_saved"])
        dups = g.get("dups", [])
        if dups:
            text += "\n\n♻️ <b>Уже скачувались раніше</b> (повторно не завантажував):"
            for d in dups:
                url = self.mh_link(d["id"])
                text += f"\n№{d['n']} · " + (f'<a href="{esc(url)}">той допис</a>' if url else "той допис") + f" · {esc(d['when'])}"
        rows = [[("📦 Відкрити групу в архіві", self.cb("grl", g["id"]))]]
        if fails:
            rows.append([("🧯 Биті посилання", self.cb("bad"))])
        await self._status(chat, gst, text[:4000], rows + [[("« Медіа", self.cb())]])
        self.gctx.pop(chat, None)
        self.gmsg.pop(chat, None)
        self.ctl.pop(chat, None)
        self.instr.pop(chat, None)

    async def group_one(self, chat, url, mode):
        """Одне посилання групи без жодних питань: знайти й одразу завантажити в обраному вигляді."""
        src = M.describe_source(url)
        self.cur_url[chat] = url
        status = await self._status(chat, self.gmsg.get(chat), f"🔎 Шукаю: <b>{esc(src)}</b>…")
        url = await M.resolve_share(url)
        with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
            items = await M.inspect_link(url, Path(work))
        job = Job(secrets.token_hex(4), url, items[:QUICK_MAX])
        dq = self.pref(chat, "q", "ask")
        job.options.height = int(dq) if dq.isdigit() and dq != "0" else (0 if dq == "0" else 1080)
        if mode in ("video", "audio", "gif", "mute"):
            job.options.mode = mode
        elif dq == "a":
            job.options.mode = "audio"
        job.__dict__["src"] = src
        self.jobs[chat] = job
        await self.download(chat, job, list(range(1, len(job.items) + 1)), status=status)

    # ---------------------------------------------------------------- 1 крок: посилання → файл
    def start_quick(self, chat, url, instr=None, force=False):
        """Повертає текст лише якщо почати не можна; інакше все показує саме повідомлення-статус.
        instr — вказівки людини про теги («сортуй по тегу полісся», «без тегів»); force — завантажити навіть повторно."""
        if self.busy(chat) and self.same_running(chat, url):  # те саме вже вантажиться — повтор просто ігноруємо
            return "⏳ Це посилання вже завантажується просто зараз — повтор пропускаю."
        if not force:
            old = self.hist.by_url(chat, url)
            if old:  # ♻️ уже скачували — показуємо той допис замість повторного завантаження
                self._bg_soft(self.already(chat, old, url, instr))
                return ""  # повідомлення надсилає сам already()
        if self.busy(chat):  # 📌 не губимо: запам'ятовуємо й запропонуємо, щойно закінчимо
            return self.pend_add(chat, [(url, instr)])
        self.instr[chat] = instr or {}
        self.cur_url[chat] = url
        self.force_now[chat] = force
        if not self.gctx.get(chat):
            self.ctl[chat] = {}  # нове одиночне завантаження — без старих пауз
        self.just_done.discard(chat)
        self.live[chat] = (f"🔎 Шукаю: {esc(M.describe_source(url))}…", [])
        self._bg(chat, self.quick(chat, url))
        return None

    def ctl_rows(self, chat, n_files=1):
        """Кнопки під статусом: пауза / пропустити — лише цей файл чи допис, решта йде далі; зупинити все."""
        c = self.ctl.get(chat) or {}
        g = self.gctx.get(chat)
        rows = []
        if n_files > 1 or g:
            rows.append([("▶️ Продовжити" if c.get("pause") else "⏸ Пауза", self.cb("pz")),
                         ("⏭ Пропустити цей", self.cb("skp"))])
        if g:
            rows.append([("🗂 Черга групи: пауза й скасування окремих", self.cb("grq"))])
            rows.append([("✖️ Скасувати групу", self.cb("stop"))])
        else:
            rows.append([("⏹ Зупинити", self.cb("stop"))])
        return rows

    async def _wait_resume(self, chat, stage, head=""):
        """⏸ Пауза: показуємо, що стоїмо, і чекаємо «▶️ Продовжити» (або зупинки)."""
        shown_once = False
        while (self.ctl.get(chat) or {}).get("pause"):
            if not shown_once:
                await stage(f"⏸ <b>На паузі</b> — натисніть «▶️ Продовжити»\n{head}", force=True)
                shown_once = True
            await asyncio.sleep(1.5)

    def group_queue(self, chat):
        g = self.gctx.get(chat)
        if not g:
            return "🗂 Групового завантаження зараз немає.", [[("« Медіа", self.cb())]]
        c = self.ctl.setdefault(chat, {})
        held = c.setdefault("held", set())
        lines = [f"🗂 <b>Черга групи №{g['id']}</b>",
                 "Пауза чи скасування окремого допису не зупиняє решту: інші качаються далі. Відкладені (⏸) після решти підуть у «📌 Збережені».", ""]
        rows = []
        for i in g.get("left", []):
            it = g["items"][i]
            mark = "⬇️ зараз" if i == g.get("i") else ("⏸ відкладено" if i in held else "⏳ у черзі")
            lines.append(f"№{i + 1} · {R.icon(M.site_name(it['url']))} {mark}\n<code>{esc(it['url'][:70])}</code>")
            rows.append([(("▶️ №" if i in held else "⏸ №") + str(i + 1), self.cb("grh", i)), (f"✖️ №{i + 1}", self.cb("grd", i))])
        rows.append([("« До завантаження", self.cb("st"))])
        return "\n".join(lines)[:4000], rows

    def same_running(self, chat, url):
        k = R.norm_url(url)
        cur = {R.norm_url(u) for u in (self.cur_url.get(chat), getattr(self.jobs.get(chat), "url", None)) if u}
        g = self.gctx.get(chat)
        if g:
            cur |= {R.norm_url(g["items"][i]["url"]) for i in g.get("left", [])}
        return k in cur

    def _bg_soft(self, coro):
        asyncio.get_running_loop().create_task(coro, context=contextvars.Context())

    async def already(self, chat, old, url, instr=None):
        """♻️ Повторне посилання: замість завантаження — той самий допис (відповідь на нього зі списком пов'язаного)."""
        t = secrets.token_hex(4)
        self.offers[t] = (chat, url)
        self.instr[chat] = instr or {}
        await self.jump(chat, old["id"],  # одним повідомленням: відповідь на той допис + меню
                        head=(f"♻️ <b>Це вже скачувалось</b> — повторно не завантажую.\n🕓 {esc(self.when_size(old))}\n"
                              "⬆️ Торкніться цитати, щоб перейти до того допису"),
                        extra_rows=[[("⬇️ Все одно завантажити ще раз", self.cb("force", t))]])

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
        new = await self.host.notify(chat, text, rows) or msg
        self._active_mid(chat, new)
        return new

    async def _remind_later(self, chat):
        await asyncio.sleep(2)
        try:
            await self.pend_remind(chat)
        except Exception:  # noqa: BLE001
            log.exception("pending remind failed")

    def _active_mid(self, chat, msg):
        """Статус переїхав у нове повідомлення — запам'ятати, щоб після перезапуску виправити саме його."""
        try:
            data = json.loads(self._active_path().read_text("utf-8"))
            if str(chat) in data and getattr(msg, "message_id", None):
                data[str(chat)][0] = msg.message_id
                self._active_path().write_text(json.dumps(data), "utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---- статуси, що пережили б перезапуск: бот відмічає, а після старту виправляє «завислі» повідомлення
    def usual_time(self, site):
        """Скільки зазвичай триває одне завантаження з цього сайту (середнє за попередніми), с."""
        try:
            d = json.loads((self.dir / "times.json").read_text("utf-8")).get(site)
        except (OSError, ValueError):
            d = None
        return int(d[0]) if d else 0

    def note_time(self, site, seconds, count=1):
        try:
            data = json.loads((self.dir / "times.json").read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        avg, n = data.get(site, [0, 0])
        per = seconds / max(1, count)
        n = min(n + 1, 20)  # ковзне середнє останніх ~20
        data[site] = [round(avg + (per - avg) / n, 1), n]
        try:
            (self.dir / "times.json").write_text(json.dumps(data), "utf-8")
        except OSError:
            pass

    def _active_path(self):
        return self.dir / "active.json"

    def _active(self, chat, status=None, url=None, done=False):
        try:
            data = json.loads(self._active_path().read_text("utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        g = self.gctx.get(chat)
        if done and g and g.get("left"):  # у групі ще є черга — пам'ятаємо її, щоб після перезапуску продовжити
            data[str(chat)] = [g.get("mid", 0), "", int(time.time()), {"grp": g}]
        elif done:
            data.pop(str(chat), None)
        elif status is not None:
            data[str(chat)] = [getattr(status, "message_id", 0), url or "", int(time.time()),
                               {"instr": self.instr.get(chat) or {}, "grp": g}]
        try:
            self._active_path().write_text(json.dumps(data), "utf-8")
        except OSError:
            pass

    async def start(self):
        """Після перезапуску (оновлення бота) перерване завантаження продовжується саме — і одиночне, і групове."""
        try:
            data = json.loads(self._active_path().read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return
        self._active_path().unlink(missing_ok=True)
        await asyncio.sleep(5)
        for chat, row in data.items():
            mid, url, ts = row[0], row[1], row[2]
            extra = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
            chat = int(chat)
            if time.time() - ts > 6 * 3600:
                continue  # надто давнє — не відновлюємо без людини
            try:
                await self.host.bot().edit_message_text("🔁 Бот оновлювався — продовжую завантаження сам.",
                                                        chat_id=chat, message_id=int(mid))
            except Exception:  # noqa: BLE001
                pass
            g = extra.get("grp")
            if g and g.get("items"):
                g["left"] = [i for i in g.get("left", []) if i < len(g["items"])] or ([g.get("i", 0)] if url else [])
                if url and g.get("i") is not None and g["i"] not in g["left"]:
                    g["left"].insert(0, g["i"])  # той, що перервався, — спочатку
                self.gctx[chat] = g
                self._bg(chat, self.group_run(chat))
            elif url:
                self.start_quick(chat, url, extra.get("instr") or None)

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
            old = self.hist.by_url(chat, url) if not self.force_now.pop(chat, False) else None
            if old:  # інше посилання на вже скачаний допис (напр. інший /share/…)
                tick.cancel()
                try:
                    await self.host.bot().delete_message(chat, status.message_id)
                except Exception:  # noqa: BLE001
                    pass
                self._active(chat, done=True)
                await self.already(chat, old, self.cur_url.get(chat) or url, self.instr.get(chat))
                return
            with tempfile.TemporaryDirectory(prefix="orion-media-") as work:
                items = await M.inspect_link(url, Path(work))
        except M.URLDenied as exc:
            self.bad_add(chat, self.cur_url.get(chat) or url, str(exc))
            await self._status(chat, status, f"⚠️ {esc(str(exc))}")
            self._active(chat, done=True)
            return
        except JobError as exc:
            if self.TRANSIENT.search(str(exc)):  # збій мережі — не «бите»: у збережені, нагадаємо
                self.pend_keep(chat, self.cur_url.get(chat) or url, "збій мережі: " + re.sub(r"<[^>]+>", "", str(exc))[:80])
            else:
                self.bad_add(chat, self.cur_url.get(chat) or url, str(exc))
            t = secrets.token_hex(4)
            self.offers[t] = (chat, url)
            rows = [[("🔁 Спробувати ще раз", self.cb("re", t))], [("🍪 Входи на сайти", self.cb("ck")), ("« Медіа", self.cb())]]
            if "APIFY_TOKEN" in str(exc) and "не підходить" not in str(exc):
                rows.insert(0, [("🔑 Вставити ключ Apify тут", self.cb("akey"))])
            await self._status(chat, status, (f"⚠️ <b>Не вийшло завантажити</b> · {esc(src)}\n🔗 {esc(self.cur_url.get(chat) or url)}"
                                              f"\n\n❓ {exc}\n\n🧯 Збережено в «Биті посилання»")[:4000], rows)
            self._active(chat, done=True)
            return
        finally:
            tick.cancel()
            M.NOTE.reset(token)
        job = Job(secrets.token_hex(4), url, items[:300])
        job.options.height = 1080  # до 1080p, щоб не впертись у межу Telegram
        job.__dict__["src"] = src
        self.jobs[chat] = job
        self.remember_token(job.token, url)  # кнопки під дописом мають працювати й після оновлення бота
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
        await self.post_total(job)  # 💾 розмір усіх файлів допису разом
        await self.card_read(job)  # 🧾 Threads: музика, геотег, текст, позначки — з картки-прев'ю, чого нема в розмітці
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
    _esc = staticmethod(esc)

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

    # ---------------------------------------------------------------- ⚙️ окремо для кожного файлу допису
    PI_MODES = (("", "як усі"), ("video", "🎬 зі звуком"), ("mute", "🔇 без звуку"), ("audio", "🔊 лише звук"), ("skip", "⛔ не качати"))

    def per(self, job, n=None):
        d = job.__dict__.setdefault("per", {})
        return d if n is None else d.setdefault(int(n), {})

    def item_opts(self, job, n, base):
        """Налаштування для одного файлу: загальні + його власні (режим, формат)."""
        p = self.per(job).get(int(n)) or {}
        if not p:
            return base
        o = replace(base)
        if p.get("mode") in ("video", "mute"):
            o.mode = p["mode"]
        if "fmt" in p:
            o.fmt = p["fmt"]
        return o

    def item_kind(self, job, n):
        it = job.items[n - 1]
        return "img" if M.is_image_item(it) else "aud" if M.is_audio_item(it) else "vid"

    def per_label(self, job, n):
        p = self.per(job).get(n) or {}
        kind = self.item_kind(job, n)
        icon = {"img": "🖼", "aud": "🎵", "vid": "🎬"}[kind]
        bits = []
        if p.get("mode"):
            bits.append(dict(self.PI_MODES)[p["mode"]])
        if p.get("fmt"):
            bits.append(p["fmt"].upper())
        return f"{icon} {n}" + (" · " + " · ".join(bits) if bits else "")

    def per_screen(self, chat, job):
        """🔢 Кожен файл допису окремо: що качати й як — до завантаження."""
        vis, aud, _ = self.parts(job)
        nums = vis + aud
        tok = job.token
        lines = ["🔢 <b>Окремо для кожного файлу</b>", "",
                 "Натисніть файл, щоб задати саме йому: зі звуком чи без, лише звук, формат, не качати; "
                 "для відео — ще музика окремо, чистий звук, розпізнати музику. Решта — як у загальних налаштуваннях."]
        if self.total_line(job):
            lines.append(self.total_line(job))
        btns = [(self.per_label(job, n), self.cb("pi", tok, n)) for n in nums[:60]]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        go = [n for n in nums if (self.per(job).get(n) or {}).get("mode") != "skip"]
        rows.append([(f"⬇️ Завантажити вибране · {len(go)} з {len(nums)}", self.cb("og", tok))])
        if self.per(job):
            rows.append([("♻️ Скинути окремі налаштування", self.cb("pir", tok))])
        rows.append([("« Налаштування завантаження", self.cb("opt", tok))])
        return "\n".join(lines)[:4000], rows

    def per_item_screen(self, chat, job, n):
        it = job.items[n - 1]
        tok, kind, p = job.token, self.item_kind(job, n), self.per(job).get(n) or {}
        info = it.info or {}
        size = None
        if info.get("formats"):
            out, a_size = self.sizes(info)
            size = a_size if kind == "aud" else (out[-1][1] if out and out[-1][1] else None)
        size = size or info.get("filesize") or info.get("filesize_approx")
        lines = [f"{self.per_label(job, n)} · <b>{esc(it.title[:120])}</b>"]
        if size:
            lines.append(f"💾 ≈{mb(size)}")
        lines += ["", "Режим і формат — лише для цього файлу. Дії нижче («🎵», «🗣», «🔎») виконуються одразу й окремо."]
        cur = p.get("mode", "")
        modes = [m for m in self.PI_MODES if kind == "vid" or m[0] in ("", "skip")]
        rows = [[(("● " if cur == m else "○ ") + lab, self.cb("pim", tok, n, m or "-")) for m, lab in modes[i:i + 3]] for i in range(0, len(modes), 3)]
        fm = self.file_formats(job, 1 if kind == "vid" else 0, [n] if kind == "img" else [])
        if kind == "aud" or cur == "audio":  # звук — формати звуку
            fm = ["", *M.AUDIO_FMTS, "orig"]
        if len(fm) > 1:
            fcur = p.get("fmt", job.options.fmt if job.options.fmt in fm else "")
            rows.append([(f"📦 Формат цього файлу: {(fcur or 'як є').upper()} ▸", self.cb("pif", tok, n))])
        if kind == "vid":
            rows.append([("🎵 Музика окремо", self.cb("pia", tok, n, "mus")), ("🔎 Розпізнати", self.cb("pia", tok, n, "rec"))])
            rows.append([("🗣 Чистий звук ▸", self.cb("pia", tok, n, "sep"))])
        rows.append([("« Усі файли", self.cb("pil", tok))])
        return "\n".join(lines)[:4000], rows

    def sub_job(self, job, n, mode=None):
        """Окреме завдання з одного файлу допису — для дій «музика окремо», «чистий звук», «розпізнати»."""
        sub = Job(secrets.token_hex(4), job.url, [job.items[n - 1]])
        sub.__dict__["src"] = job.__dict__.get("src")
        sub.options = replace(job.options)
        if mode:
            sub.options.mode = mode
        self.jobs_extra = getattr(self, "jobs_extra", {})
        if len(self.jobs_extra) > 50:
            self.jobs_extra.pop(next(iter(self.jobs_extra)))
        self.jobs_extra[sub.token] = sub
        return sub

    CARD_SYSTEM = ("На картинці — картка-прев'ю допису з Threads (така сама, як прев'ю посилання в Telegram). Прочитай з неї лише те, що "
                   "справді написано: рядок музики — дрібний текст зі значком ▶ між текстом допису й фото/відео («Назва · Виконавець», "
                   "напр. «Twinkling Lights (Reimagined) · Auni»); великий текст допису, дата й час — НЕ музика; геотег під дописом (часто «Місто • City • Город» — "
                   "бери першу назву), ім'я автора, текст допису, лічильники під дописом (вподобання, коментарі, поширення), скільки фото/відео "
                   "видно (карусель), позначки на кшталт «AI info / Створено ШІ», «Paid partnership / Реклама». Відповідай лише JSON: "
                   '{"music":{"title":"","artist":""},"place":"","author":"","text":"","likes":0,"comments":0,"reposts":0,'
                   '"media":"photo|video|carousel|none","count":1,"ai_label":false,"paid":false}. Немає — порожньо чи 0. Нічого не вигадуй.')

    @staticmethod
    def card_parse(answer):
        m = re.search(r"\{.*\}", answer or "", re.S)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return {}
        mus = d.get("music") if isinstance(d.get("music"), dict) else {}
        num = lambda v: int(v) if str(v).isdigit() else 0  # noqa: E731
        return {"title": str(mus.get("title") or "").strip()[:120], "artist": str(mus.get("artist") or "").strip()[:80],
                "place": str(d.get("place") or "").strip()[:80], "author": str(d.get("author") or "").strip()[:60],
                "text": str(d.get("text") or "").strip()[:1500], "likes": num(d.get("likes")), "comments": num(d.get("comments")),
                "reposts": num(d.get("reposts")), "media": str(d.get("media") or ""), "count": num(d.get("count")),
                "ai_label": d.get("ai_label") is True, "paid": d.get("paid") is True}

    async def self_test(self, chat):
        """🧪 Самоперевірка розпізнавання: озвучуємо фразу й розшифровуємо її; малюємо напис і просимо ШІ прочитати; перевіряємо Shazam."""
        import base64
        import io
        import media as core
        lines = ["🧪 <b>Перевірка розпізнавання</b>", ""]
        status = await self._status(chat, None, "🧪 Перевіряю розшифровку мови, зір ШІ й Shazam… (до хвилини)", [[("⏹ Зупинити", self.cb("stop"))]])
        # 1) мова: TTS → STT
        keys = [("Groq", bool(getattr(core, "GROQ_KEY", ""))), ("Cloudflare", bool(getattr(core, "CF_TOKEN", "") and getattr(core, "CF_ACCOUNT", ""))),
                ("Gemini", bool(getattr(core, "GEMINI_KEY", ""))), ("OpenRouter (безкоштовні)", bool(getattr(core, "OPENROUTER_KEY", "")))]
        lines.append("🗣 Сервіси розшифровки: " + ", ".join(f"{'✅' if ok else '—'} {n}" for n, ok in keys))
        ogg = None
        try:
            ogg = await asyncio.wait_for(core.tts("Перевірка розпізнавання мови. Сорок два."), 40)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"⚠️ Не вдалося створити тестовий голос (озвучення): {esc(str(exc)[:120])} — розшифровку не перевірено")
        try:
            if ogg is None:
                raise LookupError
            text, prov, errs = await asyncio.wait_for(core.transcribe(ogg, "t.ogg"), 90)
            ok = bool(re.search(r"(?i)перевір|сорок", text or ""))
            lines.append(f"{'✅' if ok else '⚠️'} Розшифровка мови ({esc(prov)}): «{esc((text or '')[:80])}»")
        except LookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            lines.append(f"❌ Розшифровка мови не працює: {esc(str(exc)[:200])}")
            lines.append("   Від неї залежать «👂 що говорять у відео», 🔤 субтитри, ✂️ ролики. Найпростіше — додати GROQ_API_KEY (безкоштовно).")
        # 2) зір ШІ: напис на картинці
        try:
            from PIL import Image, ImageDraw, ImageFont
            im = Image.new("RGB", (640, 240), (255, 255, 255))
            dr = ImageDraw.Draw(im)
            try:
                font = ImageFont.truetype(str(Path(__file__).resolve().parents[1] / "newsroom" / "brand" / "Lora-Bold.ttf"), 90)
            except OSError:
                font = ImageFont.load_default()
            dr.text((40, 60), "ТЕСТ 42", fill=(0, 0, 0), font=font)
            buf = io.BytesIO()
            im.save(buf, "JPEG")
            ans, model = await asyncio.wait_for(self.host.ask_ai("Який напис на картинці? Відповідай лише написом.", label="🧪 Зір",
                                                                 image=base64.b64encode(buf.getvalue()).decode()), 60)
            ok = "42" in (ans or "")
            lines.append(f"{'✅' if ok else '⚠️'} Зір ШІ ({esc(model)}): «{esc((ans or '')[:40])}»"
                         + ("" if ok else " — модель, можливо, не бачить картинок: кадри, написи й водяні знаки не розпізнаються"))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"❌ Зір ШІ не відповів: {esc(str(exc)[:150])}")
        # 3) Shazam
        try:
            import shazamio  # noqa: F401
            lines.append("✅ Shazam встановлено — музика у звуці розпізнається")
        except ImportError:
            lines.append("❌ Shazam не встановлено — музика у звуці не розпізнається")
        await self._status(chat, status, "\n".join(lines)[:4000], [[("🔁 Ще раз", self.cb("selft")), ("« Медіа", self.cb())]])

    async def card_read(self, job):
        """Threads: картка-прев'ю допису (та сама, що в Telegram під посиланням) — ШІ з зором читає з неї музику, геотег, текст,
        лічильники, позначки «створено ШІ» й «реклама»; бере лише те, чого бот ще не знає з розмітки сторінки."""
        if not job.items or "threads" not in str(job.url):
            return
        info0 = job.items[0].info or {}
        img = ""
        # Картку з рядком «▶ музика» Threads віддає ботам прев'ю (як Telegram): просимо сторінку так само, як Telegram
        for ua in ("TelegramBot (like TwitterBot)", "facebookexternalhit/1.1"):
            try:
                async with M.open_url(str(job.url), agent=ua) as r:
                    head = (await r.aread())[:400_000].decode("utf-8", "replace")
                m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', head) or \
                    re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', head)
                if m:
                    img = m.group(1).replace("&amp;", "&")
                    break
            except Exception:  # noqa: BLE001
                log.info("card: сторінка для %s не відкрилась", ua, exc_info=True)
        img = img or info0.get("og_image")
        log.info("card: картка %s", (img or "немає")[:90])
        if not img:
            return
        try:
            import base64
            data = await self._fetch_small(img, 3_000_000)
            if not data:
                return
            ans, _m = await asyncio.wait_for(self.host.ask_ai("Що написано на картці допису?", system=self.CARD_SYSTEM, label="🧾 Картка допису",
                                                              image=base64.b64encode(data).decode()), 45)
            c = self.card_parse(ans)
        except Exception as exc:  # noqa: BLE001
            log.info("card: не прочитано", exc_info=True)
            try:
                import health
                health.fail("vision", str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            import health
            health.ok("vision")
        except Exception:  # noqa: BLE001
            pass
        log.info("card: прочитано %s", {k: v for k, v in c.items() if v and k != "text"})
        if not c:
            return
        upd = {}
        cap = (info0.get("description") or "").lower()
        if c["title"] and (c["title"].lower()[:20] in cap or re.search(r"\d{1,2}:\d{2}|AM|PM|\b20\d\d\b", c["title"] + c["artist"])):
            log.info("card: «%s · %s» схоже на текст допису чи дату — не музика", c["title"][:40], c["artist"][:30])
            c["title"] = c["artist"] = ""
        if c["title"] and not self.song_of(job):
            upd.update(track=c["title"], artist=c["artist"], music_on_post=True)
        if c["place"] and not M.geotag(info0):
            upd["location"] = c["place"]
        if c["text"] and not (info0.get("description") or "").strip():
            upd["description"] = c["text"]
        for k, v in (("like_count", c["likes"]), ("comment_count", c["comments"]), ("repost_count", c["reposts"])):
            if v and not info0.get(k):
                upd[k] = v
        if c["ai_label"]:
            upd["ai_label"] = True  # ⚠️ «створено ШІ» — позначка самого Threads
        if c["paid"]:
            upd["paid_partnership"] = True
        if c["count"] > len([i for i in job.items if i.engine != "text"]) > 0:
            log.info("threads: на картці %s файл(ів), а знайдено %s — можливо, не всі", c["count"], len(job.items))
        if not upd:
            return
        for it in job.items:
            it.info.update(upd)
        if "track" in upd:
            job.__dict__.pop("song", None)
            self.song_of(job)
        log.info("threads: з картки — %s", ", ".join(f"{k}={str(v)[:40]}" for k, v in upd.items()))

    async def post_total(self, job):
        """💾 Розмір усіх файлів допису однією цифрою (до завантаження): з даних сайту, а для прямих файлів — з заголовків.
        -> (байтів, скільки файлів з відомим розміром, усього файлів); кешується в job."""
        if "total" in job.__dict__:
            return job.__dict__["total"]
        vis, aud, _ = self.parts(job)
        nums = vis + aud
        h = job.options.height or 0

        async def one(k):
            it = job.items[k - 1]
            info = it.info or {}
            if info.get("formats"):
                out, a_size = self.sizes(info)
                if M.is_audio_item(it):
                    return a_size
                fit = [s for hh, s in out if s and (not h or hh <= h)] or [s for _, s in out if s]
                return fit[-1] if fit else None
            if info.get("filesize") or info.get("filesize_approx"):
                return info.get("filesize") or info.get("filesize_approx")
            if it.engine == "direct" and str(it.url).startswith("http"):
                return await M.head_size(str(it.url))
            return None

        try:
            got = await asyncio.wait_for(asyncio.gather(*(one(k) for k in nums[:40])), 15)
        except Exception:  # noqa: BLE001 — розмір не завадить завантаженню
            got = []
        known = [g for g in got if g]
        job.__dict__["total"] = (sum(known), len(known), len(nums))
        return job.__dict__["total"]

    def total_line(self, job):
        tot, known, n = job.__dict__.get("total") or (0, 0, 0)
        if not tot:
            return ""
        many = f" · {n} файл(ів)" if n > 1 else ""
        part = f" (відомо для {known} з {n})" if known < n else ""
        return f"💾 Разом: <b>{'~' if known < n else ''}{mb(tot)}</b>{many}{part}"

    def options_screen(self, chat, job):
        """⚙️ Що буде завантажено й як — ДО завантаження; лише потрібні перемикачі."""
        vis, aud, imgs = self.parts(job)
        vids = len(vis) - len(imgs)
        song, tok = self.song_of(job), job.token
        x = job.__dict__.setdefault("x", set())
        first = job.items[(vis or [1])[0] - 1]
        what = " · ".join(t for t in [f"🖼 {len(imgs)} фото" if imgs else "", f"🎬 {vids} відео" if vids else "",
                                      "🎵 звукова доріжка" if aud or (job.__dict__.get("song_info") or {}).get("url") else ""] if t)
        lines = [f"📦 <b>{esc(first.title[:150])}</b>", f"📍 {esc(job.__dict__.get('src') or M.site_name(job.url))}" + (f" · {what}" if what else "")]
        if self.total_line(job):
            lines.append(self.total_line(job))
        lines.append("")
        sound_url_pre = (job.__dict__.get("song_info") or {}).get("url") or aud
        if song and song[2]:
            lines.append(f"🎵 Накладена музика: <b>{esc(self.song_name(song)[:120])}</b>")
        elif song:
            lines.append(f"🎙 Звук: оригінальний звук автора ({esc(song[1][:80])})")
        elif vids:
            lines.append("🎵 Сайт не вказав музику — бот сам пошукає її у звуці під час завантаження (або «🔎 Розпізнати» зараз).")
        elif imgs and sound_url_pre:
            lines.append("🎵 До фото прикріплено звук — «🎵 Звук до фото окремо» скачає його окремим файлом.")
        elif imgs:
            lines.append("🎵 Музики до цих фото сайт не віддав.")
        on = lambda k: "☑️" if k in x else "▫️"  # noqa: E731
        rows = [[(f"⬇️ Завантажити{' усе' if len(vis) > 1 else ''}" + (" без звуку" if "mute" in x else ""), self.cb("og", tok))]]
        tog = []
        sound_url = (job.__dict__.get("song_info") or {}).get("url")
        if vids or aud or (song and song[2]) or sound_url:  # і звук, прикріплений до фото
            tog.append((f"{on('mus')} 🎵 {'Звук до фото' if imgs and not vids else 'Музика'} окремо", self.cb("ox", tok, "mus")))
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
        if len(vis + aud) > 1:
            n_per = len(self.per(job))
            rows.append([("🔢 Окремо для кожного файлу" + (f" · налаштовано {n_per}" if n_per else ""), self.cb("pil", tok))])
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
        per = self.per(job)
        if per:  # окремі налаштування: «не качати» — пропускаємо; «лише звук» — окремим MP3 після допису
            nums = [n for n in (vis + aud) if (per.get(n) or {}).get("mode") not in ("skip", "audio")]
        only_audio = [n for n, p in per.items() if p.get("mode") == "audio"]
        job.options.mode, job.options.cuts = ("mute" if "mute" in x else "video"), ()
        status = await self._status(chat, None, f"⬇️ Готую: {esc(job.__dict__.get('src') or '')}…", [[("⏹ Зупинити", self.cb("stop"))]])
        if nums:
            await self.download(chat, job, nums, status=status)
        for n in sorted(only_audio):
            sub = self.sub_job(job, n, "audio")
            if per[n].get("fmt"):
                sub.options.fmt = per[n]["fmt"]
            await self.download(chat, sub, [1])
        on_post = any((it.info or {}).get("music_on_post") for it in job.items)
        if "mus" in x:
            await self.post_audio(chat, job)
        if "full" in x and self.song_of(job) and not ("mus" in x and on_post):  # для Threads «музика окремо» вже і є повна пісня
            await self.full_song(chat, job, keep=True)

    async def post_audio(self, chat, job):
        """🔊 Звук саме з цього допису: окрема доріжка (TikTok/Instagram) → інакше звук із відео (MP3)."""
        vis, aud, imgs = self.parts(job)
        song, si = self.song_of(job), job.__dict__.get("song_info") or {}
        name = self.song_name(song) if song else "Звук допису"
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        if song and not aud and not si.get("url") and any((it.info or {}).get("music_on_post") for it in job.items):
            # Threads: пісня прикріплена до допису, а не вшита у відео — звук відео тут не той; шукаємо саме цю пісню (YouTube/SoundCloud)
            await self.full_song(chat, job, keep=True)
            return
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

    def remember_token(self, tok, url):
        p = self.dir / "tokens.json"
        try:
            data = json.loads(p.read_text()) if p.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data[tok] = url
        if len(data) > 400:
            data = dict(list(data.items())[-400:])
        try:
            p.write_text(json.dumps(data))
        except OSError:
            pass

    def revive(self, chat, tok):
        """Кнопка зі старого допису (бот за цей час оновлювався й забув завдання): відкриваємо те саме посилання заново."""
        try:
            url = json.loads((self.dir / "tokens.json").read_text()).get(tok)
        except (OSError, ValueError):
            url = None
        if not url:
            return "Посилання застаріло — надішліть його ще раз", None
        res = self.start_quick(chat, url, force=True)
        return (res or "🔄 Бот оновлювався — відкриваю це посилання заново"), (self.live_screen(chat) if self.in_app() else None)

    def job_by(self, chat, tok):
        job = self.jobs.get(chat)
        if job and job.token == tok:
            return job
        return getattr(self, "jobs_extra", {}).get(tok)  # окремий файл допису (дії «🗣 чистий звук» тощо)

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
        from urllib.parse import quote_plus
        ytm = "https://music.youtube.com/search?q=" + quote_plus(query) + "#songs"  # 🎶 YouTube Music — саме пісні, не кліпи
        engines = (["ytsearch1"] if os.environ.get("MEDIA_PROXY") else []) + ["scsearch1", ytm, "ytsearch1"]
        last = ""
        engines = list(dict.fromkeys(engines))

        async def probe(eng):  # шукаємо в усіх сервісах одночасно — де знайдеться, звідти й беремо
            with tempfile.TemporaryDirectory(prefix="orion-song-") as work:
                return await M.video_probe(eng if eng.startswith("http") else f"{eng}:{query}", Path(work))
        await self._status(chat, status, f"🔎 Шукаю одночасно: SoundCloud, YouTube Music, YouTube, Audius — {esc(query[:60])}…",
                           [[("⏹ Зупинити", self.cb("stop"))]])
        found = await asyncio.gather(*(asyncio.wait_for(probe(e), 120) for e in engines), return_exceptions=True)
        log.info("song: пошук «%s» — %s", query[:60], ", ".join(f"{e[:22]}: {'помилка' if isinstance(f, Exception) else len(f or [])}"
                                                             for e, f in zip(engines, found)))
        for eng, items in zip(engines, found):  # за пріоритетом: SoundCloud → YouTube Music → YouTube
            if isinstance(items, Exception) or not items:
                if isinstance(items, Exception):
                    last = str(items)[:200]
                continue
            try:
                real = str(items[0].url) if str(items[0].url).startswith("http") else str((items[0].info or {}).get("webpage_url") or "")
                if not real.startswith("http"):
                    continue
                song = Job(secrets.token_hex(4), real, items[:1])  # справжня адреса пісні, а не «scsearch1:…»
                song.options.mode = "audio"
                song.options.post = False
                song.__dict__["src"] = "SoundCloud" if eng.startswith("sc") else "YouTube Music" if "music.youtube" in eng else "YouTube"
                if not keep:
                    self.jobs[chat] = song
                await self.download(chat, song, [1], status=status)
                log.info("song: %s → %s", song.__dict__["src"], "надіслано" if song.__dict__.get("sent_n") else "не вдалося")
                if song.__dict__.get("sent_n"):
                    self._health_ok("song")
                    return
                last = f"{song.__dict__['src']}: знайшов, але скачати не вдалося"
                status = await self._status(chat, None, f"🔁 {song.__dict__['src']} не віддав пісню — пробую наступне джерело…",
                                            [[("⏹ Зупинити", self.cb("stop"))]])
            except JobError as exc:
                last = str(exc)
        log.info("song: повної немає у SoundCloud/YouTube — пробую Audius і фрагмент")
        if await self.song_audius(chat, artist, track, status):  # 🎧 Audius — відкрита музична платформа, повні треки без ключа
            return
        if await self.song_preview(chat, artist, track, status):  # 🎵 хоча б фрагмент цієї пісні
            return
        try:
            import health
            health.fail("song", last or "ніде не знайдено")
        except Exception:  # noqa: BLE001
            pass
        await self._status(chat, status, "⚠️ Не знайшов ні повної пісні (SoundCloud, YouTube Music, YouTube, Audius), ні її фрагмента (Apple Music, Deezer)."
                           + (f"\n\n{esc(last[:300])}" if last else ""), [[("« Медіа", self.cb())]])

    @staticmethod
    def _health_ok(kind):
        try:
            import health
            health.ok(kind)
        except Exception:  # noqa: BLE001
            pass

    async def song_audius(self, chat, artist, track, status=None):
        """🎧 Повна пісня з Audius (відкрита музична платформа, безкоштовний API без ключа) — якщо сам виконавець її там виклав."""
        import httpx
        q = f"{artist} {track}".strip()
        if not q:
            return False
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
                r = await c.get("https://api.audius.co/v1/tracks/search", params={"query": q, "app_name": "OrionAI"})
                hit = None
                for it in (r.json().get("data") or []) if r.status_code == 200 else []:
                    name = (it.get("title") or "").lower()
                    who = ((it.get("user") or {}).get("name") or "").lower()
                    if track.lower().split("(")[0].strip()[:15] in name and (not artist or artist.lower()[:6] in who):
                        hit = it
                        break
                if not hit:
                    return False
                data = (await c.get(f"https://api.audius.co/v1/tracks/{hit['id']}/stream", params={"app_name": "OrionAI"})).content
        except Exception:  # noqa: BLE001
            log.info("audius не вдався", exc_info=True)
            return False
        if len(data) < 100_000:
            return False
        a, t = (hit.get("user") or {}).get("name") or artist, hit.get("title") or track
        with tempfile.TemporaryDirectory(prefix="orion-aud-") as work:
            out = Path(work) / (safe_name(f"{a} — {t}", "song")[:90] + ".mp3")
            out.write_bytes(data)
            await self.send_media(chat, out, esc(f"🎧 {a} — {t}\nПовна пісня з Audius (виклав сам виконавець)."))
        if status:
            await self._status(chat, status, "🎧 Знайшов повну пісню на Audius — надіслав.", [[("« Медіа", self.cb())]])
        return True

    async def song_preview(self, chat, artist, track, status=None):
        """🎵 Офіційний фрагмент пісні (30 с) з Apple Music (iTunes Search) або Deezer — без ключів; коли повної немає."""
        import httpx
        q = f"{artist} {track}".strip()
        if not q:
            return False
        found = None
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://itunes.apple.com/search", params={"term": q, "entity": "song", "limit": 5})
                for it in (r.json().get("results") or []) if r.status_code == 200 else []:
                    if it.get("previewUrl") and track.lower().split("(")[0].strip()[:15] in (it.get("trackName") or "").lower():
                        found = (it["previewUrl"], it.get("artistName") or artist, it.get("trackName") or track, "Apple Music")
                        break
                if not found:
                    r = await c.get("https://api.deezer.com/search", params={"q": q, "limit": 5})
                    for it in (r.json().get("data") or []) if r.status_code == 200 else []:
                        if it.get("preview") and track.lower().split("(")[0].strip()[:15] in (it.get("title") or "").lower():
                            found = (it["preview"], (it.get("artist") or {}).get("name") or artist, it.get("title") or track, "Deezer")
                            break
                if not found:
                    return False
                url, a, t, src = found
                data = (await c.get(url)).content
        except Exception:  # noqa: BLE001
            log.info("song preview не вдався", exc_info=True)
            return False
        if len(data) < 10_000:
            return False
        with tempfile.TemporaryDirectory(prefix="orion-prev-") as work:
            ext = ".m4a" if src == "Apple Music" else ".mp3"
            out = Path(work) / (safe_name(f"{a} — {t} (фрагмент)", "song")[:90] + ext)
            out.write_bytes(data)
            await self.send_media(chat, out, esc(f"🎵 {a} — {t}\nФрагмент пісні (≈30 с, офіційне прев'ю {src}). Повної версії у відкритому доступі не знайшов."))
        if status:
            await self._status(chat, status, f"🎵 Повної пісні немає у відкритому доступі — надіслав офіційний фрагмент ({src}).", [[("« Медіа", self.cb())]])
        return True

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
                await self._status(chat, status, "🔎 Слухаю все відео — початок, середину й кінець…")
                hits = await self._shazam(path, all_hits=True) or []
            if not hits:
                await self._status(chat, status, "🤷 Не впізнав музику по всьому відео — можливо, це оригінальний звук або голос.",
                                   [[("« Медіа", self.cb())]])
                return
            job.__dict__["song"] = (hits[0][0], hits[0][1], True)
            more = "".join(f"\n• {esc(a)} — {esc(t)}" for a, t in hits[1:])
            await self._status(chat, status, f"🎵 Це: <b>{esc(hits[0][0])} — {esc(hits[0][1])}</b>"
                               + (f"\n\nЩе у відео:{more}" if more else ""),
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
        skipped = []
        self.got[chat] = []
        quick = status is not None
        title = job.items[nums[0] - 1].title if len(nums) == 1 else f"{len(nums)} елементів"
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        src = job.__dict__.get("src") or M.describe_source(job.url)
        status = await self._status(chat, status, f"⬇️ Завантажую: {esc(title[:100])}\n📍 {esc(src)}…", stop)
        self._active(chat, status, job.url)
        last = [0.0]
        t0 = time.time()

        cur = {"text": "", "bar": False, "t": time.time(), "shown": 0.0}
        site = M.site_name(job.url) if str(job.url).startswith("http") else "файл"
        usual = self.usual_time(site)

        def overall():
            """Окремі бігунки: «Усього в дописі» (кілька файлів) і «Уся група» (групове завантаження)."""
            lines = []
            item_frac = cur.get("frac", 0.0)
            frac_post = ((cur.get("k", 1) - 1) + item_frac) / max(1, len(nums))
            if len(nums) > 1:
                lines.append(f"🗂 Усього в дописі ({cur.get('k', 1)}/{len(nums)}):\n{pbar(frac_post)}")
            g = self.gctx.get(chat)
            if g and g.get("n"):
                done_g = g["n"] - len(g.get("left", [])) 
                lines.append(f"📦 Уся група №{g['id']} ({done_g + 1}/{g['n']}):\n{pbar((done_g + frac_post) / g['n'], '🟪')}")
            return ("\n" + "\n".join(lines)) if lines else ""

        def clock():
            el = int(time.time() - cur["t"])
            line = f"⏱ минуло {el // 60}:{el % 60:02d}"
            if not cur["bar"] and usual:  # байтів ще не видно — орієнтир за попередніми завантаженнями з цього сайту
                left = max(0, usual - el)
                line += f" · зазвичай ≈{usual // 60}:{usual % 60:02d}" + (f", лишилось ≈{left // 60}:{left % 60:02d}" if left else ", ось-ось")
            return line

        async def stage(text, force=False, has_bar=False):
            """Живий стан: застосунок бачить кожне оновлення, повідомлення в чаті — не частіше ніж раз на 3 с."""
            nonlocal status
            cur["text"], cur["bar"] = text, has_bar
            full = text + overall() + "\n" + clock()
            self.live[chat] = (full, [])
            now = asyncio.get_running_loop().time()
            if force or now - last[0] >= 3:
                last[0] = now
                cur["shown"] = now
                status = await self._status(chat, status, full, self.ctl_rows(chat, len(nums)))

        async def ticker():  # кожні 5 с — минулий час і орієнтир, навіть коли сайт ще не віддає байтів
            while True:
                await asyncio.sleep(5)
                if cur["text"] and asyncio.get_running_loop().time() - cur["shown"] >= 4.5:
                    await stage(cur["text"], force=True, has_bar=cur["bar"])
        old_tick = self.ticks.pop(chat, None)
        if old_tick:
            old_tick.cancel()
        tick = self.ticks[chat] = asyncio.get_running_loop().create_task(ticker())
        with tempfile.TemporaryDirectory(prefix="orion-media-") as root:
            root = Path(root)
            # 🖼 кілька фото/відео з ОДНІЄЇ публікації — надсилаємо однією публікацією (альбомом), а не окремими
            album_mode = len(nums) > 1 and opts.target != "zip"
            album, album_hist = [], None
            audio_retry = False  # 🔁 була помилка звуку — запропонуємо повторити (а не повторюємо самі)
            last_media = None  # (повідомлення, кнопки автора, запис) — сюди ж додамо й меню, щоб усе було одним дописом
            base_opts = opts
            post_risks = set()
            for k, n in enumerate(nums, 1):
                item = job.items[n - 1]
                opts = self.item_opts(job, n, base_opts)  # ⚙️ окремі налаштування саме цього файлу (без звуку, формат)
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
                    cur["frac"] = (done / total * 0.9) if total else cur.get("frac", 0.0)  # завантаження — 90% частки файлу
                    await stage(f"{head}\n⬇️ Цей файл:\n{bar(done, total, speed, eta)}", has_bar=bool(total))
                cur["k"], cur["frac"] = k, 0.0
                try:
                    cur["t"] = time.time()
                    await stage(head + "\n⏳ чекаю відповідь сайту…", force=True)
                    await self._wait_resume(chat, stage, head)  # ⏸ пауза — між файлами
                    while True:  # завантаження окремою задачею: пауза чи «пропустити» зупиняють лише цей файл
                        ft = asyncio.ensure_future(M.fetch(item, job.url, opts, work, progress=on_dl))
                        self.fetching[chat] = ft
                        try:
                            path, tracks, info = await ft
                            break
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():  # зупинили все завдання — далі не йдемо
                                raise
                            act = self.ctl.setdefault(chat, {}).pop("act", "")
                            for leftover in work.iterdir():
                                if leftover.is_file():
                                    leftover.unlink()
                            if act == "skip":
                                raise SkipItem()
                            await self._wait_resume(chat, stage, head)  # пауза: чекаємо «▶️», тоді цей файл — наново
                        finally:
                            self.fetching.pop(chat, None)
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
                    rkey = (chat, str(job.url), desc or "", geotag)  # теги — на весь допис: ШІ з кадром один раз, не на кожен файл
                    rc = self.__dict__.setdefault("_relc", {})
                    if rkey in rc and time.time() - rc[rkey][0] < 900:
                        rel, guess = rc[rkey][1], rc[rkey][2]
                    else:
                        media0 = next((o for o in outs if Path(o).suffix.lower() in self.IMG_EXT + self.VIDEO_EXT + self.AUD_EXT), None) if outs else path
                        others = []  # інші фото/обкладинки допису — аналізуємо весь допис, а не лише перший файл
                        for it2 in job.items:
                            if it2 is item or it2.engine == "text":
                                continue
                            u2 = (it2.info or {}).get("thumbnail") or (str(it2.url) if M.is_image_item(it2) or it2.engine == "direct" and
                                                                       re.search(r"\.(jpe?g|png|webp)(\?|$)", str(it2.url), re.I) else "")
                            if u2 and str(u2).startswith("http"):
                                others.append(str(u2))
                        s2 = self.song_of(job)
                        detected = None
                        if not s2 and media0 and Path(media0).suffix.lower() in self.VIDEO_EXT + self.AUD_EXT \
                                and self.rel_on(chat, "shz") and await self._has_audio(media0):
                            await stage(f"🎧 Сайт не вказав музику — слухаю все відео: {esc(item.title[:60])}…", force=True)
                            detected = await self._shazam(media0)  # 🎧 сайт не вказав — шукаємо музику в самому звуці (і змішану з голосом)
                            if detected:
                                job.__dict__.update(song=(detected[0], detected[1], True),
                                                    song_info={"artist": detected[0], "title": detected[1], "url": "", "original": False,
                                                               "detected": True})
                                s2 = self.song_of(job)
                                log.info("related: музика у звуці: %s — %s", detected[0][:40], detected[1][:60])
                        files = ", ".join(sorted({(Path(M.urlsplit(str(i.url)).path).suffix or "?").lstrip(".") for i in job.items}))[:80]
                        rel = await self.detect(chat, desc or "", geotag, who, link, self.instr.get(chat), media=media0,
                                                ctx={"others": others[:3], "song": (self.song_name(s2) + ("" if s2[2] else " (оригінальний звук автора)")) if s2 else "",
                                                     "song_overlay": bool(s2 and s2[2]),
                                                     "info": info or item.info or {},
                                                     "music_on_post": bool((info or {}).get("music_on_post") or (item.info or {}).get("music_on_post")), "files": files, "site": site})
                        guess = self.__dict__.get("_geo_guess", {}).pop(chat, None)
                        rc[rkey] = (time.time(), rel, guess, self.__dict__.get("_about", {}).pop(chat, ""),
                                    self.__dict__.get("_risk", {}).pop(chat, set()), self.song_name(s2) if s2 and s2[2] else "",
                                    self.__dict__.get("_seen", {}).pop(chat, ""), self.__dict__.get("_said", {}).pop(chat, ""),
                                    str(media0) if media0 else "", [])
                        for k in [k for k, v in rc.items() if time.time() - v[0] > 900]:
                            rc.pop(k, None)
                    about = rc[rkey][3] if rkey in rc else ""
                    risks, rsong = (rc[rkey][4], rc[rkey][5]) if rkey in rc else (set(), "")
                    ocr_t, said_t = (rc[rkey][6], rc[rkey][7]) if rkey in rc else ("", "")
                    post_said = rc[rkey][9] if rkey in rc else []  # 🎙 розшифровки інших відео/аудіо цього ж допису
                    if rkey in rc:  # 📉 якість і 🎙 мова кожного файлу — паралельно, щоб не затримувати надсилання
                        do_q = self.rel_on(chat, "risk") and self.rel_on(chat, "qual")
                        do_h = self.rel_on(chat, "ai") and self.rel_on(chat, "hear")
                        av = self.VIDEO_EXT + self.AUD_EXT

                        async def _none():
                            return None
                        q_tasks = [self.quality(o) if do_q else _none() for o in outs]
                        h_tasks = [self._hear(o) if do_h and str(o) != rc[rkey][8] and Path(o).suffix.lower() in av else _none()
                                   for o in outs]
                        got = await asyncio.gather(*q_tasks, *h_tasks, return_exceptions=True)
                        qs, hs = got[:len(outs)], got[len(outs):]
                        many = len(nums) > 1 or len(outs) > 1
                        more = set()
                        for q in qs:
                            if q and not isinstance(q, Exception):
                                more.add("q:" + (f"№{n} — " if many else "") + ", ".join(q))
                        for heard in hs:
                            if heard and not isinstance(heard, Exception):
                                post_said.append(heard)
                                if self.rel_on(chat, "risk"):
                                    if R.swear_hits(heard):
                                        more.add("swear")
                                    if R.guess_lang(heard) == "ru":
                                        more.add("ru")
                        if more - set(risks):
                            risks = set(risks) | more
                            rc[rkey] = rc[rkey][:4] + (risks,) + rc[rkey][5:]
                    if rel is None:  # людина попросила без тегів — без сортування й без запису
                        rel_t = rel_g = rel_l = rel_f = rel_btns = []
                        rel_line, rel_len = "", 0
                    else:
                        rel_t, rel_g, rel_l = rel
                        rel_line, rel_len, rel_btns, rel_f = self.related_parts(chat, who, rel_t, rel_g, rel_l)
                    if geotag:
                        rel_line, rel_len = f"\n📍 {esc(geotag[:60])}" + rel_line, rel_len + len(geotag[:60]) + 3
                    elif guess and rel is not None:  # місце не з самого допису — лише припущення
                        gname = ", ".join(self.hist.name("g", k) for k in guess[0])[:60]
                        gl = f"\n📍 Імовірна локація: {esc(gname)} ({guess[1]})"
                        rel_line, rel_len = gl + rel_line, rel_len + len(gl)
                    song = job.__dict__.get("song")  # могли щойно розпізнати у звуці
                    if song and song[2] and not song_html:
                        heard_mark = " (розпізнано у звуці)" if (job.__dict__.get("song_info") or {}).get("detected") else ""
                        song_html = f"\n🎵 {esc(self.song_name(song))}{heard_mark}"
                    post_risks |= set(risks)
                    if risks:  # ⚠️ небезпека для публікації — першим рядком серед тегів
                        rl = "\n" + esc(R.risks_line(risks, rsong[:60]))
                        rel_line, rel_len = rl + rel_line, rel_len + len(rl)
                    if about and rel is not None and self.rel_on(chat, "about"):  # 🧠 що це за допис — коротко
                        al = f"\n🧠 {esc(about[:160])}"
                        rel_line, rel_len = al + rel_line, rel_len + len(about[:160]) + 3
                    gx = self.gctx.get(chat)
                    if gx:  # 📦 позначка групи й посилання на попередній із неї
                        prev = next((e for e in reversed(self.hist.items(chat)) if e.get("group") == gx["id"]), None)
                        gl = f"\n📦 Група №{gx['id']} · {gx['i'] + 1}/{gx['n']}"
                        rel_len += len(gl) + (len(" · попередній з групи") if prev else 0)
                        if prev and self.mh_link(prev["id"]):
                            gl += f' · <a href="{esc(self.mh_link(prev["id"]))}">попередній з групи</a>'
                        rel_line = gl + rel_line
                    if (info or {}).get("_no_audio") and opts.mode == "video":  # 🔇 чесно кажемо, що звуку немає й чому
                        audio_retry = audio_retry or bool((info or {}).get("_audio_retry"))
                        na = f"\n🔇 Звук не скачався: {(info or {})['_no_audio']}" + (" — можна спробувати ще раз (кнопка нижче)"
                                                                                   if (info or {}).get("_audio_retry") else "")
                        rel_line, rel_len = esc(na) + rel_line, rel_len + len(na)
                    tail = (f"\n\n👤 {who_html}" if who else "\n") + (f" · {esc(site)}" if site else "") \
                        + (f' · <a href="{esc(job.url)}">🔗 Допис</a>' if str(job.url).startswith("http") else "") + song_html + rel_line
                    tail_len = len(f"\n\n👤 {who} · {site} · 🔗 Допис") + len(re.sub(r"<[^>]+>|&\w+;", "", song_html)) + rel_len
                    recorded = rel is None and not gx  # «без тегів» у групі — запис лише для позначки групи
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
                        if album_mode and out.suffix.lower() in ALBUM_EXT and size_ <= ALBUM_MAX:
                            album.append({"out": out, "cap": cap})
                            if not recorded and album_hist is None:
                                album_hist = dict(author=R.norm_author(who), who=who, alink=link, site=site, url=str(job.url),
                                                  src_url=self.cur_url.get(chat) or "", title=(desc or item.title or "")[:80],
                                                  topics=rel_t, geos=rel_g, langs=rel_l, first=rel_f, geotag=geotag, about=about or "", risks=sorted(risks), ocr=ocr_t, said=" · ".join([said_t] + post_said)[:1500],
                                                  **({"group": gx["id"], "gi": gx["i"]} if gx else {}),
                                                  **({"notag": True} if rel is None else {}), _btns=rel_btns)
                            recorded = True
                            continue
                        up = f"⬆️ Надсилаю {esc(out.name[:60])} ({mb(size_)})"
                        await stage(up + "…", force=True)

                        async def on_up(done, total, up=up):
                            cur["frac"] = 0.9 + (done / total * 0.1 if total else 0)
                            await stage(f"{up}\n⬆️ Цей файл:\n{bar(done, total)}", has_bar=bool(total))
                        msg_ = await self.send_media(chat, out, cap, on_progress=on_up,
                                                     markup=None if recorded else self._url_markup([[b] for b in rel_btns]))
                        if not recorded and getattr(msg_, "message_id", None):  # 🔗 запам'ятовуємо для «попереднє …»
                            recorded = True
                            rid_ = self.hist.add(chat, msg=msg_.message_id, author=R.norm_author(who), who=who, alink=link, site=site,
                                          url=str(job.url), src_url=self.cur_url.get(chat) or "", size=size_, title=(desc or item.title or "")[:80], topics=rel_t, geos=rel_g, langs=rel_l, first=rel_f, geotag=geotag, about=about or "", risks=sorted(risks), ocr=ocr_t, said=" · ".join([said_t] + post_said)[:1500],
                                          **({"group": gx["id"], "gi": gx["i"]} if gx else {}), **({"notag": True} if rel is None else {}))
                            await self._cat_button(chat, msg_.message_id, rid_, rel_btns)
                            last_media = (msg_.message_id, rel_btns, rid_)
                        elif getattr(msg_, "message_id", None) and last_media is None:
                            last_media = (msg_.message_id, [], None)
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
                except SkipItem:
                    skipped.append(n)
                except JobError as exc:
                    failed.append(f"{n} · {M.explain(exc)}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("media item failed")
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    failed.append(f"{n} · " + (f"сайт не віддав файл (HTTP {code}) — спробуйте ще раз або інший формат"
                                               if code else esc(type(exc).__name__)))
            if album:
                cur["k"], cur["frac"] = len(nums), 0.95
                await stage(f"⬆️ Надсилаю однією публікацією: {len(album)} файл(ів)…", force=True)
                msgs = None
                for v in self.__dict__.get("_relc", {}).values():  # ⚠️ небезпеки, знайдені в інших файлах допису, — і в підпис альбому
                    if len(v) > 9 and v[4] and album and album_hist is not None and album_hist.get("about", "") == v[3]:
                        line = esc(R.risks_line(v[4], v[5][:60]))
                        c0 = album[0]["cap"] or ""
                        new = re.sub(r"⚠️ Увага:[^\n]*", line, c0) if "⚠️ Увага:" in c0 else (c0 + "\n" + line).strip()
                        if len(re.sub(r"<[^>]+>|&\w+;", "", new)) <= 1024:
                            album[0]["cap"] = new
                gone = [a for a in album if not a["out"].is_file()]
                if gone:  # файл зник між обробкою й надсиланням — не валимо весь допис, а пишемо в журнал, що саме і що лишилось поруч
                    for a in gone:
                        near = sorted(f.name for f in a["out"].parent.iterdir()) if a["out"].parent.is_dir() else ["(теки немає)"]
                        log.warning("album: файлу немає %s; у теці: %s", a["out"], near[:10])
                    cap0 = album[0]["cap"]
                    album = [a for a in album if a["out"].is_file()]
                    if album and not album[0]["cap"]:
                        album[0]["cap"] = cap0  # підпис допису — на перший файл, що лишився
                    failed += [f"{a['out'].parent.name.lstrip('0') or '?'} · файл загубився під час обробки — надішліть посилання ще раз"
                               for a in gone]
            if album:
                size_all = sum(a["out"].stat().st_size for a in album)  # розмір — ДО keep(): він переносить файли
                car = None
                try:  # 🖼 мозаїка: усі файли допису одним дописом (альбом до 10); дії — посиланнями в підписі
                    raise LookupError("mosaic")
                except LookupError:
                    try:
                        msgs = await self.send_album(chat, album)
                    except Exception:  # noqa: BLE001 — альбом не пройшов: ті самі файли окремо, щоб нічого не загубити
                        log.exception("album send failed — надсилаю окремо")
                        for a in album:
                            if a["out"].is_file():
                                await self.send_media(chat, a["out"], a["cap"])
                sent += [a["out"].name for a in album]
                try:
                    if msgs and album_hist is not None:
                        btns = album_hist.pop("_btns", [])
                        for v in self.__dict__.get("_relc", {}).values():  # 🎙 розшифровки всіх відео допису й нові ⚠️ — у запис
                            if len(v) > 9 and v[1] is not None and album_hist.get("about", "") == v[3]:
                                album_hist["said"] = " · ".join([v[7]] + v[9])[:1500]
                                album_hist["risks"] = sorted(v[4])
                        rid_ = self.hist.add(chat, msg=msgs[0].message_id, size=size_all, count=len(album), **album_hist)
                        if car:
                            last_media = (car.message_id, btns, rid_)  # звичайний допис — меню додасться кнопками
                        else:
                            last_media = ("album", btns, rid_, msgs[0].message_id, len(album), size_all, album[0]["cap"])
                except Exception:  # noqa: BLE001 — альбом уже надіслано; збій запису історії не має ламати завантаження
                    log.exception("album: запис в історію не вдався")
                if car and last_media is None:
                    last_media = (car.message_id, [], None)
                for a in album:  # у Mini App — лише після надсилання й підрахунків (keep переносить файл)
                    kept = self.keep(a["out"])
                    if kept:
                        self.got[chat].append(kept)
            tick.cancel()
            if sent:
                self.note_time(site, time.time() - t0, len(nums))
                self.just_done.add(chat)
                for u_ in {self.cur_url.get(chat), job.url}:  # 📌 завантажене зникає зі збережених
                    if u_:
                        self.pend_done(chat, u_)
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
        self.last_result[chat] = (len(sent), list(failed))
        if sent:
            for u_ in {self.cur_url.get(chat), job.url}:
                if u_:
                    self.bad_ok(chat, u_)
        elif (failed or not shown) and not skipped:  # 🧯 нічого не вийшло — у биті посилання з причиною
            self.bad_add(chat, self.cur_url.get(chat) or job.url, "; ".join(re.sub(r"<[^>]+>", "", f) for f in failed[:3])
                         or "файлів не знайшлося")
        if skipped:
            failed = failed + [f"{n} · ⏭ пропущено за вашим вибором" for n in skipped] if not sent else failed
        if sent and not failed:
            msg = "✅ Готово" + (f" · файлів: {len(sent)}" if len(sent) > 1 else "") + (f" · пропущено: {len(skipped)}" if skipped else "")
        elif sent:
            msg = f"✅ Надіслано файлів: {len(sent)}\n\n⚠️ Не вдалося:\n" + "\n".join(failed[:30])
        elif failed:
            msg = (f"⚠️ <b>Не вийшло завантажити</b>\n🔗 {esc(self.cur_url.get(chat) or job.url)}\n\n❓ " + "\n\n".join(failed[:10])
                   + "\n\n🧯 Збережено в «Биті посилання»")
        elif shown:
            msg = "📝 Тут лише текст допису — надіслав його вище."
        else:
            msg = "⚠️ Файлів не знайшлося. Спробуйте інший формат або якість."
        job.__dict__["sent_n"] = len(sent)  # для запасних джерел: чи щось реально надіслано
        rows = [[("⚙️ Доналаштувати й завантажити ще" if quick else "📋 До набору", self.cb("opt" if quick else "menu", job.token))], [("« Медіа", self.cb())]]
        if sent and quick:  # 🎬 студія: безпечна версія, субтитри, короткі ролики — для відео з допису
            try:
                rows[:0] = self.studio_rows(job, locals().get("post_risks") or ())
            except Exception:  # noqa: BLE001
                log.info("studio rows", exc_info=True)
        if audio_retry:
            rows.insert(0, [("🔁 Завантажити ще раз зі звуком?", self.cb("snd", job.token))])
        if sent and not failed and last_media and await self._merge_menu(chat, last_media, rows):
            try:  # усе в одному дописі: файл + кнопки автора + категорії + меню — окремого «✅ Готово» не треба
                if status:
                    await bot.delete_message(chat, status.message_id)
            except Exception:  # noqa: BLE001
                pass
            self._active(chat, done=True)
            return
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

    async def _cat_button(self, chat, mid, rid, url_btns):
        """Під файлом — «🏷 Категорії»: переглянути й змінити теми, локації, мову вручну."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = [[InlineKeyboardButton(t, url=u)] for t, u in url_btns]
        rows.append([InlineKeyboardButton("🏷 Категорії", callback_data=self.cb("ted", rid))])
        try:
            await self.host.bot().edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=InlineKeyboardMarkup(rows))
        except Exception:  # noqa: BLE001
            pass

    def find_screen(self, chat, query):
        got, res = self.hist.search(chat, query)
        head = "🔎 <b>Пошук у скачаному</b>" + (f" · {esc(' · '.join(got))}" if got else f" · «{esc(query[:60])}»")
        if not res:
            return (head + "\n\nНічого не знайшлося. Спробуйте інакше: тема («снігопад»), місце («Карпати»), автор (нік) — "
                    "можна разом: «снігопад з Карпат від oleno4ka».", [[("🔎 Шукати ще", self.cb("fnd"))], [("« Архів", self.cb("arc"))]])
        lines = [head + f" · знайдено {len(res)}", ""]
        for e in res[:40]:
            url = self.mh_link(e["id"])
            title = esc((e.get("title") or "файл").replace("\n", " ")[:55])
            tags_ = ", ".join([self.hist.name("t", k) for k in e.get("topics", [])[:2]] + [self.hist.name("g", k) for k in e.get("geos", [])[:2]])
            lines.append("• " + (f'<a href="{esc(url)}">{title}</a>' if url else title)
                         + f" · {R.icon(e.get('site'))} {esc(e.get('who') or '')}" + (f" · {esc(tags_)}" if tags_ else "")
                         + f" · {esc(self.when_size(e))}")
        if len(res) > 40:
            lines.append(f"\n…ще {len(res) - 40} — уточніть запит")
        body = lines[0] + "\n<blockquote expandable>" + "\n".join(lines[2:]) + "</blockquote>" if len(res) > 8 else "\n".join(lines)
        return body[:4000], [[("🔎 Шукати ще", self.cb("fnd"))], [("« Архів", self.cb("arc"))]]

    async def _merge_menu(self, chat, last, rows):
        """Кнопки меню — у сам допис із файлом (одним повідомленням). Альбом кнопок не має — тоді одне повідомлення-підпис до нього."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        bot = self.host.bot()
        car = self._car_load().get(f"{chat}:{last[0]}") if last[0] != "album" else None
        if car:  # 🎠 гортання: ◀️ i/N ▶️ + кнопки автора, категорії, меню — усе в одному повідомленні
            extra = [[(t, u)] for t, u in last[1]]
            if last[2]:
                extra.append([("🏷 Категорії", self.cb("ted", last[2]))])
            extra += [[(t, d) for t, d in row] for row in rows]
            car["kb"] = extra
            data = self._car_load()
            data[f"{chat}:{last[0]}"] = car
            self._car_save(data)
            try:
                await bot.edit_message_reply_markup(chat_id=chat, message_id=last[0],
                                                    reply_markup=self._car_kb(chat, last[0], 0, len(car["files"]), extra))
                return True
            except Exception:  # noqa: BLE001
                log.info("кнопки гортання не додано", exc_info=True)
                return False
        kb = [[InlineKeyboardButton(t, url=u)] for t, u in last[1]]
        if last[2]:
            kb.append([InlineKeyboardButton("🏷 Категорії", callback_data=self.cb("ted", last[2]))])
        kb += [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
        try:
            if last[0] == "album":  # альбому Telegram кнопок не дає — дії стають посиланнями в підписі (одне повідомлення)
                links = [(t, u) for t, u in last[1]]
                if last[2]:
                    links.append(("🏷 Категорії", self.mh_act("ted", last[2])))
                for row in rows:
                    for t, d in row:
                        parts = d.split(":")[3:] if d.startswith("m:pl:med") else []
                        links.append((t, self.mh_act(*(parts or [""]))))
                line = "\n" + " · ".join(f'<a href="{esc(u)}">{esc(t)}</a>' for t, u in links if u)
                cap = last[6] or ""
                vis = len(re.sub(r"<[^>]+>|&\w+;", "", cap)) + len(re.sub(r"<[^>]+>", "", line))
                placed = False
                if vis <= 1024 and self.mh_act(""):  # дії — посиланнями в підписі самого допису: одне повідомлення, без окремих кнопок
                    try:
                        await bot.edit_message_caption(chat_id=chat, message_id=last[3], caption=cap + line, parse_mode="HTML")
                        placed = True
                    except Exception:  # noqa: BLE001
                        log.info("підпис альбому не змінено", exc_info=True)
                if not placed:  # підпис задовгий чи не змінився — лише тоді кнопки окремим повідомленням, щоб дії не загубились
                    await bot.send_message(chat, f"⬆️ Публікація: {last[4]} файл(ів) · {mb(last[5])}", reply_markup=InlineKeyboardMarkup(kb),
                                           disable_notification=True)
            else:
                await bot.edit_message_reply_markup(chat_id=chat, message_id=last[0], reply_markup=InlineKeyboardMarkup(kb))
            return True
        except Exception:  # noqa: BLE001
            log.info("не вдалося додати меню до допису", exc_info=True)
            return False

    def ted_screen(self, chat, rid):
        e = self.hist.get(chat, rid)
        if not e:
            return "Цього запису вже немає.", [[("« Архів", self.cb("arc"))]]
        lines = [f"🏷 <b>Категорії</b> · {esc((e.get('title') or 'файл')[:60])}",
                 f"{R.icon(e.get('site'))} {esc(e.get('who') or '')}" + (f" · 📦 Група №{e['group']}" if e.get("group") else "")
                 + f"\n🕓 Скачано {esc(self.when_size(e))}",
                 "<blockquote expandable>ℹ️ Як змінити\n🗑 — прибрати категорію. «➕ Додати» — напишіть теги через кому "
                 "(напр. «снігопад, Полісся»). Те саме можна, відповівши на сам файл: «тег снігопад», «прибери тег Карпати».</blockquote>", ""]
        rows = []
        for kind, title in (("t", "🏷 Теми"), ("g", "📍 Локації"), ("l", "🗣 Мова")):
            ks = e.get(R.KINDS[kind], [])
            lines.append(f"{title}: " + (", ".join(esc(self.hist.name(kind, k)) for k in ks) if ks else "—"))
            btn = [(f"🗑 {self.hist.name(kind, k)[:18]}", self.cb("tdel", rid, f"{kind}{i}")) for i, k in enumerate(ks)]
            rows += [btn[j:j + 3] for j in range(0, len(btn), 3)]
        rows.append([("➕ Додати", self.cb("tadd", rid))])
        url = self.mh_link(rid)
        rows.append([("« Архів", self.cb("arc"))])
        if url:
            lines.append(f'\n<a href="{esc(url)}">⬆️ Показати файл</a>')
        return "\n".join(lines)[:4000], rows

    async def ted_apply(self, chat, rid, text):
        """Текст людини -> додати / прибрати категорії запису. -> короткий звіт."""
        e = self.hist.get(chat, rid)
        if not e:
            return "Цього запису вже немає"
        low = text.lower()
        instr = R.parse_instr(text)
        drop = instr["drop"] if instr else []
        m_rm = re.match(r"\s*(?:прибери|видали|забери|зніми|прибрати)\s+(?:тег\w*|категорі\w*)?\s*(.+)", low)
        if m_rm:
            drop += [R.clean_tag(t) for t in re.split(r"[,;]|\s+і\s+", m_rm.group(1)) if R.clean_tag(t)]
            add = []
        else:
            add = (instr or {}).get("add") or ((G.own_tags(text) or {}).get("add", []) if not instr else [])
        t_k, g_k = list(e.get("topics", [])), list(e.get("geos", []))
        for kind, k in (await self.user_tags(chat, add) if add else []):
            lst = t_k if kind == "t" else g_k
            if k not in lst:
                lst.append(k)
        for d in drop:
            rx = R.stem_rx(d)
            t_k = [k for k in t_k if not (rx and rx.search(self.hist.name("t", k)))]
            g_k = [k for k in g_k if not (rx and rx.search(self.hist.name("g", k)))]
        self.hist.update(chat, rid, topics=t_k, geos=g_k)
        return "✅ Категорії оновлено"

    # ---------------------------------------------------------------- 🎠 гортання файлів в одному повідомленні
    def _car_path(self):
        return self.dir / "carousels.json"

    def _car_load(self):
        try:
            return json.loads(self._car_path().read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _car_save(self, data):
        if len(data) > 2000:  # найстаріші — геть
            for k in list(data)[:-2000]:
                data.pop(k, None)
        self._car_path().write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    async def send_carousel(self, chat, album, stage=None):
        """Перший файл — повідомленням із підписом; решту завантажуємо в те саме повідомлення (заміна файлу), щоб Telegram
        дав їм постійні ід; потім повертаємо перший. Далі ◀️ ▶️ просто міняють файл — без повторного завантаження."""
        if any(a["out"].stat().st_size > ALBUM_MAX for a in album) or len(album) > 50:
            return None  # завеликі для заміни через Bot API — альбомом
        from telegram import InputMediaPhoto, InputMediaVideo
        bot = self.host.bot()
        cap = album[0]["cap"]
        first = await self.send_media(chat, album[0]["out"], cap)
        if not getattr(first, "message_id", None):
            return None
        files = [self._file_ref(first)]
        for j, a in enumerate(album[1:], 2):
            if stage:
                await stage(f"⬆️ Надсилаю {j}/{len(album)} в одне повідомлення…", force=True)
            path = a["out"]
            with path.open("rb") as fh:
                media = (InputMediaVideo(fh, caption=cap, parse_mode="HTML", supports_streaming=True, filename=path.name)
                         if path.suffix.lower() in (".mp4", ".mov", ".m4v") else InputMediaPhoto(fh, caption=cap, parse_mode="HTML"))
                m = await bot.edit_message_media(chat_id=chat, message_id=first.message_id, media=media, read_timeout=300, write_timeout=300)
            files.append(self._file_ref(m))
        if len(files) > 1:
            await self._car_show(chat, first.message_id, files, 0, cap, None)
        data = self._car_load()
        data[f"{chat}:{first.message_id}"] = {"files": files, "cap": cap, "kb": []}
        self._car_save(data)
        return first

    @staticmethod
    def _file_ref(m):
        if getattr(m, "video", None):
            return {"t": "video", "id": m.video.file_id}
        if getattr(m, "photo", None):
            return {"t": "photo", "id": m.photo[-1].file_id}
        if getattr(m, "animation", None):
            return {"t": "video", "id": m.animation.file_id}
        return {"t": "doc", "id": getattr(getattr(m, "document", None), "file_id", "")}

    def _car_kb(self, chat, mid, i, n, extra):
        """◀️ i/N ▶️ + решта кнопок допису."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        nav = [InlineKeyboardButton("◀️", callback_data=self.cb("cr", mid, (i - 1) % n)),
               InlineKeyboardButton(f"{i + 1}/{n}", callback_data=self.cb("cr", mid, i)),
               InlineKeyboardButton("▶️", callback_data=self.cb("cr", mid, (i + 1) % n))]
        rows = [nav]
        for row in extra or []:
            rows.append([InlineKeyboardButton(t, url=v) if v.startswith("http") else InlineKeyboardButton(t, callback_data=v) for t, v in row])
        return InlineKeyboardMarkup(rows)

    async def _car_show(self, chat, mid, files, i, cap, extra):
        from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo
        f = files[i]
        cls = {"video": InputMediaVideo, "photo": InputMediaPhoto}.get(f["t"], InputMediaDocument)
        kw = {"supports_streaming": True} if f["t"] == "video" else {}
        await self.host.bot().edit_message_media(chat_id=chat, message_id=mid, media=cls(f["id"], caption=cap, parse_mode="HTML", **kw),
                                                 reply_markup=self._car_kb(chat, mid, i, len(files), extra))

    async def send_album(self, chat, album):
        """Фото й відео однієї публікації — альбомом (до 10 у повідомленні), підпис — під першим."""
        from telegram import InputMediaPhoto, InputMediaVideo
        bot = self.host.bot()
        out_msgs = []
        for start in range(0, len(album), 10):
            chunk, media, handles = album[start:start + 10], [], []
            try:
                for j, a in enumerate(chunk):
                    path = a["out"]
                    fh = path.open("rb")
                    handles.append(fh)
                    cap = a["cap"] if (start == 0 and j == 0) else None
                    if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
                        meta = {}
                        try:
                            data = await M.probe_media(path)
                            v = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), {})
                            meta = {"width": int(v.get("width") or 0) or None, "height": int(v.get("height") or 0) or None,
                                    "duration": int(float((data.get("format") or {}).get("duration") or 0)) or None}
                        except Exception:  # noqa: BLE001
                            pass
                        thumb = await M.make_thumb(path, path.parent)
                        media.append(InputMediaVideo(fh, caption=cap, parse_mode="HTML" if cap else None, supports_streaming=True,
                                                     filename=path.name, thumbnail=thumb.read_bytes() if thumb else None, **meta))
                    else:
                        media.append(InputMediaPhoto(fh, caption=cap, parse_mode="HTML" if cap else None))
                if len(media) == 1:  # один файл у хвості — звичайним повідомленням
                    m = await self.send_media(chat, chunk[0]["out"], chunk[0]["cap"] if start == 0 else "")
                    out_msgs.append(m)
                else:
                    res = await bot.send_media_group(chat, media, read_timeout=300, write_timeout=300)
                    out_msgs += list(res) if isinstance(res, (list, tuple)) else []
            finally:
                for fh in handles:
                    fh.close()
        return out_msgs

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
