"""Спільний конструктор меню для ШІ-бота й головного бота.

Що вміє:
- кожен розділ стоїть рівно в одному місці: ⌨️ меню внизу, 🏠 «Меню», 📂 будь-яка група (підменю,
  групи можна вкладати одна в одну) або 🚫 прихований — тож кнопки ніде не дублюються;
- групи створюються, перейменовуються, вкладаються й видаляються прямо в боті;
- будь-який розділ, групу і взагалі будь-яку кнопку бота можна перейменувати;
- порядок, кількість кнопок у ряду (окремо для меню внизу і для «Меню»);
- навігація в кожному меню: ◀️ Назад · 🏠 Додому · Вперед ▶️ (як у браузері).

Модуль не залежить від Telegram: екрани — це (текст, рядки кнопок), де кнопка — (підпис, дані).
"""

import json
import secrets

PLACE_KB, PLACE_HOME, PLACE_OFF = "kb", "home", "off"
NAV_BACK, NAV_HOME, NAV_FWD = "◀️ Назад", "🏠 Додому", "Вперед ▶️"
HISTORY_MAX = 30


def pairs(items, cols=2):
    return [items[i : i + cols] for i in range(0, len(items), cols)]


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Layout:
    """items: {id: (підпис, дані)}; default_layout: [[id, місце]]; default_groups: {gid: назва}."""

    def __init__(self, items, default_layout, default_groups, get_prefs, set_pref, locked=(), admin_items=(),
                 is_admin=lambda chat: True, home_data="m:home", settings_back="m:st", use_kb=True):
        self.items = items
        self.default_layout = default_layout
        self.default_groups = default_groups
        self.get_prefs = get_prefs
        self.set_pref = set_pref
        self.locked = set(locked)
        self.admin_items = set(admin_items)
        self.is_admin = is_admin
        self.home_data = home_data
        self.settings_back = settings_back
        self.hist = {}  # chat -> [список даних екранів, позиція]; зберігається в налаштуваннях — переживає перезапуск
        self.use_kb = use_kb  # False — нижньої клавіатури немає: усе живе в головному меню (кнопки-акордеони)

    # ------------------------------------------------------------ дані

    def groups(self, chat):
        p = self.get_prefs(chat)
        g = p.get("groups")
        return dict(self.default_groups) if g is None else dict(g)

    def labels(self, chat):
        return dict(self.get_prefs(chat).get("labels") or {})

    def relabels(self, chat):
        return dict(self.get_prefs(chat).get("relabel") or {})

    def all_ids(self, chat):
        return list(self.items) + [f"g:{g}" for g in self.groups(chat)]

    def layout(self, chat):
        """[[id, місце]] з урахуванням нових розділів і видалених груп."""
        saved = self.get_prefs(chat).get("layout") or []
        ids = set(self.all_ids(chat))
        groups = self.groups(chat)
        out, have = [], set()
        for i, place in saved:
            if i in ids and i not in have:
                if place.startswith("g:") and place[2:] not in groups:
                    place = PLACE_HOME
                out.append([i, place])
                have.add(i)
        for i, place in self.default_layout:  # нові розділи з'являються на своїх типових місцях
            if i in ids and i not in have:
                if place.startswith("g:") and place[2:] not in groups:
                    place = PLACE_HOME
                out.append([i, place])
                have.add(i)
        for i in ids - have:  # нова група — у «Меню»
            out.append([i, PLACE_HOME])
        if not self.use_kb:  # нижньої клавіатури немає — те, що стояло «внизу», показуємо в головному меню
            out = [[i, PLACE_HOME if p == PLACE_KB else p] for i, p in out]
        return [x for x in out if x[0] not in self.admin_items or self.is_admin(chat)]

    def label(self, chat, i):
        own = self.labels(chat).get(i)
        if own:
            return own
        if i.startswith("g:"):
            return self.groups(chat).get(i[2:], "📂 Група")
        return self.items[i][0]

    def target(self, i):
        return f"m:grp:{i[2:]}" if i.startswith("g:") else self.items[i][1]

    def where(self, chat, i):
        return dict(self.layout(chat)).get(i, PLACE_OFF)

    def place_name(self, chat, place):
        if place == PLACE_KB:
            return "⌨️ меню внизу"
        if place == PLACE_HOME:
            return "🏠 «Меню»"
        if place == PLACE_OFF:
            return "🚫 приховано"
        return "📂 " + self.label(chat, place)

    def in_place(self, chat, place):
        return [i for i, p in self.layout(chat) if p == place]

    def chain(self, chat, gid):
        """Шлях від головного меню до групи: ["g:батько", …, "g:gid"] — щоб розгорнути вкладені групи."""
        d = dict(self.layout(chat))
        out, cur = [], f"g:{gid}"
        while cur.startswith("g:") and cur not in out:
            out.insert(0, cur)
            cur = d.get(cur, PLACE_HOME)
        return out if cur == PLACE_HOME else []

    def accordion_rows(self, chat, open_gid=None, badges=None, place=PLACE_HOME, depth=0, chain=None):
        """Головне меню-акордеон: групи лишаються на місці й розгортаються ▼ прямо під собою (вкладені — теж)."""
        badges = badges or {}
        chain = self.chain(chat, open_gid) if chain is None and open_gid else (chain or [])
        cols = self.get_prefs(chat).get("home_cols", 2)
        rows, plain = [], []
        pad = "  " * depth + ("↳ " if depth else "")

        def flush():
            rows.extend(pairs(plain, cols))
            plain.clear()

        for i in self.in_place(chat, place):
            if i.startswith("g:"):
                flush()
                is_open = i in chain
                label = pad + self.label(chat, i) + badges.get(i, "") + (" ▲" if is_open else " ▼")
                rows.append([(label, f"m:grpx:{i[2:]}" if is_open else f"m:grp:{i[2:]}")])
                if is_open:
                    rows += self.accordion_rows(chat, badges=badges, place=i, depth=depth + 1, chain=chain)
            else:
                plain.append((self.label(chat, i) + badges.get(i, ""), self.target(i)))
        flush()
        return rows

    def parent_group(self, chat, gid):
        p = dict(self.layout(chat)).get(f"g:{gid}", PLACE_HOME)
        return p[2:] if p.startswith("g:") else None

    # ------------------------------------------------------------ що показувати

    def reply_rows(self, chat):
        """Підписи кнопок меню внизу екрана."""
        labels = [self.label(chat, i) for i in self.in_place(chat, PLACE_KB)]
        cols = self.get_prefs(chat).get("kb_cols", 2)
        return pairs(labels, cols) or [[self.label(chat, "st")]]

    def reply_routes(self, chat):
        """Підпис кнопки меню внизу -> дані (з урахуванням перейменувань)."""
        out = {self.items[i][0]: self.items[i][1] for i in self.items}
        for i in self.all_ids(chat):
            out[self.label(chat, i)] = self.target(i)
        return out

    def place_rows(self, chat, place, badges=None):
        badges = badges or {}
        cols = self.get_prefs(chat).get("home_cols", 2)
        btns = [(self.label(chat, i) + badges.get(i, ""), self.target(i)) for i in self.in_place(chat, place)]
        return pairs(btns, cols)

    def scr_group(self, chat, gid, badges=None):
        title = self.label(chat, f"g:{gid}")
        rows = self.place_rows(chat, f"g:{gid}", badges)
        text = f"<b>{esc(title)}</b>\n\n" + ("Оберіть розділ:" if rows else "<i>Група порожня. Додайте сюди розділи в «🎛 Кнопки».</i>")
        rows.append([("🎛 Змінити цю групу", f"m:bi:g:{gid}")])
        return text, rows

    # ------------------------------------------------------------ редактор

    def scr_editor(self, chat):
        lay = self.layout(chat)
        p = self.get_prefs(chat)
        lines = ["<b>🎛 Кнопки й меню</b>", "", "Змінюйте все: де стоїть кожен розділ, назви, групи (підменю), порядок і кількість кнопок у ряду.", ""]
        places = ([PLACE_KB] if self.use_kb else []) + [PLACE_HOME] + [f"g:{g}" for g in self.groups(chat)] + [PLACE_OFF]
        for place in places:
            names = [self.label(chat, i) for i, pl in lay if pl == place]
            lines.append(f"<b>{esc(self.place_name(chat, place))}:</b> " + (esc(" · ".join(names)) if names else "—"))
        lines += ["", "Натисніть на розділ, щоб змінити його."]
        rows = pairs([(self.label(chat, i), f"m:bi:{i}") for i, _ in lay])
        rows.append([("➕ Нова група", "m:bgnew"), ("✏️ Будь-яка кнопка", "m:brl")])
        kc, hc = p.get("kb_cols", 2), p.get("home_cols", 2)
        if self.use_kb:
            rows.append([("⌨️ У ряд:", "m:noop")] + [(("● " if kc == n else "") + str(n), f"m:bset:kb_cols:{n}") for n in (1, 2, 3, 4)])
        rows.append([("🏠 У ряд:", "m:noop")] + [(("● " if hc == n else "") + str(n), f"m:bset:home_cols:{n}") for n in (1, 2, 3)])
        rows.append([("↺ Усе як було спочатку", "m:breset")])
        return "\n".join(lines), rows

    def _cycle(self, chat, i, place):
        """Чи не потрапить група сама в себе (або в свою підгрупу)."""
        if not (i.startswith("g:") and place.startswith("g:")):
            return False
        cur = place
        lay = dict(self.layout(chat))
        while cur.startswith("g:"):
            if cur == i:
                return True
            cur = lay.get(cur, PLACE_HOME)
        return False

    def _reachable(self, lay):
        """Захищені розділи мають лишатися досяжними з меню внизу або «Меню»."""
        d = dict(lay)
        for i in self.locked:
            cur, seen = d.get(i, PLACE_HOME), set()
            while cur.startswith("g:") and cur not in seen:
                seen.add(cur)
                cur = d.get(cur, PLACE_HOME)
            if cur not in (PLACE_KB, PLACE_HOME):
                return False
        return True

    def scr_item(self, chat, i):
        if i not in self.all_ids(chat):
            return self.scr_editor(chat)
        place = self.where(chat, i)
        is_group = i.startswith("g:")
        kind = "Група (підменю)" if is_group else "Розділ"
        text = (f"<b>🎛 {esc(self.label(chat, i))}</b>\n\n{kind}. Зараз: <b>{esc(self.place_name(chat, place))}</b>"
                + (" · 🔒 не можна приховати, щоб не загубити дорогу" if i in self.locked else ""))
        if is_group:
            inside = [self.label(chat, x) for x in self.in_place(chat, i)]
            text += "\nУсередині: " + (esc(" · ".join(inside)) if inside else "порожньо") + "\nЯкщо групу видалити, її вміст перейде в головне меню."
        text += "\n\nКуди поставити:"
        opts = ([(PLACE_KB, "⌨️ Внизу")] if self.use_kb else []) + [(PLACE_HOME, "🏠 У «Меню»")]
        opts += [(f"g:{g}", "📂 " + self.label(chat, f"g:{g}")) for g in self.groups(chat) if f"g:{g}" != i]
        if i not in self.locked:
            opts.append((PLACE_OFF, "🚫 Сховати"))
        rows = pairs([(("● " if pl == place else "") + name, f"m:bmv:{i}:{pl}") for pl, name in opts if not self._cycle(chat, i, pl)])
        rows.append([("⬆️ Вище", f"m:bup:{i}"), ("⬇️ Нижче", f"m:bdn:{i}")])
        rows.append([("✏️ Перейменувати", f"m:lren:{i}")] + ([("↺ Стара назва", f"m:lunren:{i}")] if i in self.labels(chat) else []))
        if is_group:
            rows.append([("🗑 Видалити групу", f"m:bgdel:{i[2:]}")])
        rows.append([("🎛 Усі кнопки", "m:btn")])
        return text, rows

    def scr_relabel(self, chat):
        rl = self.relabels(chat)
        text = ("<b>✏️ Перейменувати будь-яку кнопку</b>\n\nНадішліть повідомлення у форматі\n"
                "<code>Старий підпис = Новий підпис</code>\n\nНаприклад: <code>🗑 Кошик = ♻️ Смітник</code>. "
                "Працює для всіх кнопок у всіх меню, разом із «Назад», «Додому» й «Вперед».")
        if rl:
            text += "\n\n<b>Ваші заміни:</b>\n" + "\n".join(f"• {esc(a)} → {esc(b)}" for a, b in rl.items())
        rows = [[(f"✖️ {b}", f"m:brldel:{n}")] for n, (a, b) in enumerate(rl.items())]
        return text, rows

    def apply(self, chat, action, arg=None, value=None):
        """Змінює розташування. Повертає підказку для користувача або None."""
        lay = [list(x) for x in self.layout(chat)]
        idx = next((n for n, x in enumerate(lay) if x[0] == arg), None)
        if action == "mv" and idx is not None:
            if value == PLACE_OFF and arg in self.locked:
                return "Цей розділ не можна приховати"
            if self._cycle(chat, arg, value):
                return "Група не може бути всередині самої себе"
            old = lay[idx][1]
            lay[idx][1] = value
            if not self._reachable(lay):
                lay[idx][1] = old
                return "Так «Налаштування» стали б недоступні — оберіть інше місце"
        elif action in ("up", "dn") and idx is not None:
            place = lay[idx][1]
            same = [n for n, x in enumerate(lay) if x[1] == place]
            k = same.index(idx)
            j = k - 1 if action == "up" else k + 1
            if 0 <= j < len(same):
                a, b = same[k], same[j]
                lay[a], lay[b] = lay[b], lay[a]
        elif action == "set":
            self.set_pref(chat, arg, int(value))
            return None
        elif action == "ren":
            labels = self.labels(chat)
            labels[arg] = value[:40]
            self.set_pref(chat, "labels", labels)
            return None
        elif action == "unren":
            labels = self.labels(chat)
            labels.pop(arg, None)
            self.set_pref(chat, "labels", labels)
            return None
        elif action == "gnew":
            groups = self.groups(chat)
            gid = secrets.token_hex(3)
            groups[gid] = value[:40]
            self.set_pref(chat, "groups", groups)
            lay.append([f"g:{gid}", PLACE_HOME])
            self.set_pref(chat, "layout", lay)
            return f"g:{gid}"
        elif action == "gdel":
            groups = self.groups(chat)
            groups.pop(arg, None)
            self.set_pref(chat, "groups", groups)
            lay = [[i, PLACE_HOME if pl == f"g:{arg}" else pl] for i, pl in lay if i != f"g:{arg}"]
        elif action == "relabel":
            rl = self.relabels(chat)
            old, new = arg, value
            rl[old] = new[:40]
            self.set_pref(chat, "relabel", rl)
            return None
        elif action == "relabel_del":
            rl = self.relabels(chat)
            keys = list(rl)
            if 0 <= int(arg) < len(keys):
                rl.pop(keys[int(arg)])
            self.set_pref(chat, "relabel", rl)
            return None
        elif action == "reset":
            for k in ("layout", "groups", "labels", "relabel", "kb_cols", "home_cols"):
                self.set_pref(chat, k, None)
            return None
        self.set_pref(chat, "layout", lay)
        return None

    # ------------------------------------------------------------ діагностика меню

    def report(self, chat):
        """(цифри, конфлікти, добре) для 🩺 Діагностики."""
        lay = self.layout(chat)
        d = dict(lay)
        counts = {"kb": 0, "home": 0, "group": 0, "off": 0}
        hidden_inside, conflicts, good = [], [], []
        for i, place in lay:
            counts["group" if place.startswith("g:") else place] += 1
            cur, seen = place, set()
            while cur.startswith("g:") and cur not in seen:
                seen.add(cur)
                cur = d.get(cur, "home")
            if cur == "off" and place != "off":
                hidden_inside.append(self.label(chat, i))
        if hidden_inside:
            conflicts.append("Розділи лежать у прихованій групі, тож до них не дістатися: " + ", ".join(hidden_inside[:6]))
        empty = [self.label(chat, f"g:{g}") for g in self.groups(chat) if not self.in_place(chat, f"g:{g}")]
        if empty:
            conflicts.append("Порожні групи (кнопка є, а всередині нічого): " + ", ".join(empty))
        if counts["kb"] > 8:
            conflicts.append(f"У меню внизу {counts['kb']} кнопок — на телефоні вони займають пів екрана. Частину варто перенести в групи")
        if not counts["kb"]:
            conflicts.append("Меню внизу порожнє — розділи відкриваються лише через /menu")
        rl = self.relabels(chat)
        same = [b for b in rl.values() if list(rl.values()).count(b) > 1]
        if same:
            conflicts.append("Кілька різних кнопок перейменовано однаково: " + ", ".join(sorted(set(same))))
        if not conflicts:
            good.append(f"Меню впорядковане: {counts['kb']} кнопок унизу, {counts['home']} у «Меню», {counts['group']} у групах, {counts['off']} приховано")
        return counts, conflicts, good

    # ------------------------------------------------------------ перейменування й навігація

    def relabel(self, chat, text):
        return self.relabels(chat).get(text, text)

    def unrelabel(self, chat, text):
        """Для натискань у меню внизу: новий підпис -> старий."""
        for a, b in self.relabels(chat).items():
            if b == text:
                return a
        return text

    def decorate(self, chat, rows, nav=True, current=None):
        """Прибирає старі «« …» і «🏠 Меню», додає ряд навігації, застосовує перейменування."""
        out, parent = [], None
        for row in rows or []:
            r = []
            for t, d in row:
                if nav and (t.startswith("«") or d in (self.home_data,)):
                    if t.startswith("«") and d != self.home_data and parent is None:
                        parent = d  # власне посилання екрана «« Батьківський розділ»
                    continue
                r.append((t, d))
            if r:
                out.append(r)
        if nav:
            back, fwd = self.can_back(chat), self.can_fwd(chat)
            navrow = []
            if back:
                navrow.append((NAV_BACK, "m:nav:back"))
            elif parent:  # історії немає (наприклад, відкрили з сповіщення) — «Назад» веде до батьківського розділу
                navrow.append((NAV_BACK, parent))
            if current != self.home_data:
                navrow.append((NAV_HOME, "m:nav:home"))
            if fwd:
                navrow.append((NAV_FWD, "m:nav:fwd"))
            if navrow:
                out.append(navrow)
        return [[(self.relabel(chat, t), d) for t, d in row] for row in out]

    def _h(self, chat):
        if chat not in self.hist:
            saved = (self.get_prefs(chat) or {}).get("nav_hist")
            ok = isinstance(saved, list) and len(saved) == 2 and isinstance(saved[0], list)
            self.hist[chat] = [list(saved[0]), int(saved[1])] if ok else [[], -1]
        return self.hist[chat]

    def _save_hist(self, chat):
        try:
            stack, pos = self.hist[chat]
            self.set_pref(chat, "nav_hist", [stack[-HISTORY_MAX:], min(pos, len(stack[-HISTORY_MAX:]) - 1)])
        except Exception:  # noqa: BLE001
            pass

    def visit(self, chat, data):
        """Запам'ятовує відкритий екран. Однаковий екран підряд не дублюється."""
        stack, pos = self._h(chat)
        if pos >= 0 and stack[pos] == data:
            return
        del stack[pos + 1 :]
        stack.append(data)
        if len(stack) > HISTORY_MAX:
            del stack[0]
        self.hist[chat] = [stack, len(stack) - 1]
        self._save_hist(chat)

    def current(self, chat):
        stack, pos = self._h(chat)
        return stack[pos] if 0 <= pos < len(stack) else None

    def can_back(self, chat):
        return self._h(chat)[1] > 0

    def can_fwd(self, chat):
        stack, pos = self._h(chat)
        return pos < len(stack) - 1

    def back(self, chat):
        stack, pos = self._h(chat)
        if pos > 0:
            self.hist[chat][1] = pos - 1
            self._save_hist(chat)
            return stack[pos - 1]
        return self.home_data

    def fwd(self, chat):
        stack, pos = self._h(chat)
        if pos < len(stack) - 1:
            self.hist[chat][1] = pos + 1
            self._save_hist(chat)
            return stack[pos + 1]
        return self.current(chat) or self.home_data


