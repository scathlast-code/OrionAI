"""📲 Одна новина → версії для кожної соцмережі: Telegram, Instagram (підпис + тексти слайдів), Threads, X, Facebook.

Кожна мережа має свої межі (рахуємо самі, не довіряємо моделі):
- Telegram: 4096 символів тексту; під фото — 1024 (з Premium — 4096).
- Instagram: підпис до 2200 символів, до 30 хештегів; посилання в підписі не клікабельні; карусель — до 20 слайдів.
- Threads: до 500 символів; одна тема (тег) на допис.
- X: 280 символів без підписки; будь-яке посилання рахується як 23 символи.
- Facebook: межа дуже велика (63 206), але читають короткі дописи — рекомендуємо до ~600.
Версії зберігаються при новині; текст у блоці коду — натисніть на нього в Telegram, і він скопіюється цілком."""
from __future__ import annotations

import json
import logging
import re
import time

from plugins import esc

log = logging.getLogger("orion.newsroom.crosspost")

# ключ: (значок, назва, межа символів, рекомендовано, скільки хештегів максимум)
NETWORKS = {
    "tg": ("✈️", "Telegram", 4096, 1500, 5),
    "ig": ("📸", "Instagram", 2200, 1200, 30),
    "th": ("🧵", "Threads", 500, 450, 1),
    "x": ("𝕏", "X", 280, 270, 3),
    "fb": ("📘", "Facebook", 63206, 600, 5),
}
ORDER = ["tg", "ig", "th", "x", "fb"]
EMOJI = {"none": "без смайлів", "few": "небагато (1–3)", "many": "виразно (у кожному абзаці)"}
TONES = {"news": "стримано-новинний", "warm": "теплий, людяний", "brisk": "бадьорий, динамічний"}
URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"#[\wЀ-ӿ]+")

DEFAULTS = {"nets": ORDER[:], "tags": 3, "emoji": "few", "tone": "news", "ig_link": True, "slides": 6, "rules": ""}


def x_len(text):
    """Довжина для X: кожне посилання — 23 символи."""
    return len(URL_RE.sub("x" * 23, text or ""))


def net_len(net, text):
    return x_len(text) if net == "x" else len(text or "")


def trim(text, limit, net="tg"):
    """Обрізати до межі по реченню (а не посеред слова)."""
    text = (text or "").strip()
    if net_len(net, text) <= limit:
        return text
    cut = text
    while net_len(net, cut) > limit - 1 and cut:
        m = re.search(r"(?s)^(.*[.!?…])\s+\S", cut[:-1])
        cut = m[1] if m and len(m[1]) > limit * 0.5 else cut[:-max(1, (net_len(net, cut) - limit) + 1)].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + ("" if cut.endswith((".", "!", "?", "…")) else "…")


def norm_tags(tags, n):
    out = []
    for t in tags or []:
        t = re.sub(r"[^\wЀ-ӿ]", "", str(t).lstrip("#"))
        if t and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return ["#" + t for t in out[:max(0, n)]]


def prompt(d, cfg, nets):
    """Завдання моделі: одна відповідь JSON з версіями для вибраних мереж."""
    specs = []
    for k in nets:
        icon, name, limit, rec, mx = NETWORKS[k]
        n = min(cfg.get("tags", 3), mx)
        line = f'- "{k}" ({name}): до {rec} символів (жорстка межа {limit}), хештегів: {n}'
        if k == "ig":
            line += (f'; плюс "slides": {cfg.get("slides", 6)} коротких текстів для слайдів каруселі (кожен до 120 символів, перший — гачок-заголовок)'
                     + ('; посилання в підписі не клікабельні — замість них «посилання в шапці профілю»' if cfg.get("ig_link", True) else ""))
        if k == "x":
            line += "; посилання рахується як 23 символи; без води, головний факт у першому реченні"
        if k == "th":
            line += "; розмовніше, можна питання до читачів у кінці; лише одна тема-тег"
        if k == "tg":
            line += "; абзаци з відступами, жирний заголовок не потрібен (його додасть бот), можна список фактів"
        if k == "fb":
            line += "; перший рядок — суть, далі 2–4 короткі абзаци"
        specs.append(line)
    rules = (cfg.get("rules") or "").strip()
    return ("Адаптуй новину для соцмереж видання. Факти, імена, цифри, дати й цитати — ТІЛЬКИ з тексту новини, нічого не додавай "
            "і не перебільшуй. Мова — українська, без русизмів. Тон: " + TONES.get(cfg.get("tone"), TONES["news"]) +
            ". Смайли: " + EMOJI.get(cfg.get("emoji"), EMOJI["few"]) + ". Хештеги — українською, по суті, без загальних (#новини, #україна) і "
            "окремим полем, не в тексті.\n" + ("Додаткові правила редакції: " + rules + "\n" if rules else "") +
            "Мережі:\n" + "\n".join(specs) +
            '\n\nВідповідай ЛИШЕ JSON: {"<мережа>": {"text": "…", "tags": ["…"], "slides": ["…"]}} (slides лише для ig).\n\n'
            "НОВИНА\nЗаголовок: " + d.get("headline", "") + "\n\n" + d.get("body", "")[:6000] +
            ("\n\nДжерела: " + ", ".join(d.get("sources") or [])[:600] if d.get("sources") else ""))


