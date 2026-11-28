"""🌐 Екрани й фонові задачі для Facebook / Instagram / Threads (мікс до «Новинарні»). Логіка API — у meta.py."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from plugins import esc
from . import meta as MT

log = logging.getLogger("orion.newsroom.metaui")
GUIDE = ("<b>Як підключити</b> (один раз, безкоштовно):\n"
         "1. developers.facebook.com → «Мої застосунки» → «Створити» → тип «Бізнес».\n"
         "2. Додайте продукти: «Facebook Login for Business», «Instagram» (через сторінку) і «Threads API».\n"
         "3. Graph API Explorer: оберіть свою сторінку, дозволи pages_manage_posts, pages_read_engagement, pages_manage_engagement, "
         "read_insights, instagram_basic, instagram_content_publish, instagram_manage_comments, instagram_manage_insights → "
         "«Generate Access Token» → обміняйте на довгостроковий токен сторінки.\n"
         "4. Threads: у налаштуваннях Threads API згенеруйте токен користувача (threads_basic, threads_content_publish, "
         "threads_read_replies, threads_manage_replies, threads_manage_insights) і довгостроковий токен.\n"
         "5. Railway → сервіс → Variables: META_PAGE_ID, META_PAGE_TOKEN, META_IG_ID, THREADS_TOKEN, THREADS_USER_ID. "
         "У чат ключі не пишіть.")
FLAG_RE = re.compile(r"(?i)https?://|t\.me/|заробіток|крипт|казино|ставк[иа]|переходь|пиши в лс|\bdm\b|bit\.ly|подарунок за")


class MetaUI:
    def mz_screen(self):
        lines = ["🌐 <b>Facebook · Instagram · Threads</b>", "",
                 "Бот публікує сам із «🗓 Черги публікацій», забирає охоплення ваших дописів у статистику тижня, «найкращий час» "
                 "і «♻️ Варто повторити», показує нові коментарі й допомагає відповісти.", ""]
        for net, name in MT.NETS.items():
            miss = MT.missing(net)
            lines.append(f"{'✅' if not miss else '—'} {name}" + ("" if not miss else f" · потрібно: {', '.join(miss)}"))
        last = self.store.get("mz_synced")
        if last:
            lines.append(f"\n🔄 Статистика оновлена: {time.strftime('%d.%m %H:%M', time.localtime(float(last)))}")
        any_ready = any(MT.ready(n) for n in MT.NETS)
        if not any_ready:
            lines += ["", GUIDE]
        rows = []
        if any_ready:
            rows.append([("💬 Нові коментарі", self.cb("mzc")), ("🔄 Забрати статистику", self.cb("mzs"))])
            rows.append([("🧪 Перевірити підключення", self.cb("mzt"))])
        else:
            rows.append([("ℹ️ Як підключити — ще раз", self.cb("mzg"))])
        rows.append([("« Публікація", self.cb("set"))])
        return "\n".join(lines)[:4000], rows

    async def mz_sync(self):
        """Останні дописи кожної підключеної мережі з охопленням → «🏠 Наші ресурси» (для статистики тижня й «♻️»)."""
        n = 0
        for net, name in MT.NETS.items():
            if not MT.ready(net):
                continue
            try:
                posts = await MT.recent_posts(net)
            except Exception as exc:  # noqa: BLE001
                log.warning("meta: статистика %s не отримана: %s", net, exc)
                continue
            for p in posts:
                if not p.get("url") and not p.get("text"):
                    continue
                self.desk.add_ours(name, p.get("url") or "", "", p.get("text") or "(без тексту)", at=p.get("at") or None,
                                   key=("u:" + p["url"]) if p.get("url") else None, platform=net,
                                   metrics={k: p.get(k) for k in ("views", "likes", "reposts", "comments")})
                n += 1
        self.store.set("mz_synced", time.time())
        return n

    async def _mz_loop(self):
        await asyncio.sleep(200)
        while True:
            try:
                if self.on() and any(MT.ready(n) for n in MT.NETS):
                    await self.mz_sync()
                    await self.mz_new_comments(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("meta loop")
            await asyncio.sleep(3 * 3600)

    async def mz_new_comments(self, notify=False):
        """Нові коментарі під останніми дописами (по 5 у кожній мережі) -> список; зберігаємо, щоб кнопки були короткими."""
        seen = set(self.store.get("mz_seen") or [])
        found = []
        for net in MT.NETS:
            if not MT.ready(net):
                continue
            try:
                posts = (await MT.recent_posts(net, limit=5))[:5]
            except Exception:  # noqa: BLE001
                continue
            for p in posts:
                try:
                    cs = await MT.comments(net, p["id"])
                except Exception:  # noqa: BLE001
                    continue
                for c in cs:
                    if c["id"] in seen or not c.get("text"):
                        continue
                    found.append({"net": net, "cid": c["id"], "text": c["text"][:500], "who": c.get("who") or "", "post": (p.get("text") or "")[:120],
                                  "url": p.get("url") or "", "flag": bool(FLAG_RE.search(c["text"]))})
        if found:
            box = (self.store.get("mz_box") or [])[-80:] + found
            self.store.set("mz_box", box[-100:])
            self.store.set("mz_seen", list(seen | {c["cid"] for c in found})[-2000:])
            if notify:
                await self.host.notify_admins(f"💬 Нові коментарі у Facebook / Instagram / Threads: {len(found)}"
                                              + (f" · 🚩 підозрілих {sum(c['flag'] for c in found)}" if any(c["flag"] for c in found) else ""),
                                              [[("💬 Переглянути", self.cb("mzc"))]])
        return found

    def mz_box_screen(self):
        box = self.store.get("mz_box") or []
        lines = ["💬 <b>Коментарі</b> · Facebook, Instagram, Threads", "",
                 "💡 — ШІ запропонує відповідь у стилі редакції (надсилаєте ви); 🙈 — сховати (спам, образи). 🚩 — схоже на спам.", ""]
        rows = []
        for i, c in list(enumerate(box))[-8:][::-1]:
            lines.append(f"{MT.NETS[c['net']].split()[0]} {'🚩 ' if c.get('flag') else ''}<b>{esc(c['who'] or 'хтось')}</b>: {esc(c['text'][:200])}"
                         + (f"\n   ↳ до: {esc(c['post'][:70])}" if c.get("post") else ""))
            rows.append([(f"💡 #{i}", self.cb("mzr", i)), (f"🙈 #{i}", self.cb("mzh", i)), (f"✓ #{i}", self.cb("mzd", i))])
        if not box:
            lines.append("Нових коментарів немає.")
        rows.append([("🔄 Перевірити зараз", self.cb("mzc", "now")), ("« Мережі", self.cb("mz"))])
        return "\n".join(lines)[:4000], rows

    async def mz_route(self, chat, a, x, y):
        box = self.store.get("mz_box") or []
        i = int(x) if str(x).isdigit() else -1
        c = box[i] if 0 <= i < len(box) else None
        if a == "mz":
            return None, self.mz_screen()
        if a == "mzg":
            return None, (MT.__doc__.split("\n\n")[0] + "\n\n" + GUIDE, [[("« Мережі", self.cb("mz"))]])
        if a == "mzs":
            n = await self.mz_sync()
            return f"🔄 Забрано дописів: {n}", self.mz_screen()
        if a == "mzt":
            lines = ["🧪 <b>Перевірка підключення</b>", ""]
            for net, name in MT.NETS.items():
                if not MT.ready(net):
                    lines.append(f"— {name}: не налаштовано ({', '.join(MT.missing(net))})")
                    continue
                try:
                    ps = await MT.recent_posts(net, limit=3)
                    lines.append(f"✅ {name}: доступ є, останніх дописів {len(ps)}")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"❌ {name}: {esc(str(exc)[:200])}")
            return None, ("\n".join(lines), [[("« Мережі", self.cb("mz"))]])
        if a == "mzc":
            if x == "now":
                await self.mz_new_comments()
            return None, self.mz_box_screen()
        if not c:
            return "Коментар уже прибрано", self.mz_box_screen()
        if a == "mzd":
            box.pop(i)
            self.store.set("mz_box", box)
            return "Позначено як переглянуте", self.mz_box_screen()
        if a == "mzh":
            ok, err = await MT.hide(c["net"], c["cid"])
            if ok:
                box.pop(i)
                self.store.set("mz_box", box)
            return ("🙈 Сховано" if ok else f"Не вдалося: {err[:150]}"), self.mz_box_screen()
        if a == "mzr":
            try:
                ans, _m = await asyncio.wait_for(self.host.ask_ai(
                    f"Коментар під нашим дописом ({MT.NETS[c['net']]}). Допис: «{c['post']}». Коментар від {c['who']}: «{c['text']}».\n"
                    "Напиши коротку, ввічливу відповідь від імені редакції GreenPost українською (1–3 речення, без вигадок, без емодзі-спаму). "
                    "Якщо це спам чи образа — відповідай одним словом: СПАМ.", label="💬 Відповідь на коментар"), 45)
            except Exception:  # noqa: BLE001
                ans = ""
            ans = (ans or "").strip()
            if not ans or ans.upper().startswith("СПАМ"):
                return None, (f"🚩 Схоже на спам чи образу: «{esc(c['text'][:200])}»", [[(f"🙈 Сховати #{i}", self.cb("mzh", i)), ("« Коментарі", self.cb("mzc"))]])
            c["draft"] = ans[:800]
            self.store.set("mz_box", box)
            return None, (f"💡 <b>Пропонована відповідь</b> на «{esc(c['text'][:150])}»:\n\n<pre>{esc(c['draft'])}</pre>\n\n"
                          "Надішліть як є або напишіть свою.",
                          [[(f"📤 Надіслати #{i}", self.cb("mzp", i)), (f"✏️ Своя #{i}", self.cb("mzw", i))], [("« Коментарі", self.cb("mzc"))]])
        if a == "mzw":
            self.ask(chat, "mzw", i)
            return None, (f"✏️ Напишіть відповідь на «{esc(c['text'][:150])}» — бот надішле її від імені сторінки.", [[("✖️ Скасувати", self.cb("mzc"))]])
        if a == "mzp":
            ok, err = await MT.reply(c["net"], c["cid"], c.get("draft") or "")
            if ok:
                box.pop(i)
                self.store.set("mz_box", box)
            return ("📤 Відповідь надіслано" if ok else f"Не вдалося: {err[:150]}"), self.mz_box_screen()
        return None, self.mz_screen()

    async def mz_publish(self, net, text, image_url=None):
        """Для черги публікацій: опублікувати версію для мережі, якщо мережу підключено. -> (ok, посилання/помилка) або None."""
        if net not in MT.NETS or not MT.ready(net):
            return None
        return await MT.publish(net, text, image_url=image_url)

    async def mz_text(self, chat, key, data, text):
        box = self.store.get("mz_box") or []
        i = int(data) if str(data).isdigit() else -1
        if not (0 <= i < len(box)):
            return "Коментар уже прибрано", self.mz_box_screen()
        ok, err = await MT.reply(box[i]["net"], box[i]["cid"], text.strip()[:800])
        if ok:
            box.pop(i)
            self.store.set("mz_box", box)
        return ("📤 Відповідь надіслано" if ok else f"Не вдалося: {err[:150]}"), self.mz_box_screen()
