"""🧰 Інструменти редакції: відео для Reels/Shorts/TikTok, розшифровка інтерв'ю, перевірка фото, медіатека з правами.

🎬 Відео: вертикаль 9:16 (1080×1920) — розмите тло або обрізка по центру; вирізати фрагмент; субтитри українською
   (розшифровка → вшиті в кадр шрифтом Lora + окремий файл .srt). Обробка — ffmpeg на сервері.
🎙 Інтерв'ю: текст із часовими мітками, головні цитати (дослівно з розшифровки) і чернетка новини в Новинарні.
🕵️ Фото: дані з файлу (коли й чим знято, де, чи редагувалося), чи вже траплялося в нашій медіатеці, і де шукати старіші копії.
🗃 Медіатека: кожне фото, використане в картках, — з автором, ліцензією, джерелом і новиною, де воно вже було.
Звичайний шлях Telegram дає ботам файли до 20 МБ; більші (до 2 ГБ) бот забирає напряму через MTProto — той самий
механізм, яким 📥 Медіа надсилає великі файли (потрібні TELEGRAM_API_ID і TELEGRAM_API_HASH)."""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

from plugins import esc

log = logging.getLogger("orion.newsroom.tools")
BRAND = Path(__file__).with_name("brand")
MAX_TG = 20 * 1024 * 1024       # звичайний шлях Bot API
MAX_BIG = 2000 * 1024 * 1024    # через MTProto (як у 📥 Медіа)


def ts(sec, srt=False):
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{int((sec - int(sec)) * 1000):03d}"
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_srt(segs, max_chars=42):
    """Сегменти → SRT; довгі фрази діляться на рядки до max_chars (зручно читати на телефоні)."""
    out, n = [], 0
    for start, end, text in segs:
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 > max_chars and cur:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            lines.append(cur)
        chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)] or [[""]]
        dur = max(0.5, end - start) / len(chunks)
        for k, ch in enumerate(chunks):
            n += 1
            a, b = start + k * dur, start + (k + 1) * dur
            out.append(f"{n}\n{ts(a, True)} --> {ts(b, True)}\n" + "\n".join(ch) + "\n")
    return "\n".join(out)


def parse_span(t):
    """«0:10-0:40», «10-40», «1:05 – 1:30» → (з, до) у секундах або None."""
    m = re.fullmatch(r"\s*((?:\d+:)?\d{1,2}(?::\d{2})?)\s*[-–—]\s*((?:\d+:)?\d{1,2}(?::\d{2})?)\s*", t or "")
    if not m:
        return None

    def sec(x):
        p = [int(v) for v in x.split(":")]
        return sum(v * 60 ** i for i, v in enumerate(reversed(p)))
    a, b = sec(m[1]), sec(m[2])
    return (a, b) if b > a else None


async def ffmpeg(*args, timeout=900):
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _o, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("обробка відео триває задовго — спробуйте коротший фрагмент")
    if proc.returncode:
        raise RuntimeError("ffmpeg: " + err.decode("utf-8", "replace")[-300:])


VERT_BLUR = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
             "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
VERT_CROP = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"


def sub_filter(srt_path):
    style = ("FontName=Lora,Bold=1,FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00312A10,BackColour=&H80312A10,"
             "BorderStyle=3,Outline=1,Shadow=0,MarginV=60,Alignment=2")
    p = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"subtitles='{p}':fontsdir='{BRAND}':force_style='{style}'"


# ---------------------------------------------------------------- фото
EXIF_TAGS = {36867: "DateTimeOriginal", 306: "DateTime", 271: "Make", 272: "Model", 305: "Software", 34853: "GPSInfo"}


