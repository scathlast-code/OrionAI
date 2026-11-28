"""✍️ Перевірка стилю новини без ШІ (швидко й передбачувано): русизми й кальки, застарілі та російські назви міст,
«на Україні», клікбейт, крик великими літерами й «!!!», лапки, тире, подвійні пробіли, задовгий заголовок.
Безпечні заміни бот може зробити сам («🪄 Виправити»); решту лише показує. Плюс ваш словник редакції «як не можна → як треба»."""
from __future__ import annotations

import re

# (шаблон, заміна або None, пояснення). Заміни — лише однозначні; регістр першої літери зберігається.
RULES = [
    # русизми й кальки
    (r"\bприйма(ти|є|ють|в|ла|ли) участь", r"бра\1 участь", "калька: «брати участь»"),
    (r"\bприйняти участь", "взяти участь", "калька: «взяти участь»"),
    (r"\bприйня(в|ла|ли) участь", r"взя\1 участь", "калька: «взяти участь»"),
    (r"\bна протязі\b", "протягом", "русизм: «протягом»"),
    (r"\bяв(ляється|ляються)\b", None, "канцелярит: замість «являється» — «є»"),
    (r"\bслідуюч(ий|а|е|і|ого|ому|их)\b", None, "русизм: «наступний»"),
    (r"\bв якості\b", None, "калька: «як» або «у ролі»"),
    (r"\bзгідно закону\b", "згідно із законом", "керування: «згідно із законом»"),
    (r"\bміроприємств\w*", None, "русизм: «захід/заходи»"),
    (r"\bпо відношенню до\b", "щодо", "калька: «щодо»"),
    (r"\bвиключно важлив", "надзвичайно важлив", "калька: «надзвичайно важливий»"),
    (r"\bвірніше\b", "точніше", "калька: «точніше»"),
    (r"\bспівпада(є|ють|ти)\b", r"збіга\1", "калька: «збігатися»"),
    (r"\bприймати міри\b", "вживати заходів", "калька: «вживати заходів»"),
    (r"\bвжити міри\b", "вжити заходів", "калька: «вжити заходів»"),
    (r"\bпопереджати? (про|щодо)\b", None, "краще «застерігати» / «повідомляти заздалегідь»"),
    (r"\bдякуючи\b", None, "калька: «завдяки» (якщо про причину)"),
    (r"\bнаступаюч(ий|ого|им)\b", None, "калька: «прийдешній» / «наступний»"),
    (r"\bзаключа(ється|ються)\s+в\b", r"поляга\1 в", "калька: «полягає в»"),
    (r"\bзадача\b", None, "у значенні мети — «завдання»"),
    # назви міст і країни
    (r"\bКиев\b|\bKiev\b", "Київ", "назва: «Київ» (Kyiv)"),
    (r"\bКиев[ауе]\b", None, "назва: «Київ» (у потрібному відмінку: Києва, Києві)"),
    (r"\bХарьков\b", "Харків", "назва: «Харків»"),
    (r"\bОдесса\b", "Одеса", "назва: «Одеса»"),
    (r"\bЛьвов\b", "Львів", "назва: «Львів»"),
    (r"\bНиколаев\b", "Миколаїв", "назва: «Миколаїв»"),
    (r"\bКировоград\b|\bКіровоград\b", "Кропивницький", "назва: «Кропивницький» (з 2016)"),
    (r"\bДнепропетровськ\b|\bДніпропетровськ\b", "Дніпро", "назва: «Дніпро» (з 2016)"),
    (r"\bна Україні\b", "в Україні", "«в Україні»"),
    (r"\bна Україну\b", "в Україну", "«в Україну»"),
    (r"\bБелорусія\b|\bБєлорусія\b", "Білорусь", "назва: «Білорусь»"),
    (r"\bМолдавія\b", "Молдова", "назва: «Молдова»"),
    # клікбейт
    (r"\bшок(уюч\w*)?\b|\bви не повірите\b|\bсенсаці\w*\b|\bжах!+", None, "клікбейт — приберіть"),
    # типографіка
    (r"!{2,}|\?{2,}", None, "кілька знаків оклику/питання поспіль"),
    (r"(?<=\S) - (?=\S)", " — ", "тире між словами: « — »"),
    (r" {2,}", " ", "подвійні пробіли"),
]
AUTO_TYPO = [(r'"([^"\n]{1,200})"', r"«\1»", "лапки-ялинки «»")]


def _keep_case(src, rep):
    return rep[:1].upper() + rep[1:] if src[:1].isupper() and rep[:1].islower() else rep


def custom_rules(text):
    """«погано → добре» або «погано = добре», по рядку; просте «слово» — лише попередити."""
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.split(r"\s*(?:→|->|=)\s*", ln, maxsplit=1)
        bad = m[0].strip()
        if not bad:
            continue
        out.append((r"\b" + re.escape(bad) + r"\b", (m[1].strip() if len(m) > 1 and m[1].strip() else None), "словник редакції"))
    return out