def handle_editor(L, chat, parts, ask):
    """Спільна обробка дій редактора кнопок. parts = data.split(':').
    ask(action, data, prompt) — попросити в користувача текст. Повертає (екран, підказка) або None."""
    a = parts[1]
    if a == "btn":
        return L.scr_editor(chat), None
    if a == "bi":
        return L.scr_item(chat, ":".join(parts[2:])), None
    if a == "bmv":
        item = ":".join(parts[2:-1]) if parts[-2] != "g" else ":".join(parts[2:-2])
        place = parts[-1] if parts[-2] != "g" else "g:" + parts[-1]
        tip = L.apply(chat, "mv", item, place)
        return L.scr_item(chat, item), tip or "Переміщено"
    if a in ("bup", "bdn"):
        item = ":".join(parts[2:])
        L.apply(chat, "up" if a == "bup" else "dn", item)
        return L.scr_item(chat, item), None
    if a == "bset":
        L.apply(chat, "set", parts[2], parts[3])
        return L.scr_editor(chat), None
    if a == "lren":
        item = ":".join(parts[2:])
        ask("lren", item, f"✏️ Надішліть нову назву для «{esc(L.label(chat, item))}». Емодзі на початку — за бажанням.")
        return None, None
    if a == "lunren":
        item = ":".join(parts[2:])
        L.apply(chat, "unren", item)
        return L.scr_item(chat, item), "Назву повернуто"
    if a == "bgnew":
        ask("bgnew", None, "📂 Як назвати нову групу? Наприклад: «🧰 Інструменти». Потім перенесіть у неї розділи.")
        return None, None
    if a == "bgdel":
        L.apply(chat, "gdel", parts[2])
        return L.scr_editor(chat), "Групу видалено"
    if a == "brl":
        ask("brl", None, None)
        return L.scr_relabel(chat), None
    if a == "brldel":
        L.apply(chat, "relabel_del", parts[2])
        return L.scr_relabel(chat), "Заміну прибрано"
    if a == "breset":
        L.apply(chat, "reset")
        return L.scr_editor(chat), "Повернуто типові кнопки"
    return None


def handle_editor_text(L, chat, action, data, text):
    """Відповідь користувача на запит редактора. Повертає (екран, підказка) або None."""
    if action == "lren":
        L.apply(chat, "ren", data, text.strip())
        return L.scr_item(chat, data), "✅ Перейменовано"
    if action == "bgnew":
        item = L.apply(chat, "gnew", None, text.strip())
        return L.scr_item(chat, item), "✅ Групу створено. Тепер переносьте в неї розділи."
    if action == "brl":
        if "=" not in text:
            return L.scr_relabel(chat), "⚠️ Потрібен формат: Старий = Новий"
        old, new = [x.strip() for x in text.split("=", 1)]
        if not old or not new:
            return L.scr_relabel(chat), "⚠️ Обидва підписи мають бути непорожні"
        L.apply(chat, "relabel", old, new)
        return L.scr_relabel(chat), f"✅ «{old}» тепер «{new}»"
    return None


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
