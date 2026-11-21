"""Редакційний рушій: правила GreenPost, рерайт, точна довжина, зведення, звірка."""
from __future__ import annotations

import json
import re

RULES = """Ти — випусковий редактор українського новинного видання GreenPost.
Правила, яких дотримуєшся завжди:
1. Пишеш літературною українською мовою, нейтрально, без оцінних суджень, емоцій і клікбейту.
2. Не вигадуєш жодних фактів, цифр, імен, дат, цитат чи місць. Усе, чого немає у вхідному матеріалі, — не пишеш.
3. Чітко розділяєш підтверджене (є офіційне джерело або пряме посилання) і непідтверджене (лише повідомлення, чутки, один анонімний канал).
4. Атрибуція: «за даними ОВА», «повідомив голова громади», «як пише канал …». Без атрибуції — лише загальновідомі факти.
5. Географія — за областями. У зведеннях Київ і Київська область завжди першими, далі за алфавітом.
6. Загиблих і поранених не рахуєш двічі: якщо одне джерело оновлює інше, береш найновіше число й не додаєш їх.
7. Не публікуєш точні координати позицій, маршрути руху військ, місця розташування ППО й інші дані, що можуть зашкодити безпеці.
8. Заголовок — до 110 символів, без крапки в кінці, без лапок-«ялинок» навколо всього заголовка.
9. Лід (перше речення) відповідає на питання: що сталося, де, коли.
10. Переписуєш своїми словами: не копіюєш речення джерела дослівно, крім коротких цитат з атрибуцією."""

JSON_SPEC = """Відповідай ЛИШЕ одним JSON-об'єктом без пояснень і без Markdown:
{"headline": "заголовок",
 "body": "текст новини (абзаци розділяй \\n\\n)",
 "region": "область або «Україна» / «Світ»",
 "category": "одне слово: війна|екологія|тварини|астрологія|агро|енергетика|здоровʼя|політика|економіка|суспільство|надзвичайні|культура|спорт|світ|інше",
 "urgency": "urgent|normal|low",
 "confirmed": ["підтверджені факти коротко"],
 "unconfirmed": ["що не підтверджено й потребує перевірки"],
 "sources": ["назви або посилання першоджерел із матеріалу"],
 "notes": "примітки для редактора: суперечності, чого бракує, що перевірити",
 "skip": false}
Якщо матеріал — не новина (реклама, привітання, розіграш, порожній текст), поверни {"skip": true, "notes": "чому"}."""


def system_prompt(custom_rules: str = "") -> str:
    s = RULES
    if custom_rules.strip():
        s += "\n\nДодаткові правила редакції (пріоритетні):\n" + custom_rules.strip()
    return s


def length_of(text: str) -> int:
    """Кількість символів тіла статті так, як її рахує редакція: Unicode-символи разом із пробілами й переносами."""
    return len(text or "")


def parse_json(text: str) -> dict:
    """Дістає перший JSON-об'єкт із відповіді моделі (моделі інколи додають ```json чи пояснення)."""
    if not text:
        raise ValueError("порожня відповідь моделі")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        raise ValueError("у відповіді немає JSON")
    depth, in_str, escp = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escp:
                escp = False
            elif ch == "\\":
                escp = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1], strict=False)
    raise ValueError("JSON обірвано")


def clean(data: dict) -> dict:
    out = {
        "headline": str(data.get("headline") or "").strip().strip("«»\"").rstrip(".")[:200],
        "body": re.sub(r"\n{3,}", "\n\n", str(data.get("body") or "").strip()),
        "region": str(data.get("region") or "").strip()[:60],
        "category": str(data.get("category") or "").strip().lower()[:30],
        "urgency": str(data.get("urgency") or "normal").strip().lower(),
        "notes": str(data.get("notes") or "").strip()[:1500],
        "skip": bool(data.get("skip")),
    }
    for k in ("confirmed", "unconfirmed", "sources"):
        v = data.get(k) or []
        if isinstance(v, str):
            v = [v]
        out[k] = [str(x).strip()[:300] for x in v if str(x).strip()][:12]
    if out["urgency"] not in ("urgent", "normal", "low"):
        out["urgency"] = "normal"
    return out


def news_prompt(raw: str, meta: dict, target: int = 0, related: str = "") -> str:
    parts = ["Опрацюй вхідний матеріал і підготуй новину для GreenPost.", ""]
    if meta.get("source_name") or meta.get("source_url"):
        parts.append(f"Звідки матеріал: {meta.get('source_name') or ''} {meta.get('source_url') or ''}".strip())
    parts.append(f"Тип надходження: {meta.get('origin', 'manual')}")
    if meta.get("hint"):
        parts.append(f"Підказка редакції щодо джерела: {meta['hint']} (якщо в тексті не вказано інше місце — вважай це областю події)")
    if related:
        parts += ["", "Раніше вже була схожа новина (можливо, це оновлення тієї самої події — тоді так і напиши в лід і в notes):", related[:1500]]
    if target:
        parts += ["", f"Довжина поля body — РІВНО {target} символів разом із пробілами й переносами рядків. "
                      "Це жорстка вимога: бот перевірить довжину програмно."]
    parts += ["", "ВХІДНИЙ МАТЕРІАЛ:", "<<<", raw[:12000], ">>>", "", JSON_SPEC]
    return "\n".join(parts)


