"""🔌 Плагіни OrionAI — підключувані модулі, що живуть усередині ШІ-бота.

Кожен плагін — окремий пакет у plugins/<id>/ з класом Plugin. Плагін:
- має власні екрани в головному меню (кнопки з даними «m:pl:<id>:…»), які отримують
  ту саму навігацію «◀️ Назад · 🏠 Додому · Вперед ▶️», що й решта бота;
- просить у користувача текст через host.ask_input(...) — відповідь повертається в on_text;
- може перехоплювати повідомлення (intercept), дописи каналів (on_channel_post) і стікери (on_sticker);
- може мати фонові завдання (start) і HTTP-адреси (routes) на тому ж сервері, що й конектор MCP;
- користується моделями Оріона через host.ask_ai — з тим самим «Авто»-перемиканням, чергою пріоритетів і лімітами.

Модуль не імпортує bot.py: усе, що плагінам потрібно від бота, передається через Host.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("orion.plugins")

# Порядок — це порядок кнопок у меню «🔌 Плагіни».
BUILTIN = ["hub", "newsroom", "editorial", "stickers", "inbox", "media", "weather"]


def esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Host:
    """Усе, що бот дає плагінам. Заповнюється в bot.py."""
    data_dir: Path
    token: str = ""
    admins: Callable[[], list] = lambda: []
    is_admin: Callable[[int], bool] = lambda uid: False
    is_allowed: Callable[[int], bool] = lambda uid: False
    ask_ai: Callable[..., Awaitable[tuple]] | None = None      # (prompt, system=None, label=None, image=None) -> (text, model)
    browse: Callable[[str], Awaitable[tuple]] | None = None     # url -> (text, error)
    search: Callable[[str], Awaitable[tuple]] | None = None     # query -> (text, error)
    ask_input: Callable[..., None] = lambda chat, key, data=None, pid=None: None
    cancel_input: Callable[[int], None] = lambda chat: None
    bot: Callable[[], Any] = lambda: None                      # -> telegram.Bot або None
    tz: Any = None
    extra: dict = field(default_factory=dict)

    async def notify(self, chat_id, text, rows=None):
        bot = self.bot()
        if not bot:
            return None
        markup = None
        if rows and self.extra.get("improve_btn", True):
            rows = [list(r) for r in rows]
            rows[-1].append(("💡", "imp:s"))
        if rows:
            kb = self.extra.get("kb")
            if kb:
                markup = kb(rows)
            else:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) if not d.startswith("url:")
                                                else InlineKeyboardButton(t, url=d[4:]) for t, d in row] for row in rows])
        from footnotes import html_split
        import notify as _nt
        quiet = _nt.apply(chat_id, {}, True)  # 🔕 тихий режим людини для цього типу (ранкове зведення — тихо)
        parts = html_split(text or "", 3900)  # довге (ранкове зведення тощо) — кількома повідомленнями, теги не рвуться
        sent = None
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            try:
                sent = await bot.send_message(chat_id, part, parse_mode="HTML", reply_markup=markup if last else None,
                                              disable_web_page_preview=True, **quiet)
            except Exception:  # noqa: BLE001
                log.exception("plugin notify failed — надсилаю без оформлення")
                try:  # запасний шлях: той самий текст без розмітки, щоб повідомлення не пропало
                    import html as _h
                    import re as _re
                    plain = _h.unescape(_re.sub(r"<[^>]+>", "", part))[:4096]
                    sent = await bot.send_message(chat_id, plain, reply_markup=markup if last else None,
                                                  disable_web_page_preview=True, **quiet)
                except Exception:  # noqa: BLE001
                    log.exception("plugin notify failed")
        return sent

    async def notify_admins(self, text, rows=None):
        import notify
        k = notify.KIND.get()
        with notify.kind("system" if k == "other" else k):
            for uid in dict.fromkeys(self.admins()):  # той самий ID двічі — одне повідомлення
                if notify.wanted(uid, notify.KIND.get()):  # ✖️ людина вимкнула цей тип
                    await self.notify(uid, text, rows)


class Plugin:
    """Базовий клас. Перевизначайте лише те, що потрібно."""
    id = "base"
    icon = "🔌"
    title = "Плагін"
    version = "1.0"
    summary = ""
    admin_only = False
    commands: list = []          # [(команда, async fn(update, ctx), опис)]

    def __init__(self, host: Host):
        self.host = host
        self.dir = Path(host.data_dir) / "plugins" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)

    def on(self) -> bool:
        """Чи увімкнений плагін зараз (фонові цикли перевіряють це на кожному кроці)."""
        return (self.host.extra.get("enabled", lambda pid: True)(self.id)
                and self.host.extra.get("node_active", lambda: True)())

    def ask(self, chat, key, data=None):
        """Попросити в користувача текст; відповідь прийде в on_text(…, key, data, text)."""
        self.host.ask_input(chat, key, data, pid=self.id)

    # --- меню
    def cb(self, *parts) -> str:
        data = ":".join(["m", "pl", self.id] + [str(p) for p in parts if p is not None and p != ""])
        if len(data.encode()) > 64:
            raise ValueError("callback_data > 64 bytes: " + data)
        return data

    def is_view(self, args: list) -> bool:
        return not args or args[0] in self.VIEWS

    VIEWS: set = set()

    async def route(self, update, ctx, chat: int, args: list):
        """-> (toast | None, (текст, рядки) | None)"""
        return None, ("🔌 " + esc(self.title), [])

    async def on_text(self, update, ctx, chat: int, key: str, data, text: str):
        return None

    async def intercept(self, update, ctx) -> bool:
        return False

    async def on_channel_post(self, update, ctx):
        return None

    async def on_sticker(self, update, ctx) -> bool:
        return False

    async def on_document(self, update, ctx) -> bool:
        return False

    async def on_private(self, update, ctx) -> bool:
        """Будь-яке приватне повідомлення ДО перевірки доступу (і від незнайомих). True — оброблено, далі не йде."""
        return False

    async def on_file(self, update, ctx) -> bool:
        """Будь-який файл/фото/відео/аудіо в приватному чаті від учасника — ДО звичайної обробки. True — оброблено."""
        return False

    async def start(self):
        return None

    def routes(self, secret: str) -> list:
        return []

    def status_line(self) -> str:
        return ""


class Registry:
    def __init__(self, host: Host):
        self.host = host
        self.state_path = Path(host.data_dir) / "plugins" / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}
        self._bg: set = set()
        host.extra["enabled"] = self.enabled
        host.extra["plugin"] = self.get
        off = {x.strip() for x in os.environ.get("ORION_PLUGINS_OFF", "").split(",") if x.strip()}  # напр. hub — якщо головний бот окремий
        for pid in BUILTIN:
            if pid in off:
                continue
            try:
                mod = importlib.import_module(f"plugins.{pid}")
                inst = mod.Plugin(host)
                self.plugins[inst.id] = inst
            except Exception as exc:  # noqa: BLE001 — один зламаний плагін не валить бота
                log.exception("plugin %s failed to load", pid)
                self.errors[pid] = f"{type(exc).__name__}: {exc}"

    # --- стан увімкнення
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def enabled(self, pid: str) -> bool:
        return self._state().get(pid, {}).get("enabled", True)

    def set_enabled(self, pid: str, on: bool):
        st = self._state()
        st.setdefault(pid, {})["enabled"] = bool(on)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    def active(self):
        return [p for pid, p in self.plugins.items() if self.enabled(pid)]

    def get(self, pid):
        p = self.plugins.get(pid)
        return p if p and self.enabled(pid) else None

    def visible(self, uid) -> list:
        return [p for p in self.active() if not p.admin_only or self.host.is_admin(uid)]

    # --- меню «🔌 Плагіни»
    def is_view(self, args: list) -> bool:
        if not args or args[0] in ("home", "info", "mgr"):
            return True
        p = self.plugins.get(args[0])
        return bool(p) and p.is_view(args[1:])

    def banner(self, chat):
        """Закріплені плагінами рядки над головним меню Оріона (напр. 📌 «де просідаємо» зі звіту новинарні)."""
        out = []
        for pid, p in self.plugins.items():
            fn = getattr(p, "banner", None)
            if fn and self.enabled(pid):
                try:
                    b = fn(chat)
                except Exception:  # noqa: BLE001
                    b = ""
                if b:
                    out.append(b)
        return "\n\n".join(out)

    def home(self, chat):
        lines = ["🔌 <b>Плагіни</b>", "", "Підключувані модулі всередині Оріона. Кожен працює на тих самих моделях ШІ, "
                 "резервних копіях і доступах, що й увесь бот.", ""]
        rows = []
        for pid, p in self.plugins.items():
            if p.admin_only and not self.host.is_admin(chat):
                continue
            on = self.enabled(pid)
            st = p.status_line() if on else "вимкнено"
            lines.append(f"{'✅' if on else '⏸'} {p.icon} <b>{esc(p.title)}</b> · v{esc(p.version)}" + (f"\n   {esc(st)}" if st else ""))
            rows.append([(f"{p.icon} {p.title}", f"m:pl:{pid}")] if on else [(f"⏸ {p.title} (вимкнено)", f"m:pl:info:{pid}")])
        for pid, err in self.errors.items():
            lines.append(f"⚠️ {esc(pid)}: не завантажився — {esc(err)[:200]}")
        hidden = [p.title for p in self.plugins.values() if p.admin_only and p.id != "hub"]
        if hidden and not self.host.is_admin(chat):
            lines.append(f"\n🔒 {esc(', '.join(hidden))} — лише для власника бота. Якщо власник — ви, додайте свій ID "
                         f"<code>{chat}</code> у змінну ALLOWED_USERS у Railway (через кому, якщо там уже є інші).")
        tail = [("ℹ️ Як це влаштовано", "m:pl:info")]
        if self.host.is_admin(chat):
            tail.insert(0, ("🔀 Увімкнути / вимкнути", "m:pl:mgr"))
        rows.append(tail)
        return "\n".join(lines), rows

    def manager(self, chat):
        rows = []
        for pid, p in self.plugins.items():
            on = self.enabled(pid)
            rows.append([(f"{'✅' if on else '⏸'} {p.title} — {'вимкнути' if on else 'увімкнути'}", f"m:pl:tog:{pid}")])
        rows.append([("« Плагіни", "m:pl")])
        return ("🔀 <b>Увімкнення плагінів</b>\n\nВимкнений плагін зникає з меню, не перехоплює повідомлень і не запускає "
                "фонових перевірок. Його дані зберігаються."), rows

    def info(self, pid=None):
        if pid and pid in self.plugins:
            p = self.plugins[pid]
            text = f"{p.icon} <b>{esc(p.title)}</b> · v{esc(p.version)}\n\n{p.summary}"
            rows = [[("▶️ Увімкнути", f"m:pl:tog:{pid}")] if not self.enabled(pid) else [(f"{p.icon} Відкрити", f"m:pl:{pid}")],
                    [("« Плагіни", "m:pl")]]
            return text, rows
        text = ("ℹ️ <b>Плагіни Оріона</b>\n\n"
                "• Плагін — окремий модуль у теці <code>plugins/</code>. Помилка в одному плагіні не зупиняє бота.\n"
                "• Вимкнений плагін зникає з меню, не перехоплює повідомлень і не запускає фонових перевірок; "
                "дані зберігаються й повертаються після увімкнення.\n"
                "• Дані плагінів лежать у <code>data/plugins/&lt;назва&gt;/</code>. Їхні бази додаються в резервні копії ZIP і tar.gz "
                "окремими файлами (<code>data/plugins/…</code>); відновлення з меню повертає основну базу, бази плагінів кладуться на місце вручну.\n"
                "• Кнопки плагінів можна переносити, перейменовувати й ховати в «🧭 Меню й кнопки», як і будь-який розділ.\n\n"
                "Команди: /news — новинарня, /digest 2000 — зведення, /stickers — стікери.")
        return text, [[("« Плагіни", "m:pl")]]

    async def route(self, update, ctx, chat, args):
        if not args or args[0] == "home":
            return None, self.home(chat)
        if args[0] == "info":
            return None, self.info(args[1] if len(args) > 1 else None)
        if args[0] == "mgr":
            return None, self.manager(chat) if self.host.is_admin(chat) else self.home(chat)
        if args[0] == "tog" and len(args) > 1:
            if not self.host.is_admin(chat):
                return "Лише власник може вмикати плагіни", self.home(chat)
            pid = args[1]
            self.set_enabled(pid, not self.enabled(pid))
            if self.enabled(pid) and pid in self.plugins:
                self._spawn(self.plugins[pid])
            return ("Увімкнено" if self.enabled(pid) else "Вимкнено"), self.manager(chat)
        pid = args[0]
        p = self.get(pid)
        if not p:
            return "Плагін вимкнено або не знайдено", self.home(chat)
        if p.admin_only and not self.host.is_admin(chat):
            return "Лише для власника бота", self.home(chat)
        try:
            return await p.route(update, ctx, chat, args[1:])
        except Exception as exc:  # noqa: BLE001
            log.exception("plugin %s route failed", pid)
            return f"Помилка: {type(exc).__name__}", (f"⚠️ <b>{esc(p.title)}</b>: помилка — {esc(exc)[:300]}", [[("« Плагіни", "m:pl")]])

    async def on_text(self, update, ctx, chat, data, text):
        pid, key, payload = data
        p = self.get(pid)
        if not p:
            return None
        return await p.on_text(update, ctx, chat, key, payload, text)

    async def intercept(self, update, ctx) -> bool:
        for p in self.active():
            if p.admin_only and not self.host.is_admin(update.effective_user.id):
                continue
            try:
                if await p.intercept(update, ctx):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("plugin %s intercept failed", p.id)
        return False

    async def on_channel_post(self, update, ctx):
        for p in self.active():
            try:
                await p.on_channel_post(update, ctx)
            except Exception:  # noqa: BLE001
                log.exception("plugin %s channel post failed", p.id)

    async def on_document(self, update, ctx):
        for p in self.active():
            if p.admin_only and not self.host.is_admin(update.effective_user.id):
                continue
            try:
                if await p.on_document(update, ctx):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("plugin %s document failed", p.id)
        return False

    async def on_file(self, update, ctx):
        for p in self.active():
            if p.admin_only and not self.host.is_admin(update.effective_user.id):
                continue
            try:
                if await p.on_file(update, ctx):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("plugin %s file failed", p.id)
        return False

    async def on_private(self, update, ctx):
        for p in self.active():
            try:
                if await p.on_private(update, ctx):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("plugin %s private failed", p.id)
        return False

    async def on_sticker(self, update, ctx):
        for p in self.active():
            try:
                if await p.on_sticker(update, ctx):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("plugin %s sticker failed", p.id)
        return False

    def commands(self):
        out = []
        for p in self.plugins.values():
            for name, fn, desc in p.commands:
                out.append((name, self._guard(p, fn), desc))
        return out

    def _guard(self, p, fn):
        async def wrapped(update, ctx):
            if not self.enabled(p.id):
                return await update.effective_message.reply_text(f"{p.icon} Плагін «{p.title}» вимкнено. Увімкнути: ☰ Меню → 🔌 Плагіни.")
            if p.admin_only and not self.host.is_admin(update.effective_user.id):
                return await update.effective_message.reply_text("Лише для власника бота.")
            return await fn(update, ctx)
        return wrapped

    def _spawn(self, p):
        async def run():
            try:
                await p.start()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("plugin %s background failed", p.id)
        if getattr(p, "_started", False):
            return
        p._started = True
        t = asyncio.get_running_loop().create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def start(self):
        for p in self.plugins.values():
            self._spawn(p)  # кожен плагін сам перевіряє, чи він увімкнений, на кожному кроці

    def routes(self, secret):
        out = []
        for p in self.plugins.values():
            try:
                out += p.routes(secret)
            except Exception:  # noqa: BLE001
                log.exception("plugin %s routes failed", p.id)
        return out
