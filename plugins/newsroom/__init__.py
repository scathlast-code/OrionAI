"""📰 Новинарня GreenPost — ШІ-редакція новин усередині Оріона.

Що робить:
- приймає новини: текст, посилання, переслані дописи, фото (у режимі прийому або /news), дописи каналів,
  де бот адміністратор, RSS/Atom-стрічки, WhatsApp Business Cloud API та вебхук для n8n/Make/Zapier;
- відсіює дублікати (точні й майже однакові тексти) і помічає оновлення вже відомої події;
- переписує новину за правилами GreenPost на моделях Оріона (Авто-перемикання між безкоштовними ШІ), з
  розділенням підтвердженого й непідтвердженого, джерелами, примітками для редактора;
- точна довжина тексту (напр. рівно 2000 символів) з програмною перевіркою й блокуванням схвалення;
- погодження: ✅ Схвалити · 🔁 Переписати · ✍️ Правка своїми словами · 📏 Довжина · 🔎 Звірити · ❌ Відхилити;
- історія версій, зведення за добу 2000/2100/2200 (Київ першим), публікація в канал лише після схвалення людиною.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime

from plugins import Plugin as Base, esc
from .engine import Engine, length_of, region_key
from .feeds import FeedError, fetch, parse, public_https
from .store import Store
from .lists import Lists, display_ref, passes
from .lists_ui import ListsUI
from .desk import Desk
from .desk_ui import DeskUI
from .actions import ActionsUI
from .digestmode import DigestMode
from .observances import ObservancesUI
from .report import ReportUI
from .crosspost import CrosspostUI
from .pubqueue import QueueUI
from .cards import CardsUI
from .styleguide import StyleUI
from .ourswrote import OursWroteUI
from .echo import KINDS as ECHO_KINDS, Echo
from .collectors import MTProto, WebCollector

log = logging.getLogger("orion.plugins.newsroom")

STATUS = {
    "draft": "🆕 Чернетка",
    "length_mismatch": "📏 Не та довжина",
    "approved": "✅ Схвалено",
    "rejected": "❌ Відхилено",
    "published": "📤 Опубліковано",
    "archived": "🗄 В архіві",
}
FILTERS = [("new", "🆕 Нові", ("draft", "length_mismatch")), ("ok", "✅ Схвалені", ("approved",)),
           ("pub", "📤 Опубліковані", ("published",)), ("rej", "❌ Відхилені", ("rejected",)), ("arc", "🗄 Архівні", ("archived",))]
LENGTHS = [0, 1000, 1500, 2000, 2100, 2200]
URGENCY = {"urgent": "🔴", "normal": "", "low": "⚪️"}
URL_RE = re.compile(r"https?://\S+")
PAGE = 8


def short(s, n=40):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def ago(ts):
    m = int((time.time() - ts) // 60)
    if m < 1:
        return "щойно"
    if m < 60:
        return f"{m} хв тому"
    if m < 1440:
        return f"{m // 60} год тому"
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


SOCIAL_KEYS_HELP = (
    "🔑 <b>Як підключити соцмережі</b>\n\n"
    "Ключі вписуються лише у файл <code>.env</code> на сервері — ніколи не надсилайте їх у чат.\n\n"
    "𝕏 <b>X (Twitter)</b>: <code>X_BEARER_TOKEN</code> (developer.x.com, платний тариф) — будь-які публічні акаунти + перегляди, лайки, репости.\n"
    "🧵 <b>Threads</b>: <code>THREADS_TOKEN</code>, <code>THREADS_USERNAME</code> — ваш акаунт із переглядами й реакціями; чужі — через RSSHub.\n"
    "📸 <b>Instagram</b>: <code>META_TOKEN</code> + <code>IG_USER_ID</code> (Business/Creator, привʼязаний до сторінки Facebook) — "
    "ваш акаунт і чужі Business/Creator-акаунти; особисті — лише через RSSHub.\n"
    "📘 <b>Facebook</b>: <code>FB_PAGE_TOKENS=сторінка=токен,…</code> — ваші сторінки з реакціями, коментарями, поширеннями; "
    "чужі сторінки — якщо застосунок Meta має дозвіл Page Public Content Access, інакше RSSHub.\n"
    "🌀 <b>Tumblr</b>: працює без ключа (RSS); <code>TUMBLR_API_KEY</code> додає кількість нотаток.\n"
    "▶️ <b>YouTube</b> і будь-який <b>RSS</b> — без ключів.\n\n"
    "🧰 <b>RSSHub</b> (<code>RSSHUB_URL</code>, необовʼязково <code>RSSHUB_KEY</code>) — власний сервіс у docker-compose, запасний шлях для "
    "чужих X/Threads/Instagram/Facebook без офіційного доступу. Він читає публічні сторінки, тож працює не завжди — і дотримання "
    "правил платформ на вас.\n\n"
    "Обмеження частоти: X — раз на 30 хв, Instagram — 20 хв, Facebook і Threads — 15 хв на акаунт."
)


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _dom(u):
    m = re.match(r"https?://(?:www\.)?([^/:?#]+)", u)
    return m.group(1) if m else u[:30]


def linkify(text):
    """Текст із «голими» адресами → HTML з гіперпосиланнями (назва сайту замість довгої адреси)."""
    out, pos = [], 0
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?")
        out.append(esc(text[pos:m.start()]))
        out.append(f'<a href="{esc(url)}">{esc(_dom(url))}</a>')
        pos = m.start() + len(url)
    out.append(esc((text or "")[pos:]))
    return "".join(out)


def src_links(sources, sep=" · "):
    return sep.join(f'<a href="{esc(s)}">{esc(_dom(s))}</a>' if str(s).startswith("http") else esc(s) for s in sources)


class Plugin(OursWroteUI, StyleUI, CardsUI, QueueUI, CrosspostUI, ReportUI, ObservancesUI, DigestMode, ActionsUI, DeskUI, ListsUI, Base):
    id = "news"
    icon = "✍️"
    title = "Публікації"
    version = "2.0"
    admin_only = True
    summary = ("ШІ-редакція GreenPost: збирає новини з пересилань, каналів, RSS, WhatsApp і вебхуків, відсіює дублікати, "
               "переписує за правилами редакції на моделях Оріона, тримає точну довжину, готує зведення за добу й "
               "публікує в канал лише після вашого схвалення.\n\n"
               "Автоматичний рерайт — не фактчек: перед публікацією звіряйте цифри й імена (кнопка «🔎 Звірити» допомагає, але не замінює редактора).")
    VIEWS = {"ow", "ows", "sty", "styd", "cd", "cds", "pql", "pq", "pqh", "xp", "xps", "ls", "d", "src", "dg", "so", "set", "q", "help", "ver", "len", "rules",
             "L", "Lo", "Ls", "Lsort", "Lbulk", "Le", "Lacc", "Lhelp", "Elst", "Lmerge",
             "H1", "H2", "H3", "H4", "H5", "D", "Dm", "Fl", "Fo", "Ff", "Fi", "Fmv", "Fbl", "Tg", "Tgi", "Tp", "Bk", "Bkl",
             "Sp", "Ou", "Ours", "Sl", "St", "Wt", "more", "acs", "ace", "sch", "fld", "flds", "echo", "eci", "cls", "ctp", "cte", "cmk", "csl", "obh", "obt", "obc", "obd", "ob", "obi", "obl", "obs", "obup", "obws", "obdy", "obcp", "obcm"}

    def __init__(self, host):
        super().__init__(host)
        self.cd_arm = {}  # 🖼 чат -> ("photo"|"logo", id новини): чекаємо зображення для картки
        self.store = Store(self.dir / "newsroom.db")
        self.engine = Engine(self._ai)
        self.queue: asyncio.Queue | None = None
        self.wait_forward: dict = {}
        self.wait_file: dict = {}
        self.lists = Lists(self.store)
        self.desk = Desk(self.store)
        self.echo = Echo(self.store, self.desk)
        self.web = WebCollector()
        from .social import SocialCollector
        self.social = SocialCollector()
        self.mt = MTProto(on_message=self._on_mt)
        self._hour: list = []
        self._migrate_tg_sources()
        self.commands = [
            ("news", self.cmd_news, "Новинарня: надіслати новину"),
            ("digest", self.cmd_digest, "Новинарня: зведення (шаблони й збір)"),
            ("collect", self.cmd_collect, "Новинарня: увімкнути/вимкнути збір для зведення"),
            ("drafts", self.cmd_drafts, "Новинарня: чернетки"),
        ]

    # ================================================================ ШІ
    async def _ai(self, prompt, system=None, label=None, image=None):
        if not self.host.ask_ai:
            raise RuntimeError("у бота не підключено жодної моделі ШІ")
        return await self.host.ask_ai(prompt, system=system, label=label or "📰 Новинарня", image=image)

    # ================================================================ прийом
    def ingest(self, origin, text, source_name="", source_url="", photo="", requested_by=0, hint=""):
        text = (text or "").strip()
        if not text and not photo and not source_url:
            return None, "empty", None
        why = self.desk.blocked(text, source_name, source_url)
        if not why and not requested_by:
            why = self.desk.is_spam(text, self.store.get("spam_level") or "soft")
        if why:
            self.desk.log_spam(why, origin, source_name, source_url, text)
            return None, "blocked", why
        iid, status, dup = self.store.add_item(origin, text or source_url, source_name, source_url, photo, requested_by, hint=hint)
        if status == "queued":
            self._enqueue(iid)
        return iid, status, dup

    def _enqueue(self, iid, delay=0):
        if self.queue is None:
            return  # воркер ще не запущений — матеріал підхопиться зі статусу queued під час старту
        if delay:
            async def later():
                await asyncio.sleep(delay)
                await self.queue.put(iid)
            asyncio.get_running_loop().create_task(later())
        else:
            self.queue.put_nowait(iid)

    async def _process(self, iid):
        item = self.store.item(iid)
        if not item or item["status"] not in ("queued", "processing"):
            return
        self.store.set_item(iid, status="processing", attempts=item["attempts"] + 1)
        raw, url = item["raw_text"], item["source_url"]
        if self.store.get("strip_junk") is not False:  # 🧹 «підпишись», адреси каналів, «написати адміну» — прибираємо, коли пишемо новину
            try:
                import postclean
                raw = postclean.clean(raw)
            except Exception:  # noqa: BLE001
                pass
        try:
            only_url = URL_RE.fullmatch(raw.strip()) if raw else None
            if (only_url or (not raw.strip() and url)) and self.host.browse:
                url = url or raw.strip()
                page, err = await self.host.browse(url)
                if not page:
                    raise RuntimeError(f"не вдалося прочитати сторінку: {err}")
                raw = f"Посилання: {url}\n\n{page[:9000]}"
            relation, ours, _sid = await self.check_against_ours(item, raw)
            if relation == "povtor":
                self.store.set_item(iid, status="skipped", error="повтор нашої публікації", photo="")
                return
            related = ""
            if ours:
                related = "\n\n".join(f"НАША ПУБЛІКАЦІЯ ({o['url'] or o['source']}): {o['title']}\n{o['text'][:800]}" for o in ours[:2])
                related += f"\n\nТип зв'язку: {relation}. Пиши як оновлення/доповнення/спростування нашої новини, не повторюй уже сказане."
            elif item["related_to"]:
                rd = self.store.draft_for_item(item["related_to"])
                if rd:
                    d = self.store.draft(rd)
                    related = f"#{rd} {d['headline']}\n{d['body'][:1000]}"
            image = item["photo"] or None
            if image and not raw.strip():
                raw = "Новина на фото. Прочитай текст на зображенні й опиши лише те, що видно."
            target = int(self.store.get("target") or 0)
            data, model, ok = await self.engine.write(raw, item, target, self.store.get("rules") or "", related, image)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("newsroom item %s failed: %s", iid, exc)
            if item["attempts"] + 1 < 3:
                self.store.set_item(iid, status="queued", error=str(exc)[:500])
                self._enqueue(iid, delay=60 * (item["attempts"] + 1))
            else:
                self.store.set_item(iid, status="error", error=str(exc)[:500], photo="")
                await self._tell(item, f"⚠️ <b>Новинарня</b>: матеріал #{iid} не оброблено після 3 спроб.\n{esc(str(exc))[:400]}",
                                 [[("🔁 Ще раз", self.cb("retry", iid)), ("🧾 Черга", self.cb("q"))]])
            return
        if data["skip"]:
            self.store.set_item(iid, status="skipped", error=data["notes"][:500], photo="")
            if item["requested_by"]:
                await self.host.notify(item["requested_by"], f"ℹ️ #{iid} схоже не на новину: {esc(data['notes'] or 'без пояснень')}",
                                       [[("📝 Все одно оформити", self.cb("force", iid))]])
            return
        if url and url not in data["sources"]:
            data["sources"].insert(0, url)
        if item["source_name"] and not any(item["source_name"] in s for s in data["sources"]):
            data["sources"].append(item["source_name"])
        if item["related_to"]:
            data["notes"] = (f"🔗 Схоже на матеріал #{item['related_to']} — можливо, оновлення події. " + data["notes"]).strip()
        did = self.store.add_draft(iid, data, model=model, target=target, status="draft" if ok else "length_mismatch")
        self._autocheck(did)  # 🔎 кожна чернетка перевіряється одразу: факти й мова
        self.store.set_item(iid, status="done", photo="", error="")
        it2 = self.store.item(iid)
        if it2.get("story_id"):
            self.desk.add_ref(it2["story_id"], "draft", did, data["headline"], relation=it2.get("relation") or "")
        await self._tell(item, *self.card(did))

    async def _tell(self, item, text, rows=None):
        if item.get("requested_by"):
            await self.host.notify(item["requested_by"], text, rows)
        elif self.store.get("notify"):
            await self.host.notify_admins(text, rows)

    async def _worker(self):
        while True:
            iid = await self.queue.get()
            try:
                item = self.store.item(iid)
                if item and not item["requested_by"]:
                    cap = int(self.store.get("hour_cap") or 0)
                    now = time.time()
                    self._hour = [t for t in self._hour if t > now - 3600]
                    if cap and len(self._hour) >= cap:
                        await asyncio.sleep(max(5, self._hour[0] + 3600 - now))  # береже ліміти моделей; черга не губиться
                    self._hour.append(time.time())
                if self.on():
                    await self._process(iid)
                else:
                    await asyncio.sleep(30)
                    self._enqueue(iid, delay=60)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("newsroom worker")

    async def _rss_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(20)
        while True:
            try:
                if self.on() and self.store.get("auto_rss"):
                    await self.poll_rss()
                    self.store.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("newsroom rss loop")
            await asyncio.sleep(max(60, int(self.store.get("rss_every") or 300)))

    async def poll_rss(self, only=None):
        added, errors = 0, []
        limit = int(self.store.get("rss_max_per_feed") or 12)
        for src in self.store.sources("rss"):
            if not src["enabled"] or (only and src["id"] != only):
                continue
            first = src["last_poll"] == 0
            try:
                title, entries = parse(await fetch(src["ref"]))
                fresh = 0
                for e in entries[:limit]:
                    if not self.store.seen_once(f"rss:{src['id']}:{e['guid']}"):
                        continue
                    if src.get("ours"):  # наш сайт: лише для звірки, у роботу не беремо
                        self.desk.add_ours(src["title"] or title, e["link"], e["title"], (e["title"] + "\n" + e["text"]).strip())
                        continue
                    self.scan_post((e["title"] + "\n" + e["text"]).strip(), src["title"] or title, e["link"])
                    rec = self.echo.check((e["title"] + "\n" + e["text"]).strip(), src["title"] or title, e["link"])
                    if rec:
                        await self.tell_echo(rec)
                        if rec["kind"] in ("copy", "plagiarism"):
                            continue
                    if first:
                        continue  # перше читання лише запам'ятовує наявне, щоб не залити чергу старими новинами
                    text = (e["title"] + "\n\n" + e["text"]).strip()
                    iid, st, _ = self.ingest("rss", text, src["title"] or title, e["link"])
                    fresh += 1 if st == "queued" else 0
                added += fresh
                self.store.set_source(src["id"], last_poll=time.time(), last_error="", taken=src["taken"] + fresh,
                                      title=src["title"] or title[:120])
            except (FeedError, OSError, ValueError, Exception) as exc:  # noqa: BLE001
                errors.append(f"{src['title'] or src['ref']}: {exc}")
                self.store.set_source(src["id"], last_poll=time.time(), last_error=str(exc)[:300])
        return added, errors

    async def start(self):
        self.queue = asyncio.Queue()
        for iid in self.store.queued():
            self.store.set_item(iid, status="queued")
            self.queue.put_nowait(iid)
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._worker()), loop.create_task(self._rss_loop()),
                       loop.create_task(self._lists_loop()), loop.create_task(self._mt_loop()), loop.create_task(self._tags_loop()), loop.create_task(self._sched_loop()), loop.create_task(self._pq_loop()), loop.create_task(self._obs_loop()), loop.create_task(self.cplan_watch()),
                       loop.create_task(self._report_loop())]
        await asyncio.gather(*self._tasks)

    def status_line(self):
        s = self.store.stats()
        return f"нових чернеток {s['new']} · у черзі {s['queued']} · опубліковано за добу {s['published24']}"

    # ================================================================ повідомлення Telegram
    @staticmethod
    def _origin(msg):
        """-> (origin, назва джерела, посилання) для пересланих дописів."""
        o = getattr(msg, "forward_origin", None)
        if not o:
            return "manual", "", ""
        chat = getattr(o, "chat", None) or getattr(o, "sender_chat", None)
        if chat is not None:
            uname = getattr(chat, "username", None)
            mid = getattr(o, "message_id", None)
            url = f"https://t.me/{uname}/{mid}" if uname and mid else ""
            return "forward", (chat.title or uname or str(chat.id)), url
        user = getattr(o, "sender_user", None)
        if user is not None:
            return "forward", user.full_name, ""
        return "forward", getattr(o, "sender_user_name", "") or "", ""

    @staticmethod
    def _links(msg):
        out = []
        for e in list(msg.entities or []) + list(msg.caption_entities or []):
            if e.type == "text_link" and e.url:
                out.append(e.url)
        return out

    async def _photo_b64(self, msg):
        if not msg.photo:
            return ""
        ph = msg.photo[-1]
        if (ph.file_size or 0) > 8 * 1024 * 1024:
            return ""
        f = await ph.get_file()
        return base64.b64encode(bytes(await f.download_as_bytearray())).decode()

    async def _take(self, msg, requested_by):
        origin, name, url = self._origin(msg)
        text = msg.text or msg.caption or ""
        links = self._links(msg)
        if links:
            text += "\n\nПосилання в дописі: " + " ".join(links)
        photo = await self._photo_b64(msg)
        iid, status, dup = self.ingest(origin, text, name, url, photo, requested_by)
        if iid is None:
            return await msg.reply_text("Порожнє повідомлення — немає що обробляти." if status == "empty" else f"🚫 Не беру: {dup}")
        if status == "duplicate":
            d = self.store.draft_for_item(dup)
            rows = [[("📝 Все одно обробити", self.cb("force", iid))]]
            if d:
                rows[0].insert(0, ("👀 Оригінал", self.cb("d", d)))
            return await self.host.notify(msg.chat_id, f"♻️ Це повтор матеріалу #{dup}. Не обробляю, щоб не дублювати.", rows)
        await self.host.notify(msg.chat_id, f"📥 Узяв у новинарню #{iid}. Чернетка прийде окремим повідомленням.",
                               [[("💬 Ні, це питання до ШІ", self.cb("toai", iid))]])

    async def intercept(self, update, ctx):
        msg = update.message
        if not msg or msg.chat.type != "private":
            return False
        chat = msg.chat_id
        if chat in self.wait_forward:
            self.wait_forward.pop(chat, None)
            o = getattr(msg, "forward_origin", None)
            ch = getattr(o, "chat", None)
            if ch is None or ch.type != "channel":
                await msg.reply_text("Це не пересланий допис каналу. Підключення скасовано — спробуйте ще раз: ⚙️ Налаштування новинарні → 📤 Публікація.")
                return True
            try:
                test = await ctx.bot.send_message(ch.id, "✅ Новинарня Оріона підключено для публікацій.")
                await ctx.bot.delete_message(ch.id, test.message_id)
            except Exception as exc:  # noqa: BLE001
                await msg.reply_text(f"⚠️ Не можу писати в «{ch.title}»: {exc}\nДодайте бота адміністратором із правом публікації.")
                return True
            self.store.set("publish_chat", str(ch.id))
            self.store.set("publish_title", ch.title or str(ch.id))
            self.store.set("publish_username", ch.username or "")
            await self.host.notify(chat, f"✅ Канал публікацій: <b>{esc(ch.title)}</b>", [[("📤 Публікація", self.cb("set"))]])
            return True
        if self.host.extra.get("awaiting", lambda c: False)(chat):
            return False
        if self.collecting(chat) and (msg.text or msg.caption or msg.photo):
            await self.collect_message(msg)  # 🧾 режим збору: лише «✅ Прийнято»
            return True
        forwarded = getattr(msg, "forward_origin", None) is not None
        if not ((forwarded and self.store.get("forward_auto")) or chat in (self.store.get("intake") or [])):
            return False
        if not (msg.text or msg.caption or msg.photo):
            return False
        await self._take(msg, chat)
        return True

    async def on_channel_post(self, update, ctx):
        """Дописи каналів, де бот адміністратор, і повідомлення груп, куди доданий бот."""
        post = update.channel_post or (update.message if update.message and update.message.chat.type in ("group", "supergroup") else None)
        if not post:
            return
        ch = post.chat
        if str(ch.id) == str(self.store.get("publish_chat") or ""):
            return  # власний канал публікацій не моніторимо — інакше цикл
        ctype = "channel" if ch.type == "channel" else "group"
        if ctype == "group" and post.from_user and post.from_user.is_bot:
            return
        found = self.lists.entries_by_ref([str(ch.id), (ch.username or "").lower()])
        if not found:
            lid = self.lists.auto_list("bot")
            eid, new = self.lists.upsert_chat(lid, "public" if ch.username else "id", (ch.username or "").lower() or str(ch.id),
                                              ch.id, ch.title or ch.username or str(ch.id), ctype, enabled=0)
            if new:
                await self.host.notify_admins(
                    f"📡 <b>Новинарня</b>: бот бачить {'канал' if ctype == 'channel' else 'групу'} «{esc(ch.title or ch.username)}». "
                    "Брати звідти новини автоматично?",
                    [[("✅ Моніторити", self.cb("Etog", eid)), ("📋 Список", self.cb("Lo", lid))]])
            return
        text = post.text or post.caption or ""
        links = self._links(post)
        if links:
            text += "\n\nПосилання в дописі: " + " ".join(links)
        photo = await self._photo_b64(post) if not text.strip() and post.photo else ""
        url = f"https://t.me/{ch.username}/{post.message_id}" if ch.username else ""
        for e in found:
            if not e.get("chat_id"):
                self.lists.update_entry(e["id"], chat_id=str(ch.id), chat_type=ctype, title=ch.title or e["title"])
        fo = getattr(post, "forward_origin", None)
        fch = getattr(fo, "chat", None)
        fwd = (str(fch.id) if fch else "")
        if fch is not None and fch.username and fch.username.lower() in self.echo.our_refs():
            fwd = fch.username.lower()
        await self._take_source(found, post.message_id, text, photo, url, ch.title or "", via="bot", fwd=fwd)

    # ================================================================ списки: збір
    def _migrate_tg_sources(self):
        old = self.store.sources("tg")
        if not old:
            return
        lid = self.lists.auto_list("bot")
        for s in old:
            self.lists.upsert_chat(lid, "id", str(s["ref"]), s["ref"], s["title"], "channel", enabled=s["enabled"])
            self.store.del_source(s["id"])

    async def _take_source(self, entries, msg_id, text, photo, url, title, via, fwd="", key=None, metrics=None, at=None, platform="tg"):
        """Один допис з одного чату (може бути в кількох списках) -> не більше одного матеріалу в новинарню."""
        active = [e for e in entries if e["enabled"] and e.get("list_on", 1)]
        if not active:
            return False
        e = active[0]
        key = key or f"tg:{e.get('chat_id') or e['ref']}:{msg_id}"
        now = time.time()
        for x in entries:
            self.lists.update_entry(x["id"], last_id=max(int(x["last_id"] or 0), int(msg_id)), last_post=now, via=via, last_error="")
        if not self.store.seen_once(key):
            return False
        body = (text or "").strip()
        if e.get("auto") == "ours":  # наш ресурс: не беремо, лише запам'ятовуємо для звірки
            if body:
                self.desk.add_ours(e["label"] or title or e["title"], url, "", body, at=at, platform=platform, metrics=metrics)
            return False
        name0 = e["label"] or title or e["title"] or display_ref(e["kind"], e["ref"])
        try:
            self.scan_post(body, name0, url)  # 📅 «Сьогодні — Всесвітній день …» → у календар тем
        except Exception:  # noqa: BLE001
            log.exception("observance scan")
        rec = self.echo.check(body, name0, url, fwd)
        if rec:
            await self.tell_echo(rec)
            if rec["kind"] in ("repost", "copy", "plagiarism"):
                return False  # це наш же текст — новиною не вважаємо
        if not photo and (len(body) < int(e.get("min_len") or 0) or not passes(body, e.get("include", ""), e.get("exclude", ""))):
            self.lists.update_entry(e["id"], filtered=e["filtered"] + 1)
            return False
        name = e["label"] or title or e["title"] or display_ref(e["kind"], e["ref"])
        hint = e["region"] or e.get("list_region") or ""
        iid, st, _ = self.ingest("channel" if e.get("chat_type") != "group" else "group", body, f"{name} · {e.get('list_name', '')}".strip(" ·"),
                                 url, photo, hint=hint)
        if st == "queued":
            self.lists.update_entry(e["id"], taken=e["taken"] + 1)
            return True
        return False

    async def _check_entry(self, e, force=False):
        """Перевіряє одне джерело зі списку. -> скільки нових матеріалів узято."""
        method = e["method"]
        if e["kind"] == "social":
            return await self._check_social(e, force)
        if method == "bot" or (e.get("auto") == "bot" and method == "auto"):
            self.lists.update_entry(e["id"], last_check=time.time())
            return 0
        if e.get("auto") == "account" and self.mt.ready and not force:
            self.lists.update_entry(e["id"], last_check=time.time(), via="mtproto")
            return 0  # підписки акаунта приходять у реальному часі
        use_mt = method == "mtproto" or (method == "auto" and self.mt.ready)
        if method == "web" or (not use_mt and e["kind"] == "public"):
            use_mt = False
        elif not self.mt.ready:
            self.lists.update_entry(e["id"], last_check=time.time(), last_error=(
                "потрібен службовий акаунт (📲) або бот у цьому чаті" if e["kind"] != "public" else f"службовий акаунт: {self.mt.error}"))
            return 0
        first = not e["last_id"]
        taken = 0
        try:
            if use_mt:
                title, chat_id, ctype, posts = await self.mt.fetch(e, int(e["last_id"] or 0), limit=5 if first else 30)
                self.lists.update_entry(e["id"], chat_id=chat_id, chat_type=ctype, title=title[:120] or e["title"])
                e = dict(e, chat_id=chat_id, chat_type=ctype)
                via = "mtproto"
            else:
                title, posts = await self.web.fetch(e["ref"], int(e["last_id"] or 0))
                self.lists.update_entry(e["id"], title=title[:120] or e["title"], chat_type="channel")
                via = "web"
            if e.get("auto") == "ours":  # охоплення наших дописів оновлюємо щоразу (перегляди ростуть днями)
                if not first:
                    try:
                        if use_mt:
                            *_r, recent = await self.mt.fetch(e, 0, limit=60)
                        else:
                            _t, recent = await self.web.fetch(e["ref"], 0)
                    except Exception:  # noqa: BLE001
                        recent = []
                else:
                    recent = posts
                for p in recent:
                    u = f"https://t.me/{e['ref']}/{p['id']}" if e["kind"] == "public" else ""
                    if first and p["text"].strip():
                        self.desk.add_ours(e["label"] or title, u, "", p["text"], at=p.get("at") or None, metrics=p.get("metrics"))
                    elif u and p.get("metrics"):
                        self.desk.set_metrics(u, p["metrics"])
            if first:  # перша перевірка лише запам'ятовує, з якого допису почати
                top = max([p["id"] for p in posts], default=0)
                self.lists.update_entry(e["id"], last_id=top, last_check=time.time(), via=via, last_error="")
                for p in posts:
                    self.store.seen_once(f"tg:{e.get('chat_id') or e['ref']}:{p['id']}")
                return 0
            for p in posts[-30:]:
                photo = p["photo"]
                if via == "web" and photo and not p["text"].strip():
                    photo = await self.web.photo_b64(photo)
                elif via == "web":
                    photo = ""
                url = f"https://t.me/{e['ref']}/{p['id']}" if e["kind"] == "public" else ""
                fresh = self.lists.entry(e["id"])
                fresh.update({k: e.get(k) for k in ("list_name", "include", "exclude", "list_region", "min_len")})
                fresh["list_on"] = 1
                if await self._take_source([fresh], p["id"], p["text"], photo, url, title, via, p.get("fwd", ""),
                                           metrics=p.get("metrics"), at=p.get("at") or None):
                    taken += 1
            self.lists.update_entry(e["id"], last_check=time.time(), via=via, last_error="")
        except Exception as exc:  # noqa: BLE001
            self.lists.update_entry(e["id"], last_check=time.time(), last_error=str(exc)[:300])
            raise
        return taken

    async def _check_social(self, e, force=False):
        """Соцмережа або RSS зі списку: X, Threads, Instagram, Facebook, Tumblr, YouTube, RSS."""
        from . import social
        platform = social.split(e["ref"])[0]
        ours = e.get("auto") == "ours"
        gap = {"x": 1800, "ig": 1200, "fb": 900, "threads": 900}.get(platform, 0)   # бережемо ліміти API
        if gap and not force and time.time() - float(e["last_check"] or 0) < gap:
            return 0
        try:
            title, posts = await self.social.fetch(e["ref"], ours=ours)
        except Exception as exc:  # noqa: BLE001
            self.lists.update_entry(e["id"], last_check=time.time(), last_error=str(exc)[:300], via=platform)
            raise
        first = not e["last_id"]
        self.lists.update_entry(e["id"], title=(title or e["title"])[:120], chat_type="social")
        taken = 0
        for n, p in enumerate(posts, 1):
            key = f"so:{e['ref']}:{p['id']}"
            if ours:
                if p["text"].strip():
                    self.desk.add_ours(e["label"] or title, p["url"], "", p["text"], at=p["at"], key=("u:" + p["url"]) if p["url"] else key,
                                       platform=platform, metrics=p.get("metrics"))
                self.store.seen_once(key)
                continue
            if first:
                self.store.seen_once(key)
                continue
            fresh = self.lists.entry(e["id"])
            fresh.update({k: e.get(k) for k in ("list_name", "include", "exclude", "list_region", "min_len")})
            fresh["list_on"] = 1
            photo = ""
            if p.get("photo") and not p["text"].strip():
                photo = await self.web.photo_b64(p["photo"])
            if await self._take_source([fresh], int(p["at"]), p["text"], photo, p["url"], title, platform, p.get("fwd", ""), key=key,
                                       metrics=p.get("metrics"), at=p["at"], platform=platform):
                taken += 1
        self.lists.update_entry(e["id"], last_id=max(int(e["last_id"] or 0), 1, *[int(p["at"]) for p in posts]),
                                last_check=time.time(), via=platform, last_error="")
        return taken

    async def check_sources(self, list_id=None, entry_id=None, force=False):
        if entry_id:
            es = [self.lists.entry(entry_id)]
            l = self.lists.get_list(es[0]["list_id"])
            es[0].update(list_name=l["name"], include=l["include"], exclude=l["exclude"], list_region=l["region"],
                         min_len=l["min_len"], auto=l["auto"], interval=l["interval"])
        elif force:
            es = [x for x in self.lists.due(now=10 ** 12) if not list_id or x["list_id"] == int(list_id)]
        else:
            es = self.lists.due()
        n = errs = 0
        for e in es:
            try:
                n += await self._check_entry(e, force=force)
            except Exception:  # noqa: BLE001
                errs += 1
            await asyncio.sleep(0.4 if e["method"] == "mtproto" or self.mt.ready else 1.5)
        return n, errs

    async def _lists_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(30)
        while True:
            try:
                if self.on():
                    await self.check_sources()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("newsroom lists loop")
            await asyncio.sleep(45)

    async def mt_connect(self):
        if self.mt.ready:
            return True
        ok = await self.mt.start()
        if ok:
            await self.sync_account(None)
        return ok

    async def _mt_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(10)
        while True:
            try:
                if self.on():
                    if not self.mt.ready:
                        await self.mt_connect()
                    elif time.time() - float(self.store.get("account_synced") or 0) > 6 * 3600:
                        await self.sync_account(None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("newsroom mtproto loop")
            await asyncio.sleep(300)

    async def sync_account(self, chat):
        """Підписки службового акаунта -> список «📲 Підписки акаунта». -> (нових, зниклих)"""
        if not self.mt.ready:
            return 0, 0
        dialogs = await self.mt.dialogs()
        lid = self.lists.auto_list("account")
        on = bool(self.store.get("account_new_on"))
        seen, new = set(), 0
        for d in dialogs:
            if str(d["chat_id"]) == str(self.store.get("publish_chat") or ""):
                continue
            kind = "public" if d["username"] else "id"
            _eid, is_new = self.lists.upsert_chat(lid, kind, d["username"] or d["chat_id"], d["chat_id"], d["title"], d["type"], enabled=on)
            seen.add(str(d["chat_id"]))
            new += 1 if is_new else 0
        gone = 0
        for e in self.lists.entries(lid):
            if e["chat_id"] and e["chat_id"] not in seen and not e["last_error"].startswith("акаунт"):
                self.lists.update_entry(e["id"], last_error="акаунт більше не підписаний")
                gone += 1
        self.store.set("account_synced", time.time())
        if new and not chat:
            await self.host.notify_admins(f"📲 <b>Новинарня</b>: нових підписок акаунта: {new} — "
                                          + ("вже моніторю." if on else "додано вимкненими, увімкніть потрібні."),
                                          [[("📋 Підписки", self.cb("Lo", lid))]])
        return new, gone

    async def _on_mt(self, chat_id, username, title, ctype, msg_id, text, photo, fwd=""):
        if not self.on() or chat_id == str(self.store.get("publish_chat") or ""):
            return
        found = self.lists.entries_by_ref([chat_id, username.lower()])
        if not found:
            lid = self.lists.auto_list("account")
            eid, _ = self.lists.upsert_chat(lid, "public" if username else "id", username.lower() or chat_id, chat_id, title, ctype,
                                            enabled=bool(self.store.get("account_new_on")))
            found = self.lists.entries_by_ref([chat_id])
        url = f"https://t.me/{username}/{msg_id}" if username and ctype == "channel" else ""
        await self._take_source(found, msg_id, text, photo, url, title, via="mtproto", fwd=fwd)

    # ================================================================ 📣 відгомін
    async def tell_echo(self, rec):
        mode = self.store.get("echo_notify") or "important"
        if mode == "off" or (mode == "important" and rec["kind"] not in ("plagiarism", "repost", "copy", "quote")):
            return
        icon, title = ECHO_KINDS[rec["kind"]]
        o = rec.get("ours")
        text = (f"{icon} <b>{title}</b> нашої новини\n\n"
                + (f"🏠 Наша: {esc(o['title'][:120])}\n{esc(o['url'])}\n\n" if o else "")
                + f"📡 {esc(rec['source'])}" + (f"\n{esc(rec['url'])}" if rec["url"] else "")
                + (f"\nЗбіг: {int(rec['score'] * 100)} %" if rec["score"] else "")
                + ("\n✅ з посиланням на нас" if rec["attributed"] else ("\n⚠️ без посилання на нас" if rec["kind"] != "repost" else ""))
                + f"\n\n«{esc(rec['excerpt'][:220])}»")
        rows = [[("📣 Увесь відгомін", self.cb("echo")), ("🔍 Деталі", self.cb("eci", rec["id"]))]]
        await self.host.notify_admins(text[:4000], rows)

    def echo_screen(self, kind=None):
        c = self.echo.counts()
        lines = ["📣 <b>Відгомін наших новин</b>", "",
                 "Хто репостить, пересилає, цитує, переказує, згадує нас — і хто передруковує майже дослівно без посилання.",
                 "<i>Рерайт своїми словами — це переказ, не плагіат. Плагіат — лише ідентичний або майже ідентичний текст (≥ 85 %) без посилання.</i>", ""]
        tabs = [(("● " if kind is None else "") + f"Усе · {sum(c.values())}", self.cb("echo"))] + \
               [(("● " if kind == k else "") + f"{v[0]} {c.get(k, 0)}", self.cb("echo", k)) for k, v in ECHO_KINDS.items()]
        rows = [tabs[:4], tabs[4:]]
        for r in self.echo.items(kind, limit=8):
            icon, title = ECHO_KINDS[r["kind"]]
            lines.append(f"{icon} {esc(short(r['source'], 30))} · {ago(r['at'])}" + (f" · {int(r['score'] * 100)} %" if r["score"] else "")
                         + ("" if r["attributed"] or r["kind"] in ("repost", "mention") else " · без посилання")
                         + f"\n   «{esc(short(r['excerpt'], 110))}»")
            rows.append([(f"{icon} {short(r['source'], 26)}", self.cb("eci", r["id"]))])
        if not c:
            lines.append("Поки нічого. Додайте наші ресурси (🏠) і назви бренду — бот почне звіряти все, що збирає.")
        mode = self.store.get("echo_notify") or "important"
        rows.append([(("● " if mode == m else "") + t, self.cb("ecn", m)) for m, t in (("important", "🔔 Важливе"), ("all", "🔔 Усе"), ("off", "🔕 Тихо"))])
        rows.append([("🏷 Наші назви", self.cb("ecb")), ("« Наші ресурси", self.cb("Ou"))])
        return "\n".join(lines)[:4000], rows

    def echo_card(self, eid):
        r = next((x for x in self.echo.items(limit=500) if x["id"] == int(eid)), None)
        if not r:
            return "Не знайдено.", [[("« Відгомін", self.cb("echo"))]]
        icon, title = ECHO_KINDS[r["kind"]]
        o = None
        if r["ours_id"]:
            q = self.desk.q("SELECT * FROM ours_posts WHERE id=?", (r["ours_id"],))
            o = q[0] if q else None
        text = (f"{icon} <b>{title}</b>\n📡 {esc(r['source'])} · {ago(r['at'])}" + (f"\nЗбіг: {int(r['score'] * 100)} %" if r["score"] else "")
                + ("\n✅ з посиланням на нас" if r["attributed"] else "\n⚠️ без посилання на нас") + f"\n\n«{esc(r['excerpt'][:900])}»"
                + (f"\n\n🏠 <b>Наша:</b> {esc(o['title'])}\n{esc(o['url'])}" if o else ""))
        rows = []
        if r["url"].startswith("http"):
            rows.append([("🌐 Їхній допис", "url:" + r["url"])])
        if o and o["url"].startswith("http"):
            rows.append([("🏠 Наша новина", "url:" + o["url"])])
        rows.append([("🚫 Не брати з цього джерела", self.cb("ecbl", r["id"])), ("« Відгомін", self.cb("echo"))])
        return text, rows

    # ================================================================ команди
    async def cmd_news(self, update, ctx):
        msg = update.message
        body = (msg.text or "").partition(" ")[2].strip()
        if msg.reply_to_message:
            return await self._take(msg.reply_to_message, msg.chat_id)
        if body:
            iid, st, dup = self.ingest("manual", body, requested_by=msg.chat_id)
            if st == "duplicate":
                return await msg.reply_text(f"♻️ Це повтор матеріалу #{dup}.")
            return await msg.reply_text(f"📥 Прийнято #{iid}. Готую чернетку…")
        text, rows = self.home(msg.chat_id)
        await self.host.notify(msg.chat_id, text, rows)

    async def cmd_collect(self, update, ctx):
        chat = update.message.chat_id
        on = not self.collecting(chat)
        self.set_collecting(chat, on)
        await self.host.notify(chat, ("🟢 <b>Збір увімкнено.</b> Надсилайте новини й тексти — я лише відповідатиму «✅ Прийнято».\n"
                                      "Коли все закинете — «🧾 Готуй: …»." if on else "⏹ Збір зупинено."), self.digest_screen(chat)[1])

    async def cmd_digest(self, update, ctx):
        arg = (update.message.text or "").partition(" ")[2].strip()
        if not arg:
            t, rows = self.digest_screen(update.message.chat_id)
            return await self.host.notify(update.message.chat_id, t, rows)
        n = int(arg) if arg.isdigit() else 2000
        if not 300 <= n <= 3800:
            return await update.message.reply_text("Довжина зведення — від 300 до 3800 символів. Приклад: /digest 2000")
        await update.message.reply_text(f"🧾 Складаю зведення на {n} символів…")
        asyncio.get_running_loop().create_task(self._make_digest(update.message.chat_id, n))

    async def cmd_drafts(self, update, ctx):
        text, rows = self.list_screen("new", 0)
        await self.host.notify(update.message.chat_id, text, rows)

    # ================================================================ екрани
    def home(self, chat):
        s = self.store.stats()
        lists = self.lists.lists()
        watching = sum(l["n_on"] for l in lists if l["enabled"] and l["auto"] != "ours")
        pub = self.store.get("publish_title")
        line = [f"нових чернеток <b>{s['new']}</b>", f"стежу за {watching} джерелами"]
        if s["queued"]:
            line.append(f"в обробці {s['queued']}")
        ban = self.banner(chat)
        text = ((ban + "\n\n" if ban else "") + "📰 <b>Новини · чернетки й публікації</b>\n" + " · ".join(line) + "\n"
                + (f"Публікую в: {esc(pub)}" if pub else "Канал для публікацій ще не підключено (⚙️ Налаштування новинарні → Публікація).")
                + "\n\nПерешліть сюди будь-який допис — він стане чернеткою.")
        rows = [
            [(f"📥 Нові чернетки · {s['new']}", self.cb("ls", "new"))],
            [("🔎 Знайти й написати", self.cb("D"))],
            [(f"🗓 Черга публікацій · {sum(1 for r in self.pq() if r['st'] in ('wait', 'pause'))}", self.cb("pql")),
             ("🔎 Пошук у наших", self.cb("ows"))],
            [("📡 Джерела", self.cb("H1")), ("⚙️ Налаштування новинарні", self.cb("H2"))],
            [("🗂 Архів", self.cb("H5")), ("ℹ️ Довідка", self.cb("help"))],
        ]
        return text, rows

    # ----------------------------------------------------------- 🌐 соцмережі
    def social_count(self, ours=None):
        n = 0
        for l in self.lists.lists():
            if ours is not None and (l["auto"] == "ours") != ours:
                continue
            n += sum(1 for e in self.lists.entries(l["id"]) if e["kind"] == "social")
        return n

    def social_screen(self):
        from .social import PLATFORMS, configured
        way = configured()
        per = {}
        for l in self.lists.lists():
            for e in self.lists.entries(l["id"]):
                if e["kind"] == "social":
                    p = e["ref"].split(":", 1)[0]
                    per.setdefault(p, [0, 0])[1 if l["auto"] == "ours" else 0] += 1
        lines = ["🌐 <b>Соцмережі</b> — новини з X, Threads, Instagram, Facebook, Tumblr, YouTube і будь-яких RSS", "",
                 "Чужі акаунти — джерела новин (як канали). Наші — не беремо, а звіряємо: повтори, відгомін, плагіат і охоплення "
                 "для 📊 звіту. Додавати можна і сюди, і в будь-який 📋 список — просто посиланням.", ""]
        for p, (icon, name) in PLATFORMS.items():
            o, ou = per.get(p, (0, 0))
            w = way.get(p) or "⚠️ не підключено"
            lines.append(f"{icon} <b>{name}</b>: чужих {o} · наших {ou} — {w}")
        lines += ["", "Telegram теж: канали, групи, чати, коментарі (через 📲 службовий акаунт), бот у групах, вебпревʼю t.me/s."]
        rows = [[("➕ Чужі акаунти", self.cb("SNadd", "other")), ("➕ Наші акаунти", self.cb("SNadd", "ours"))],
                [("🔑 Як підключити ключі", self.cb("SNk"))],
                [("« Джерела", self.cb("H1"))]]
        return "\n".join(lines), rows

    def social_route(self, chat, a, x):
        if a == "SNk":
            return None, (SOCIAL_KEYS_HELP, [[("« Соцмережі", self.cb("SN"))]])
        if a == "SNadd":
            if x == "ours":
                lid = self.lists.auto_list("ours")
            else:
                l = self.lists.find_list("🌐 Соцмережі")
                lid = l["id"] if l else self.lists.create_list("🌐 Соцмережі")
            self.ask(chat, "Ladd", lid)
            return None, (("🏠 <b>Наші акаунти в соцмережах</b>" if x == "ours" else "🌐 <b>Чужі акаунти в соцмережах</b>")
                          + "\n\nВставте посилання — по одному в рядку, можна з назвою й областю через «|»:\n"
                          "<code>https://x.com/name\nhttps://www.threads.net/@name | Назва | Київ\nhttps://instagram.com/name\n"
                          "https://facebook.com/page\nhttps://name.tumblr.com\nhttps://site.ua/rss</code>\n\n"
                          "Або коротко: <code>x:name · threads:name · ig:name · fb:page · tumblr:name · rss:https://…</code>",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, self.social_screen()

    # ----------------------------------------------------------- хаби другого рівня
    def hub_H1(self, chat):
        lists = self.lists.lists()
        mine = [l for l in lists if not l["auto"]]
        acc = next((l for l in lists if l["auto"] == "account"), None)
        ours = sum(l["n"] for l in lists if l["auto"] == "ours")
        text = ("📡 <b>Джерела</b> — звідки новинарня бере новини\n\n"
                f"📋 Мої списки: {len(mine)} ({sum(l['n'] for l in mine)} каналів і чатів)\n"
                f"📲 Підписки акаунта: {('🟢 ' + str(acc['n'])) if acc and self.mt.ready else '⚪️ не підключено'}\n"
                f"🏠 Наші ресурси (не беремо, лише звіряємо): {ours} каналів · {self.desk.ours_count()} публікацій\n"
                f"🏷 Теги (лише стежити): {len(self.desk.tags())}\n"
                f"🌐 Соцмережі: {self.social_count()} акаунтів (X, Threads, Instagram, Facebook, Tumblr, YouTube)\n"
                f"📶 RSS-стрічок: {len(self.store.sources('rss'))}")
        rows = [[("📋 Списки каналів і чатів", self.cb("L"))],
                [("📲 Підписки акаунта", self.cb("Lacc")), ("🏠 Наші ресурси", self.cb("Ou"))],
                [("🌐 Соцмережі", self.cb("SN")), ("🏷 Теги", self.cb("Tg"))],
                [("📶 RSS і вебхуки", self.cb("so"))],
                [("✍️ Додати новину вручну", self.cb("in"))],
                [("« Новинарня", self.cb())]]
        return text, rows

    def hub_H2(self, chat):
        st = self.store
        text = ("⚙️ <b>Налаштування новинарні</b>\n\n"
                f"📤 Публікація: {esc(st.get('publish_title') or 'канал не підключено')}\n"
                f"✍️ Як писати: довжина {int(st.get('target') or 0) or 'вільна'}, {'свої правила' if st.get('rules') else 'базові правила'}, "
                f"Word UA+RU {'так' if st.get('word_bilingual') else 'ні'}\n"
                f"🛡 Фільтри: блокувань {len(self.desk.blocks())}, антиспам {st.get('spam_level') or 'soft'}\n"
                f"↪️ Переслане — у новинарню: {'так' if st.get('forward_auto') else 'ні'}\n"
                f"🔔 Сповіщення про автоматичні чернетки: {'так' if st.get('notify') else 'ні'}")
        rows = [[("📤 Публікація", self.cb("set")), ("✍️ Як писати", self.cb("H3"))],
                [("🧩 Кнопки під новиною", self.cb("acs")), ("🛡 Фільтри", self.cb("H4"))],
                [(("↪️ Переслане → новинарня: так" if st.get("forward_auto") else "↪️ Переслане → новинарня: ні"), self.cb("Hfwd"))],
                [(("🔔 Сповіщення: так" if st.get("notify") else "🔕 Сповіщення: ні"), self.cb("tg", "notify"))],
                [("« Новинарня", self.cb())]]
        return text, rows

    def hub_H3(self, chat):
        st = self.store
        text = ("✍️ <b>Як писати</b>\n\n"
                f"📏 Довжина звичайної новини: {int(st.get('target') or 0) or 'вільна'}\n"
                f"📜 Правила редакції: {'базові + ваші' if st.get('rules') else 'базові GreenPost'}\n"
                f"🗺 Тематика медіа: {esc(self.media_topics())}\n"
                f"📄 Велика стаття одразу у Word українською й російською: {'так' if st.get('word_bilingual') else 'ні'}")
        rows = [[("📏 Довжина за замовчуванням", self.cb("setlen")), ("📜 Правила", self.cb("rules"))],
                [("📐 Шаблони", self.cb("Wt")), ("🗂 Теми пошуку", self.cb("Tp"))],
                [("🗺 Тематика медіа", self.cb("Dst"))],
                [(("📄 Word UA+RU: так" if st.get("word_bilingual") else "📄 Word UA+RU: ні"), self.cb("Dwb"))],
                [("« Налаштування новинарні", self.cb("H2"))]]
        return text, rows

    def hub_H4(self, chat):
        lvl = {"off": "вимкнено", "soft": "мʼякий", "strict": "суворий"}.get(self.store.get("spam_level") or "soft")
        n = self.desk.q("SELECT COUNT(*) AS n FROM spamlog")[0]["n"]
        text = ("🛡 <b>Фільтри</b>\n\n"
                f"🚫 Блокування тем, людей, джерел і сайтів: {len(self.desk.blocks())}\n"
                f"🧹 Антиспам: {lvl} · відсіяно {n}\n\nВідсіяне не потрапляє в чернетки й не витрачає ліміти моделей.")
        rows = [[("🚫 Блокування", self.cb("Bk")), ("🧹 Антиспам", self.cb("Sp"))], [("« Налаштування новинарні", self.cb("H2"))]]
        return text, rows

    def hub_H5(self, chat):
        text = "🗂 <b>Архів</b>\n\nУсе, що вже пройшло через новинарню."
        rows = [[("🗂 Усі чернетки", self.cb("ls", "ok")), ("📚 Історії", self.cb("Sl"))],
                [("⭐ Обране й папки", self.cb("Fo")), ("🧾 Зведення", self.cb("dg"))],
                [("📁 Теки й архів новин", self.cb("flds")), ("⏳ Черга й помилки", self.cb("q"))],
                [("« Новинарня", self.cb())]]
        return text, rows

    def desk_settings(self):
        return self.hub_H3(None)

    def list_screen(self, flt="new", offset=0):
        f = next((x for x in FILTERS if x[0] == flt), FILTERS[0])
        ds = self.store.drafts(f[2], limit=PAGE + 1, offset=offset)
        tabs = [(("● " if k == f[0] else "") + t, self.cb("ls", k)) for k, t, _ in FILTERS]
        rows = [tabs[:2], tabs[2:]]
        for d in ds[:PAGE]:
            mark = URGENCY.get(d["urgency"], "") + ("🧾" if d["kind"] == "digest" else "")
            ln = f" · {length_of(d['body'])}" if d["target"] else ""
            rows.append([(f"{mark}#{d['id']} {short(d['headline'], 34)}{ln}", self.cb("d", d["id"]))])
        nav = []
        if offset:
            nav.append(("⬅️", self.cb("ls", f[0], max(0, offset - PAGE))))
        if len(ds) > PAGE:
            nav.append(("➡️", self.cb("ls", f[0], offset + PAGE)))
        if nav:
            rows.append(nav)
        rows.append([("« Новинарня", self.cb())])
        empty = "" if ds else "\n\nТут поки порожньо."
        return f"📰 <b>{f[1]}</b>" + empty, rows

    def card(self, did):
        d = self.store.draft(did)
        if not d:
            return "Чернетку не знайдено.", [[("« Новинарня", self.cb())]]
        n = length_of(d["body"])
        head = f"{URGENCY.get(d['urgency'], '')}{'🧾 Зведення' if d['kind'] == 'digest' else '📰 Новина'} #{d['id']} · {STATUS.get(d['status'], d['status'])} · v{d['version']}"
        meta = " · ".join(x for x in [d["region"], d["category"]] if x)
        ln = f"📏 {n} символів" + (f" із {d['target']} {'✅' if n == d['target'] else '❌'}" if d["target"] else "")
        parts = [head, "", f"<b>{esc(d['headline'])}</b>", "", linkify(d["body"][:2600]), ""]
        if meta:
            parts.append("🗺 " + esc(meta))
        parts.append(ln + (f" · 🤖 {esc(d['model'])}" if d["model"] else ""))
        if n > 1024:  # ⭐ підпис під фото: 1024 без Premium, 4096 з Premium
            prem = self.host.extra.get("premium_now", lambda: None)()
            parts.append("⭐ Під фото з вашого акаунта з Premium вміститься (до 4096)." if prem and n <= 4096 else
                         "ℹ️ Під фото без Telegram Premium вміщується лише 1024 символи (з Premium — 4096): "
                         "довший текст публікуйте окремим повідомленням або з акаунта, де Premium є.")
        if d["unconfirmed"]:
            parts.append("⚠️ Не підтверджено: " + esc("; ".join(d["unconfirmed"]))[:400])
        if d["sources"]:
            parts.append("🔗 " + src_links(d["sources"][:6]))
        if d["notes"]:
            parts.append("📝 " + esc(d["notes"])[:400])
        if d["check_report"]:
            parts.append("🔎 " + esc(d["check_report"])[:600])
        if d.get("folder"):
            parts.append("📁 " + esc(d["folder"]))
        if d["status"] == "published" and d["published_msg"]:
            parts.append("📤 Опубліковано в " + esc(self.store.get("publish_title") or d["published_chat"]))
        text = "\n".join(parts)[:4000]
        i = d["id"]
        rows = []
        if d["status"] in ("draft", "length_mismatch"):
            rows.append([("✅ Схвалити", self.cb("ok", i)), ("🔁 Переписати", self.cb("rw", i)), ("✍️ Правка", self.cb("ed", i))])
        rows += self.action_rows(i)          # 🧩 ваші програмовані кнопки
        xp = self.xp_get(i)
        rows.append([("📲 Під мережі" + (f" · {sum(1 for k in xp if not k.startswith('_'))}" if xp else ""), self.cb("xp", i))]
                    + [("🖼 Картка", self.cb("cd", i))] + ([("🗓 Запланувати", self.cb("pq", i))] if d["status"] == "approved" else []))
        ow = self.ow_find(i)
        if ow:
            rows.append([(f"🗂 Ми вже про це писали · {len(ow)}", self.cb("ow", i))])
        rows.append([("🌐 Переклад", self.cb("tr", i)), ("⋯ Ще", self.cb("more", i)), ("« Чернетки", self.cb("ls", "new"))])
        return text, rows

    def len_screen(self, did):
        d = self.store.draft(did)
        cur = d["target"]
        rows = [[(("● " if n == cur else "") + (str(n) if n else "Вільна"), self.cb("lenset", did, n)) for n in LENGTHS[:3]],
                [(("● " if n == cur else "") + str(n), self.cb("lenset", did, n)) for n in LENGTHS[3:]],
                [("✍️ Своя довжина", self.cb("lenask", did))], [("« Чернетка", self.cb("d", did))]]
        return (f"📏 <b>Довжина тексту #{did}</b>\n\nЗараз: {length_of(d['body'])} символів"
                + (f", ціль {cur}" if cur else ", ціль не задана") +
                ".\nПісля вибору модель підганятиме текст до 5 разів; бот рахує символи сам і не схвалить текст, поки довжина не збігається."), rows

    def src_screen(self, did):
        d = self.store.draft(did)
        it = self.store.item(d["item_id"]) if d and d["item_id"] else None
        if not it:
            return "📄 Оригіналу немає (це зведення або матеріал видалено).", [[("« Чернетка", self.cb("d", did))]]
        head = f"📄 <b>Оригінал #{it['id']}</b> · {esc(it['origin'])} · {ago(it['created'])}"
        if it["source_name"]:
            head += f"\nДжерело: {esc(it['source_name'])}"
        if it["source_url"]:
            head += f"\n{esc(it['source_url'])}"
        return head + "\n\n" + esc(it["raw_text"])[:3200], [[("« Чернетка", self.cb("d", did))]]

    def ver_screen(self, did):
        vs = self.store.versions(did)
        if not vs:
            return "🕘 Це перша версія — попередніх немає.", [[("« Чернетка", self.cb("d", did))]]
        lines = [f"🕘 <b>Попередні версії #{did}</b>"]
        rows = []
        for v in vs:
            lines.append(f"\n<b>{esc(short(v['headline'], 80))}</b> · {ago(v['created'])}" + (f" · {esc(v['note'])}" if v["note"] else "")
                         + f"\n{esc(short(v['body'], 220))}")
            rows.append([(f"↩️ Повернути «{short(v['headline'], 26)}»", self.cb("vback", did, v["id"]))])
        rows.append([("« Чернетка", self.cb("d", did))])
        return "\n".join(lines)[:4000], rows

    def digest_day_screen(self):
        day = time.time() - 86400
        n = len([d for d in self.store.drafts(("draft", "approved", "published"), since=day, limit=200) if d["kind"] == "news"])
        rows = [[(f"🧾 {x}", self.cb("dgo", x)) for x in (2000, 2100, 2200)], [("✍️ Інша довжина", self.cb("dgask"))],
                [("« Новинарня", self.cb())]]
        return ("🧾 <b>Зведення за добу</b>\n\n"
                f"Матеріалів за 24 години: {n} (чернетки, схвалені й опубліковані; відхилені не беруться).\n"
                "Групування за областями, Київ першим, без подвійного підрахунку загиблих і поранених. "
                "Готове зведення стає чернеткою з тими самими кнопками погодження."), rows

    def sources_screen(self):
        rows, lines = [], ["📡 <b>RSS і вебхуки</b>", ""]
        rss = self.store.sources("rss")
        lines.append("Канали, чати й групи Telegram — у «📋 Списки каналів і чатів».")
        rows.append([("📋 Списки каналів і чатів", self.cb("L"))])
        lines += ["", f"<b>RSS / Atom</b> (перевірка кожні {int(self.store.get('rss_every') or 300) // 60} хв):"]
        if not rss:
            lines.append("— поки жодної стрічки.")
        for s in rss:
            err = f" · ⚠️ {esc(short(s['last_error'], 60))}" if s["last_error"] else ""
            lines.append(f"{'🟢' if s['enabled'] else '⚪️'} {esc(s['title'] or s['ref'])} · узято {s['taken']}{err}")
            rows.append([(("🟢 " if s["enabled"] else "⚪️ ") + short(s["title"] or s["ref"], 28), self.cb("sox", s["id"])), ("🗑", self.cb("sodel", s["id"]))])
        rows.append([("➕ Додати RSS", self.cb("soadd")), ("🔄 Перевірити RSS", self.cb("poll"))])
        rows.append([(("🟢 Автоперевірка RSS" if self.store.get("auto_rss") else "⚪️ Автоперевірка RSS"), self.cb("autorss"))])
        rows.append([("🌐 WhatsApp і вебхук", self.cb("hooks")), ("« Джерела", self.cb("H1"))])
        return "\n".join(lines)[:4000], rows

    def hooks_screen(self):
        base = os.getenv("PUBLIC_URL", "").rstrip("/")
        secret = self.host.extra.get("secret", "")
        ingest = f"{base}/{secret[:4]}…/newsroom/ingest" if base else "/‹MCP_SECRET›/newsroom/ingest"
        wa = "🟢 налаштовано" if os.getenv("WHATSAPP_APP_SECRET") and os.getenv("WHATSAPP_VERIFY_TOKEN") else "⚪️ не налаштовано"
        text = ("🌐 <b>Зовнішні входи новинарні</b>\n\n"
                f"<b>Вебхук для n8n / Make / Zapier</b>\nPOST {esc(ingest)}\n"
                "Тіло JSON: <code>{\"text\": \"…\", \"url\": \"…\", \"source\": \"назва\"}</code>\n"
                "Повна адреса — PUBLIC_URL/MCP_SECRET/newsroom/ingest (секрет у самій адресі, не публікуйте її).\n\n"
                f"<b>WhatsApp Business Cloud API</b>: {wa}\n"
                "Callback URL: PUBLIC_URL/MCP_SECRET/newsroom/whatsapp\n"
                "Змінні: WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, WHATSAPP_ALLOWED_SENDERS (номери без +), "
                "за бажанням WHATSAPP_ACCESS_TOKEN для фото. Підпис X-Hub-Signature-256 перевіряється.\n\n"
                "Загального доступу до особистих чатів і груп WhatsApp офіційний API не дає. Деталі — docs/17-plugins.md.")
        return text, [[("« Джерела", self.cb("so"))]]

    def settings_screen(self):
        st = self.store
        target = int(st.get("target") or 0)
        rules = st.get("rules") or ""
        text = ("📤 <b>Публікація</b>\n\n"
                f"📤 Канал публікацій: {esc(st.get('publish_title') or 'не підключено')}\n"
                f"🚀 Публікувати одразу після «Схвалити»: {'так' if st.get('publish_on_approve') else 'ні (вручну кнопкою «📤»)'}\n"
                f"🔗 Джерела під дописом: {'показувати' if st.get('show_sources') else 'ховати'}\n"
                f"✒️ Підпис: {esc(st.get('signature') or '—')}\n"
                f"📏 Довжина окремих новин: {target or 'вільна'}\n"
                f"🔔 Нові автоматичні чернетки надсилати власнику: {'так' if st.get('notify') else 'ні'}\n"
                f"📜 Власні правила редакції: {esc(short(rules, 120)) if rules else 'немає (діють базові правила GreenPost)'}")
        rows = [
            [("📤 Підключити канал", self.cb("pubch"))] + ([("✖️ Відключити", self.cb("pubchx"))] if st.get("publish_chat") else []),
            [(("🚀 Одразу: так" if st.get("publish_on_approve") else "🚀 Одразу: ні"), self.cb("tg", "publish_on_approve")),
             (("🔗 Джерела: так" if st.get("show_sources") else "🔗 Джерела: ні"), self.cb("tg", "show_sources"))],
            [("✒️ Підпис", self.cb("sig")), ("⚙️ Версії для мереж", self.cb("xps"))],
            [("⚙️ Вигляд карток", self.cb("cds")), ("📖 Словник редакції", self.cb("styd"))],
            [("« Налаштування новинарні", self.cb("H2"))],
        ]
        return text, rows

    def rules_screen(self):
        from .engine import RULES
        own = self.store.get("rules") or ""
        text = ("📜 <b>Правила редакції</b>\n\n<b>Базові (завжди діють):</b>\n" + esc(RULES.split("\n", 1)[1]) +
                "\n\n<b>Ваші додаткові:</b>\n" + (esc(own) if own else "— немає"))
        rows = [[("✍️ Задати свої", self.cb("rulesask"))] + ([("🗑 Прибрати свої", self.cb("rulesx"))] if own else []),
                [("« Як писати", self.cb("H3"))]]
        return text[:4000], rows

    def setlen_screen(self):
        cur = int(self.store.get("target") or 0)
        rows = [[(("● " if n == cur else "") + (f"📌{n}" if n else "📌Вільна"), self.cb("setlenv", n)) for n in LENGTHS[:3]],
                [(("● " if n == cur else "") + f"📌{n}", self.cb("setlenv", n)) for n in LENGTHS[3:]],
                [("« Як писати", self.cb("H3"))]]
        return "📏 <b>Довжина нових новин</b>\n\nЗастосовується до матеріалів, що надійдуть після зміни.", rows

    def queue_screen(self):
        with self.store.db() as con:
            rows_db = [dict(r) for r in con.execute(
                "SELECT id, origin, source_name, status, error, created, attempts FROM items "
                "WHERE status IN ('queued','processing','error','skipped') ORDER BY id DESC LIMIT 12")]
        lines = ["⏳ <b>Черга й помилки</b>", ""]
        rows = []
        icon = {"queued": "⏳", "processing": "⚙️", "error": "⚠️", "skipped": "⏭"}
        for r in rows_db:
            lines.append(f"{icon.get(r['status'], '•')} #{r['id']} {esc(r['origin'])} {esc(short(r['source_name'], 24))} · {ago(r['created'])}"
                         + (f"\n   {esc(short(r['error'], 120))}" if r["error"] else ""))
            if r["status"] in ("error", "skipped"):
                rows.append([(f"🔁 #{r['id']} ще раз", self.cb("retry", r["id"]))])
        if not rows_db:
            lines.append("Порожньо — усе оброблено.")
        rows.append([("🔄 Оновити", self.cb("q")), ("« Новинарня", self.cb())])
        return "\n".join(lines)[:4000], rows

    def help_screen(self):
        text = ("ℹ️ <b>Новинарня: як користуватися</b>\n\n"
                "<b>1. Віддати новину</b>\n• «📥 Надіслати новину» і вставте текст або посилання;\n"
                "• /news текст — або відповідайте /news на будь-яке повідомлення;\n"
                "• «Прийом: увімк.» — усе, що пересилаєте боту (дописи, фото з підписом), іде в новинарню.\n\n"
                "<b>2. Автоматично</b>\n• канали, де бот адміністратор (бот сам запропонує моніторинг);\n"
                "• RSS-стрічки (перше читання лише запам'ятовує старі пости);\n• WhatsApp Business і вебхук n8n — «📡 Джерела» → «🌐».\n\n"
                "<b>3. Чернетка</b> приходить із кнопками: ✅ схвалити, 🔁 інша версія, ✍️ правка своїми словами, "
                "📏 точна довжина, 🔎 звірити з вебпошуком, ❌ відхилити, 📄 оригінал, 🕘 версії.\n\n"
                "<b>4. Публікація</b> — лише схваленого і лише в підключений канал. Автопублікація без людини неможлива.\n\n"
                "<b>5. Зведення</b> — /digest 2000 (або 2100, 2200, будь-яке 300–3800).\n\n"
                "Повтори (той самий або майже той самий текст за 72 год) відсіюються; схожі матеріали позначаються як можливе оновлення події.")
        return text, [[("« Новинарня", self.cb())]]

    # ================================================================ дії
    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else None
        y = args[2] if len(args) > 2 else None
        st = self.store
        if a[:1] in ("L", "E"):
            return await self.lists_route(chat, a, args[1:])
        if a in ("act", "more", "tobot", "tofld", "fldnew", "delok", "acs", "ace", "act2", "actg", "actt", "acl", "acm", "acd", "acn", "acr",
                 "sch", "sadd", "sdel", "fld", "flds"):
            return await self.actions_route(chat, a, x, y)
        if a in ("rp", "rpsel", "rpt", "rpin", "rpshow", "rpau", "rpun", "rpa", "rgo"):
            return await self.report_route(chat, a, x, y)
        if a in ("ow", "owa", "ows", "owq"):
            return await self.ow_route(chat, a, x, y)
        if a in ("sty", "styfix", "styd", "stydi", "stydx"):
            return await self.sty_route(chat, a, x, y)
        if a in ("cd", "cdv", "cda", "cdtone", "cdx", "cdp", "cdr", "cdu", "cdl", "cdlx", "cdn", "cdf", "cds", "cdc", "cdz", "cdb"):
            return await self.cd_route(chat, a, x, y)
        if a in ("pq", "pql", "pqs", "pqt", "pqi", "pqp", "pqx", "pqn", "pqr", "pqdone", "pqh", "pqhi", "pqhr"):
            return await self.pq_route(chat, a, x, y)
        if a in ("xp", "xpg", "xps", "xpt", "xpc", "xpr"):
            return await self.xp_route(chat, a, x, y)
        if a in ("SN", "SNk", "SNadd"):
            return self.social_route(chat, a, x)
        if a == "echo":
            return None, self.echo_screen(x)
        if a == "eci":
            return None, self.echo_card(x)
        if a == "ecn":
            st.set("echo_notify", x)
            return None, self.echo_screen()
        if a == "ecb":
            self.ask(chat, "ecb")
            return None, (f"🏷 Як нас називають і як на нас посилаються — через кому (назви, @канали, домени).\nЗараз: {esc(', '.join(self.echo.brands()) or '—')}",
                          [[("✖️ Скасувати", "m:cancel")]])
        if a == "ecbl":
            r = next((e for e in self.echo.items(limit=500) if e["id"] == int(x)), None)
            if r:
                self.desk.add_block("source", r["source"])
            return "🚫 Джерело заблоковано", self.echo_screen()
        if a in ("H1", "H2", "H3", "H4", "H5"):
            return None, getattr(self, "hub_" + a)(chat)
        if a == "Hfwd":
            st.set("forward_auto", not st.get("forward_auto"))
            return None, self.hub_H2(chat)
        if a == "toai":
            it = st.item(int(x))
            st.set_item(int(x), status="skipped", error="користувач: це не новина")
            ask = self.host.extra.get("chat_answer")
            if ask and it:
                self._bg(ask(chat, it["raw_text"]))
                return "💬 Відповідаю як ШІ", None
            return "Скасовано", None
        if a[:1] in ("D", "F", "T", "B", "S", "O", "W"):
            return await self.desk_route(chat, a, args[1:])
        if a == "":
            return None, self.home(chat)
        if a == "in":
            self.ask(chat, "in")
            return None, ("📥 <b>Надішліть новину</b>\n\nТекст, посилання або кілька абзаців. Фото з підписом — просто перешліть у режимі прийому.",
                          [[("✖️ Скасувати", "m:cancel")]])
        if a == "intake":
            lst = list(st.get("intake") or [])
            on = chat not in lst
            lst = lst + [chat] if on else [c for c in lst if c != chat]
            st.set("intake", lst)
            return ("Режим прийому ввімкнено" if on else "Режим прийому вимкнено"), self.home(chat)
        if a == "ls":
            return None, self.list_screen(x or "new", int(y or 0))
        if a == "d":
            return None, self.card(int(x))
        if a == "src":
            return None, self.src_screen(int(x))
        if a == "ver":
            return None, self.ver_screen(int(x))
        if a == "vback":
            vs = {v["id"]: v for v in st.versions(int(x))}
            v = vs.get(int(y))
            if v:
                st.update_draft(int(x), headline=v["headline"], body=v["body"], note="повернуто стару версію", status="draft")
                self._recheck_len(int(x))
            return "Версію повернуто", self.card(int(x))
        if a == "ok":
            d = st.draft(int(x))
            if d["status"] == "length_mismatch":
                return "Спочатку підженіть довжину (📏)", self.card(d["id"])
            if d["status"] not in ("draft",):
                return "Вже вирішено", self.card(d["id"])
            from .engine import blocking
            if not d.get("check_report"):  # 🔎 неперевірене не схвалюємо — спершу звірка
                self._autocheck(d["id"])
                return "🔎 Чернетку ще перевіряють (факти, цитати, унікальність) — за хвилину відкрийте знову", self.card(d["id"])
            probs = blocking(d["check_report"])
            if probs and y != "force":
                text, rows = self.card(d["id"])
                rows = [[("🔁 Переписати з урахуванням звірки", self.cb("rw", d["id"]))],
                        [("⚠️ Схвалити попри зауваження", self.cb("ok", d["id"], "force"))]] + rows
                return "⛔ Є незвірені факти чи цитати — див. 🔎 звіт", ("⛔ <b>Схвалення заблоковано перевіркою</b>\n"
                                                                         + "\n".join(esc(p) for p in probs[:6]) + "\n\n" + text, rows)
            st.update_draft(d["id"], keep_version=True, status="approved", decided_by=chat)
            if st.get("publish_on_approve") and st.get("publish_chat"):
                ok, msg = await self.publish(d["id"])
                return msg, self.card(d["id"])
            return "✅ Схвалено", self.card(d["id"])
        if a == "no":
            st.update_draft(int(x), keep_version=True, status="rejected", decided_by=chat)
            return "❌ Відхилено", self.card(int(x))
        if a == "re":
            st.update_draft(int(x), keep_version=True, status="draft")
            self._recheck_len(int(x))
            return "Повернуто в чернетки", self.card(int(x))
        if a == "txt":
            d = st.draft(int(x))
            bot = self.host.bot()
            if bot and d:
                await bot.send_message(chat, (d["headline"] + "\n\n" + d["body"])[:4096])
            return "Надіслав окремим повідомленням — утримуйте, щоб скопіювати", None
        if a == "pub":
            ok, msg = await self.publish(int(x))
            return msg, self.card(int(x))
        if a == "rw":
            self._bg(self._revise(chat, int(x), ""))
            return "🔁 Переписую…", ("🔁 <b>Готую нову версію</b> — прийде окремим повідомленням. Стара версія збережеться в «🕘 Версії».",
                                    [[("« Чернетка", self.cb("d", x))]])
        if a == "ed":
            self.ask(chat, "ed", int(x))
            return None, ("✍️ <b>Що змінити?</b>\n\nНапишіть вказівку своїми словами, наприклад: «Переписати лід без оцінок, "
                          "додати, що дані попередні, заголовок коротший».", [[("✖️ Скасувати", "m:cancel")]])
        if a == "len":
            return None, self.len_screen(int(x))
        if a == "lenset":
            n = int(y)
            st.update_draft(int(x), keep_version=True, target=n)
            if n:
                self._bg(self._refit(chat, int(x)))
                return f"📏 Підганяю до {n}…", ("📏 <b>Підганяю довжину</b> — результат прийде окремим повідомленням.", [[("« Чернетка", self.cb("d", x))]])
            self._recheck_len(int(x))
            return "Довжина вільна", self.card(int(x))
        if a == "lenask":
            self.ask(chat, "len", int(x))
            return None, ("📏 Напишіть потрібну кількість символів (100–3800).", [[("✖️ Скасувати", "m:cancel")]])
        if a == "chk":
            if not self.host.search:
                return "Вебпошук недоступний", self.card(int(x))
            self._bg(self._check(chat, int(x)))
            return "🔎 Звіряю…", ("🔎 <b>Звіряю з вебпошуком</b> — звіт додасться до чернетки й прийде окремим повідомленням.",
                                 [[("« Чернетка", self.cb("d", x))]])
        if a == "tr":  # 🌐 переклад чернетки будь-якою мовою (бот надішле переклад окремим повідомленням)
            d = self.store.draft(int(x))
            ts = self.host.extra.get("translate_screen")
            if not d or not ts:
                return "Переклад недоступний", self.card(int(x))
            t, r = ts(f"{d['headline']}\n\n{d['body']}", f"🌐 <b>Переклад новини #{d['id']}</b> — оберіть мову:")
            return None, (t, r + [[("« Чернетка", self.cb("d", x))]])
        if a in ("obh", "obt", "obc", "obd", "ob", "obw", "obz", "obx", "obu", "obm", "oba", "obi", "obl", "obs", "obup", "obws", "obwt",
                 "obf", "obv", "obr", "obp", "obe", "oby", "obn", "obg", "obj", "obq", "obcp", "obcpr", "obcps", "obsec", "obce", "obcd", "obca", "obcw", "obcx", "obcu"):
            return await self.obs_route(chat, a, x, y)
        if a in ("dg", "cgo", "cls", "cpop", "cclr", "cday", "cmk", "ctp", "cte", "ctd", "ctn", "ctx", "ctc", "ctl", "cti", "ctz",
                 "csg", "csp", "csl", "csd", "csn", "cse", "cdt"):
            return await self.digest_route(chat, a, x, y)
        if a == "dgo":
            self._bg(self._make_digest(chat, int(x)))
            return "🧾 Складаю…", ("🧾 <b>Складаю зведення</b> — прийде окремим повідомленням.", [[("« Новинарня", self.cb())]])
        if a == "dgask":
            self.ask(chat, "dg")
            return None, ("🧾 Скільки символів має бути у зведенні? (300–3800)", [[("✖️ Скасувати", "m:cancel")]])
        if a == "so":
            return None, self.sources_screen()
        if a == "sox":
            s = st.source(int(x))
            if s:
                st.set_source(s["id"], enabled=0 if s["enabled"] else 1)
            return ("Увімкнено" if s and not s["enabled"] else "Вимкнено"), self.sources_screen()
        if a == "sodel":
            st.del_source(int(x))
            return "Джерело видалено", self.sources_screen()
        if a == "soadd":
            self.ask(chat, "rss")
            return None, ("➕ <b>Адреса RSS або Atom</b>\n\nНаприклад https://site.ua/rss. Лише публічні https-адреси.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "poll":
            added, errors = await self.poll_rss()
            msg = f"Нових: {added}" + (f", помилок: {len(errors)}" if errors else "")
            return msg, self.sources_screen()
        if a == "autorss":
            st.set("auto_rss", not st.get("auto_rss"))
            return None, self.sources_screen()
        if a == "hooks":
            return None, self.hooks_screen()
        if a == "set":
            return None, self.settings_screen()
        if a == "tg" and x in ("publish_on_approve", "show_sources", "notify"):
            st.set(x, not st.get(x))
            return None, self.settings_screen()
        if a == "pubch":
            self.wait_forward[chat] = True
            return None, ("📤 <b>Підключення каналу</b>\n\n1. Додайте бота в канал адміністратором із правом публікації.\n"
                          "2. Перешліть сюди будь-який допис із цього каналу.", [[("« Публікація", self.cb("set"))]])
        if a == "pubchx":
            st.set("publish_chat", "")
            st.set("publish_title", "")
            return "Канал відключено", self.settings_screen()
        if a == "setlen":
            return None, self.setlen_screen()
        if a == "setlenv":
            st.set("target", int(x))
            return "Збережено", self.hub_H3(chat)
        if a == "sig":
            self.ask(chat, "sig")
            return None, ("✒️ Напишіть підпис під дописами (наприклад «🌿 GreenPost · підписатися»), або «-», щоб прибрати.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "rules":
            return None, self.rules_screen()
        if a == "rulesask":
            self.ask(chat, "rules")
            return None, ("📜 Напишіть додаткові правила редакції одним повідомленням. Вони доповнять базові й матимуть пріоритет.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "rulesx":
            st.set("rules", "")
            return "Прибрано", self.rules_screen()
        if a == "q":
            return None, self.queue_screen()
        if a == "retry":
            st.set_item(int(x), status="queued", attempts=0, error="")
            self._enqueue(int(x))
            return "Повторюю", self.queue_screen()
        if a == "force":
            st.set_item(int(x), status="queued", attempts=0, dup_of=None, requested_by=chat)
            self._enqueue(int(x))
            return "📝 Оформлюю", ("📝 Беру в роботу — чернетка прийде окремим повідомленням.", [[("« Новинарня", self.cb())]])
        if a == "help":
            return None, self.help_screen()
        return None, self.home(chat)

    def is_view(self, args):
        return not args or args[0] in self.VIEWS

    async def on_file(self, update, ctx):
        return await self.cd_on_file(update, ctx)

    async def on_text(self, update, ctx, chat, key, data, text):
        st = self.store
        if key[:1] in ("L", "E"):
            return await self.lists_text(chat, key, data, text)
        if key == "xpr":
            return await self.xp_text(chat, key, data, text)
        if key == "cdb":
            return await self.cd_text(chat, key, data, text)
        if key == "owq":
            return await self.ow_text(chat, key, data, text)
        if key == "styd":
            return await self.sty_text(chat, key, data, text)
        if key in ("pqi", "pqh"):
            return await self.pq_text(chat, key, data, text)
        if key == "ecb":
            st.set("brands", text.strip()[:500])
            return "Збережено", self.echo_screen()
        if key in ("obm", "oba", "obe", "oby", "obn", "obce", "obcu"):
            return await self.obs_text(chat, key, data, text)
        if key in ("ctl", "cti", "ctz", "csp", "csn", "cse"):
            return await self.digest_text(chat, key, data, text)
        if key in ("fldnew", "actchat", "aclabel", "sadd"):
            return await self.actions_text(chat, key, data, text)
        if key[:1] in ("D", "F", "T", "B", "S", "O", "W"):
            return await self.desk_text(chat, key, data, text)
        if key == "in":
            iid, s, dup = self.ingest("manual", text, requested_by=chat)
            if s == "duplicate":
                return f"♻️ Повтор матеріалу #{dup}", self.home(chat)
            return f"📥 Прийнято #{iid}", ("📥 Прийнято. Чернетка прийде окремим повідомленням.", [[("📥 Ще новина", self.cb("in")), ("« Новинарня", self.cb())]])
        if key == "ed":
            self._bg(self._revise(chat, int(data), text))
            return "✍️ Виконую правку…", ("✍️ <b>Виконую правку</b> — нова версія прийде окремим повідомленням.", [[("« Чернетка", self.cb("d", data))]])
        if key == "len":
            if not text.strip().isdigit() or not 100 <= int(text) <= 3800:
                return "Потрібне число 100–3800", self.len_screen(int(data))
            st.update_draft(int(data), keep_version=True, target=int(text))
            self._bg(self._refit(chat, int(data)))
            return "📏 Підганяю…", ("📏 Підганяю довжину — результат прийде окремим повідомленням.", [[("« Чернетка", self.cb("d", data))]])
        if key == "dg":
            if not text.strip().isdigit() or not 300 <= int(text) <= 3800:
                return "Потрібне число 300–3800", self.digest_screen()
            self._bg(self._make_digest(chat, int(text)))
            return "🧾 Складаю…", ("🧾 Складаю зведення — прийде окремим повідомленням.", [[("« Новинарня", self.cb())]])
        if key == "rss":
            url = text.strip().split()[0]
            if not url.startswith("https://") or not await public_https(url):
                return "Потрібна публічна https-адреса", self.sources_screen()
            try:
                title, entries = parse(await fetch(url))
            except (FeedError, Exception) as exc:  # noqa: BLE001
                return None, (f"⚠️ Не вдалося прочитати стрічку: {esc(exc)}", [[("➕ Інша адреса", self.cb("soadd")), ("« Джерела", self.cb("so"))]])
            s = st.add_source("rss", url, title or url)
            for e in entries:  # усе наявне — як уже бачене
                st.seen_once(f"rss:{s['id']}:{e['guid']}")
            st.set_source(s["id"], last_poll=time.time())
            return f"✅ Додано: {short(title or url, 40)}", self.sources_screen()
        if key == "sig":
            st.set("signature", "" if text.strip() == "-" else text.strip()[:200])
            return "Збережено", self.settings_screen()
        if key == "rules":
            st.set("rules", text.strip()[:3000])
            return "Правила збережено", self.rules_screen()
        return None, self.home(chat)

    # ================================================================ фонові дії
    def _bg(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.__dict__.setdefault("_jobs", set()).add(t)
        t.add_done_callback(self._jobs.discard)

    def _recheck_len(self, did):
        d = self.store.draft(did)
        if d and d["status"] in ("draft", "length_mismatch"):
            ok = not d["target"] or length_of(d["body"]) == d["target"]
            self.store.update_draft(did, keep_version=True, status="draft" if ok else "length_mismatch")

    async def _revise(self, chat, did, instruction):
        d = self.store.draft(did)
        try:
            data, model, ok = await self.engine.revise(d, instruction, d["target"], self.store.get("rules") or "")
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Не вдалося переписати #{did}: {esc(exc)[:300]}", [[("« Чернетка", self.cb("d", did))]])
        self.store.update_draft(did, note=("правка: " + instruction[:80]) if instruction else "переписано",
                                headline=data["headline"] or d["headline"], body=data["body"] or d["body"],
                                region=data["region"] or d["region"], category=data["category"] or d["category"],
                                unconfirmed=data["unconfirmed"], confirmed=data["confirmed"],
                                sources=list(dict.fromkeys(d["sources"] + data["sources"])),
                                notes=data["notes"], model=model, check_report="",
                                status="draft" if ok else "length_mismatch")
        await self.host.notify(chat, *self.card(did))

    async def _refit(self, chat, did):
        d = self.store.draft(did)
        try:
            data, ok = await self.engine.refit(d, d["target"], self.store.get("rules") or "")
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Не вдалося підігнати довжину #{did}: {esc(exc)[:300]}")
        self.store.update_draft(did, note=f"довжина {d['target']}", body=data["body"], status="draft" if ok else "length_mismatch")
        n = length_of(data["body"])
        if not ok:
            await self.host.notify(chat, f"📏 #{did}: вийшло {n} замість {d['target']} після 5 спроб. Схвалення заблоковано — спробуйте ще раз або змініть довжину.")
        await self.host.notify(chat, *self.card(did))

    def _autocheck(self, did):
        """🔎 Автоматична перевірка нової чернетки: звірка фактів із джерелами + мова (іншомовні вставки), у фоні."""
        async def go():
            d = self.store.draft(did)
            if not d:
                return
            it = self.store.item(d["item_id"]) if d.get("item_id") else None
            try:
                report = await self.engine.check(d, self.host.search, (it or {}).get("raw_text") or "")
            except Exception as exc:  # noqa: BLE001
                report = f"⚠️ Автоматична звірка не вдалася: {exc} — факти не звірено"
            import langguard
            bits = langguard.foreign((d.get("headline") or "") + "\n" + (d.get("body") or ""))
            if bits:
                report = (report or "") + "\n🔤 Іншомовні вставки — замініть: " + ", ".join(bits[:10])
            from . import styleguide
            sn = styleguide.check(d.get("headline") or "", d.get("body") or "", self.sty_custom())
            if sn:
                report = (report or "") + "\n" + styleguide.report(sn, 5)
            self.store.update_draft(did, keep_version=True, check_report=report or "✅ Перевірено автоматично: зауважень немає")
        try:
            t = asyncio.get_running_loop().create_task(go())
            self.__dict__.setdefault("_auto_tasks", set()).add(t)
            t.add_done_callback(lambda x: self._auto_tasks.discard(x))
        except RuntimeError:
            pass

    async def _check(self, chat, did):
        d = self.store.draft(did)
        it = self.store.item(d["item_id"]) if d.get("item_id") else None
        try:
            report = await self.engine.check(d, self.host.search, (it or {}).get("raw_text") or "")
        except Exception as exc:  # noqa: BLE001
            report = f"⚠️ Звірка не вдалася: {exc}"
        self.store.update_draft(did, keep_version=True, check_report=report)
        await self.host.notify(chat, *self.card(did))

    async def _make_digest(self, chat, target):
        day = time.time() - 86400
        ds = [d for d in self.store.drafts(("approved", "published", "draft"), since=day, limit=80) if d["kind"] == "news"]
        if not ds:
            return await self.host.notify(chat, "🧾 За останню добу немає матеріалів для зведення.")
        ds.sort(key=lambda d: (region_key(d["region"]), d["created"]))
        try:
            data, model, ok = await self.engine.digest(ds, target, self.store.get("rules") or "")
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Зведення не вдалося: {esc(exc)[:300]}")
        did = self.store.add_draft(None, data, kind="digest", model=model, target=target, status="draft" if ok else "length_mismatch")
        self._autocheck(did)  # 🔎 кожна чернетка перевіряється одразу: факти й мова
        await self.host.notify(chat, *self.card(did))

    def post_html(self, d):
        parts = [f"<b>{esc(d['headline'])}</b>", "", linkify(d["body"])]
        if self.store.get("show_sources") and d["sources"] and d["kind"] == "news":
            parts += ["", "Джерела: " + src_links(d["sources"][:4], ", ")]
        sig = self.store.get("signature")
        if sig:
            parts += ["", esc(sig)]
        return "\n".join(parts)

    async def publish(self, did):
        d = self.store.draft(did)
        chat = self.store.get("publish_chat")
        if not chat:
            return False, "Канал публікацій не підключено"
        if d["status"] != "approved":
            return False, "Публікується лише схвалене"
        bot = self.host.bot()
        if not bot:
            return False, "Telegram недоступний"
        try:
            m = await bot.send_message(int(chat), self.post_html(d)[:4096], parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"Не вдалося: {str(exc)[:150]}"
        self.store.update_draft(did, keep_version=True, status="published", published_chat=str(chat), published_msg=m.message_id)
        uname = self.store.get("publish_username") or ""
        self.desk.add_ours(self.store.get("publish_title") or "наш канал", f"https://t.me/{uname}/{m.message_id}" if uname else "",
                           d["headline"], d["headline"] + "\n" + d["body"])
        return True, "📤 Опубліковано"

    # ================================================================ HTTP-входи
    def routes(self, secret):
        from starlette.responses import JSONResponse, PlainTextResponse, Response
        from starlette.routing import Route

        async def ingest(request):
            if not self.on():
                return JSONResponse({"ok": False, "error": "plugin disabled"}, status_code=503)
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"ok": False, "error": "json expected"}, status_code=400)
            text = str(body.get("text") or "")[:20000]
            url = str(body.get("url") or "")[:500]
            iid, status, dup = self.ingest("webhook", text, str(body.get("source") or "webhook")[:120], url)
            if iid is None:
                return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
            return JSONResponse({"ok": True, "id": iid, "status": status, "duplicate_of": dup})

        async def wa_verify(request):
            q = request.query_params
            token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
            if token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), token):
                return PlainTextResponse(q.get("hub.challenge", ""))
            return PlainTextResponse("forbidden", status_code=403)

        async def wa_event(request):
            raw = await request.body()
            app_secret = os.getenv("WHATSAPP_APP_SECRET", "").encode()
            sig = request.headers.get("x-hub-signature-256", "")
            if not app_secret or not verify_signature(app_secret, raw, sig):
                return PlainTextResponse("bad signature", status_code=403)
            if not self.on():
                return Response(status_code=200)
            try:
                payload = json.loads(raw)
            except ValueError:
                return Response(status_code=400)
            for sender, kind, text, media in whatsapp_messages(payload, allowed_senders()):
                photo = ""
                if media and os.getenv("WHATSAPP_ACCESS_TOKEN"):
                    try:
                        photo = await whatsapp_media(media)
                    except Exception:  # noqa: BLE001
                        log.warning("whatsapp media download failed")
                self.ingest("whatsapp", text, f"WhatsApp +{sender}", "", photo)
            return Response(status_code=200)  # Meta чекає 200 швидко, обробка йде у фоні

        p = f"/{secret}/newsroom"
        result = [Route(p + "/ingest", ingest, methods=["POST"]),
                  Route(p + "/whatsapp", wa_verify, methods=["GET"]),
                  Route(p + "/whatsapp", wa_event, methods=["POST"])]
        short_base = os.getenv("ORION_SHORT_BASE", "").strip()
        if short_base:
            from .utm_tracking import ShortLinks, routes as short_routes
            tracker = ShortLinks(self.dir / "utm_tracking.sqlite", short_base)
            result.extend(short_routes(tracker))
        return result


# ==================================================================== WhatsApp
def verify_signature(secret: bytes, raw: bytes, header: str) -> bool:
    if not header.startswith("sha256="):
        return False
    good = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, header[7:])


def allowed_senders():
    return {x.strip().lstrip("+") for x in os.getenv("WHATSAPP_ALLOWED_SENDERS", "").split(",") if x.strip()}


def whatsapp_messages(payload, allowed):
    """-> [(номер, тип, текст, media_id)] лише від дозволених номерів."""
    out = []
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            for m in (ch.get("value") or {}).get("messages") or []:
                sender = str(m.get("from") or "")
                if not allowed or sender not in allowed:
                    continue
                kind = m.get("type")
                if kind == "text":
                    out.append((sender, kind, (m.get("text") or {}).get("body", ""), None))
                elif kind == "image":
                    img = m.get("image") or {}
                    out.append((sender, kind, img.get("caption", ""), img.get("id")))
    return out


async def whatsapp_media(media_id):
    import httpx
    ver = os.getenv("META_GRAPH_VERSION", "v21.0")
    head = {"Authorization": "Bearer " + os.getenv("WHATSAPP_ACCESS_TOKEN", "")}
    async with httpx.AsyncClient(timeout=20) as c:
        meta = (await c.get(f"https://graph.facebook.com/{ver}/{media_id}", headers=head)).json()
        url = meta.get("url", "")
        if not url.startswith("https://") or int(meta.get("file_size") or 0) > 8 * 1024 * 1024:
            return ""
        r = await c.get(url, headers=head)
        r.raise_for_status()
        return base64.b64encode(r.content[: 8 * 1024 * 1024]).decode()