def fix_length_prompt(body: str, target: int) -> str:
    n = length_of(body)
    delta = target - n
    how = (f"ДОДАЙ рівно {delta} символів" if delta > 0 else f"СКОРОТИ рівно на {-delta} символів")
    return (f"Ось текст новини. Зараз у ньому {n} символів, потрібно рівно {target} (разом із пробілами й переносами). "
            f"{how}, не змінюючи фактів і не додаючи нових. Додавати можна лише уточнення з уже наявного тексту, "
            "перефразування або з'єднувальні слова. Поверни ЛИШЕ JSON {\"body\": \"…\"}.\n\n<<<\n" + body + "\n>>>")


def revise_prompt(draft: dict, instruction: str, target: int = 0) -> str:
    base = {k: draft.get(k) for k in ("headline", "body", "region", "category", "confirmed", "unconfirmed", "sources", "notes")}
    t = (f"\nДовжина body — РІВНО {target} символів." if target else "")
    instr = instruction.strip() or "Перепиши інакше: інша структура й формулювання, ті самі факти."
    return ("Ось чернетка новини в JSON. Зроби нову версію за вказівкою редактора, зберігаючи фактичну основу. "
            "Нових фактів не додавай." + t + "\n\nВказівка редактора: " + instr + "\n\nЧЕРНЕТКА:\n"
            + json.dumps(base, ensure_ascii=False) + "\n\n" + JSON_SPEC)


REGION_FIRST = ("київ", "kyiv")


def region_key(region: str):
    r = (region or "").lower()
    if any(r.startswith(x) or x in r for x in REGION_FIRST):
        return (0, r)
    if r in ("україна", "", "загальне"):
        return (1, r)
    if r in ("світ", "world"):
        return (3, r)
    return (2, r)


def digest_prompt(drafts: list, target: int) -> str:
    groups: dict = {}
    for d in drafts:
        groups.setdefault(d.get("region") or "Україна", []).append(d)
    lines = []
    for region in sorted(groups, key=region_key):
        lines.append(f"## {region}")
        for d in groups[region]:
            lines.append(f"- [{d['id']}] {d['headline']}: {d['body'][:900]}")
            if d.get("unconfirmed"):
                lines.append("  (не підтверджено: " + "; ".join(d["unconfirmed"][:3]) + ")")
    return (f"Склади зведення новин GreenPost за останню добу. Довжина body — РІВНО {target} символів разом із пробілами "
            "й переносами. Групуй за областями: Київ і Київська область першими, далі за алфавітом; кожна область — "
            "окремий абзац, що починається назвою області й двокрапкою. Загиблих і поранених не рахуй двічі: якщо кілька "
            "матеріалів про ту саму подію — бери найновіше число. Непідтверджене позначай «за неперевіреними даними». "
            "Заголовок — «Головне за добу» плюс найважливіша подія.\n\nМАТЕРІАЛИ:\n" + "\n".join(lines)[:14000]
            + "\n\n" + JSON_SPEC)


def check_prompt(draft: dict, found: str) -> str:
    return ("Звір факти чернетки з результатами вебпошуку. Для кожного ключового твердження напиши один рядок: "
            "✅ підтверджено (джерело) / ⚠️ не знайдено / ❌ суперечить (що саме). В кінці — один рядок висновку. "
            "Не вигадуй джерел: посилайся лише на ті, що є в результатах. Відповідай простим текстом, без Markdown, до 900 символів.\n\n"
            f"ЧЕРНЕТКА:\n{draft['headline']}\n{draft['body'][:2500]}\n\nРЕЗУЛЬТАТИ ПОШУКУ:\n{found[:7000]}")


