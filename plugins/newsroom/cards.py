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
from . import stock

log = logging.getLogger("orion.newsroom.cards")

# Розміри — за шаблоном редакції: квадрат для більшості мереж, вертикаль 1170×1540 для Instagram і TikTok
SIZES = {"sq": ("1080×1080", (1080, 1080), "Telegram, Facebook, Threads, Tumblr, YouTube, X"),
         "ig": ("1170×1540", (1170, 1540), "Instagram, TikTok")}
# 🌿 Фірмові кольори GreenPost (брендбук 2020 + шаблон карток): тло міняється, решта стилю — та сама
NAVY, GREEN, LEAF, WHITE, MIST = (16, 42, 49), (37, 107, 74), (140, 195, 148), (255, 255, 255), (208, 220, 206)
BG = {"mist": ("🩶", "світло-сіро-зелений (як у шаблоні)", MIST), "white": ("⚪", "білий", WHITE),
      "leaf": ("🟢", "світло-зелений", LEAF), "green": ("🌲", "темно-зелений", GREEN), "navy": ("🔵", "темно-синій", NAVY)}
DARK_BG = {"green", "navy"}
THEMES = {"dark": ("🌑 Темна", "фото з темно-синім затемненням, білий текст, біле лого"),
          "light": ("⚪ Світла", "світла смуга під текстом, темно-синій текст, кольорове лого"),
          "green": ("🌿 Зелена", "темно-зелена смуга під текстом, білий текст, біле лого")}
POS = {"bottom": "унизу", "top": "угорі", "center": "по центру"}
SHADE = {"light": ("легке", 140), "mid": ("середнє", 195), "strong": ("сильне", 235)}
DEFAULTS = {"bg": "mist", "upper": False, "sizes": ["sq", "ig"], "brand": "", "label": False, "logo": True,
            "theme": "dark", "pos": "bottom", "shade": "mid", "plabel": True, "pbrand": "greenpost.ua", "pstyle": "tpl", "tone": "brand", "stock": True}
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


TONES = {"brand": "фірмові кольори (темно-синій · зелений · світлий)", "soft": "м'які фірмові (оригінал + зелений відтінок)",
         "orig": "оригінальні кольори", "bw": "чорно-біле"}


def tone(img, mode):
    if mode in (True, "brand"):
        return brand_tone(img)
    if mode == "soft":
        return Image.blend(img.convert("RGB"), brand_tone(img), 0.55)
    if mode == "bw":
        return ImageOps.autocontrast(img.convert("L"), cutoff=1).convert("RGB")
    return img


def brand_tone(img):
    """Фото → лише фірмові кольори: тіні — темно-синій, середні — темно-зелений, світлі — світло-сіро-зелений."""
    g = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    stops = [(0, NAVY), (150, GREEN), (255, MIST)]
    lut = []
    for ch in range(3):
        for v in range(256):
            for (a, ca), (b, cb) in zip(stops, stops[1:]):
                if a <= v <= b:
                    t = (v - a) / max(1, b - a)
                    lut.append(int(ca[ch] + (cb[ch] - ca[ch]) * t))
                    break
    return Image.merge("RGB", (g, g, g)).point(lut)


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
    """Шаблон GreenPost (з фото вгорі або лише текст); стиль «поверх фото» — якщо так вибрано в налаштуваннях."""
    if photo and cfg.get("pstyle") == "over":
        return render_photo(photo, headline, cfg, key, label, logo)
    return render_template(headline, cfg, key, label, logo, photo)


