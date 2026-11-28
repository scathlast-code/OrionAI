"""🖼 Картки до новин: заголовок на фото у фірмовому стилі, формати 1:1 · 4:5 · 9:16, підпис видання й логотип.

Фото — ваше (надіслати боту), з джерела новини (головне зображення сторінки) або без фото (кольорове тло).
Налаштування спільні для всіх новин: колір, розташування заголовка, затемнення, великі літери, формати, підпис, логотип."""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from plugins import esc

log = logging.getLogger("orion.newsroom.cards")

SIZES = {"sq": ("1:1", (1080, 1080), "Instagram, Facebook, Telegram"), "pt": ("4:5", (1080, 1350), "стрічка Instagram"),
         "st": ("9:16", (1080, 1920), "сторіс, Reels, Shorts")}
# 🌿 Фірмовий стиль GreenPost (з брендбука 2020): темно-зелений, світло-зелений, темно-синій; шрифт Lora (жирний)
NAVY, GREEN, LEAF, WHITE = (16, 42, 49), (37, 107, 74), (140, 195, 148), (255, 255, 255)
THEMES = {"dark": ("🌑 Темна", "фото з темно-синім затемненням, білий текст, біле лого"),
          "light": ("⚪ Світла", "біле тло або світла смуга під текстом, темно-синій текст, кольорове лого"),
          "green": ("🌿 Зелена", "темно-зелене тло/смуга, білий текст, біле лого")}
POS = {"bottom": "унизу", "top": "угорі", "center": "по центру"}
SHADE = {"light": ("легке", 140), "mid": ("середнє", 195), "strong": ("сильне", 235)}
DEFAULTS = {"theme": "dark", "pos": "bottom", "shade": "mid", "upper": False, "sizes": ["sq", "pt", "st"], "brand": "greenpost.ua",
            "label": True, "logo": True}
BRAND = Path(__file__).with_name("brand")
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]


def font(size, bold=True):
    p = BRAND / ("Lora-Bold.ttf" if bold else "Lora-Regular.ttf")
    if p.exists():
        return ImageFont.truetype(str(p), size)
    for d in FONT_DIRS:
        q = Path(d) / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        if q.exists():
            return ImageFont.truetype(str(q), size)
    return ImageFont.load_default(size)


def brand_logo(theme):
    p = BRAND / ("logo_color.png" if theme == "light" else "logo_white.png")
    return p.read_bytes() if p.exists() else None


def wrap(draw, text, fnt, width):
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if draw.textlength(test, font=fnt) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, width, max_h, start=92, low=40, max_lines=6):
    """Найбільший кегль, з яким заголовок вміщується в рамку (не більше max_lines рядків)."""
    size = start
    while size >= low:
        f = font(size)
        lines = wrap(draw, text, f, width)
        if len(lines) <= max_lines and len(lines) * int(size * 1.2) <= max_h:
            return f, lines, size
        size -= 4
    f = font(low)
    lines = wrap(draw, text, f, width)[:max_lines]
    if lines:
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return f, lines, low