def gps_deg(v, ref):
    try:
        d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        return -d if ref in ("S", "W") else d
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def photo_facts(data):
    """-> dict з даними файлу: розмір, дата зйомки, камера, програма, GPS, середній хеш."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    f = {"size": im.size, "format": im.format}
    try:
        ex = im.getexif()
        for k, name in EXIF_TAGS.items():
            if k in ex and k != 34853:
                f[name] = str(ex.get(k)).strip("\x00 ")
        dto = ex.get_ifd(0x8769).get(36867) if hasattr(ex, "get_ifd") else None
        if dto:
            f["DateTimeOriginal"] = str(dto).strip("\x00 ")
        gps = ex.get_ifd(0x8825) if hasattr(ex, "get_ifd") else {}
        if gps and 2 in gps and 4 in gps:
            lat, lon = gps_deg(gps[2], gps.get(1)), gps_deg(gps[4], gps.get(3))
            if lat is not None and lon is not None:
                f["gps"] = (round(lat, 5), round(lon, 5))
    except Exception:  # noqa: BLE001 — зіпсовані дані файлу: просто без них
        log.info("exif read failed", exc_info=True)
    f["ahash"] = ahash(im)
    return f


def ahash(im):
    g = im.convert("L").resize((8, 8))
    px = list(g.getdata())
    avg = sum(px) / 64
    return "".join("1" if p > avg else "0" for p in px)


def hamming(a, b):
    return sum(x != y for x, y in zip(a or "", b or "")) if a and b and len(a) == len(b) else 64


def facts_text(f, now=None):
    now = now or dt.datetime.now()
    L = [f"📐 {f['size'][0]}×{f['size'][1]} · {f.get('format') or '?'}"]
    when = f.get("DateTimeOriginal") or f.get("DateTime")
    if when:
        L.append(f"📅 Знято: {esc(when)}")
        try:
            d = dt.datetime.strptime(when[:19], "%Y:%m:%d %H:%M:%S")
            age = (now - d).days
            if age > 3:
                L.append(f"⚠️ Фото зроблене {age} дн. тому — якщо його подають як сьогоднішнє, перевірте")
        except ValueError:
            pass
    else:
        L.append("📅 Дати зйомки у файлі немає (Telegram і соцмережі її стирають — надсилайте оригінал файлом)")
    cam = " ".join(x for x in (f.get("Make"), f.get("Model")) if x)
    if cam:
        L.append(f"📷 Камера: {esc(cam)}")
    if f.get("Software"):
        L.append(f"🛠 Програма: {esc(f['Software'])}" + (" — можливо, редагувалося" if re.search(r"photoshop|gimp|lightroom|snapseed|canva|picsart", f["Software"], re.I) else ""))
    if f.get("gps"):
        lat, lon = f["gps"]
        L.append(f"📍 Місце зйомки: {lat}, {lon} — https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}")
    return "\n".join(L)


class ToolsUI:
    """Мікс до «Новинарні»."""

    # ---------------------------------------------------------------- медіатека
    def ml(self):
        return self.store.get("mlib") or []

    def ml_add(self, kind, author="", license_="", page="", src="", did=0, ahash_="", note=""):
        lib = self.ml()
        nid = max([x["id"] for x in lib] + [0]) + 1
        lib.append({"id": nid, "at": time.time(), "kind": kind, "author": author[:80], "license": license_[:60], "page": page[:400],
                    "src": src[:60], "did": did, "hash": ahash_, "note": note[:120]})
        self.store.set("mlib", lib[-600:])
        return nid

    def ml_seen(self, h, skip_id=None):
        return [x for x in self.ml() if x.get("hash") and x["id"] != skip_id and hamming(h, x["hash"]) <= 6]

    def ml_screen(self, q=""):
        lib = self.ml()
        if q:
            ql = q.lower()
            lib = [x for x in lib if ql in (x["author"] + " " + x["license"] + " " + x["note"] + " " + x["src"]).lower()]
        L = ["🗃 <b>Медіатека</b>", "", f"Фото, що використовувались у картках і перевірках, — з автором і ліцензією. Усього: {len(self.ml())}"
             + (f" · за «{esc(q)}»: {len(lib)}" if q else ""), ""]
        kinds = {"stock": "🔎 вільна бібліотека", "source": "🌐 з джерела новини", "own": "📷 ваше", "check": "🕵️ перевірене"}
        for x in lib[-15:][::-1]:
            when = dt.datetime.fromtimestamp(x["at"]).strftime("%d.%m")
            link = f" · <a href=\"{esc(x['page'])}\">сторінка</a>" if x.get("page") else ""
            L.append(f"#{x['id']} · {when} · {kinds.get(x['kind'], x['kind'])}" + (f" · новина #{x['did']}" if x.get("did") else "")
                     + f"\n   {esc(x['author'] or 'автор невідомий')} · {esc(x['license'] or 'ліцензія невідома')}{link}")
        if not lib:
            L.append("Поки порожньо.")
        return "\n".join(L)[:4000], [[("🔎 Пошук", self.cb("tmq"))], [("« Інструменти", self.cb("tl"))]]

    # ---------------------------------------------------------------- екрани
    def tools_screen(self):
        ff = "✅" if shutil.which("ffmpeg") else "⚠️ немає ffmpeg"
        text = ("🧰 <b>Інструменти редакції</b>\n\n"
                f"🎬 <b>Відео для Reels, Shorts, TikTok</b> {ff} — вертикаль 9:16, фрагмент, субтитри.\n"
                "🎙 <b>Розшифрувати інтерв'ю</b> — текст із часом, цитати, чернетка новини.\n"
                "🕵️ <b>Перевірити фото</b> — коли й де знято, чи редагувалося, чи вже було в нас.\n"
                "🗃 <b>Медіатека</b> — фото з авторами й ліцензіями.\n"
                "🔴 <b>Онлайн-трансляція</b> — один допис у каналі, що оновлюється.\n"
                "📆 <b>Рік тому ми писали</b> · 📬 <b>Тижнева розсилка</b> · 🧾 <b>Журнал виправлень</b>\n\n"
                "SEO для сайту, англійська версія, опитування й оновлення опублікованого — під кожною новиною в «⋯ Ще».\n"
                "Файли — до 2 ГБ (понад 20 МБ бот забирає напряму, трохи довше).")
        rows = [[("🎬 Відео для Reels", self.cb("tv")), ("🎙 Інтерв'ю", self.cb("ti"))],
                [("🕵️ Перевірити фото", self.cb("tp")), ("🗃 Медіатека", self.cb("tm"))],
                [("🔴 Онлайн-трансляція", self.cb("lv")), ("🧾 Журнал виправлень", self.cb("crl"))],
                [("📆 Рік тому ми писали", self.cb("yago")), ("📬 Тижнева розсилка", self.cb("nl"))],
                [("« Новинарня", self.cb())]]
        return text, rows

    def video_screen(self):
        c = self.store.get("tv_cfg") or {"fit": "blur", "subs": True}
        return ("🎬 <b>Відео для Reels, Shorts, TikTok</b>\n\nНадішліть відео (до 2 ГБ) — бот зробить вертикальне 1080×1920"
                + (" з розмитим тлом (нічого не обрізається)" if c["fit"] == "blur" else " з обрізкою по центру")
                + (" і субтитрами українською (шрифт Lora; окремо — файл .srt)" if c["subs"] else "") + ".\n\n"
                "Щоб вирізати фрагмент — у підписі до відео напишіть час, напр. <code>0:10-0:45</code>.",
                [[("📤 Надіслати відео", self.cb("tva"))],
                 [("🖼 Тло: " + ("розмите" if c["fit"] == "blur" else "обрізка"), self.cb("tvc", "fit")),
                  ("💬 Субтитри: " + ("так" if c["subs"] else "ні"), self.cb("tvc", "subs"))],
                 [("« Інструменти", self.cb("tl"))]])

    async def tools_route(self, chat, a, x, y):
        if a == "tl":
            return None, self.tools_screen()
        if a == "tv":
            return None, self.video_screen()
        if a == "tvc":
            c = self.store.get("tv_cfg") or {"fit": "blur", "subs": True}
            if x == "fit":
                c["fit"] = "crop" if c["fit"] == "blur" else "blur"
            else:
                c["subs"] = not c["subs"]
            self.store.set("tv_cfg", c)
            return None, self.video_screen()
        if a == "tva":
            self.tool_arm[chat] = ("video", 0)
            return None, ("🎬 Надішліть відео одним повідомленням (до 2 ГБ). Фрагмент — у підписі: <code>0:10-0:45</code>.",
                          [[("✖️ Скасувати", self.cb("tv"))]])
        if a == "ti":
            self.tool_arm[chat] = ("interview", 0)
            return None, ("🎙 <b>Розшифровка інтерв'ю</b>\n\nНадішліть аудіо, голосове або відео (до 20 МБ). Отримаєте текст із часовими "
                          "мітками (файлом), головні цитати — дослівно з розшифровки, і кнопку «📰 Зробити новину».",
                          [[("✖️ Скасувати", self.cb("tl"))]])
        if a == "tp":
            self.tool_arm[chat] = ("photo", 0)
            return None, ("🕵️ <b>Перевірка фото</b>\n\nНадішліть фото <b>файлом</b> (як документ): так зберігаються дата, камера й місце "
                          "зйомки. Звичайне фото Telegram стискає й стирає ці дані — тоді перевірю лише, чи траплялося воно в нас.",
                          [[("✖️ Скасувати", self.cb("tl"))]])
        if a == "tm":
            return None, self.ml_screen()
        if a == "tmq":
            self.ask(chat, "tmq")
            return None, ("🔎 Що шукаємо в медіатеці? Автор, ліцензія, джерело…", [[("✖️ Скасувати", "m:cancel")]])
        if a == "tid":  # з розшифровки — у новину
            tr = self.store.get(f"tr:{chat}") or {}
            if not tr.get("text"):
                return "Розшифровку не знайдено", self.tools_screen()
            iid, _st, _o = self.store.add_item("interview", tr["text"][:12000], source_name="інтерв'ю (розшифровка)", requested_by=chat)
            self._enqueue(iid)
            return "📰 Пишу новину", ("📰 Новинарня пише новину з розшифровки — чернетка прийде окремим повідомленням.",
                                     [[("« Інструменти", self.cb("tl"))]])
        return None, self.tools_screen()

    async def tools_text(self, chat, key, data, text):
        return None, self.ml_screen(text.strip()[:60])

    # ---------------------------------------------------------------- файли
    async def tools_on_file(self, update, ctx):
        msg = update.message
        chat = msg.chat_id if msg else None
        armed = self.tool_arm.pop(chat, None) if chat is not None else None
        if not armed:
            return False
        kind = armed[0]
        att = (msg.video or msg.audio or msg.voice or getattr(msg, "video_note", None) or msg.document
               or (msg.photo[-1] if msg.photo else None))
        if not att:
            await msg.reply_text("Потрібен файл. Натисніть кнопку ще раз і надішліть його.")
            return True
        size = getattr(att, "file_size", 0) or 0
        if size > MAX_TG:  # більше 20 МБ — напряму через MTProto (до 2 ГБ), як великі файли в 📥 Медіа
            big = getattr(self.host.extra.get("plugin", lambda pid: None)("med"), "big", None)
            if size > MAX_BIG or not big or not big.ready():
                await msg.reply_text("⚠️ Файл більший за " + ("2 ГБ" if size > MAX_BIG else "20 МБ") + " — "
                                     + ("це межа Telegram." if size > MAX_BIG else "для великих файлів потрібні TELEGRAM_API_ID і TELEGRAM_API_HASH у Railway."))
                return True
            await msg.reply_text(f"📥 Великий файл ({size // (1024 * 1024)} МБ) — забираю напряму, це може зайняти хвилину-дві…")
            try:  # великий файл лишається на диску (у пам'ять не читаємо) — обробка бере його звідти й прибирає за собою
                keep = Path(tempfile.mkdtemp(prefix="orion-tool-"))
                src_path = await big.download(chat, msg.message_id, keep / ("in" + (Path(getattr(att, "file_name", "") or "").suffix or ".bin")))
            except Exception as exc:  # noqa: BLE001
                await msg.reply_text(f"⚠️ Не вдалося забрати файл: {str(exc)[:200]}")
                return True
            data = src_path.read_bytes() if kind == "photo" else b""
        else:
            src_path = None
            data = bytes(await (await att.get_file()).download_as_bytearray())
        name = getattr(att, "file_name", None) or {"video": "video.mp4", "interview": "audio.mp3", "photo": "photo.jpg"}[kind]
        if kind == "photo":
            await self.photo_check(chat, data, is_file=bool(msg.document))
        elif kind == "video":
            self._bg(self.video_job(chat, data, name, (msg.caption or "").strip(), src_path))
            await msg.reply_text("🎬 Обробляю відео — це кілька хвилин. Результат прийде окремим повідомленням.")
        else:
            self._bg(self.interview_job(chat, data, name, src_path))
            await msg.reply_text("🎙 Розшифровую — це до кількох хвилин. Результат прийде окремим повідомленням.")
        return True

    async def photo_check(self, chat, data, is_file=True):
        try:
            f = await asyncio.to_thread(photo_facts, data)
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Не вдалося прочитати зображення: {esc(str(exc))[:200]}")
        seen = self.ml_seen(f["ahash"])
        nid = self.ml_add("check", ahash_=f["ahash"], note="перевірка фото")
        L = ["🕵️ <b>Перевірка фото</b>", "", facts_text(f)]
        if not is_file:
            L.append("ℹ️ Надіслано як фото — Telegram стер дані зйомки. Для повної перевірки надішліть оригінал файлом.")
        if seen:
            L += ["", "🔁 <b>Схоже фото вже було в нас:</b>"] + [
                f"• #{x['id']} {dt.datetime.fromtimestamp(x['at']):%d.%m.%Y}" + (f" · новина #{x['did']}" if x.get("did") else "")
                + (f" · {esc(x['author'])}" if x.get("author") else "") for x in seen[:5]]
        else:
            L += ["", "У нашій медіатеці такого фото ще не було."]
        L += ["", "🔎 <b>Де шукати старіші копії</b> (завантажте туди це фото):",
              "• Google Лінза — https://lens.google.com/upload",
              "• TinEye — https://tineye.com (показує найдавнішу появу в мережі)",
              "• Bing Візуальний пошук — https://www.bing.com/visualsearch",
              "Якщо фото з'являлося раніше за подію, про яку йдеться, — його використали повторно."]
        await self.host.notify(chat, "\n".join(L)[:4000], [[("🗃 Медіатека", self.cb("tm")), ("« Інструменти", self.cb("tl"))]])
        return nid

    async def video_job(self, chat, data, name, caption, src_path=None):
        c = self.store.get("tv_cfg") or {"fit": "blur", "subs": True}
        span = parse_span(caption)
        bot = self.host.bot()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if src_path:
                    src = Path(src_path)
                else:
                    src = Path(tmp) / ("in" + (Path(name).suffix or ".mp4"))
                    src.write_bytes(data)
                cut = ["-ss", str(span[0]), "-to", str(span[1])] if span else []
                srt_text = ""
                vf = VERT_BLUR if c["fit"] == "blur" else VERT_CROP
                if c["subs"]:
                    aud = Path(tmp) / "a.mp3"
                    await ffmpeg(*cut, "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(aud))
                    import media
                    segs, _prov = await media.transcribe_timed(aud.read_bytes(), "a.mp3", "uk")
                    if segs and segs[0][1] > 0:
                        srt_text = to_srt(segs)
                        (Path(tmp) / "s.srt").write_text(srt_text, "utf-8")
                        vf = vf + "," + sub_filter(Path(tmp) / "s.srt") if c["fit"] == "crop" else vf + "[v0];[v0]" + sub_filter(Path(tmp) / "s.srt")
                out = Path(tmp) / "reels.mp4"
                fargs = ["-filter_complex", vf] if c["fit"] == "blur" else ["-vf", vf]
                await ffmpeg(*cut, "-i", str(src), *fargs, "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-r", "30", str(out))
                size = out.stat().st_size
                from telegram import InputFile
                if size > 49 * 1024 * 1024:  # більше 50 МБ — теж напряму через MTProto
                    big = getattr(self.host.extra.get("plugin", lambda pid: None)("med"), "big", None)
                    if not big or not big.ready():
                        raise RuntimeError("результат більший за 50 МБ — оберіть коротший фрагмент (у підписі: 0:10-0:45)")
                    await big.send(chat, out, caption="🎬 Вертикальне відео 1080×1920", video={"width": 1080, "height": 1920})
                    if srt_text:
                        await bot.send_document(chat, InputFile(srt_text.encode("utf-8"), filename="subtitles.srt"), caption="💬 Субтитри (.srt)")
                    return
                cap = "🎬 Вертикальне відео 1080×1920" + (f" · фрагмент {ts(span[0])}–{ts(span[1])}" if span else "") + \
                      (" · із субтитрами" if srt_text else (" · без субтитрів (мовлення не розпізнано)" if c["subs"] else ""))
                await bot.send_video(chat, InputFile(out.read_bytes(), filename="reels_1080x1920.mp4"), caption=cap,
                                     supports_streaming=True, width=1080, height=1920, read_timeout=120, write_timeout=120)
                if srt_text:
                    await bot.send_document(chat, InputFile(srt_text.encode("utf-8"), filename="subtitles.srt"),
                                            caption="💬 Субтитри окремим файлом (.srt) — для YouTube, TikTok чи редагування")
        except Exception as exc:  # noqa: BLE001
            log.info("video job failed", exc_info=True)
            await self.host.notify(chat, f"⚠️ Відео не вдалося обробити: {esc(str(exc))[:300]}", [[("🎬 Ще раз", self.cb("tv"))]])
        finally:
            if src_path:
                shutil.rmtree(Path(src_path).parent, ignore_errors=True)

    async def interview_job(self, chat, data, name, src_path=None):
        import media
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if src_path:
                    src = Path(src_path)
                else:
                    src = Path(tmp) / ("in" + (Path(name).suffix or ".mp3"))
                    src.write_bytes(data)
                aud = Path(tmp) / "a.mp3"
                try:  # стискаємо до голосу: менше файл — швидше й надійніше розшифровка
                    await ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(aud))
                except Exception:  # noqa: BLE001
                    aud = src
                segs = []
                if aud.stat().st_size > 18 * 1024 * 1024:  # довгий запис (> ~50 хв) — частинами по 20 хв, час зсуваємо
                    await ffmpeg("-i", str(aud), "-f", "segment", "-segment_time", "1200", "-c", "copy", str(Path(tmp) / "part%03d.mp3"))
                    for k, part in enumerate(sorted(Path(tmp).glob("part*.mp3"))):
                        ps, _prov = await media.transcribe_timed(part.read_bytes(), part.name)
                        segs += [(a + k * 1200, b + k * 1200 if b else 0.0, t) for a, b, t in ps]
                else:
                    segs, _prov = await media.transcribe_timed(aud.read_bytes(), "a.mp3")
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Не вдалося розшифрувати: {esc(str(exc))[:300]}", [[("🎙 Ще раз", self.cb("ti"))]])
        finally:
            if src_path:
                shutil.rmtree(Path(src_path).parent, ignore_errors=True)
        timed = segs and segs[0][1] > 0
        text = "\n".join((f"[{ts(a)}] " if timed else "") + t for a, _b, t in segs)
        self.store.set(f"tr:{chat}", {"text": text, "at": time.time()})
        quotes = []
        try:
            data_, _m = await self.engine._json(
                "Вибери 3–5 найсильніших цитат з розшифровки інтерв'ю для новини. Цитати ДОСЛІВНО з тексту (не змінюй жодного слова), "
                "з часовою міткою, якщо вона є. Відповідай ЛИШЕ JSON {\"quotes\": [{\"t\": \"01:23\", \"q\": \"…\"}]}.\n\n" + text[:12000],
                "Ти — редактор новин.", "🎙 Цитати з інтерв'ю")
            for q in data_.get("quotes") or []:
                qt = str(q.get("q") or "").strip().strip("«»\"")
                if qt and re.sub(r"\W+", " ", qt.lower()).strip() in re.sub(r"\W+", " ", text.lower()):  # лише справді дослівні
                    quotes.append((str(q.get("t") or ""), qt))
        except Exception:  # noqa: BLE001
            pass
        L = [f"🎙 <b>Розшифровка готова</b> · {len(segs)} фраз · {len(text)} символів" + (" · з часовими мітками" if timed else ""), ""]
        if quotes:
            L += ["<b>Головні цитати</b> (дослівно):"] + [f"• {esc(t) + ' · ' if t else ''}«{esc(q)}»" for t, q in quotes[:5]]
        else:
            L.append("Цитати автоматично не виділено — повний текст у файлі.")
        from telegram import InputFile
        bot = self.host.bot()
        await bot.send_document(chat, InputFile(text.encode("utf-8"), filename="rozshyfrovka.txt"), caption="📝 Повна розшифровка")
        await self.host.notify(chat, "\n".join(L)[:4000], [[("📰 Зробити новину", self.cb("tid"))], [("« Інструменти", self.cb("tl"))]])