def render_template(headline, cfg, key="sq", label="", logo=None, photo=None):
    """-> JPEG-байти картки за шаблоном GreenPost: суцільне фірмове тло, заголовок Lora жирним по центру,
    логотип по центру внизу (ширина ≈ 42% картки, відступ знизу ≈ 4.7%)."""
    W, H = SIZES.get(key, SIZES["sq"])[1]
    bg = cfg.get("bg") if cfg.get("bg") in BG else "mist"
    dark = bg in DARK_BG
    ink = WHITE if dark else NAVY
    img = Image.new("RGB", (W, H), BG[bg][2])
    draw = ImageDraw.Draw(img)
    pad = int(W * 0.09)
    # логотип
    lg_bytes = logo or (brand_logo("dark" if dark else "light") if cfg.get("logo", True) else None)
    logo_top = H - int(H * 0.047)
    if lg_bytes:
        try:
            lg = Image.open(io.BytesIO(lg_bytes)).convert("RGBA")
            lw = int(W * 0.417)
            lg = lg.resize((lw, max(1, int(lg.height * lw / lg.width))), Image.LANCZOS)
            logo_top = H - int(H * 0.047) - lg.height
            img.paste(lg, ((W - lw) // 2, logo_top), lg)
        except Exception:  # noqa: BLE001 — зіпсований логотип не має ламати картку
            log.info("logo paste failed", exc_info=True)
    top = int(H * 0.08)
    big, low_, max_lines = int(W * 0.085), int(W * 0.04), 7
    if photo:  # фото вгорі в рамці фірмового тла, із заокругленими кутами
        m = int(W * 0.04)
        ph_h = int(H * (0.55 if key == "ig" else 0.52))
        try:
            ph = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
            ph = ImageOps.fit(ph, (W - 2 * m, ph_h), Image.LANCZOS, centering=(0.5, 0.4))
            ph = tone(ph, cfg.get("tone", "brand"))
            mask = Image.new("L", ph.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ph.width, ph.height), int(W * 0.025), fill=255)
            img.paste(ph, (m, m), mask)
            top = m + ph_h + int(H * 0.03)
            big, low_, max_lines = int(W * 0.064), int(W * 0.036), 4
        except Exception:  # noqa: BLE001 — зіпсоване фото: картка лише з текстом
            log.info("card photo failed", exc_info=True)
    brand = (cfg.get("brand") or "").strip()
    bottom = logo_top - int(H * 0.04)
    if brand:
        bf = font(int(W * 0.033), bold=False)
        bw = draw.textlength(brand, font=bf)
        draw.text(((W - bw) / 2, bottom - int(W * 0.04)), brand, font=bf, fill=ink)
        bottom -= int(W * 0.07)
    # заголовок по центру вільного поля
    text = headline.upper() if cfg.get("upper") else headline
    fnt, lines, size = fit_text(draw, text, W - 2 * pad, bottom - top - 20, start=big, low=low_, max_lines=max_lines)
    lh = int(size * 1.22)
    lab_h = 0
    if label and cfg.get("label"):
        lab_h = int(W * 0.05) + 30
    block = len(lines) * lh + lab_h
    y = top + max(0, (bottom - top - block) // 2)
    if lab_h:
        lf = font(int(W * 0.031))
        t = label.upper() if cfg.get("upper") else label
        tw = int(draw.textlength(t, font=lf)) + 44
        x0 = (W - tw) // 2
        draw.rounded_rectangle((x0, y, x0 + tw, y + int(W * 0.05)), int(W * 0.025), fill=(LEAF if bg != "leaf" else WHITE))
        draw.text((x0 + 22, y + int(W * 0.005)), t, font=lf, fill=NAVY)
        y += lab_h
    rec = cfg.get("_out")
    if rec is not None:
        rec.update(size=size, ink=ink, lines=[])
    for ln in lines:
        w = draw.textlength(ln, font=fnt)
        if rec is not None:
            rec["lines"].append(((W - w) / 2, y, ln))
        if not cfg.get("_notext"):
            draw.text(((W - w) / 2, y), ln, font=fnt, fill=ink)
        y += lh
    out = io.BytesIO()
    img.save(out, "JPEG", quality=93, optimize=True)
    return out.getvalue()


def render_photo(photo, headline, cfg, key="sq", label="", logo=None):
    """-> JPEG-байти картки «на фото»: фото на все тло, заголовок Lora поверх (теми темна/світла/зелена)."""
    W, H = SIZES[key][1]
    theme = cfg.get("theme") if cfg.get("theme") in THEMES else "dark"
    cfg = {**cfg, "label": cfg.get("plabel", True), "brand": cfg.get("pbrand", "greenpost.ua")}
    bg = {"dark": NAVY, "light": WHITE, "green": GREEN}[theme]
    ink = NAVY if theme == "light" else WHITE
    pad = 80
    safe = 200 if key == "ig" else pad  # TikTok перекриває низ і верх інтерфейсом — текст туди не ставимо
    if photo:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
        img = ImageOps.fit(img, (W, H), Image.LANCZOS, centering=(0.5, 0.4))
        img = tone(img, cfg.get("tone", "brand"))
    else:
        img = Image.new("RGB", (W, H), bg)
    text = headline.upper() if cfg.get("upper") else headline
    draw = ImageDraw.Draw(img)
    zone_h = int(H * (0.44 if key != "ig" else 0.40))
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
    rec = cfg.get("_out")
    if rec is not None:
        rec.update(size=size, ink=ink, lines=[(pad, y0 + i * lh, ln) for i, ln in enumerate(lines)])
    for i, ln in enumerate(lines):
        if not cfg.get("_notext"):
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


def card_pdf(photo, headline, cfg, key="sq", label="", logo=None):
    """PDF для Canva: тло з фото й лого — зображенням, заголовок — справжнім текстом (шрифт Lora), тож у Canva його
    можна редагувати, а фото, кольори й розташування — міняти як завгодно."""
    import fitz  # PyMuPDF
    W, H = SIZES.get(key, SIZES["sq"])[1]
    rec = {}
    render(photo, headline, {**cfg, "_out": rec}, key, label, logo)  # де й яким кеглем стоїть заголовок
    base = render(photo, headline, {**cfg, "_notext": True}, key, label, logo)  # те саме без заголовка
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    page.insert_image(fitz.Rect(0, 0, W, H), stream=base)
    fpath = BRAND / "Lora-Bold.ttf"
    page.insert_font(fontname="lora", fontfile=str(fpath))
    asc = font(rec.get("size", 60)).getmetrics()[0]
    color = tuple(c / 255 for c in rec.get("ink", NAVY))
    for x, y, ln in rec.get("lines", []):
        page.insert_text((x, y + asc), ln, fontname="lora", fontsize=rec["size"], color=color)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


async def canva_import(pdf, title):
    """Імпорт PDF у Canva вашого акаунта -> посилання на редагування (живе 30 днів, відкривається лише вам)."""
    import asyncio
    import base64
    import json as _json
    import canva  # модуль бота: вхід у Canva (OAuth)
    import httpx
    tok = await canva.access_token()
    meta = {"title_base64": base64.b64encode(title[:50].encode()).decode(), "mime_type": "application/pdf"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post("https://api.canva.com/rest/v1/imports", content=pdf,
                          headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/octet-stream",
                                   "Import-Metadata": _json.dumps(meta)})
        if r.status_code >= 400:
            raise RuntimeError(f"Canva: {r.status_code} {r.text[:200]}")
        job = r.json()["job"]
        for _ in range(40):
            if job.get("status") == "success":
                d = (job.get("result") or {}).get("designs") or [{}]
                return d[0].get("urls", {}).get("edit_url") or d[0].get("url")
            if job.get("status") == "failed":
                raise RuntimeError("Canva: " + ((job.get("error") or {}).get("message") or "імпорт не вдався"))
            await asyncio.sleep(2)
            job = (await cl.get(f"https://api.canva.com/rest/v1/imports/{job['id']}", headers={"Authorization": f"Bearer {tok}"})).json()["job"]
    raise RuntimeError("Canva довго не відповідає — спробуйте ще раз")


OG_RE = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)(?::src)?[\"'][^>]*>", re.I)


def og_image(html):
    for tag in OG_RE.findall(html or ""):
        m = re.search(r"content=[\"']([^\"']+)", tag)
        if m and m[1].startswith("http"):
            return m[1].replace("&amp;", "&")
    return None


class CardsUI:
    """Мікс до «Новинарні»: картки за шаблоном GreenPost."""

    def cd_cfg(self):
        c = {**DEFAULTS, **(self.store.get("cd_cfg") or {})}
        c["sizes"] = [k for k in c["sizes"] if k in SIZES] or list(SIZES)
        return c

    def cd_save(self, **kw):
        self.store.set("cd_cfg", {**(self.store.get("cd_cfg") or {}), **kw})

    def cd_logo(self):
        p = self.dir / "card_logo.png"
        return p.read_bytes() if p.exists() else None

    def _cd_label(self, d):
        return d.get("category") or d.get("region") or ""

    def cd_screen(self, did):
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        c = self.cd_cfg()
        own = self.store.get(f"cdown:{did}") and self.cd_photo_path(did).exists()
        nophoto = self.store.get(f"cdnoph:{did}")
        ph = ("ваше (закріплене) ✅" if own else "без фото (лише текст)" if nophoto else
              "щоразу нова під тему новини" if c["stock"] else "з джерела новини")
        text = (f"🖼 <b>Картки до новини #{did}</b>\n<b>{esc(d['headline'])}</b>\n\n"
                f"📷 Картинка: {ph}\n🎨 Стиль картинки: {TONES.get(c['tone'] if c['tone'] in TONES else 'brand')}\n"
                + "\n".join(f"{'✅' if k in c['sizes'] else '▫️'} {SIZES[k][0]} — {SIZES[k][2]}" for k in SIZES)
                + "\n\n<b>Шаблон GreenPost</b> (фото в рамці, заголовок під ним) — виберіть колір тла:\n"
                + "<b>На все фото</b> (заголовок поверх) — виберіть тему:\nКартки прийдуть одразу; «🔀 Інша картинка» — нова під ту саму тему.")
        colors = [((("● " if k == c["bg"] else "") + v[0]), self.cb("cdn", did, k)) for k, v in BG.items()]
        overs = [(v[0], self.cb("cdn", did, "o" + k)) for k, v in THEMES.items()]
        rows = [colors, overs,
                [("📷 Своє фото", self.cb("cdp", did)), ("🌐 Фото з джерела", self.cb("cdu", did))],
                [("🔀 Під тему (знову щоразу нова)", self.cb("cda", did)), ("🚫 Без фото", self.cb("cdx", did))],
                [("🎨 Стиль картинки", self.cb("cdtone", did)), ("⚙️ Вигляд карток", self.cb("cds", did))],
                [("« Новина", self.cb("d", did))]]
        return text, rows

    def cd_photo_path(self, did):
        return self.dir / f"card_{did}.jpg"

    def cd_settings(self, did=0):
        c = self.cd_cfg()
        back = self.cb("cd", did) if did else self.cb("set")
        text = ("⚙️ <b>Вигляд карток</b>\n\nДіє для всіх новин. Шаблон GreenPost: фото в рамці вгорі (або лише текст), міняється тло; або фото на все тло з заголовком поверх. Розміри завжди: 1080×1080 і 1170×1540.\n\n"
                f"🎨 Тло за замовчуванням: {BG[c['bg']][0]} {BG[c['bg']][1]}\n"
                f"🔠 Великі літери: {'так' if c['upper'] else 'ні'}\n🏷 Плашка рубрики над заголовком: {'так' if c['label'] else 'ні'}\n"
                f"✒️ Рядок над логотипом: {esc(c['brand'].strip()) or 'немає'}\n"
                f"🖼 Логотип: {'свій' if self.cd_logo() else 'GreenPost' if c['logo'] else 'без логотипа'}\n"
                "🔤 Шрифт: Lora (жирний)\n"
                f"🎨 Стиль картинки: {TONES.get(c['tone'] if c['tone'] in TONES else 'brand')}\n"
                f"🔎 Картинка під тему (щоразу інша, з вільних бібліотек Openverse і Wikimedia Commons): {'так' if c['stock'] else 'ні'}\n\n"
                f"<b>Фото на все тло</b> (якщо вибрано замість рамки): тема {THEMES[c['theme']][0]} — {THEMES[c['theme']][1]}; заголовок {POS[c['pos']]}; "
                f"затемнення {SHADE[c['shade']][0]}\n\n"
                + "\n".join(f"{'✅' if k in c['sizes'] else '▫️'} {SIZES[k][0]} — {SIZES[k][2]}" for k in SIZES))
        rows = [[(f"🎨 Тло за замовчуванням: {BG[c['bg']][0]}", self.cb("cdc", "bg", did))],
                [("🔠 Великі літери", self.cb("cdc", "upper", did)), ("🏷 Плашка рубрики", self.cb("cdc", "label", did))],
                [((("✅ " if k in c["sizes"] else "▫️ ") + SIZES[k][0]), self.cb("cdz", k, did)) for k in SIZES],
                [("✒️ Рядок над логотипом", self.cb("cdb", did)), ("🌿 Лого GreenPost: " + ("так" if c["logo"] else "ні"), self.cb("cdc", "logo", did))],
                [("📷 Фото: " + ("у рамці вгорі" if c["pstyle"] != "over" else "на все тло"), self.cb("cdc", "pstyle", did))],
                [("🎨 Стиль картинки: " + TONES.get(c["tone"] if c["tone"] in TONES else "brand").split(" (")[0], self.cb("cdc", "tone", did))],
                [("🔎 Картинка під тему: " + ("так" if c["stock"] else "ні"), self.cb("cdc", "stock", did))],
                [("📷 Тема на фото", self.cb("cdc", "theme", did)), ("📐 Де заголовок", self.cb("cdc", "pos", did)),
                 ("🌑 Затемнення", self.cb("cdc", "shade", did))],
                [("🖼 Своє лого", self.cb("cdl", did))] + ([("🗑 Прибрати своє лого", self.cb("cdlx", did))] if self.cd_logo() else []),
                [("« Назад", back)]]
        return text, rows

    def _cd_images(self, d, bg=None, photo=None):
        c = self.cd_cfg()
        if bg in BG:
            c["bg"] = bg
            c["pstyle"] = "tpl"
        elif bg and bg[:1] == "o" and bg[1:] in THEMES:  # «на все фото» з вибраною темою
            c["pstyle"], c["theme"] = "over", bg[1:]
        return [(k, render(photo, d["headline"], c, k, self._cd_label(d), self.cd_logo())) for k in SIZES if k in c["sizes"]]

    @staticmethod
    def _cd_name(did, k):
        return f"kartka_{did}_{SIZES[k][0].replace('×', 'x')}.jpg"

    async def cd_send(self, chat, did, bg=None, photo=None):
        d, bot = self.store.draft(did), self.host.bot()
        if not d or not bot:
            return
        from telegram import InputFile, InputMediaPhoto
        imgs = self._cd_images(d, bg, photo)
        self.store.set(f"cdbg:{did}", bg or self.store.get(f"cdbg:{did}") or self.cd_cfg()["bg"])
        self.store.set(f"cdph:{did}", bool(photo))
        cred = (self.store.get(f"cdcred:{did}") or "") if photo else ""
        cap = (f"🖼 Картки до #{did}: " + ", ".join(f"{SIZES[k][0]} ({SIZES[k][2]})" for k, _ in imgs) + (f"\n📸 {cred}" if cred else ""))[:1000]
        media = [InputMediaPhoto(InputFile(io.BytesIO(b), filename=self._cd_name(did, k)), caption=cap if i == 0 else None)
                 for i, (k, b) in enumerate(imgs)]
        if len(media) == 1:
            await bot.send_photo(chat, media[0].media, caption=cap)
        else:
            await bot.send_media_group(chat, media)
        cred_page = (self.store.get(f"cdcredurl:{did}") or "") if photo else ""
        await self.host.notify(chat, "🖼 Готово. Telegram трохи стискає фото — для публікації візьміть файли без стиснення."
                               + (f"\n📸 {esc(cred)}" + (f" — <a href=\"{esc(cred_page)}\">сторінка фото</a>" if cred_page else "")
                                  + "\nПри публікації вкажіть автора й ліцензію (вимога вільних ліцензій)." if cred and cred_page else ""),
                               [[("🔀 Інша картинка", self.cb("cdn", did)), ("📎 Файлами без стиснення", self.cb("cdf", did))],
                                [("🖌 Відкрити в Canva", self.cb("cdv", did))],
                                [("🎨 Інше тло", self.cb("cd", did)), ("⚙️ Вигляд карток", self.cb("cds", did))],
                                [("« Новина", self.cb("d", did))]])

    async def cd_stock_photo(self, did):
        """Нова картинка під тему новини (щоразу інша) -> (байти, опис) або (None, None)."""
        d = self.store.draft(did)
        qs = self.store.get(f"cdq:{did}")
        if not qs:
            try:
                data, _m = await self.engine._json(stock.QUERY_PROMPT + d["headline"] + "\n\n" + d["body"][:600],
                                                   "Ти — фоторедактор новинного видання.", "🖼 Картка: пошук фото")
            except Exception:  # noqa: BLE001
                data = {}
            qs = stock.parse_queries(data, d["headline"])
            self.store.set(f"cdq:{did}", qs)
        used = set(self.store.get(f"cdused:{did}") or [])
        recent = set(self.store.get("cdrecent") or [])
        cands = self.store.get(f"cdcand:{did}") or []
        if not [c for c in cands if c["id"] not in used]:
            cands = []
            for q in qs:
                cands += await stock.search(q)
                if len([c for c in cands if c["id"] not in used and c["id"] not in recent]) >= 6:
                    break
            self.store.set(f"cdcand:{did}", cands[:60])
        fresh = [c for c in cands if c["id"] not in used and c["id"] not in recent] or [c for c in cands if c["id"] not in used]
        for it in fresh[:5]:
            used.add(it["id"])
            try:
                b = await stock.fetch(it["url"])
            except Exception:  # noqa: BLE001
                b = None
            if b:
                self.store.set(f"cdused:{did}", list(used)[-200:])
                self.store.set("cdrecent", (list(recent) + [it["id"]])[-300:])
                return b, it
        self.store.set(f"cdused:{did}", list(used)[-200:])
        return None, None

    async def cd_make(self, chat, did, bg=None):
        c, p = self.cd_cfg(), self.cd_photo_path(did)
        photo = None
        if self.store.get(f"cdnoph:{did}"):
            photo = None
        elif self.store.get(f"cdown:{did}") and p.exists():
            photo = p.read_bytes()
        else:
            if c.get("stock", True):
                photo, it = await self.cd_stock_photo(did)
                if photo:
                    self.store.set(f"cdcred:{did}", stock.credit(it))
                    self.store.set(f"cdcredurl:{did}", it.get("page") or "")
            if photo is None:
                photo = await self.cd_source_photo(did)
                if photo:
                    self.store.set(f"cdcred:{did}", "Фото: з джерела новини")
                    self.store.set(f"cdcredurl:{did}", "")
            if photo:
                p.write_bytes(photo)
        await self.cd_send(chat, did, bg, photo)

    async def cd_canva(self, chat, did):
        """Ті самі картки — у Canva на повне редагування (кожен розмір окремим дизайном)."""
        try:
            import canva
            ok = canva.connected()
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return await self.host.notify(chat, "🖌 <b>Canva в боті ще не підключена.</b>\n\nУвійдіть один раз: Меню → 🎨 Створити → 🖌 Canva → "
                                          "«🔑 Увійти в Canva». Після цього кнопка «🖌 Відкрити в Canva» одразу створюватиме дизайни.",
                                          [[("🖌 Canva", "m:canva")], [("« Картки", self.cb("cd", did))]])
        d = self.store.draft(did)
        c = self.cd_cfg()
        bg = self.store.get(f"cdbg:{did}")
        if bg in BG:
            c["bg"], c["pstyle"] = bg, "tpl"
        elif bg and bg[:1] == "o" and bg[1:] in THEMES:
            c["pstyle"], c["theme"] = "over", bg[1:]
        p = self.cd_photo_path(did)
        photo = p.read_bytes() if self.store.get(f"cdph:{did}") and p.exists() else None
        links = []
        for k in [k for k in SIZES if k in c["sizes"]]:
            try:
                pdf = card_pdf(photo, d["headline"], c, k, self._cd_label(d), self.cd_logo())
                url = await canva_import(pdf, f"GreenPost #{did} {SIZES[k][0]}")
                links.append((k, url))
            except Exception as exc:  # noqa: BLE001
                log.info("canva import failed", exc_info=True)
                links.append((k, None, str(exc)[:150]))
        lines = ["🖌 <b>Картки в Canva</b> — заголовок там справжнім текстом, усе можна міняти.", ""]
        rows = []
        for it in links:
            if it[1]:
                lines.append(f"✅ {SIZES[it[0]][0]} — {SIZES[it[0]][2]}")
                rows.append([(f"🖌 {SIZES[it[0]][0]} у Canva", "url:" + it[1])])
            else:
                lines.append(f"⚠️ {SIZES[it[0]][0]}: {esc(it[2])}")
        lines.append("\nПосилання відкриваються лише у вашому акаунті Canva і діють 30 днів (дизайн лишається у ваших дизайнах).")
        await self.host.notify(chat, "\n".join(lines), rows + [[("« Картки", self.cb("cd", did))]])

    async def cd_files(self, chat, did):
        bot, d = self.host.bot(), self.store.draft(did)
        if not bot or not d:
            return
        from telegram import InputFile
        p = self.cd_photo_path(did)
        photo = p.read_bytes() if self.store.get(f"cdph:{did}") and p.exists() else None
        for k, b in self._cd_images(d, self.store.get(f"cdbg:{did}"), photo):
            await bot.send_document(chat, InputFile(io.BytesIO(b), filename=self._cd_name(did, k)))

    async def cd_route(self, chat, a, x, y, z=None):
        if a == "cd":
            return None, self.cd_screen(int(x))
        if a == "cdn":  # фото: своє/закріплене → нова картинка під тему → з джерела → лише текст
            did = int(x)
            ok = y in BG or (y or "")[:1] == "o" and (y or "")[1:] in THEMES
            self._bg(self.cd_make(chat, did, y if ok else self.store.get(f"cdbg:{did}")))
            return "🖼 Підбираю картинку й малюю…", None
        if a == "cdv":
            self._bg(self.cd_canva(chat, int(x)))
            return "🖌 Надсилаю в Canva…", None
        if a == "cda":  # повернутися до автопідбору картинок під тему
            self.store.set(f"cdown:{x}", None)
            self.store.set(f"cdnoph:{x}", None)
            return "🔀 Картинки знову під тему", self.cd_screen(int(x))
        if a == "cdtone":
            ks = list(TONES)
            cur = self.cd_cfg()["tone"]
            self.cd_save(tone=ks[(ks.index(cur) + 1) % len(ks)] if cur in ks else "soft")
            return "🎨 " + TONES[self.cd_cfg()["tone"]], self.cd_screen(int(x))
        if a == "cdx":
            self.cd_photo_path(int(x)).unlink(missing_ok=True)
            self.store.set(f"cdnoph:{x}", True)
            self.store.set(f"cdown:{x}", None)
            self._bg(self.cd_send(chat, int(x), None, None))
            return "🖼 Без фото", None
        if a == "cdp":
            self.cd_arm[chat] = ("photo", int(x))
            return None, ("📷 <b>Надішліть фото</b> одним повідомленням (файлом — якість краща).", [[("✖️ Скасувати", self.cb("cd", x))]])
        if a in ("cdr", "cdu"):
            did = int(x)
            if a == "cdr":
                p = self.cd_photo_path(did)
                photo = p.read_bytes() if p.exists() else None
            else:
                photo = await self.cd_source_photo(did)
                if photo:
                    self.cd_photo_path(did).write_bytes(photo)
                    self.store.set(f"cdnoph:{did}", None)
                    self.store.set(f"cdown:{did}", True)
                    self.store.set(f"cdcred:{did}", "Фото: з джерела новини")
            if not photo:
                return "Фото не знайдено", self.cd_screen(did)
            self._bg(self.cd_send(chat, did, None, photo))
            return "🖼 Малюю…", None
        if a == "cdf":
            self._bg(self.cd_files(chat, int(x)))
            return "📎 Надсилаю файлами", None
        if a == "cdl":
            self.cd_arm[chat] = ("logo", int(x or 0))
            return None, ("🖼 <b>Надішліть логотип</b> файлом PNG з прозорим тлом (стане по центру внизу).",
                          [[("✖️ Скасувати", self.cb("cds", x or 0))]])
        if a == "cdlx":
            (self.dir / "card_logo.png").unlink(missing_ok=True)
            return "🗑 Прибрано", self.cd_settings(int(x or 0))
        if a == "cds":
            return None, self.cd_settings(int(x or 0))
        if a == "cdc":  # x — що змінити; y — новина (для «Назад»); далі — значення
            c = self.cd_cfg()
            did, _, val = (y or "0").partition(".")
            if x == "bg":
                ks = list(BG)
                self.cd_save(bg=val if val in BG else ks[(ks.index(c["bg"]) + 1) % len(ks)] if c["bg"] in ks else ks[0])
            elif x == "pstyle":
                self.cd_save(pstyle="tpl" if c["pstyle"] == "over" else "over")
            elif x in ("theme", "pos", "shade"):
                cyc = {"theme": list(THEMES), "pos": list(POS), "shade": list(SHADE)}[x]
                self.cd_save(**{x: cyc[(cyc.index(c[x]) + 1) % len(cyc)] if c[x] in cyc else cyc[0]})
            elif x == "tone":
                ks = list(TONES)
                cur = c["tone"] if c["tone"] in ks else "brand"
                self.cd_save(tone=ks[(ks.index(cur) + 1) % len(ks)])
            elif x in ("upper", "label", "logo", "stock"):
                self.cd_save(**{x: not c[x]})
            return None, self.cd_settings(int(did or 0))
        if a == "cdz":
            s = self.cd_cfg()["sizes"]
            s = [k for k in s if k != x] if x in s else [k for k in SIZES if k in s or k == x]
            self.cd_save(sizes=s)
            return None, self.cd_settings(int(y or 0))
        if a == "cdb":
            self.ask(chat, "cdb", int(x or 0))
            return None, ("✒️ <b>Рядок над логотипом</b> — напр. адреса сайту «greenpost.ua». «-» — без рядка (як у шаблоні).",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, self.cd_screen(int(x))

    async def cd_text(self, chat, key, data, text):
        t = text.strip()
        self.cd_save(brand="" if t in ("-", "—") else t[:60])
        return "Збережено", self.cd_settings(int(data or 0))

    async def cd_on_file(self, update, ctx):
        msg = update.message
        chat = msg.chat_id if msg else None
        armed = self.cd_arm.pop(chat, None) if chat is not None else None
        if not armed:
            return False
        kind, did = armed
        f = (msg.photo[-1] if msg.photo else None) or (msg.document if msg.document and (msg.document.mime_type or "").startswith("image/") else None)
        if not f:
            await msg.reply_text("Потрібне зображення. Натисніть «🖼 Своє лого» ще раз і надішліть PNG.")
            return True
        data = bytes(await (await ctx.bot.get_file(f.file_id)).download_as_bytearray())
        if kind == "photo":
            self.cd_photo_path(did).write_bytes(data)
            self.store.set(f"cdnoph:{did}", None)
            self.store.set(f"cdown:{did}", True)
            self.store.set(f"cdcred:{did}", "")
            await self.cd_send(chat, did, None, data)
            return True
        (self.dir / "card_logo.png").write_bytes(data)
        await self.host.notify(chat, *self.cd_settings(did))
        return True

    async def cd_source_photo(self, did):
        """Головне зображення сторінки першого джерела новини (og:image)."""
        d = self.store.draft(did)
        import httpx
        for url in (d.get("sources") or [])[:3]:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as cl:
                    img = og_image((await cl.get(url)).text[:400_000])
                    if not img:
                        continue
                    r = await cl.get(img)
                    if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF", b"GIF"):
                        return r.content
            except Exception:  # noqa: BLE001
                log.info("og image failed for %s", url, exc_info=True)
        return None