def render(photo, headline, cfg, key="sq", label="", logo=None):
    """-> JPEG-байти картки потрібного формату у фірмовому стилі."""
    W, H = SIZES[key][1]
    theme = cfg.get("theme") if cfg.get("theme") in THEMES else "dark"
    bg = {"dark": NAVY, "light": WHITE, "green": GREEN}[theme]
    ink = NAVY if theme == "light" else WHITE
    pad = 80
    safe = 250 if key == "st" else pad  # сторіс: верх і низ перекриває інтерфейс Instagram
    if photo:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
        img = ImageOps.fit(img, (W, H), Image.LANCZOS, centering=(0.5, 0.4))
    else:
        img = Image.new("RGB", (W, H), bg)
    text = headline.upper() if cfg.get("upper") else headline
    draw = ImageDraw.Draw(img)
    zone_h = int(H * (0.44 if key != "st" else 0.36))
    fnt, lines, size = fit_text(draw, text, W - 2 * pad - 10, zone_h - 120)
    lh = int(size * 1.2)
    block = len(lines) * lh
    pos = cfg.get("pos", "bottom")
    foot = 70 if (cfg.get("brand") or "").strip() else 0
    lab_h = 74 if (label and cfg.get("label", True)) else 0
    if pos == "top":
        y0 = safe + 150 + lab_h
    elif pos == "center":
        y0 = (H - block) // 2
    else:
        y0 = H - safe - foot - block
    if photo:
        if theme == "dark":  # плавне темно-синє затемнення там, де текст
            alpha = SHADE.get(cfg.get("shade"), SHADE["mid"])[1]
            grad = Image.new("L", (1, H), 0)
            top, bot = max(0, y0 - lab_h - 280), min(H, y0 + block + foot + 140)
            for y in range(H):
                if pos == "center":
                    v = alpha if top <= y <= bot else 0
                elif pos == "top":
                    v = int(alpha * max(0.0, 1 - y / max(1, bot)))
                else:
                    v = int(alpha * max(0.0, (y - top) / max(1, H - top)))
                grad.putpixel((0, y), max(0, min(255, v)))
            img = Image.composite(Image.new("RGB", (W, H), NAVY), img, grad.resize((W, H)).filter(ImageFilter.GaussianBlur(30)))
            top_shade = Image.new("L", (1, H), 0)  # легке затемнення вгорі — щоб біле лого читалося на світлому фото
            for y in range(min(H, 260)):
                top_shade.putpixel((0, y), int(110 * (1 - y / 260)))
            img = Image.composite(Image.new("RGB", (W, H), NAVY), img, top_shade.resize((W, H)))
        else:  # світла чи зелена: суцільна смуга під текстом
            y_a, y_b = (y0 - lab_h - 50, y0 + block + foot + 40)
            if pos == "bottom":
                y_b = H
            if pos == "top":
                y_a = 0
            band = Image.new("RGB", (W, max(1, y_b - y_a)), bg)
            img.paste(band, (0, max(0, y_a)))
    draw = ImageDraw.Draw(img)
    if label and cfg.get("label", True):  # плашка рубрики: світло-зелена, темно-синій текст
        lf = font(34)
        t = label.upper() if cfg.get("upper") else label
        lw = int(draw.textlength(t, font=lf)) + 44
        ly = y0 - lab_h
        draw.rounded_rectangle((pad, ly, pad + lw, ly + 54), 27, fill=LEAF)
        draw.text((pad + 22, ly + 6), t, font=lf, fill=NAVY)
    for i, ln in enumerate(lines):
        draw.text((pad, y0 + i * lh), ln, font=fnt, fill=ink)
    brand = (cfg.get("brand") or "").strip()
    if brand:
        bf = font(36, bold=False)
        by = y0 + block + 22 if pos != "top" else H - safe - 50
        draw.line((pad, by + 22, pad + 46, by + 22), fill=LEAF if theme != "green" else WHITE, width=4)
        draw.text((pad + 62, by), brand, font=bf, fill=(LEAF if theme == "dark" else ink))
    lg_bytes = logo if logo else (brand_logo(theme if (photo is None or theme != "dark") else "dark") if cfg.get("logo", True) else None)
    if lg_bytes:
        try:
            lg = Image.open(io.BytesIO(lg_bytes)).convert("RGBA")
            lg.thumbnail((330, 130))
            ly = safe - 20 if pos != "top" else H - safe - lg.height
            if pos == "top" and brand:
                ly = H - safe - lg.height - 80
            img.paste(lg, (pad, max(30, ly)), lg)
        except Exception:  # noqa: BLE001 — зіпсований логотип не має ламати картку
            log.info("logo paste failed", exc_info=True)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


OG_RE = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)(?::src)?[\"'][^>]*>", re.I)


def og_image(html):
    for tag in OG_RE.findall(html or ""):
        m = re.search(r"content=[\"']([^\"']+)", tag)
        if m and m[1].startswith("http"):
            return m[1].replace("&amp;", "&")
    return None