class Engine:
    def __init__(self, ask_ai, max_fix=5):
        self.ask_ai = ask_ai
        self.max_fix = max_fix

    async def _json(self, prompt, system, label, image=None, avoid=None):
        kw = {"avoid": avoid} if avoid else {}
        text, model = await self.ask_ai(prompt, system=system, label=label, image=image, **kw)
        try:
            return parse_json(text), model
        except (ValueError, json.JSONDecodeError):
            # одна повторна спроба з нагадуванням про формат
            text, model = await self.ask_ai(prompt + "\n\nУВАГА: попередня відповідь не була коректним JSON. Лише JSON!",
                                            system=system, label=label, image=image, **kw)
            return parse_json(text), model

    async def fit_length(self, data: dict, target: int, system: str, label: str):
        """До max_fix спроб довести body до точної довжини. Повертає (data, ok, спроб)."""
        if not target:
            return data, True, 0
        tries = 0
        best = data["body"]
        while length_of(data["body"]) != target and tries < self.max_fix:
            tries += 1
            try:
                fixed, _ = await self._json(fix_length_prompt(data["body"], target), system, label + " · довжина")
            except Exception:  # noqa: BLE001
                break
            body = str(fixed.get("body") or "").strip()
            if body:
                if abs(length_of(body) - target) <= abs(length_of(best) - target):
                    best = body
                data["body"] = body
        data["body"] = best
        return data, length_of(best) == target, tries

    async def write(self, raw, meta, target=0, rules="", related="", image=None):
        system = system_prompt(rules)
        label = "📰 Новинарня: рерайт"
        data, model = await self._json(news_prompt(raw, meta, target, related), system, label, image=image)
        data = clean(data)
        ok, tries = True, 0
        if not data["skip"] and target:
            data, ok, tries = await self.fit_length(data, target, system, label)
        return data, model, ok

    async def revise(self, draft, instruction, target=0, rules=""):
        system = system_prompt(rules)
        data, model = await self._json(revise_prompt(draft, instruction, target), system, "📰 Новинарня: правка")
        data = clean(data)
        ok = True
        if target:
            data, ok, _ = await self.fit_length(data, target, system, "📰 Новинарня: правка")
        return data, model, ok

    async def refit(self, draft, target, rules=""):
        data = {k: draft.get(k) for k in ("headline", "body")}
        data, ok, _ = await self.fit_length(data, target, system_prompt(rules), "📰 Новинарня: довжина")
        return data, ok

    async def digest(self, drafts, target, rules=""):
        system = system_prompt(rules)
        data, model = await self._json(digest_prompt(drafts, target), system, "📰 Новинарня: зведення")
        data = clean(data)
        data["sources"] = [f"#{d['id']} {d['headline'][:60]}" for d in drafts][:12]
        data, ok, _ = await self.fit_length(data, target, system, "📰 Новинарня: зведення")
        return data, model, ok

    async def check(self, draft, search, source_text=""):
        """🔎 Фактчек чернетки по РІЗНИХ ресурсах + дослівна перевірка цитат + унікальність тексту.
        Звіт: рядки ✅/⚠️/❌; «❌» або неперевірена цитата блокують схвалення (див. blocking())."""
        body = draft.get("body") or ""
        first = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0][:200]
        found_parts, hosts, err = [], set(), ""
        for q in dict.fromkeys([draft["headline"][:200], first]):
            if not q:
                continue
            f, e = await search(q)
            if f:
                found_parts.append(f)
                hosts |= {h.removeprefix("www.") for h in re.findall(r"URL: https?://([^/\s]+)", f)}
            err = err or e or ""
        lines = []
        if not found_parts:
            lines.append(f"⚠️ Пошук не дав результатів: {err or 'невідомо'} — факти не звірено")
        else:
            text, model = await self.ask_ai(check_prompt(draft, "\n\n".join(found_parts)),
                                            system="Ти — фактчекер. Пиши стисло українською.", label="📰 Новинарня: звірка")
            lines.append((text or "").strip()[:1500])
            lines.append(f"🌐 Незалежних ресурсів у звірці: {len(hosts)}" + (" — ⚠️ замало, бажано 2+" if len(hosts) < 2 else "")
                         + f" · звіряв: {model}")
        # 💬 цитати — лише дослівно з першоджерела чи знайдених сторінок
        quotes = [q.strip() for q in re.findall(r"[«“\"]([^«»“”\"]{12,300})[»”\"]", body)]
        hay = norm(source_text + " " + " ".join(found_parts))
        for q in quotes[:8]:
            if norm(q) and norm(q) not in hay:
                lines.append(f"❌ Цитату не знайдено в першоджерелі: «{q[:120]}» — перевірте дослівно або приберіть")
        # 📋 унікальність: текст не має бути переписаним першоджерелом
        if source_text:
            ov = overlap(body, source_text)
            if ov > 0.6:
                lines.append(f"📋 {int(ov * 100)}% тексту збігається з першоджерелом — перепишіть своїми словами")
        return "\n".join(lines)


def norm(t):
    return re.sub(r"[^\wа-яіїєґ]+", " ", (t or "").lower()).strip()


def overlap(a, b, n=5):
    """Частка 5-слівних фрагментів тексту a, що трапляються в b (0…1)."""
    wa, wb = norm(a).split(), norm(b).split()
    sa = {tuple(wa[i:i + n]) for i in range(max(0, len(wa) - n + 1))}
    sb = {tuple(wb[i:i + n]) for i in range(max(0, len(wb) - n + 1))}
    return len(sa & sb) / len(sa) if sa else 0.0


def blocking(report):
    """Що в звіті заважає схвалити: суперечності, вигадані цитати, неперевірені факти."""
    out = []
    for ln in (report or "").splitlines():
        if ln.startswith("❌") or "суперечить" in ln.lower() or "факти не звірено" in ln:
            out.append(ln)
    return out
