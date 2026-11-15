"""🧭 Мої боти — головний (материнський) бот усередині Оріона.

Увесь manager.py працює тут без переписування: його екрани виконуються як є, а дані кнопок
«m:…» перекладаються в «m:pl:hub:…» (і назад), щоб жити поруч із меню Оріона в одному Telegram-боті.
HTTP-адреси для дочірніх ботів (/hub/<секрет>/in, /hb) лишаються тими самими. Оріон водночас може бути
дочірнім для чужого головного бота (hubclient: HUB_URL / HUB_KEY).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import os
import secrets
import sys
import types
from pathlib import Path

from plugins import Plugin as Base, esc

log = logging.getLogger("orion.plugins.hub")
PFX = "m:pl:hub"


class Plugin(Base):
    id = "hub"
    icon = "🧭"
    title = "Мої боти"
    version = "1.0"
    admin_only = True
    summary = ("Оріон — головний бот для всіх ваших ботів (Telegram, WhatsApp, Instagram, будь-які через HTTP): стан і сповіщення про падіння, "
               "розклад перевірок, вхідні від ботів, пересилання між ботами за правилами, захист від зациклень. І водночас Оріон може бути "
               "дочірнім для іншого головного бота.")

    def __init__(self, host):
        super().__init__(host)
        self.tok: dict[str, str] = {}
        self.M = None
        self.error = ""
        try:
            self.M = self._load()
        except Exception as exc:  # noqa: BLE001
            log.exception("hub: manager import failed")
            self.error = f"{type(exc).__name__}: {exc}"

    # ================================================================ завантаження manager.py у вбудованому режимі
    def _load(self):
        data_dir = Path(self.host.data_dir)
        os.environ.setdefault("MANAGER_DB_PATH", str(data_dir / "manager.db"))
        if not os.environ.get("MANAGER_SECRET"):
            base = os.environ.get("MCP_SECRET", "") or secrets.token_hex(16)
            os.environ["MANAGER_SECRET"] = "hub-" + hashlib.sha256(base.encode()).hexdigest()[:40]  # стабільний між перезапусками
        if not os.environ.get("MANAGER_PUBLIC_URL") and os.environ.get("PUBLIC_URL", "").startswith("https://"):
            os.environ["MANAGER_PUBLIC_URL"] = os.environ["PUBLIC_URL"].rstrip("/") + "/hub"
        saved = os.environ.pop("MANAGER_TOKEN", None)  # без окремого Telegram-бота: говоримо через Оріон
        try:
            M = importlib.import_module("manager")
        finally:
            if saved is not None:
                os.environ["MANAGER_TOKEN"] = saved
        M.init_db()
        M.app = types.SimpleNamespace(bot=_Proxy(self, lambda: self.host.extra.get("real_bot", lambda: None)()))
        M.EMBEDDED = True
        return M

    # ================================================================ переклад кнопок
    def tr(self, d):
        if not d or d.startswith(("url:", "copy:", "app:")):
            return d
        if d.startswith("m:nav"):
            return None
        if d == "m:home":
            return PFX
        if d.startswith("m:cm"):
            return d  # 🏘 Спільноти — одні на весь Оріон
        if d.startswith("m:"):
            nd = PFX + ":" + d[2:]
            if len(nd.encode()) <= 64:
                return nd
        t = secrets.token_hex(4)
        self.tok[t] = d
        if len(self.tok) > 3000:
            self.tok.pop(next(iter(self.tok)))
        return f"{PFX}:~:{t}"

    def tr_rows(self, rows):
        out = []
        for row in rows or []:
            # навігаційний ряд головного бота не потрібен: «Назад · Додому · Вперед» додає сам Оріон
            r = [(t, self.tr(d)) for t, d in row if t not in ("◀️ Назад", "🏠 Додому", "Вперед ▶️")]
            r = [(t, d) for t, d in r if d]
            if r:
                out.append(r)
        return out

    def tr_markup(self, markup):
        from telegram import InlineKeyboardMarkup
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup
        import miniapp
        kb = self.host.extra.get("kb")
        rows = self.tr_rows(miniapp._rows(markup))
        return kb(rows) if kb else markup

    def back(self, args):
        if not args:
            return "m:home"
        if args[0] == "~":
            return self.tok.get(args[1] if len(args) > 1 else "", "")
        return "m:" + ":".join(args)

    # ================================================================ екрани
    def is_view(self, args):
        if not self.M:
            return True
        d = self.back(args)
        return bool(d) and self.M.is_view(d)

    def status_line(self):
        if not self.M:
            return "не завантажено: " + self.error[:60]
        try:
            bots = self.M.bots_list() if hasattr(self.M, "bots_list") else []
            return f"ботів {len(bots)}"
        except Exception:  # noqa: BLE001
            return ""

    async def _capture(self, chat, data=None, msg=None):
        import miniapp
        sink = miniapp._Sink()
        real = self.host.extra.get("real_bot", lambda: None)()
        proxy = _Proxy(self, lambda: real)
        if msg is not None:
            upd = miniapp._Update(chat, sink, proxy, text=msg.text or "")
            for k in ("caption", "forward_origin", "document", "photo", "reply_to_message", "entities", "caption_entities", "message_id"):
                setattr(upd.message, k, getattr(msg, k, None))
            upd.effective_user = msg.from_user
            await self.M.on_text(upd, miniapp._Ctx(proxy))
        else:
            upd = miniapp._Update(chat, sink, proxy, data=data)
            await self.M.open_data(upd, miniapp._Ctx(proxy), data, edit=True, record=False)
        return sink

    def parent_screen(self):
        hub = self.host.extra.get("hubclient")
        ready = bool(hub and hub.ready)
        text = ("⬆️ <b>Мій головний бот</b>\n\n"
                "Оріон сам — головний бот для ваших ботів (🧭 Мої боти). Водночас він може бути <b>дочірнім</b> для іншого головного бота: "
                "звітувати йому про свій стан, надсилати події й приймати команди.\n\n"
                + (f"Зараз: 🟢 підключено до {esc(hub.url)}" if ready else "Зараз: ⚪️ не підключено (Оріон працює сам по собі).")
                + "\n\n<b>Як підключити</b>\n1. В іншому головному боті: «🤖 Боти» → «➕ Додати бота» → тип HTTP → «🔌 Підключення».\n"
                "2. Скопіюйте адресу й ключ у .env Оріона: <code>HUB_URL=…</code>, <code>HUB_KEY=…</code>.\n"
                "3. <code>docker compose up -d ai</code> — Оріон почне надсилати сигнал «живий» і події.\n\n"
                "Зв'язок у два боки не зациклюється: кожне повідомлення має лічильник передач (RELAY_MAX_HOPS).")
        return text, [[("« Мої боти", PFX)]]

    async def route(self, update, ctx, chat, args):
        if args and args[0] == "parent":
            return None, self.parent_screen()
        if not self.M:
            return None, (f"🧭 Модуль головного бота не завантажився: {esc(self.error)}", [])
        data = self.back(args)
        if not data:
            return "Кнопка застаріла — відкрийте розділ ще раз", (await self.route(update, ctx, chat, []))[1]
        sink = await self._capture(chat, data=data)
        for n in sink.notes:
            await self.host.notify(chat, esc(n))
        if not sink.screen:
            return sink.toast, None
        text, rows = sink.screen
        rows = self.tr_rows(rows)
        if data == "m:home":
            # розділи, що в окремому боті жили в нижній клавіатурі, тут — першим рядом (нічого не губиться)
            L = self.M.L
            top = [(L.label(chat, i), self.tr(L.target(i))) for i in L.in_place(chat, "kb") if i != "menu"]
            top = [(t, d) for t, d in top if d]
            rows = [top[i:i + 2] for i in range(0, len(top), 2)] + rows
            rows.append([("⬆️ Мій головний бот", f"{PFX}:parent")])
            text = text.replace("Головний бот", "Мої боти", 1) + "\n\n<i>Оріон — головний бот для цих ботів і сам може бути дочірнім.</i>"
        return sink.toast, (text, rows)

    async def on_private(self, update, ctx):
        """Введення тексту для екранів головного бота й переслане від ботів — до 📥 Вхідних."""
        if not self.M or not self.on():
            return False
        msg = update.message
        if not msg or not self.host.is_admin(update.effective_user.id):
            return False
        chat = msg.chat_id
        o = getattr(msg, "forward_origin", None)
        from_bot = o is not None and getattr(getattr(o, "sender_user", None), "is_bot", False)
        if chat not in self.M.AWAIT and not from_bot:
            return False
        if (msg.text or "").startswith("/") and chat in self.M.AWAIT:
            self.M.AWAIT.pop(chat, None)
            return False
        sink = await self._capture(chat, msg=msg)
        for n in sink.notes:
            await self.host.notify(chat, esc(n))
        if sink.screen:
            await self.host.notify(chat, sink.screen[0], self.tr_rows(sink.screen[1]))
        return True

    async def on_channel_post(self, update, ctx):
        """Повідомлення інших ботів у групах (режим bot-to-bot) — у 📥 Вхідні."""
        m = update.message
        if not self.M or not m or not m.from_user or not m.from_user.is_bot:
            return
        await self.M.on_text(update, types.SimpleNamespace(bot=self.M.app.bot, args=[]))

    async def start(self):
        if not self.M:
            return
        while not self.on():
            await asyncio.sleep(30)
        await self.M.monitor_loop()

    def routes(self, secret):
        if not self.M:
            return []
        from starlette.routing import Mount
        return [Mount("/hub", app=self.M.web)]


class _Proxy:
    """Справжній бот Оріона для коду головного бота: кнопки «m:…» перекладаються в «m:pl:hub:…»."""

    def __init__(self, plugin, getter):
        self._p, self._get = plugin, getter

    def __getattr__(self, name):
        real = self._get()
        attr = getattr(real, name)
        if name in ("send_message", "edit_message_text", "send_document", "copy_message", "send_photo", "edit_message_reply_markup"):
            async def wrapped(*a, **kw):
                if "reply_markup" in kw:
                    kw["reply_markup"] = self._p.tr_markup(kw["reply_markup"])
                return await attr(*a, **kw)
            return wrapped
        return attr