class CardsUI:
    """Мікс до «Новинарні»: екрани й надсилання карток."""

    def cd_cfg(self):
        return {**DEFAULTS, **(self.store.get("cd_cfg") or {})}

    def cd_save(self, **kw):
        self.store.set("cd_cfg", {**(self.store.get("cd_cfg") or {}), **kw})

    def cd_photo_path(self, did):
        return self.dir / f"card_{did}.jpg"

    def cd_logo(self):
        p = self.dir / "card_logo.png"
        return p.read_bytes() if p.exists() else None

    def cd_screen(self, did):
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        c = self.cd_cfg()
        has = self.cd_photo_path(did).exists()
        fm = " · ".join(f"{SIZES[k][0]}" for k in ["sq", "pt", "st"] if k in c["sizes"])
        text = (f"🖼 <b>Картка до новини #{did}</b>\n<b>{esc(d['headline'])}</b>\n\n"
                "Заголовок на фото у вашому стилі — готові зображення для стрічки й сторіс.\n\n"
                f"Формати: {fm or 'не вибрано'}\nФото: {'вибране раніше ✅' if has else 'ще не вибрано'}")
        rows = [[("📷 Своє фото", self.cb("cdp", did)), ("🌐 Фото з джерела", self.cb("cdu", did))],
                [("🎨 Без фото", self.cb("cdn", did))] + ([("🔁 Ще раз з тим самим фото", self.cb("cdr", did))] if has else []),
                [("⚙️ Вигляд карток", self.cb("cds", did)), ("« Новина", self.cb("d", did))]]
        return text, rows

    def cd_settings(self, did=0):
        c = self.cd_cfg()
        back = self.cb("cd", did) if did else self.cb("set")
        text = ("⚙️ <b>Вигляд карток</b>\n\nДіє для всіх новин.\n\n"
                f"🎨 Тема: {THEMES[c['theme']][0]} — {THEMES[c['theme']][1]}\n📐 Заголовок: {POS[c['pos']]}\n🌑 Затемнення фото: {SHADE[c['shade']][0]}\n"
                f"🔠 Великі літери: {'так' if c['upper'] else 'ні'}\n🏷 Плашка рубрики: {'так' if c['label'] else 'ні'}\n"
                f"✒️ Підпис: {esc(c['brand'].strip()) or 'немає'}\n🖼 Логотип: {'свій' if self.cd_logo() else 'GreenPost' if c['logo'] else 'без логотипа'}\n"
                "🔤 Шрифт: Lora (жирний), кольори GreenPost\n\n"
                + "\n".join(f"{'✅' if k in c['sizes'] else '▫️'} {SIZES[k][0]} — {SIZES[k][2]}" for k in SIZES))
        rows = [[("🎨 Тема", self.cb("cdc", "theme", did)), ("📐 Де заголовок", self.cb("cdc", "pos", did))],
                [("🌑 Затемнення", self.cb("cdc", "shade", did)), ("🔠 Великі літери", self.cb("cdc", "upper", did))],
                [("🏷 Плашка рубрики", self.cb("cdc", "label", did)), ("✒️ Підпис на картці", self.cb("cdb", did))],
                [((("✅ " if k in c["sizes"] else "▫️ ") + SIZES[k][0]), self.cb("cdz", k, did)) for k in SIZES],
                [("🌿 Лого GreenPost: " + ("так" if c["logo"] else "ні"), self.cb("cdc", "logo", did))],
                [("🖼 Своє лого", self.cb("cdl", did))] + ([("🗑 Прибрати своє лого", self.cb("cdlx", did))] if self.cd_logo() else []),
                [("« Назад", back)]]
        return text, rows

    async def cd_send(self, chat, did, photo):
        d = self.store.draft(did)
        bot = self.host.bot()
        if not d or not bot:
            return
        c = self.cd_cfg()
        keys = [k for k in ["sq", "pt", "st"] if k in c["sizes"]] or ["sq"]
        label = d.get("category") or d.get("region") or ""
        from telegram import InputFile, InputMediaPhoto
        imgs = [(k, render(photo, d["headline"], c, k, label, self.cd_logo())) for k in keys]
        self.store.set(f"cdlast:{chat}", did)
        media = [InputMediaPhoto(InputFile(io.BytesIO(b), filename=f"kartka_{did}_{SIZES[k][0].replace(':', 'x')}.jpg"),
                                 caption=(f"🖼 Картки до #{did}: " + ", ".join(SIZES[x][0] for x, _ in imgs)) if i == 0 else None)
                 for i, (k, b) in enumerate(imgs)]
        if len(media) == 1:
            await bot.send_photo(chat, media[0].media, caption=media[0].caption)
        else:
            await bot.send_media_group(chat, media)
        await self.host.notify(chat, "🖼 Готово. Telegram трохи стискає фото — для публікації візьміть файли без стиснення.",
                               [[("📎 Файлами без стиснення", self.cb("cdf", did))],
                                [("🔁 Інше фото", self.cb("cd", did)), ("⚙️ Вигляд карток", self.cb("cds", did))],
                                [("« Новина", self.cb("d", did))]])

    async def cd_files(self, chat, did):
        bot, d = self.host.bot(), self.store.draft(did)
        p = self.cd_photo_path(did)
        photo = p.read_bytes() if p.exists() else None
        c = self.cd_cfg()
        from telegram import InputFile
        for k in [k for k in ["sq", "pt", "st"] if k in c["sizes"]] or ["sq"]:
            b = render(photo, d["headline"], c, k, d.get("category") or d.get("region") or "", self.cd_logo())
            await bot.send_document(chat, InputFile(io.BytesIO(b), filename=f"kartka_{did}_{SIZES[k][0].replace(':', 'x')}.jpg"))

    async def cd_route(self, chat, a, x, y):
        if a == "cd":
            return None, self.cd_screen(int(x))
        if a == "cdp":
            self.cd_arm[chat] = ("photo", int(x))
            return None, ("📷 <b>Надішліть фото</b> одним повідомленням (як фото або файлом — файлом якість краща).",
                          [[("✖️ Скасувати", self.cb("cd", x))]])
        if a == "cdl":
            self.cd_arm[chat] = ("logo", int(x or 0))
            return None, ("🖼 <b>Надішліть логотип</b> файлом PNG з прозорим тлом (стане в куток картки).",
                          [[("✖️ Скасувати", self.cb("cds", x or 0))]])
        if a == "cdlx":
            (self.dir / "card_logo.png").unlink(missing_ok=True)
            return "🗑 Прибрано", self.cd_settings(int(x or 0))
        if a in ("cdn", "cdr", "cdu"):
            did = int(x)
            if a == "cdn":
                self.cd_photo_path(did).unlink(missing_ok=True)
                photo = None
            elif a == "cdr":
                p = self.cd_photo_path(did)
                photo = p.read_bytes() if p.exists() else None
            else:
                photo = await self.cd_source_photo(did)
                if not photo:
                    return "Фото в джерелі не знайдено", self.cd_screen(did)
                self.cd_photo_path(did).write_bytes(photo)
            self._bg(self.cd_send(chat, did, photo))
            return "🖼 Малюю…", None
        if a == "cdf":
            self._bg(self.cd_files(chat, int(x)))
            return "📎 Надсилаю файлами", None
        if a == "cds":
            return None, self.cd_settings(int(x or 0))
        if a == "cdc":
            c = self.cd_cfg()
            cyc = {"theme": list(THEMES), "pos": list(POS), "shade": list(SHADE)}
            if x in cyc:
                v = cyc[x][(cyc[x].index(c[x]) + 1) % len(cyc[x])] if c[x] in cyc[x] else cyc[x][0]
                self.cd_save(**{x: v})
            elif x in ("upper", "label", "logo"):
                self.cd_save(**{x: not c[x]})
            return None, self.cd_settings(int(y or 0))
        if a == "cdz":
            s = self.cd_cfg()["sizes"]
            s = [k for k in s if k != x] if x in s else [k for k in SIZES if k in s or k == x]
            self.cd_save(sizes=s)
            return None, self.cd_settings(int(y or 0))
        if a == "cdb":
            self.ask(chat, "cdb", int(x or 0))
            return None, ("✒️ <b>Підпис на картках</b> — назва видання чи адреса сайту, напр. «greenpost.ua». «-» — без підпису.",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, self.cd_screen(int(x))

    async def cd_text(self, chat, key, data, text):
        t = text.strip()
        self.cd_save(brand=" " if t in ("-", "—") else t[:60])
        return "Збережено", self.cd_settings(int(data or 0))

    async def cd_source_photo(self, did):
        """Головне зображення сторінки першого джерела новини (og:image)."""
        d = self.store.draft(did)
        import httpx
        for url in (d.get("sources") or [])[:3]:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as cl:
                    r = await cl.get(url)
                    img = og_image(r.text[:400_000])
                    if not img:
                        continue
                    r2 = await cl.get(img)
                    if r2.status_code == 200 and r2.content[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF", b"GIF"):
                        return r2.content
            except Exception:  # noqa: BLE001
                log.info("og image failed for %s", url, exc_info=True)
        return None

    async def cd_on_file(self, update, ctx):
        msg = update.message
        chat = msg.chat_id if msg else None
        armed = self.cd_arm.pop(chat, None) if chat is not None else None
        if not armed:
            return False
        kind, did = armed
        f = (msg.photo[-1] if msg.photo else None) or (msg.document if msg.document and (msg.document.mime_type or "").startswith("image/") else None)
        if not f:
            await msg.reply_text("Потрібне зображення. Натисніть кнопку ще раз і надішліть фото.")
            return True
        data = bytes(await (await ctx.bot.get_file(f.file_id)).download_as_bytearray())
        if kind == "logo":
            (self.dir / "card_logo.png").write_bytes(data)
            await self.host.notify(chat, *self.cd_settings(did))
            return True
        self.cd_photo_path(did).write_bytes(data)
        await self.cd_send(chat, did, data)
        return True
