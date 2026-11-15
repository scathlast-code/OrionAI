"""🧷 Стікери — завантаження, архівування й стеження за пакетами стікерів і емодзі Telegram.

Поєднує Sticker Oracle Bot (Oracle Cloud) і вбудований /stickers Оріона в одному розділі меню.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from plugins import Plugin as Base, esc
from . import exporter as X

log = logging.getLogger("orion.plugins.stickers")
CHECK_EVERY = 6 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, at REAL, packs TEXT, mode TEXT,
    files INTEGER, dups INTEGER, parts INTEGER, bytes INTEGER, errors INTEGER);
CREATE TABLE IF NOT EXISTS tracked (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, name TEXT, kind TEXT, title TEXT,
    signature TEXT, ids TEXT, count INTEGER, checked REAL, changed REAL, UNIQUE(chat, name));
CREATE TABLE IF NOT EXISTS prefs (chat INTEGER PRIMARY KEY, mode TEXT DEFAULT 'one', png INTEGER DEFAULT 0);
"""


def mb(n):
    return f"{n / 1048576:.1f} МБ"


class Plugin(Base):
    id = "stk"
    icon = "🧷"
    title = "Стікери"
    version = "2.0"
    admin_only = False
    summary = ("Завантажує пакети стікерів і кастомних емодзі Telegram в оригінальних форматах (.webp, .tgs, .webm): "
               "один спільний архів без повторів або окремий архів на кожен пакет, великі архіви діляться на частини до 45 МБ. "
               "PNG-копії статичних стікерів, інформація про пакет, історія, стеження за оновленнями пакетів. "
               "Надішліть боту будь-який стікер — він запропонує завантажити весь пакет.")
    VIEWS = {"hist", "trk", "help", "info"}

    def __init__(self, host):
        super().__init__(host)
        self.db_path = self.dir / "stickers.db"
        with closing(self.db()) as con, con:
            con.executescript(SCHEMA)
        self.pending: dict[str, tuple[int, str]] = {}   # токен -> (чат, текст із посиланнями)
        self.busy: set[int] = set()
        self.commands = [("stickerpack", self.cmd_pack, "Стікери: завантажити пакети за посиланнями")]

    def db(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def prefs(self, chat):
        with closing(self.db()) as con:
            r = con.execute("SELECT mode, png FROM prefs WHERE chat=?", (chat,)).fetchone()
        return {"mode": r["mode"], "png": bool(r["png"])} if r else {"mode": "one", "png": False}

    def set_pref(self, chat, **kw):
        p = self.prefs(chat) | kw
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO prefs(chat,mode,png) VALUES(?,?,?) ON CONFLICT(chat) DO UPDATE SET mode=excluded.mode, png=excluded.png",
                        (chat, p["mode"], int(p["png"])))

    def _token(self, chat, text):
        t = secrets.token_hex(4)
        self.pending[t] = (chat, text)
        if len(self.pending) > 200:
            self.pending.pop(next(iter(self.pending)))
        return t

    def status_line(self):
        with closing(self.db()) as con:
            h = con.execute("SELECT COUNT(*), COALESCE(SUM(files),0) FROM history").fetchone()
            t = con.execute("SELECT COUNT(*) FROM tracked").fetchone()[0]
        return f"завантажень {h[0]} · файлів {h[1]} · під стеженням {t}"

    # ================================================================ екрани
    def home(self, chat):
        p = self.prefs(chat)
        mode = "📦 один архів без повторів" if p["mode"] == "one" else "🗂 окремий архів на кожен пакет"
        text = ("🧷 <b>Стікери й емодзі</b>\n\n"
                "Надішліть посилання <code>t.me/addstickers/…</code> або <code>t.me/addemoji/…</code> (до "
                f"{X.MAX_PACKS} за раз) — або просто сам стікер.\n\n"
                f"Режим: {mode}\nPNG-копії статичних: {'так' if p['png'] else 'ні'}\n"
                f"Великі архіви діляться на частини до {X.PART_BYTES // 1048576} МБ.")
        rows = [
            [("📥 Завантажити пакети", self.cb("in")), ("🔎 Про пакет", self.cb("infoask"))],
            [(("📦 Один архів" if p["mode"] == "one" else "🗂 Окремо"), self.cb("mode")),
             (("🖼 PNG: так" if p["png"] else "🖼 PNG: ні"), self.cb("png"))],
            [("🧩 Вибрати окремі стікери", self.cb("selask"))],
            [("🕘 Історія", self.cb("hist")), ("👁 Стеження", self.cb("trk"))],
            [("ℹ️ Як це працює", self.cb("help")), ("« Плагіни", "m:pl")],
        ]
        return text, rows

    def hist_screen(self, chat):
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM history WHERE chat=? ORDER BY id DESC LIMIT 10", (chat,))]
        lines = ["🕘 <b>Історія завантажень</b>", ""]
        rows = []
        for r in rs:
            packs = json.loads(r["packs"])
            names = ", ".join(n for _k, n in packs)[:70]
            lines.append(f"• {time.strftime('%d.%m %H:%M', time.localtime(r['at']))} · {len(packs)} пак. · {r['files']} файлів"
                         f" · {mb(r['bytes'])}" + (f" · ⚠️{r['errors']}" if r["errors"] else "") + f"\n  {esc(names)}")
            rows.append([(f"🔁 Повторити: {names[:28]}", self.cb("again", r["id"]))])
        if not rs:
            lines.append("Поки порожньо.")
        rows.append([("« Стікери", self.cb())])
        return "\n".join(lines)[:4000], rows

    def trk_screen(self, chat):
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM tracked WHERE chat=? ORDER BY id", (chat,))]
        lines = ["👁 <b>Стеження за пакетами</b>", "",
                 f"Раз на {CHECK_EVERY // 3600} год бот перевіряє пакети й пише, якщо автор додав або прибрав стікери.", ""]
        rows = []
        for r in rs:
            ch = time.strftime("%d.%m", time.localtime(r["changed"])) if r["changed"] else "—"
            lines.append(f"• {esc(r['title'])} · {r['count']} шт · зміна: {ch}")
            rows.append([(f"📦 {r['title'][:24]}", self.cb("one", r["id"])), ("🗑", self.cb("untrk", r["id"]))])
        if not rs:
            lines.append("Поки жодного. Додайте з «🔎 Про пакет» або надіславши стікер.")
        rows.append([("➕ Стежити за пакетом", self.cb("trkask")), ("🔄 Перевірити пакети", self.cb("chk"))])
        rows.append([("« Стікери", self.cb())])
        return "\n".join(lines)[:4000], rows

    def help_screen(self):
        text = ("ℹ️ <b>Стікери: як це працює</b>\n\n"
                "• Працює через офіційний Telegram Bot API (getStickerSet + getFile) — без входу у ваш акаунт.\n"
                "• Формати оригінальні: .webp — статичні, .tgs — анімовані (Lottie), .webm — відео-стікери.\n"
                "• «📦 Один архів» — усі пакети в одному ZIP, кожен у своїй теці, однакові файли між пакетами збережено один раз "
                "(у manifest.json видно, що на що посилається). «🗂 Окремо» — свій ZIP на кожен пакет, повтори не прибираються.\n"
                f"• Файл понад ~20 МБ Bot API не віддає; архів понад {X.PART_BYTES // 1048576} МБ ділиться на частини — кожна відкривається окремо.\n"
                "• PNG-копії робляться лише для статичних .webp; анімацію в GIF не конвертуємо (див. «Розвиток»).\n"
                "• 🧩 Вибрати окремі — архів-чернетка, з якого кнопками прибираєте зайве, подаєте й затверджуєте.\n"
                "• Видалені, приватні чи недоступні пакети не завантажаться — помилка буде у звіті.\n\n"
                "Команди: /stickerpack посилання · /stickers посилання (вибірковий режим).")
        return text, [[("« Стікери", self.cb())]]

    def info_text(self, info):
        tp = {"regular": "стікери", "custom_emoji": "кастомні емодзі", "mask": "маски"}.get(info["type"], info["type"])
        return (f"🔎 <b>{esc(info['title'])}</b>\n<code>{esc(info['name'])}</code> · {tp}\n\n"
                f"Усього: {info['count']} · статичних {info['static']} · анімованих {info['animated']} · відео {info['video']}")

    # ================================================================ дії
    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else None
        if a == "":
            return None, self.home(chat)
        if a == "in":
            self.ask(chat, "links")
            return None, ("📥 <b>Надішліть посилання на пакети</b>\n\nМожна кілька в одному повідомленні.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "infoask":
            self.ask(chat, "info")
            return None, ("🔎 Надішліть посилання на пакет (або сам стікер — тоді просто перешліть його боту).", [[("✖️ Скасувати", "m:cancel")]])
        if a == "trkask":
            self.ask(chat, "track")
            return None, ("👁 Надішліть посилання на пакети, за якими стежити.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "selask":
            self.ask(chat, "select")
            return None, ("🧩 Надішліть посилання на пакети — прийде архів-чернетка з кнопками вибору.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "mode":
            p = self.prefs(chat)
            self.set_pref(chat, mode="each" if p["mode"] == "one" else "one")
            return None, self.home(chat)
        if a == "png":
            self.set_pref(chat, png=not self.prefs(chat)["png"])
            return None, self.home(chat)
        if a == "hist":
            return None, self.hist_screen(chat)
        if a == "trk":
            return None, self.trk_screen(chat)
        if a == "help":
            return None, self.help_screen()
        if a == "go" and x in self.pending:
            owner, text = self.pending.pop(x)
            if owner != chat:
                return "Це завдання іншого користувача", None
            return self._start(chat, text)
        if a == "go":
            return "Посилання застаріли — надішліть ще раз", None
        if a == "again":
            with closing(self.db()) as con:
                r = con.execute("SELECT packs FROM history WHERE id=? AND chat=?", (int(x), chat)).fetchone()
            if not r:
                return "Не знайдено", self.hist_screen(chat)
            text = " ".join(f"t.me/{k}/{n}" for k, n in json.loads(r["packs"]))
            return self._start(chat, text)
        if a == "one":
            with closing(self.db()) as con:
                r = con.execute("SELECT kind, name FROM tracked WHERE id=? AND chat=?", (int(x), chat)).fetchone()
            if not r:
                return "Не знайдено", self.trk_screen(chat)
            return self._start(chat, f"t.me/{r['kind']}/{r['name']}")
        if a == "untrk":
            with closing(self.db()) as con, con:
                con.execute("DELETE FROM tracked WHERE id=? AND chat=?", (int(x), chat))
            return "Більше не стежу", self.trk_screen(chat)
        if a == "chk":
            n = await self.check_tracked(chat)
            return (f"Змін: {n}" if n else "Змін немає"), self.trk_screen(chat)
        if a == "trk1" and x in self.pending:
            owner, text = self.pending[x]
            added = await self.track(chat, X.parse_links(text))
            return (f"👁 Стежу: {added}" if added else "Вже під стеженням"), self.trk_screen(chat)
        if a == "inf" and x in self.pending:
            owner, text = self.pending[x]
            packs = X.parse_links(text)
            return None, await self._info_screen(chat, packs[0][1] if packs else "", packs[0][0] if packs else "addstickers")
        return None, self.home(chat)

    def is_view(self, args):
        return not args or args[0] in self.VIEWS

    async def _info_screen(self, chat, name, kind):
        try:
            info = await X.pack_info(self.host.token, name)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Не вдалося отримати пакет «{esc(name)}»: {esc(exc)}", [[("« Стікери", self.cb())]]
        t = self._token(chat, f"t.me/{kind}/{name}")
        return self.info_text(info), [[("📦 Завантажити", self.cb("go", t)), ("👁 Стежити", self.cb("trk1", t))], [("« Стікери", self.cb())]]

    async def on_text(self, update, ctx, chat, key, data, text):
        packs = X.parse_links(text)
        if not packs:
            return "Не знайшов посилань на пакети", (
                "⚠️ Потрібні посилання виду <code>https://t.me/addstickers/НАЗВА</code> або <code>https://t.me/addemoji/НАЗВА</code>.",
                [[("📥 Ще раз", self.cb("in")), ("« Стікери", self.cb())]])
        if key == "links":
            return self._start(chat, text)
        if key == "info":
            return None, await self._info_screen(chat, packs[0][1], packs[0][0])
        if key == "track":
            n = await self.track(chat, packs)
            return f"👁 Додано: {n}", self.trk_screen(chat)
        if key == "select":
            fn = self.host.extra.get("sticker_select")
            if not fn:
                return "Вибірковий режим недоступний", self.home(chat)
            self._bg(fn(chat, text))
            return "🧩 Готую чернетку…", ("🧩 Готую архів-чернетку — прийде окремим повідомленням із кнопками вибору.", [[("« Стікери", self.cb())]])
        return None, self.home(chat)

    async def intercept(self, update, ctx):
        """Повідомлення лише з посиланнями на пакети — пропонуємо завантажити, а не віддаємо ШІ."""
        msg = update.message
        if not msg or msg.chat.type != "private" or not msg.text or not X.only_links(msg.text):
            return False
        if self.host.extra.get("awaiting", lambda c: False)(msg.chat_id):
            return False
        packs = X.parse_links(msg.text)
        t = self._token(msg.chat_id, msg.text)
        names = ", ".join(n for _k, n in packs[:5]) + ("…" if len(packs) > 5 else "")
        await self.host.notify(msg.chat_id, f"🧷 Бачу {len(packs)} пакет(и): {esc(names)}",
                               [[(f"📦 Завантажити · {len(packs)}", self.cb("go", t)), ("🔎 Про пакет", self.cb("inf", t))],
                                [("👁 Стежити", self.cb("trk1", t))]])
        return True

    async def on_sticker(self, update, ctx):
        msg = update.message
        st = msg.sticker if msg else None
        if not st or not st.set_name or msg.chat.type != "private":
            return False
        kind = "addemoji" if st.type == "custom_emoji" else "addstickers"
        t = self._token(msg.chat_id, f"t.me/{kind}/{st.set_name}")
        await self.host.notify(msg.chat_id, f"🧷 Стікер із пакета <code>{esc(st.set_name)}</code>",
                               [[("📦 Завантажити пакет", self.cb("go", t)), ("🔎 Про пакет", self.cb("inf", t))],
                                [("👁 Стежити за оновленнями", self.cb("trk1", t))]])
        return True

    async def cmd_pack(self, update, ctx):
        text = (update.message.text or "").partition(" ")[2]
        if not X.parse_links(text):
            t, rows = self.home(update.message.chat_id)
            return await self.host.notify(update.message.chat_id, t, rows)
        toast, screen = self._start(update.message.chat_id, text)
        await update.message.reply_text(toast)

    # ================================================================ завантаження
    def _bg(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.__dict__.setdefault("_jobs", set()).add(t)
        t.add_done_callback(self._jobs.discard)

    def _start(self, chat, text):
        if chat in self.busy:
            return "⏳ Попереднє завантаження ще триває", None
        packs = X.parse_links(text)
        if not packs:
            return "Не знайшов посилань", None
        self.busy.add(chat)
        self._bg(self._run(chat, packs))
        return f"📦 Почав: {len(packs)} пак.", ("📦 <b>Завантажую</b> — прогрес і архів прийдуть окремими повідомленнями.", [[("« Стікери", self.cb())]])

    async def _run(self, chat, packs):
        bot = self.host.bot()
        p = self.prefs(chat)
        status = await bot.send_message(chat, f"⏳ Готую {len(packs)} пакет(и)…") if bot else None
        last = [0.0]

        async def progress(pn, total, title, done, count):
            if status and time.time() - last[0] > 2.5:
                last[0] = time.time()
                bar = "▰" * round(done / max(count, 1) * 10) + "▱" * (10 - round(done / max(count, 1) * 10))
                try:
                    await status.edit_text(f"⏳ Пакет {pn}/{total}: {title[:40]}\n{bar} {done}/{count}")
                except Exception:  # noqa: BLE001
                    pass

        folder = Path(tempfile.mkdtemp(prefix="orion_stk_"))
        try:
            paths, rep = await X.export(self.host.token, packs, folder, p["mode"], p["png"], progress)
            for i, path in enumerate(paths, 1):
                cap = f"🧷 {path.name}" + (f" · частина {i}/{len(paths)}" if len(paths) > 1 else "")
                with path.open("rb") as fh:
                    await bot.send_document(chat, fh, filename=path.name, caption=cap)
            lines = [f"✅ Готово: {rep['files']} файлів · {mb(rep['bytes'])} · архівів {len(paths)}"]
            if rep["duplicates"]:
                lines.append(f"♻️ Повторів прибрано: {rep['duplicates']}")
            if rep["png"]:
                lines.append(f"🖼 PNG-копій: {rep['png']}")
            for pk in rep["packs"]:
                err = f" · ⚠️ {len(pk['errors'])} помилок" if pk["errors"] else ""
                lines.append(f"• {pk['title']}: {pk['saved']}/{pk['count']}{err}")
            for e in rep["errors"]:
                lines.append(f"⚠️ {e['pack']}: {e['error'][:150]}")
            await self.host.notify(chat, esc("\n".join(lines))[:4000], [[("🕘 Історія", self.cb("hist")), ("🧷 Стікери", self.cb())]])
            nerr = len(rep["errors"]) + sum(len(pk["errors"]) for pk in rep["packs"])
            with closing(self.db()) as con, con:
                con.execute("INSERT INTO history(chat,at,packs,mode,files,dups,parts,bytes,errors) VALUES(?,?,?,?,?,?,?,?,?)",
                            (chat, time.time(), json.dumps(packs), p["mode"], rep["files"], rep["duplicates"], len(paths), rep["bytes"], nerr))
        except Exception as exc:  # noqa: BLE001
            log.warning("sticker export failed: %s", exc)
            await self.host.notify(chat, f"⚠️ Не вдалося: {esc(exc)[:500]}", [[("🧷 Стікери", self.cb())]])
        finally:
            self.busy.discard(chat)
            shutil.rmtree(folder, ignore_errors=True)
            if status:
                try:
                    await status.delete()
                except Exception:  # noqa: BLE001
                    pass

    # ================================================================ стеження
    async def track(self, chat, packs):
        added = 0
        for kind, name in packs:
            try:
                info = await X.pack_info(self.host.token, name)
            except Exception:  # noqa: BLE001
                continue
            with closing(self.db()) as con, con:
                cur = con.execute("INSERT OR IGNORE INTO tracked(chat,name,kind,title,signature,ids,count,checked,changed) VALUES(?,?,?,?,?,?,?,?,0)",
                                  (chat, name, kind, info["title"], info["signature"], json.dumps(info["ids"]), info["count"], time.time()))
                added += cur.rowcount
        return added

    async def check_tracked(self, chat=None):
        with closing(self.db()) as con:
            q = "SELECT * FROM tracked" + (" WHERE chat=?" if chat else "")
            rs = [dict(r) for r in con.execute(q, (chat,) if chat else ())]
        changed = 0
        for r in rs:
            try:
                info = await X.pack_info(self.host.token, r["name"])
            except Exception:  # noqa: BLE001
                continue
            now = time.time()
            if info["signature"] != r["signature"]:
                old = set(json.loads(r["ids"] or "[]"))
                new = set(info["ids"])
                plus, minus = len(new - old), len(old - new)
                changed += 1
                t = self._token(r["chat"], f"t.me/{r['kind']}/{r['name']}")
                await self.host.notify(r["chat"], f"👁 Пакет «{esc(info['title'])}» оновився: +{plus} / −{minus} · тепер {info['count']}",
                                       [[("📦 Завантажити", self.cb("go", t)), ("👁 Стеження", self.cb("trk"))]])
                with closing(self.db()) as con, con:
                    con.execute("UPDATE tracked SET signature=?, ids=?, count=?, title=?, checked=?, changed=? WHERE id=?",
                                (info["signature"], json.dumps(info["ids"]), info["count"], info["title"], now, now, r["id"]))
            else:
                with closing(self.db()) as con, con:
                    con.execute("UPDATE tracked SET checked=? WHERE id=?", (now, r["id"]))
            await asyncio.sleep(1)  # м'яко до лімітів Telegram
        return changed

    async def start(self):
        await asyncio.sleep(60)
        while True:
            try:
                if self.on() and self.host.token:
                    await self.check_tracked()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("sticker tracking loop")
            await asyncio.sleep(CHECK_EVERY)