def parse(data, cfg, nets):
    """Відповідь моделі → перевірені версії: межі символів, кількість хештегів, слайди."""
    out = {}
    for k in nets:
        v = (data or {}).get(k) or {}
        if isinstance(v, str):
            v = {"text": v}
        text = str(v.get("text") or "").strip()
        if not text:
            continue
        icon, name, limit, rec, mx = NETWORKS[k]
        found = TAG_RE.findall(text)
        text = TAG_RE.sub("", text).strip() if found and not v.get("tags") else text
        tags = norm_tags(v.get("tags") or found, min(cfg.get("tags", 3), mx))
        tail = ("\n\n" + " ".join(tags)) if tags else ""
        room = limit - net_len(k, tail)
        body = trim(text, room, k)
        item = {"text": body + tail, "tags": tags, "cut": body != text}
        if k == "ig":
            item["slides"] = [trim(str(s), 120) for s in (v.get("slides") or []) if str(s).strip()][:20]
        out[k] = item
    return out


class CrosspostUI:
    """Мікс до плагіна «Новинарня»: екрани «📲 Під мережі» для чернетки й налаштування."""

    def xp_cfg(self):
        return {**DEFAULTS, **(self.store.get("xp_cfg") or {})}

    def xp_save(self, **kw):
        self.store.set("xp_cfg", {**self.xp_cfg(), **kw})

    def xp_get(self, did):
        return self.store.get(f"xp:{did}") or {}

    async def xp_make(self, chat, did, only=None):
        d = self.store.draft(did)
        if not d:
            return
        cfg = self.xp_cfg()
        nets = [only] if only else [k for k in ORDER if k in cfg["nets"]]
        try:
            data, model = await self.engine._json(prompt(d, cfg, nets), "Ти — SMM-редактор новинного видання.", "📲 Під мережі")
            got = parse(data, cfg, nets)
        except Exception as exc:  # noqa: BLE001
            log.info("crosspost failed: %s", exc)
            await self.host.notify(chat, f"⚠️ Не вдалося підготувати версії для мереж: {esc(str(exc))[:200]}",
                                   [[("🔁 Спробувати ще", self.cb("xpg", did)), ("« Новина", self.cb("d", did))]])
            return
        saved = self.xp_get(did) if only else {}
        saved.update(got)
        saved["_at"], saved["_model"], saved["_v"] = time.time(), model, d.get("version")
        self.store.set(f"xp:{did}", saved)
        first = only or next((k for k in ORDER if k in got), "tg")
        await self.host.notify(chat, *self.xp_screen(did, first))

    def xp_screen(self, did, net=None):
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        saved, cfg = self.xp_get(did), self.xp_cfg()
        have = [k for k in ORDER if k in saved]
        if not have:
            nets = " · ".join(f"{NETWORKS[k][0]} {NETWORKS[k][1]}" for k in ORDER if k in cfg["nets"])
            return (f"📲 <b>Під мережі · #{did}</b>\n\n<b>{esc(d['headline'])}</b>\n\nПідготую окрему версію новини для кожної мережі — "
                    f"з її межею символів, хештегами й манерою.\n\nМережі: {nets or 'жодної не вибрано'}\n"
                    f"Тон: {TONES.get(cfg['tone'])} · смайли: {EMOJI.get(cfg['emoji'])} · хештегів: {cfg['tags']}",
                    [[("✨ Підготувати", self.cb("xpg", did))], [("⚙️ Налаштування", self.cb("xps", did))], [("« Новина", self.cb("d", did))]])
        net = net if net in have else have[0]
        icon, name, limit, rec, _ = NETWORKS[net]
        v = saved[net]
        n = net_len(net, v["text"])
        lines = [f"📲 <b>Під мережі · #{did}</b> · {icon} {name}", ""]
        if saved.get("_v") and saved["_v"] != d.get("version"):
            lines += ["⚠️ Новину змінено після підготовки версій — оновіть («🔁 Усі заново»).", ""]
        lines += [f"<pre>{esc(v['text'])}</pre>"]
        if v.get("slides"):
            lines += ["", f"🖼 <b>Слайди каруселі · {len(v['slides'])}</b>"] + [f"{i}. {esc(s)}" for i, s in enumerate(v["slides"], 1)]
        lines += ["", f"📏 {n} / {limit} символів{' (посилання — по 23)' if net == 'x' else ''}"
                  + (" · ✂️ скорочено ботом до межі" if v.get("cut") else "") + (f" · рекомендовано до {rec}" if n > rec else " ✅"),
                  "👆 Натисніть на текст у рамці — скопіюється цілком."]
        if saved.get("_model"):
            lines.append(f"🤖 {esc(saved['_model'])}")
        tabs = [((("● " if k == net else "") + NETWORKS[k][0]), self.cb("xp", did, k)) for k in have]
        rows = [tabs]
        if len(v["text"]) <= 256:
            rows.append([("📋 Копіювати", "copy:" + v["text"])])
        rows += [[(f"🔁 Заново {icon}", self.cb("xpg", did, net)), ("🔁 Усі заново", self.cb("xpg", did))],
                 [("⚙️ Налаштування", self.cb("xps", did)), ("« Новина", self.cb("d", did))]]
        return "\n".join(lines)[:4000], rows

    def xp_settings(self, did=0):
        c = self.xp_cfg()
        back = self.cb("xp", did) if did else self.cb("set")
        lines = ["⚙️ <b>Під мережі: налаштування</b>", "", "Для яких мереж готувати версії й у якій манері. Діє для всіх новин.", ""]
        for k in ORDER:
            icon, name, limit, rec, mx = NETWORKS[k]
            lines.append(f"{'✅' if k in c['nets'] else '▫️'} {icon} {name} — до {limit} симв." + (f", хештегів до {mx}" if mx < 30 else ""))
        lines += ["", f"🎙 Тон: {TONES.get(c['tone'])}", f"😀 Смайли: {EMOJI.get(c['emoji'])}", f"#️⃣ Хештегів: {c['tags']} (у Threads завжди 1)",
                  f"🖼 Слайдів для Instagram: {c['slides']}",
                  f"🔗 Instagram: {'«посилання в шапці профілю» замість адрес' if c['ig_link'] else 'адреси як є'}",
                  f"📜 Свої правила: {esc(c['rules'][:150]) if c['rules'] else 'немає'}"]
        nets = [((("✅ " if k in c["nets"] else "▫️ ") + NETWORKS[k][0]), self.cb("xpt", k, did)) for k in ORDER]
        rows = [nets,
                [("🎙 Тон", self.cb("xpc", "tone", did)), ("😀 Смайли", self.cb("xpc", "emoji", did))],
                [("#️⃣ Хештеги", self.cb("xpc", "tags", did)), ("🖼 Слайди", self.cb("xpc", "slides", did))],
                [("🔗 Посилання в Instagram", self.cb("xpc", "ig_link", did)), ("📜 Свої правила", self.cb("xpr", did))],
                [("« Назад", back)]]
        return "\n".join(lines), rows

    async def xp_route(self, chat, a, x, y):
        if a == "xp":
            return None, self.xp_screen(int(x), y)
        if a == "xpg":
            did = int(x)
            self._bg(self.xp_make(chat, did, y if y in NETWORKS else None))
            what = NETWORKS[y][1] if y in NETWORKS else "усіх вибраних мереж"
            return "✨ Готую…", (f"✨ <b>Готую версію для {esc(what)}</b> — прийде окремим повідомленням за хвилину.",
                                [[("« Новина", self.cb("d", did))]])
        if a == "xps":
            return None, self.xp_settings(int(x or 0))
        if a == "xpt":
            nets = self.xp_cfg()["nets"]
            nets = [k for k in nets if k != x] if x in nets else [k for k in ORDER if k in nets or k == x]
            self.xp_save(nets=nets)
            return None, self.xp_settings(int(y or 0))
        if a == "xpc":
            c = self.xp_cfg()
            nxt = {"tone": lambda v: list(TONES)[(list(TONES).index(v) + 1) % len(TONES)] if v in TONES else "news",
                   "emoji": lambda v: list(EMOJI)[(list(EMOJI).index(v) + 1) % len(EMOJI)] if v in EMOJI else "few",
                   "tags": lambda v: {0: 1, 1: 3, 3: 5, 5: 10, 10: 0}.get(v, 3),
                   "slides": lambda v: {4: 6, 6: 8, 8: 10, 10: 4}.get(v, 6),
                   "ig_link": lambda v: not v}
            if x in nxt:
                self.xp_save(**{x: nxt[x](c.get(x))})
            return None, self.xp_settings(int(y or 0))
        if a == "xpr":
            self.ask(chat, "xpr", int(x or 0))
            return None, ("📜 <b>Свої правила для соцмереж</b>\n\nНапишіть, наприклад: «Завжди закінчувати закликом підписатися на канал; "
                          "без слова «шокуючий»». Щоб прибрати — надішліть «-».", [[("✖️ Скасувати", "m:cancel")]])
        return None, self.xp_screen(int(x))

    async def xp_text(self, chat, key, data, text):
        self.xp_save(rules="" if text.strip() in ("-", "—") else text.strip()[:600])
        return "📜 Збережено", self.xp_settings(int(data or 0))