def check(headline, body, custom="", max_head=110):
    """-> список зауважень [{"msg", "frag", "fix"}] (fix — є безпечна заміна)."""
    notes = []
    text = (headline or "") + "\n" + (body or "")
    for pat, rep, msg in RULES + custom_rules(custom) + AUTO_TYPO:
        for m in list(re.finditer(pat, text, re.I))[:3]:
            notes.append({"msg": msg, "frag": m.group(0).strip() or "␣␣", "fix": rep is not None})
    if len(headline or "") > max_head:
        notes.append({"msg": f"задовгий заголовок: {len(headline)} символів (до {max_head})", "frag": "", "fix": False})
    caps = [w for w in re.findall(r"\b[А-ЯІЇЄҐ]{4,}\b", text) if w not in ("ЗСУ", "НАТО", "ООН", "СБУ", "НБУ", "МВС", "ДСНС", "ЄС", "США", "ВООЗ")]
    if caps:
        notes.append({"msg": "слова великими літерами", "frag": ", ".join(dict.fromkeys(caps[:4])), "fix": False})
    seen, uniq = set(), []
    for n in notes:
        k = (n["msg"], n["frag"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    return uniq


def fix(text, custom=""):
    """Застосувати лише безпечні заміни. -> (новий текст, скільки замін)."""
    count = 0
    for pat, rep, _msg in RULES + custom_rules(custom) + AUTO_TYPO:
        if rep is None:
            continue

        def sub(m, rep=rep):
            return _keep_case(m.group(0), m.expand(rep))
        text, n = re.subn(pat, sub, text, flags=re.I)
        count += n
    return text, count


def report(notes, limit=8):
    if not notes:
        return "✍️ Стиль: зауважень немає"
    lines = [f"✍️ Стиль: {len(notes)} зауваж." + (" (🪄 частину бот виправить сам)" if any(n["fix"] for n in notes) else "")]
    for n in notes[:limit]:
        lines.append(("🪄 " if n["fix"] else "• ") + (f"«{n['frag']}» — " if n["frag"] else "") + n["msg"])
    if len(notes) > limit:
        lines.append(f"… ще {len(notes) - limit}")
    return "\n".join(lines)


class StyleUI:
    """Мікс до «Новинарні»: екран «✍️ Стиль» для новини й словник редакції."""

    def sty_custom(self):
        return self.store.get("sty_dict") or ""

    def sty_screen(self, did):
        from plugins import esc
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        notes = check(d["headline"], d["body"], self.sty_custom())
        n_fix = sum(1 for n in notes if n["fix"])
        text = (f"✍️ <b>Стиль новини #{did}</b>\n\n" + esc(report(notes, 20)) +
                "\n\n🪄 — бот може виправити сам (стара версія збережеться в «🕘 Версії»); • — вирішіть самі або через «✍️ Правка».")
        rows = []
        if n_fix:
            rows.append([(f"🪄 Виправити автоматично · {n_fix}", self.cb("styfix", did))])
        rows += [[("📖 Словник редакції", self.cb("styd", did))], [("« Новина", self.cb("d", did))]]
        return text, rows

    def sty_dict_screen(self, did=0):
        from plugins import esc
        cur = self.sty_custom()
        text = ("📖 <b>Словник редакції</b>\n\nВаші правила понад вбудовані (русизми, назви міст, клікбейт, типографіка). "
                "По рядку: <code>як не можна → як треба</code> — бот виправлятиме сам; просто <code>слово</code> — лише попереджатиме.\n\n"
                "Зараз:\n" + (f"<pre>{esc(cur)}</pre>" if cur else "— порожньо"))
        back = self.cb("sty", did) if did else self.cb("set")
        return text, [[("✍️ Задати словник", self.cb("stydi", did))] + ([("🗑 Очистити", self.cb("stydx", did))] if cur else []),
                      [("« Назад", back)]]

    async def sty_route(self, chat, a, x, y):
        did = int(x or 0)
        if a == "sty":
            return None, self.sty_screen(did)
        if a == "styfix":
            d = self.store.draft(did)
            head, n1 = fix(d["headline"], self.sty_custom())
            body, n2 = fix(d["body"], self.sty_custom())
            if n1 + n2:
                self.store.update_draft(did, headline=head, body=body, note=f"✍️ стиль: {n1 + n2} замін")
                if hasattr(self, "_recheck_len"):
                    self._recheck_len(did)
            return f"🪄 Виправлено: {n1 + n2}", self.sty_screen(did)
        if a == "styd":
            return None, self.sty_dict_screen(did)
        if a == "stydi":
            self.ask(chat, "styd", did)
            return None, ("📖 Надішліть словник одним повідомленням, по рядку:\n<code>зрада → неочікуваний поворот</code>\n<code>шокуючий</code>",
                          [[("✖️ Скасувати", "m:cancel")]])
        if a == "stydx":
            self.store.set("sty_dict", "")
            return "🗑 Очищено", self.sty_dict_screen(did)
        return None, self.sty_screen(did)

    async def sty_text(self, chat, key, data, text):
        self.store.set("sty_dict", text.strip()[:3000])
        return "📖 Збережено", self.sty_dict_screen(int(data or 0))
