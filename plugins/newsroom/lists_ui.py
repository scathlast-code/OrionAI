"""Екрани й дії 📋 списків джерел ньюзруму (міксин до Plugin)."""
from __future__ import annotations

import io
import time

from plugins import esc
from .collectors import mtproto_configured
from .lists import SORTS, display_ref, link_of, parse_file, parse_text

PAGE = 8
INTERVALS = [(300, "5 хв"), (600, "10 хв"), (1800, "30 хв"), (3600, "1 год"), (10800, "3 год")]
METHODS = {"auto": "🔁 авто", "web": "🌐 вебпревʼю", "mtproto": "📲 акаунт", "bot": "🤖 бот",
           "x": "𝕏", "threads": "🧵 Threads", "ig": "📸 Instagram", "fb": "📘 Facebook", "tumblr": "🌀 Tumblr", "yt": "▶️ YouTube", "rss": "🌐 RSS"}
TYPES = {"channel": "канал", "group": "група", "chat": "чат", "social": "соцмережа"}


def short(s, n=30):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def when(ts):
    if not ts:
        return "ще не перевірявся"
    m = int((time.time() - ts) // 60)
    return "щойно" if m < 1 else f"{m} хв тому" if m < 60 else f"{m // 60} год тому" if m < 1440 else time.strftime("%d.%m %H:%M", time.localtime(ts))


FORMAT_HELP = (
    "❓ <b>Формат списків</b>\n\n"
    "Надішліть файл <b>.txt, .csv, .xlsx або .json</b> (до 5 МБ) або вставте текст. Один рядок — одне джерело:\n"
    "<code>@kanal | Назва | Область | так</code>\n"
    "<code>https://t.me/kanal</code>\n<code>t.me/+запрошення | Закритий чат</code>\n<code>-1001234567890 | Група за id</code>\n\n"
    "• Назва, область і «так/ні» (увімкнено) — необов'язкові. Роздільник: | ; табуляція або кома.\n"
    "• Рядок <code>## Назва списку</code> починає новий список — один файл може містити кілька списків.\n"
    "• У CSV/Excel можна мати заголовки: посилання, назва, область, увімк. Кожен аркуш Excel — окремий список.\n"
    "• Рядки з # — коментарі. Повтори в межах списку пропускаються, наявні джерела оновлюються.\n\n"
    "<b>Хто що читає</b>\n🌐 публічні канали — без акаунта, через вебпревʼю t.me/s;\n"
    "📲 групи, чати, приватні канали й запрошення — потрібен службовий акаунт (TELEGRAM_SESSION);\n"
    "🤖 канали, де бот адміністратор, і групи з ботом — дописи приходять самі.\n\n"
    "Файл, вивантажений кнопкою «📥 Вивантажити», можна відредагувати й завантажити назад.")


class ListsUI:
    # ================================================================ екрани
    def L_home(self, chat):
        ls = self.lists.lists()
        mt = ("🟢 підключено" + (f" як {esc(self.mt.me.first_name)}" if self.mt.me else "")) if self.mt.ready else \
             ("🟡 налаштовано, підключаюсь…" if mtproto_configured() and not self.mt.error else f"⚪️ {esc(self.mt.error or 'не налаштовано')}")
        lines = ["📋 <b>Списки каналів і чатів</b>", "",
                 "Бот сам перевіряє кожне увімкнене джерело зі списків і бере звідти нові дописи в ньюзрум.", "",
                 "🌐 Публічні канали (вебпревʼю): 🟢 працює",
                 f"📲 Службовий акаунт: {mt}",
                 "🤖 Де бот адмін / учасник: дописи приходять самі", ""]
        rows = []
        if not ls:
            lines.append("Списків поки немає. Завантажте файл або створіть список і вставте посилання.")
        for l in ls:
            mark = "🟢" if l["enabled"] else "⚪️"
            auto = {"account": " · авто", "bot": " · авто", "ours": " · 🏠 наші"}.get(l["auto"], "")
            err = f" · ⚠️{l['n_err']}" if l["n_err"] else ""
            lines.append(f"{mark} <b>{esc(l['name'])}</b> — {l['n_on']}/{l['n']} увімк. · узято {l['taken']}{err}{auto}")
            rows.append([(f"{mark} {short(l['name'], 26)} · {l['n']}", self.cb("Lo", l["id"]))])
        rows.append([("➕ Новий список", self.cb("Lnew")), ("📤 Завантажити файл", self.cb("Lup"))])
        rows.append([("📥 Вивантажити все", self.cb("Lexp")), ("🔄 Перевірити все зараз", self.cb("Lnow"))])
        rows.append([("❓ Формат файлу", self.cb("Lhelp")), ("« Джерела", self.cb("H1"))])
        return "\n".join(lines)[:4000], rows

    def L_list(self, lid, offset=0):
        l = self.lists.get_list(lid)
        if not l:
            return "Список не знайдено.", [[("« Списки", self.cb("L"))]]
        es = self.lists.entries(lid, offset, PAGE + 1)
        filt = []
        if l["include"]:
            filt.append("лише з: " + l["include"])
        if l["exclude"]:
            filt.append("без: " + l["exclude"])
        if l["min_len"]:
            filt.append(f"від {l['min_len']} символів")
        iv = dict(INTERVALS).get(l["interval"], f"{l['interval'] // 60} хв")
        head = (f"📋 <b>{esc(l['name'])}</b> · {'🟢 увімкнено' if l['enabled'] else '⚪️ вимкнено'}\n"
                f"Джерел: {l['n']} (увімк. {l['n_on']}) · узято {l['taken']} · перевірка кожні {iv}"
                + (f"\nОбласть за замовчуванням: {esc(l['region'])}" if l["region"] else "")
                + (f"\nФільтр: {esc('; '.join(filt))}" if filt else "")
                + ("\n<i>Службовий список: поповнюється сам.</i>" if l["auto"] else ""))
        lines = [head, ""]
        rows = []
        for e in es[:PAGE]:
            mark = "🟢" if e["enabled"] else "⚪️"
            warn = "⚠️" if e["last_error"] else ""
            name = e["label"] or e["title"] or display_ref(e["kind"], e["ref"])
            lines.append(f"{e['position']}. {mark}{warn} {esc(short(name, 40))} · {esc(display_ref(e['kind'], e['ref']))}"
                         + (f" · {esc(e['region'])}" if e["region"] else "") + f" · {e['taken']}")
            rows.append([(f"{mark}{warn} {e['position']}. {short(name, 30)}", self.cb("Le", e["id"]))])
        if not es:
            lines.append("Порожньо. «➕ Додати» — вставте посилання, або «📤 Імпорт» — файл.")
        nav = []
        if offset:
            nav.append(("⬅️", self.cb("Lo", lid, max(0, offset - PAGE))))
        if len(es) > PAGE:
            nav.append(("➡️", self.cb("Lo", lid, offset + PAGE)))
        if nav:
            rows.append(nav)
        rows.append([("➕ Додати", self.cb("Ladd", lid)), ("📤 Імпорт", self.cb("Lup", lid))])
        rows.append([("🔃 Сортувати", self.cb("Lsort", lid)), ("✂️ Масові дії", self.cb("Lbulk", lid))])
        rows.append([("⚙️ Цей список", self.cb("Ls", lid)), ("🔄 Перевірити список", self.cb("Lnow", lid))])
        rows.append([("« Списки", self.cb("L"))])
        return "\n".join(lines)[:4000], rows

    def L_settings(self, lid):
        l = self.lists.get_list(lid)
        if not l:
            return "Список не знайдено.", [[("« Списки", self.cb("L"))]]
        ivs = [(("● " if l["interval"] == v else "") + t, self.cb("Liv", lid, v)) for v, t in INTERVALS]
        rows = [
            [(("⏸ Вимкнути список" if l["enabled"] else "▶️ Увімкнути список"), self.cb("Ltog", lid)), ("✏️ Назва списку", self.cb("Lren", lid))],
            ivs[:3], ivs[3:],
            [("✅ Лише зі словами", self.cb("Linc", lid)), ("🚫 Без слів", self.cb("Lexc", lid))],
            [("📏 Мін. довжина", self.cb("Lmin", lid)), ("🗺 Область списку", self.cb("Lreg", lid))],
            [("⬆️ Список вище", self.cb("Lmove", lid, "up")), ("⬇️ Список нижче", self.cb("Lmove", lid, "down"))],
            [("🔗 Об'єднати з іншим", self.cb("Lmerge", lid)), ("📥 Вивантажити CSV", self.cb("Lexp", lid))],
            [("🗑 Видалити список", self.cb("Ldelask", lid))],
            [("« Список", self.cb("Lo", lid))],
        ]
        text = (f"⚙️ <b>Список «{esc(l['name'])}»</b>\n\n"
                f"Фільтр «лише зі словами»: {esc(l['include'] or '—')}\nФільтр «без слів»: {esc(l['exclude'] or '—')}\n"
                f"Мінімальна довжина допису: {l['min_len'] or '—'}\nОбласть за замовчуванням: {esc(l['region'] or '—')}\n\n"
                "Слова — через кому, без урахування регістру. Фільтри економлять ліміти моделей: відсіяне не йде в ШІ.")
        return text, rows

    def L_sort(self, lid):
        rows = [[(title, self.cb("Lsortv", lid, k))] for k, (title, _f) in SORTS.items()]
        rows.append([("« Список", self.cb("Lo", lid))])
        return ("🔃 <b>Сортування</b>\n\nПорядок зберігається; далі можна підправити вручну кнопками ⬆️ ⬇️ у картці джерела "
                "або «🔢 На позицію»."), rows

    def L_bulk(self, lid):
        rows = [
            [("🗑 Видалити за номерами", self.cb("Lbdel", lid))],
            [("⚪️ Вимкнути за номерами", self.cb("Lboff", lid)), ("🟢 Увімкнути за номерами", self.cb("Lbon", lid))],
            [("🟢 Увімкнути всі", self.cb("Lall", lid, 1)), ("⚪️ Вимкнути всі", self.cb("Lall", lid, 0))],
            [("🧹 Прибрати з помилками", self.cb("Lclean", lid, "broken")), ("🧹 Прибрати вимкнені", self.cb("Lclean", lid, "off"))],
            [("🧹 Прибрати ті, що є у вищих списках", self.cb("Lclean", lid, "dups"))],
            [("♻️ Замінити весь список файлом", self.cb("Lup", lid, "replace"))],
            [("« Список", self.cb("Lo", lid))],
        ]
        return ("✂️ <b>Масові дії</b>\n\nНомери — як у списку: <code>3, 5-8, 12</code>.\n"
                "«Замінити файлом» — список стане рівно таким, як у файлі (порядок теж), зайве видалиться, "
                "а лічильники наявних джерел збережуться."), rows

    def L_entry(self, eid):
        e = self.lists.entry(eid)
        if not e:
            return "Джерело не знайдено.", [[("« Списки", self.cb("L"))]]
        l = self.lists.get_list(e["list_id"])
        link = link_of(e["kind"], e["ref"])
        text = (f"{'🟢' if e['enabled'] else '⚪️'} <b>{esc(e['label'] or e['title'] or display_ref(e['kind'], e['ref']))}</b>\n"
                f"{esc(display_ref(e['kind'], e['ref']))}" + (f" · {TYPES.get(e['chat_type'], '')}" if e["chat_type"] else "") + "\n"
                + (f"Назва {'на платформі' if e['kind'] == 'social' else 'в Telegram'}: {esc(e['title'])}\n" if e["title"] and e["label"] else "")
                + f"Список: {esc(l['name'] if l else '?')} · позиція {e['position']}\n"
                f"Область: {esc(e['region'] or (l or {}).get('region') or 'визначить ШІ')}\n"
                + (f"Спосіб: {self._social_way(e['ref'])}\n" if e["kind"] == "social" else
                   f"Спосіб: {METHODS.get(e['method'], e['method'])}" + (f" (зараз: {METHODS.get(e['via'], e['via'])})" if e["via"] else "") + "\n")
                + f"Перевірено: {when(e['last_check'])}" + ("" if e["kind"] == "social" else f" · останній допис №{e['last_id'] or '—'}") + "\n"
                f"Узято в ньюзрум: {e['taken']} · відсіяно фільтром: {e['filtered']}"
                + (f"\n⚠️ {esc(e['last_error'])}" if e["last_error"] else ""))
        rows = [
            [(("⚪️ Вимкнути" if e["enabled"] else "🟢 Увімкнути"), self.cb("Etog", eid)), ("🔄 Перевірити джерело", self.cb("Enow", eid))],
            [("⏫", self.cb("Emv", eid, "top")), ("⬆️", self.cb("Emv", eid, "up")), ("⬇️", self.cb("Emv", eid, "down")), ("⏬", self.cb("Emv", eid, "bottom"))],
            [("🔢 На позицію", self.cb("Epos", eid)), ("✏️ Назва джерела", self.cb("Elab", eid))],
            [("🗺 Область джерела", self.cb("Ereg", eid))] + ([] if e["kind"] == "social" else [("🔁 Спосіб", self.cb("Emeth", eid))]),
            [("📁 В інший список", self.cb("Elst", eid)), ("🗑 Видалити", self.cb("Edel", eid))],
        ]
        if link:
            rows.append([("🔗 Відкрити джерело", "url:" + link)])
        rows.append([("« Список", self.cb("Lo", e["list_id"]))])
        return text, rows

    def _social_way(self, ref):
        from . import social
        p = social.split(ref)[0]
        way = social.configured().get(p, "")
        return way or "⚠️ не підключено — див. 📡 Джерела → 🌐 Соцмережі"

    def L_pick_list(self, eid=None, merge_from=None):
        rows = []
        for l in self.lists.lists():
            if merge_from and l["id"] == int(merge_from):
                continue
            rows.append([(f"📋 {short(l['name'], 30)}", self.cb("Elstt", eid, l["id"]) if eid else self.cb("Lmerget", merge_from, l["id"]))])
        if eid:
            rows.append([("➕ У новий список", self.cb("Elstn", eid))])
        back = self.cb("Le", eid) if eid else self.cb("Ls", merge_from)
        rows.append([("« Назад", back)])
        title = "📁 <b>Куди перенести джерело?</b>" if eid else "🔗 <b>З яким списком об'єднати?</b>\nУсі джерела перейдуть туди, цей список зникне."
        return title, rows

    def L_account(self):
        l = next((x for x in self.lists.lists() if x["auto"] == "account"), None)
        new_on = self.store.get("account_new_on")
        if self.mt.ready:
            st = f"🟢 Підключено як <b>{esc(self.mt.me.first_name if self.mt.me else '?')}</b>."
        else:
            st = f"⚪️ Не підключено: {esc(self.mt.error or 'не налаштовано')}."
        text = ("📲 <b>Підписки службового акаунта</b>\n\n" + st + "\n\n"
                "Бот читає всі канали, групи й чати, на які підписаний акаунт, у реальному часі. Підписки самі стають "
                "списком «📲 Підписки акаунта», де кожне джерело можна вимкнути, перейменувати, сортувати й переносити. "
                "Підпишіться на новий канал з телефону — він з'явиться тут сам.\n\n"
                f"Нові підписки: {'одразу 🟢 увімкнені' if new_on else '⚪️ додаються вимкненими (вмикаєте вручну)'}\n"
                "Особисті листування не читаються (можна дозволити змінною NEWSROOM_READ_PRIVATE=1).\n"
                + (f"\nУ списку: {l['n']} джерел, увімкнено {l['n_on']}." if l else "")
                + ("" if self.mt.ready else "\n\n<b>Як підключити</b> (один раз, ≈5 хв):\n"
                   "1. my.telegram.org → API development tools → отримати api_id і api_hash.\n"
                   "2. На сервері: <code>docker compose run --rm -it ai python -m plugins.newsroom.make_session</code> — "
                   "увійти кодом із Telegram.\n3. У .env: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION → "
                   "<code>docker compose up -d ai</code>.\nКраще окремий акаунт із 2FA: рядок сесії дає повний доступ до нього."))
        rows = []
        if self.mt.ready:
            rows.append([("🔄 Синхронізувати підписки", self.cb("Lsync"))])
        else:
            rows.append([("🔌 Підключитися ще раз", self.cb("Lmtre"))])
        rows.append([(("🟢 Нові — увімкнені" if new_on else "⚪️ Нові — вимкнені"), self.cb("Lnewon"))])
        if l:
            rows.append([("📋 Відкрити список підписок", self.cb("Lo", l["id"]))])
        rows.append([("« Джерела", self.cb("H1"))])
        return text[:4000], rows

    # ================================================================ дії
    async def lists_route(self, chat, a, args):
        x = args[0] if args else None
        y = args[1] if len(args) > 1 else None
        L = self.lists
        cancel = [[("✖️ Скасувати", "m:cancel")]]
        if a == "L":
            return None, self.L_home(chat)
        if a == "Lhelp":
            return None, (FORMAT_HELP, [[("📤 Завантажити файл", self.cb("Lup")), ("« Списки", self.cb("L"))]])
        if a == "Lnew":
            self.ask(chat, "Lnew")
            return None, ("➕ <b>Назва нового списку</b>\n\nНаприклад: «Області», «Екологія», «Офіційні джерела».", cancel)
        if a == "Lup":
            self.wait_file[chat] = (int(x) if x else 0, y or "add")
            self.ask(chat, "Lpaste", (int(x) if x else 0, y or "add"))
            where = f"у список «{esc(L.get_list(x)['name'])}»" if x and L.get_list(x) else "— списки створяться за назвами з файлу"
            mode = " Список буде <b>замінено</b> вмістом файлу." if y == "replace" else ""
            return None, (f"📤 <b>Надішліть файл</b> .txt / .csv / .xlsx / .json {where}.{mode}\n\nАбо вставте список текстом "
                          "одним повідомленням. Формат — «❓ Формат файлу».", [[("❓ Формат", self.cb("Lhelp")), ("✖️ Скасувати", "m:cancel")]])
        if a == "Lexp":
            bot = self.host.bot()
            if bot:
                from telegram import InputFile
                name = "spysky_dzherel"
                if x and L.get_list(x):
                    name = "spysok_" + str(x)
                await bot.send_document(chat, InputFile(io.BytesIO(L.export_csv(int(x) if x else None)), filename=name + ".csv"),
                                        caption="📥 CSV для Excel — відредагуйте й завантажте назад (📤).")
                if not x:
                    await bot.send_document(chat, InputFile(io.BytesIO(L.export_txt()), filename=name + ".txt"),
                                            caption="📥 Той самий вміст текстом (## — назви списків).")
            return "Надіслав файли", None
        if a == "Lacc":
            return None, self.L_account()
        if a == "Lsync":
            n_new, n_gone = await self.sync_account(chat)
            return f"Нових: {n_new} · зникло: {n_gone}", self.L_account()
        if a == "Lmtre":
            await self.mt_connect()
            return ("Підключено" if self.mt.ready else "Не вдалося: " + self.mt.error[:120]), self.L_account()
        if a == "Lnewon":
            self.store.set("account_new_on", not self.store.get("account_new_on"))
            return None, self.L_account()
        if a == "Lnow":
            n, errs = await self.check_sources(list_id=int(x) if x else None, force=True)
            return f"Нових дописів: {n}" + (f" · помилок: {errs}" if errs else ""), (self.L_list(x) if x else self.L_home(chat))
        if a == "Lo":
            return None, self.L_list(int(x), int(y or 0))
        if a == "Ls":
            return None, self.L_settings(int(x))
        if a == "Ltog":
            l = L.get_list(x)
            L.update_list(x, enabled=0 if l["enabled"] else 1)
            return ("Список вимкнено" if l["enabled"] else "Список увімкнено"), self.L_settings(int(x))
        if a == "Liv":
            L.update_list(x, interval=int(y))
            return "Збережено", self.L_settings(int(x))
        if a in ("Lren", "Linc", "Lexc", "Lmin", "Lreg", "Ladd", "Lbdel", "Lboff", "Lbon"):
            prompts = {
                "Lren": "✏️ Нова назва списку:",
                "Linc": "✅ Слова через кому — брати лише дописи, де є хоч одне (або «-», щоб прибрати фільтр):",
                "Lexc": "🚫 Слова через кому — пропускати дописи з ними (наприклад: реклама, розіграш, знижка) або «-»:",
                "Lmin": "📏 Мінімальна довжина допису в символах (0 — без обмеження). Для груп зручно 60–150:",
                "Lreg": "🗺 Область за замовчуванням для джерел цього списку (або «-»):",
                "Ladd": "➕ Вставте посилання — по одному в рядку (можна з назвою й областю через |):",
                "Lbdel": "🗑 Номери джерел для видалення, наприклад 3, 5-8, 12:",
                "Lboff": "⚪️ Номери джерел, які вимкнути:",
                "Lbon": "🟢 Номери джерел, які увімкнути:",
            }
            self.ask(chat, a, int(x))
            return None, (prompts[a], cancel)
        if a == "Lsort":
            return None, self.L_sort(int(x))
        if a == "Lsortv":
            L.sort(int(x), y)
            return "Відсортовано", self.L_list(int(x))
        if a == "Lbulk":
            return None, self.L_bulk(int(x))
        if a == "Lall":
            for e in L.entries(int(x)):
                L.update_entry(e["id"], enabled=int(y))
            return ("Усі увімкнено" if y == "1" else "Усі вимкнено"), self.L_list(int(x))
        if a == "Lclean":
            n = L.cleanup(int(x), y)
            L.renumber(int(x))
            return f"Прибрано: {n}", self.L_list(int(x))
        if a == "Lmove":
            L.move_list(int(x), y)
            return None, self.L_settings(int(x))
        if a == "Lmerge":
            return None, self.L_pick_list(merge_from=x)
        if a == "Lmerget":
            n = L.merge_lists(int(x), int(y))
            return f"Перенесено: {n}", self.L_list(int(y))
        if a == "Ldelask":
            l = L.get_list(x)
            return None, (f"🗑 <b>Видалити список «{esc(l['name'])}» і всі {l['n']} джерел?</b>\nНовини, уже взяті з них, лишаться.",
                          [[("🗑 Так, видалити", self.cb("Ldel", x)), ("« Ні", self.cb("Ls", x))]])
        if a == "Ldel":
            L.delete_list(int(x))
            return "Список видалено", self.L_home(chat)
        # ----- окреме джерело
        if a == "Le":
            return None, self.L_entry(int(x))
        if a == "Etog":
            e = L.entry(x)
            L.update_entry(x, enabled=0 if e["enabled"] else 1)
            return None, self.L_entry(int(x))
        if a == "Emv":
            L.reorder(int(x), y)
            return None, self.L_entry(int(x))
        if a in ("Epos", "Elab", "Ereg"):
            prompts = {"Epos": "🔢 На яку позицію поставити (число)?", "Elab": "✏️ Назва джерела (або «-», щоб узяти з Telegram):",
                       "Ereg": "🗺 Область для цього джерела (або «-»):"}
            self.ask(chat, a, int(x))
            return None, (prompts[a], cancel)
        if a == "Emeth":
            e = L.entry(x)
            order = ["auto", "web", "mtproto", "bot"]
            nxt = order[(order.index(e["method"]) + 1) % len(order)] if e["method"] in order else "auto"
            L.update_entry(x, method=nxt, last_error="")
            return f"Спосіб: {METHODS[nxt]}", self.L_entry(int(x))
        if a == "Elst":
            return None, self.L_pick_list(eid=x)
        if a == "Elstt":
            e = L.entry(x)
            ok = L.move_entry(int(x), int(y))
            return ("Перенесено" if ok else "Там уже є — прибрав дубль"), self.L_list(int(y))
        if a == "Elstn":
            self.ask(chat, "Elstn", int(x))
            return None, ("📁 Назва нового списку для цього джерела:", cancel)
        if a == "Edel":
            e = L.entry(x)
            L.delete_entries([int(x)])
            L.renumber(e["list_id"])
            return "Видалено", self.L_list(e["list_id"])
        if a == "Enow":
            e = L.entry(x)
            n, errs = await self.check_sources(entry_id=int(x), force=True)
            return f"Нових: {n}" + (" · помилка" if errs else ""), self.L_entry(int(x))
        return None, self.L_home(chat)

    async def lists_text(self, chat, key, data, text):
        L = self.lists
        t = text.strip()
        dash = t in ("-", "—")
        if key == "Lnew":
            lid = L.create_list(t[:60])
            return "Список створено", self.L_list(lid)
        if key == "Lpaste":
            self.wait_file.pop(chat, None)
            groups, errors = parse_text(t)
            return await self.import_groups(chat, groups, errors, data[0], data[1], "текст")
        if key == "Lren":
            L.update_list(data, name=t[:60])
            return "Перейменовано", self.L_settings(data)
        if key in ("Linc", "Lexc"):
            L.update_list(data, **{"include" if key == "Linc" else "exclude": "" if dash else t[:500]})
            return "Збережено", self.L_settings(data)
        if key == "Lmin":
            L.update_list(data, min_len=int(t) if t.isdigit() else 0)
            return "Збережено", self.L_settings(data)
        if key == "Lreg":
            L.update_list(data, region="" if dash else t[:60])
            return "Збережено", self.L_settings(data)
        if key == "Ladd":
            groups, errors = parse_text(t)
            return await self.import_groups(chat, groups, errors, data, "add", "текст")
        if key in ("Lbdel", "Lboff", "Lbon"):
            if key == "Lbdel":
                n = L.delete_positions(data, t)
                return f"Видалено: {n}", self.L_list(data)
            n = L.set_enabled_positions(data, t, key == "Lbon")
            return f"Змінено: {n}", self.L_list(data)
        if key == "Epos":
            if t.isdigit():
                L.place(data, int(t))
            return None, self.L_entry(data)
        if key == "Elab":
            L.update_entry(data, label="" if dash else t[:80])
            return "Збережено", self.L_entry(data)
        if key == "Ereg":
            L.update_entry(data, region="" if dash else t[:60])
            return "Збережено", self.L_entry(data)
        if key == "Elstn":
            lid = L.create_list(t[:60])
            L.move_entry(data, lid)
            return "Перенесено в новий список", self.L_list(lid)
        return None, self.L_home(chat)

    async def import_groups(self, chat, groups, errors, lid, mode, what):
        L = self.lists
        if not any(items for _n, items in groups):
            msg = "⚠️ Не знайшов жодного каналу чи чату." + ("\n" + "\n".join(esc(e) for e in errors[:8]) if errors else "")
            return None, (msg + "\n\nФормат — «❓ Формат файлу».", [[("❓ Формат", self.cb("Lhelp")), ("« Списки", self.cb("L"))]])
        added = updated = created = 0
        touched = []
        if lid:
            items = [i for _n, its in groups for i in its]
            a, u = L.add_entries(lid, items, mode)
            added, updated, touched = a, u, [lid]
        else:
            for name, items in groups:
                name = name or (f"Імпорт {time.strftime('%d.%m %H:%M')}")
                l = L.find_list(name)
                if not l:
                    nid = L.create_list(name)
                    created += 1
                else:
                    nid = l["id"]
                a, u = L.add_entries(nid, items, mode)
                added += a
                updated += u
                touched.append(nid)
        lines = [f"✅ Імпорт ({what}): додано {added}, оновлено {updated}" + (f", нових списків {created}" if created else "")
                 + (" · список замінено" if mode == "replace" else "")]
        if errors:
            lines.append(f"⚠️ Не розпізнано рядків: {len(errors)}")
            lines += ["• " + esc(e) for e in errors[:6]]
        lines.append("Перша перевірка лише запам'ятає наявні дописи; далі бот братиме нові.")
        screen = self.L_list(touched[0]) if len(set(touched)) == 1 else self.L_home(chat)
        return "Імпортовано", ("\n".join(lines) + "\n\n" + screen[0], screen[1])

    async def on_document(self, update, ctx):
        msg = update.message
        chat = msg.chat_id
        if chat not in self.wait_file:
            if self.collecting(chat):
                await self.collect_document(msg)  # 🧾 збір для зведення
                return True
            return False
        lid, mode = self.wait_file.pop(chat)
        self.host.cancel_input(chat)
        doc = msg.document
        if (doc.file_size or 0) > 5 * 1024 * 1024:
            await msg.reply_text("Файл більший за 5 МБ.")
            return True
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        try:
            groups, errors = parse_file(doc.file_name or "list.txt", data)
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"⚠️ Не вдалося прочитати файл: {exc}")
            return True
        toast, screen = await self.import_groups(chat, groups, errors, lid, mode, doc.file_name or "файл")
        await self.host.notify(chat, *screen)
        return True
