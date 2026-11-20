"""🌤 Погода: Україна загалом (3 дні докладно, 5 і 7 — згорнуто) і ваші міста. Дані — Open-Meteo (безкоштовно, без ключа).

Щоранку кожному — у час, який він обрав сам (або ніколи), — приходить звіт по Україні й обраних містах.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time

import httpx

from plugins import Plugin, esc

log = logging.getLogger("orion.weather")

API = "https://api.open-meteo.com/v1/forecast"
GEO = "https://geocoding-api.open-meteo.com/v1/search"

# Обласні центри (крім тимчасово окупованих), згруповані за частинами країни.
REGIONS = {
    "Захід": [("Львів", 49.84, 24.03), ("Ужгород", 48.62, 22.30), ("Івано-Франківськ", 48.92, 24.71), ("Тернопіль", 49.55, 25.59),
              ("Луцьк", 50.75, 25.33), ("Рівне", 50.62, 26.25), ("Чернівці", 48.29, 25.94), ("Хмельницький", 49.42, 26.99)],
    "Північ": [("Київ", 50.45, 30.52), ("Житомир", 50.25, 28.66), ("Чернігів", 51.49, 31.29), ("Суми", 50.91, 34.80)],
    "Центр": [("Вінниця", 49.23, 28.47), ("Черкаси", 49.44, 32.06), ("Кропивницький", 48.51, 32.26), ("Полтава", 49.59, 34.55)],
    "Схід": [("Харків", 49.99, 36.23), ("Дніпро", 48.46, 35.05), ("Запоріжжя", 47.84, 35.14)],
    "Південь": [("Одеса", 46.48, 30.72), ("Миколаїв", 46.97, 32.00), ("Херсон", 46.64, 32.62)],
}
UA_POINTS = [(name, lat, lon, region) for region, cities in REGIONS.items() for name, lat, lon in cities]

# Коди погоди WMO → емодзі й опис.
WMO = {0: ("☀️", "ясно"), 1: ("🌤", "переважно ясно"), 2: ("⛅", "мінлива хмарність"), 3: ("☁️", "хмарно"),
       45: ("🌫", "туман"), 48: ("🌫", "туман із памороззю"), 51: ("🌤", "легка мряка"), 53: ("🌤", "мряка"), 55: ("🌧", "сильна мряка"),
       56: ("🌧", "крижана мряка"), 57: ("🌧", "крижана мряка"), 61: ("🌧", "невеликий дощ"), 63: ("🌧", "дощ"), 65: ("🌧", "сильний дощ"),
       66: ("🌧", "крижаний дощ"), 67: ("🌧", "сильний крижаний дощ"), 71: ("🌨", "невеликий сніг"), 73: ("🌨", "сніг"),
       75: ("❄️", "сильний сніг"), 77: ("🌨", "снігова крупа"), 80: ("🌤", "короткочасний дощ"), 81: ("🌧", "зливи"),
       82: ("⛈", "сильні зливи"), 85: ("🌨", "снігопад"), 86: ("❄️", "сильний снігопад"), 95: ("⛈", "гроза"),
       96: ("⛈", "гроза з градом"), 99: ("⛈", "сильна гроза з градом")}
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
# Джерела: дані — Open-Meteo; інші — щоб людина могла звірити прогноз.
SOURCES = [("Open-Meteo", "https://open-meteo.com/"), ("Укргідрометцентр", "https://meteo.gov.ua/"),
           ("Sinoptik", "https://sinoptik.ua/"), ("Meteoprog", "https://www.meteoprog.com/ua/"), ("Windy", "https://www.windy.com/")]
DEFAULT_TIME = "07:30"
TIMES = ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00", "10:00"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


def wmo(code):
    return WMO.get(int(code or 0), ("🌡", "погода"))


def t(v):
    v = round(v or 0)
    return f"+{v}°C" if v > 0 else f"{v}°C"


def src_rows():
    """Мінікнопки джерел — спільні для всього екрана."""
    b = [(f"🔗 {n}", "url:" + u) for n, u in SOURCES]
    return [b[:3], b[3:]]


def wet(code):
    return int(code or 0) >= 51


class WeatherPlugin(Plugin):
    id = "wx"
    icon = "🌤"
    title = "Погода"
    version = "1.2"
    summary = "Погода в Україні загалом і у ваших містах; щоранку — разом із темами дня."
    VIEWS = {"", "ua", "d3", "my", "set"}

    def __init__(self, host):
        super().__init__(host)
        self.cfg_path = self.dir / "weather.json"
        self._cache: dict = {}

    # ------------------------------------------------------------ налаштування
    def cfg(self) -> dict:
        try:
            return json.loads(self.cfg_path.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def save(self, c):
        self.cfg_path.write_text(json.dumps(c, ensure_ascii=False), "utf-8")

    def cities(self, chat) -> list:
        return (self.cfg().get("cities") or {}).get(str(chat), [])

    def set_cities(self, chat, items):
        c = self.cfg()
        c.setdefault("cities", {})[str(chat)] = items[:12]
        self.save(c)

    def morning_on(self, chat) -> bool:
        return (self.cfg().get("morning") or {}).get(str(chat), True)

    def morning_time(self, chat) -> str:
        return (self.cfg().get("mtime") or {}).get(str(chat)) or DEFAULT_TIME

    def set_morning(self, chat, hm):
        """hm = "ГГ:ХХ" — надсилати о цій порі; None — не надсилати."""
        c = self.cfg()
        c.setdefault("morning", {})[str(chat)] = bool(hm)
        if hm:
            c.setdefault("mtime", {})[str(chat)] = hm
        self.save(c)

    def morning_label(self, chat):
        return f"🌅 Вранці: {self.morning_time(chat)}" if self.morning_on(chat) else "🌅 Вранці: ні"

    def users(self):
        get = self.host.extra.get("users")
        try:
            ids = list(get()) if get else []
        except Exception:  # noqa: BLE001
            ids = []
        return list(dict.fromkeys(list(self.host.admins()) + ids))

    # ------------------------------------------------------------ дані
    async def fetch(self, points):
        """points: [(lat, lon)] → список прогнозів Open-Meteo (поточна погода + 4 дні). Кеш 20 хв."""
        key = tuple((round(a, 2), round(b, 2)) for a, b in points)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 1200:
            return hit[1]
        params = {
            "latitude": ",".join(str(a) for a, _ in key), "longitude": ",".join(str(b) for _, b in key),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            "timezone": "Europe/Kyiv", "forecast_days": 7, "wind_speed_unit": "ms",
        }
        async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
            r = await client.get(API, params=params)
            r.raise_for_status()
            data = r.json()
        data = data if isinstance(data, list) else [data]
        self._cache[key] = (time.time(), data)
        return data

    async def geocode(self, name):
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            r = await client.get(GEO, params={"name": name, "count": 5, "language": "uk", "format": "json"})
            r.raise_for_status()
        return [x for x in (r.json().get("results") or []) if "latitude" in x]

    # ------------------------------------------------------------ тексти
    @staticmethod
    def day_title(iso, i):
        d = dt.date.fromisoformat(iso)
        head = ("Сьогодні", "Завтра")[i] if i < 2 else WD[d.weekday()].capitalize()  # noqa: E501
        return f"{head}, {d.day} {MONTHS[d.month - 1]}"

    def ua_day(self, data, i):
        """Підсумок дня по Україні: діапазон температур, найтепліше/найхолодніше, де опади й сильний вітер."""
        rows = []
        for (name, _la, _lo, region), f in zip(UA_POINTS, data):
            d = f.get("daily") or {}
            try:
                rows.append(dict(name=name, region=region, lo=d["temperature_2m_min"][i], hi=d["temperature_2m_max"][i],
                                 code=d["weather_code"][i], pp=d.get("precipitation_probability_max", [0] * 4)[i] or 0,
                                 wind=d.get("wind_speed_10m_max", [0] * 4)[i] or 0))
            except (KeyError, IndexError, TypeError):
                continue
        if not rows:
            return ""
        lo, hi = min(rows, key=lambda r: r["lo"]), max(rows, key=lambda r: r["hi"])
        lines = [f"🌡 від {t(lo['lo'])} ({lo['name']}) до {t(hi['hi'])} ({hi['name']})"]
        for region in REGIONS:
            part = [r for r in rows if r["region"] == region]
            if not part:
                continue
            codes = [int(r["code"]) for r in part]
            e, _ = wmo(max(set(codes), key=lambda c: (codes.count(c), c)))  # найчастіша погода в цій частині країни
            rlo, rhi = min(r["lo"] for r in part), max(r["hi"] for r in part)
            lines.append(f"{e} <b>{region}</b>: {t(rlo)}…{t(rhi)}")
        rain = [r["name"] for r in rows if wet(r["code"]) and r["pp"] >= 50]
        snow = [r["name"] for r in rows if 71 <= int(r["code"]) <= 86 and int(r["code"]) not in (80, 81, 82)]
        storm = [r["name"] for r in rows if int(r["code"]) >= 95]
        windy = [r["name"] for r in rows if r["wind"] >= 12]
        if storm:
            lines.append("⛈ Гроза: " + ", ".join(storm))
        if snow:
            lines.append("❄️ Сніг: " + ", ".join(snow))
        rain = [n for n in rain if n not in snow and n not in storm]
        if rain:
            lines.append("☔ Опади: " + (", ".join(rain) if len(rain) < len(rows) * 0.7 else "майже по всій країні"))
        elif not snow and not storm:
            lines.append("🌂 Без істотних опадів")
        if windy:
            lines.append("💨 Сильний вітер (12+ м/с): " + ", ".join(windy))
        return "\n".join(lines)

    def ua_compact(self, data, i):
        """Один рядок на день (для 4–7 днів): діапазон по країні й головне явище."""
        lo, hi, wet_n, snow_n, storm_n, n, title = 99, -99, 0, 0, 0, 0, ""
        for f in data:
            d = f.get("daily") or {}
            try:
                lo, hi = min(lo, d["temperature_2m_min"][i]), max(hi, d["temperature_2m_max"][i])
                code, pp = int(d["weather_code"][i]), (d.get("precipitation_probability_max") or [0] * 7)[i] or 0
                title = title or self.day_title(d["time"][i], i)
            except (KeyError, IndexError, TypeError):
                continue
            n += 1
            storm_n += code >= 95
            snow_n += 71 <= code <= 86 and code not in (80, 81, 82)
            wet_n += wet(code) and pp >= 50
        if not n:
            return ""
        part = lambda k: "майже скрізь" if k >= n * 0.7 else "місцями"
        what = (f"⛈ гроза {part(storm_n)}" if storm_n else f"🌨 сніг {part(snow_n)}" if snow_n else
                f"☔ опади {part(wet_n)}" if wet_n else "🌂 без істотних опадів")
        return f"<b>{title}</b>: {t(lo)}…{t(hi)} · {what}"

    def city_line(self, name, f, full=False):
        cur, d = f.get("current") or {}, f.get("daily") or {}
        e, what = wmo(cur.get("weather_code", (d.get("weather_code") or [0])[0]))
        out = f"{e} <b>{esc(name)}</b>: {t(cur.get('temperature_2m'))} ({what})"
        try:
            out += f" · день {t(d['temperature_2m_min'][0])}…{t(d['temperature_2m_max'][0])}"
            if (d.get("precipitation_probability_max") or [0])[0]:
                out += f" · ☂️ {d['precipitation_probability_max'][0]}%"
        except (KeyError, IndexError, TypeError):
            pass
        if full:
            out += f"\n   відчувається як {t(cur.get('apparent_temperature'))} · 💨 {round(cur.get('wind_speed_10m') or 0)} м/с · 💧 {cur.get('relative_humidity_2m', '—')}%"
            for i in range(1, 7):
                try:
                    de, _ = wmo(d["weather_code"][i])
                    out += f"\n   {self.day_title(d['time'][i], i)}: {de} {t(d['temperature_2m_min'][i])}…{t(d['temperature_2m_max'][i])}"
                except (KeyError, IndexError, TypeError):
                    break
        return out

    async def ua_text(self, days=1, start=0):
        data = await self.fetch([(la, lo) for _n, la, lo, _r in UA_POINTS])
        times = (data[0].get("daily") or {}).get("time") or []
        parts = []
        for i in range(start, min(days, len(times))):
            body = self.ua_day(data, i)
            if body:
                parts.append(f"<b>{self.day_title(times[i], i)}</b>\n{body}")
        return "\n\n".join(parts)

    async def ua_week_tail(self, start=3):
        """Дні 4–7 — коротко, по рядку."""
        data = await self.fetch([(la, lo) for _n, la, lo, _r in UA_POINTS])
        n = len((data[0].get("daily") or {}).get("time") or [])
        return "\n".join(x for x in (self.ua_compact(data, i) for i in range(start, n)) if x)

    async def my_text(self, chat, full=False):
        items = self.cities(chat)
        if not items:
            return ""
        data = await self.fetch([(c["lat"], c["lon"]) for c in items])
        return "\n".join(self.city_line(c["name"], f, full) for c, f in zip(items, data))

    async def forecast_text(self, chat=None, today_only=False):
        """3 дні докладно; 5 і 7 днів — згорнуто (розгортаються торканням)."""
        data = await self.fetch([(la, lo) for _n, la, lo, _r in UA_POINTS])
        n = len((data[0].get("daily") or {}).get("time") or [])
        text = await self.ua_text(1 if today_only else 3)
        if today_only:
            mine = await self.my_text(chat) if chat else ""
            if mine:
                text += "\n\n📍 <b>Ваші міста</b>\n" + mine
            nxt = await self.ua_text(3, start=1)
            if nxt:
                text += "\n\n📅 <b>3 дні</b>\n<blockquote expandable>" + nxt + "</blockquote>"
        d5 = "\n".join(x for x in (self.ua_compact(data, i) for i in range(3, min(5, n))) if x)
        d7 = "\n".join(x for x in (self.ua_compact(data, i) for i in range(5, min(7, n))) if x)
        if d5:
            text += "\n🗓 <b>5 днів</b>\n<blockquote expandable>" + d5 + "</blockquote>"
        if d7:
            text += "\n🗓 <b>7 днів</b>\n<blockquote expandable>" + d7 + "</blockquote>"
        return text

    async def morning_text(self, chat):
        """Ранковий звіт: сьогодні докладно й ваші міста; 3, 5 і 7 днів — згорнуто."""
        return ("🌤 <b>Погода в Україні</b>\n\n" + await self.forecast_text(chat, today_only=True))[:4000]

    async def send_morning(self, only=None):
        """Ранковий звіт кожному, хто його хоче, — у його час (only — список, кому саме зараз)."""
        import notify
        with notify.kind("morning"):
            for uid in (only if only is not None else self.users()):
                if not self.morning_on(uid) or not notify.wanted(uid, "morning"):
                    continue
                try:
                    await self.host.notify(uid, await self.morning_text(uid),
                                           [[("📜 Прикмети й звичаї дня", self.cb("lore")), ("🌤 Погода", self.cb())]] + src_rows())
                except Exception:  # noqa: BLE001
                    log.exception("weather morning")

    async def start(self):
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self):
        """Щопівхвилини: кому зараз пора — тому ранковий звіт (раз на день, навіть після перезапуску; до 15 хв запізнення)."""
        await asyncio.sleep(45)
        while True:
            try:
                if self.on():
                    now = dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now()
                    today, c = now.date().isoformat(), self.cfg()
                    sent = c.get("sent") or {}
                    due = []
                    for uid in self.users():
                        if not self.morning_on(uid) or sent.get(str(uid)) == today:
                            continue
                        hh, mm = (int(x) for x in self.morning_time(uid).split(":"))
                        late = (now.hour * 60 + now.minute) - (hh * 60 + mm)
                        if 0 <= late <= 15:
                            due.append(uid)
                    if due:
                        c = self.cfg()
                        c.setdefault("sent", {}).update({str(u): today for u in due})
                        self.save(c)
                        await self.send_morning(due)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("weather loop")
            await asyncio.sleep(30)

    # ------------------------------------------------------------ екрани
    async def home(self, chat):
        try:
            body = await self.forecast_text()
            mine = await self.my_text(chat)
        except Exception as exc:  # noqa: BLE001
            return (f"🌤 <b>Погода</b>\n\n⚠️ Не вдалося отримати прогноз: {esc(type(exc).__name__)}. Спробуйте за хвилину.",
                    [[("🔄 Оновити прогноз", self.cb())]] + src_rows())
        text = "🌤 <b>Погода · Україна · 3 дні</b>\n\n" + body
        text += ("\n\n📍 <b>Ваші міста</b>\n" + mine) if mine else "\n\n📍 Додайте свої міста — «➕ Місто»."
        rows = [[("📜 Прикмети дня", self.cb("lore")), ("📅 Тиждень", self.cb("d3"))],
                [("📍 Мої міста", self.cb("my")), ("➕ Місто", self.cb("add"))],
                [(self.morning_label(chat), self.cb("mt")), ("🔄 Оновити прогноз", self.cb())]] + src_rows()
        return text[:3900] + "\n\n<i>Дані: Open-Meteo · температура в °C</i>", rows

    async def lore(self, day=None):
        """📜 Народний календар дня: назва дня в народі, прикмети про погоду, звичаї й заборони. Кеш на дату (ММ-ДД)."""
        now = dt.datetime.now(self.host.tz) if getattr(self.host, "tz", None) else dt.datetime.now()
        day = day or now.date()
        key = day.strftime("%m-%d")
        c = self.cfg()
        cached = (c.get("lore") or {}).get(key)
        if cached:
            return cached
        if not getattr(self.host, "ask_ai", None):
            return ""
        prompt = (f"Народний календар України на {day.day} {MONTHS[day.month - 1]}. Напиши українською, коротко:\n"
                  "1) як цей день називають у народі й чия пам'ять (одним рядком);\n"
                  "2) 3–5 народних прикмет про погоду цього дня;\n"
                  "3) 2–4 звичаї, обряди чи заборони цього дня;\n"
                  "4) 1–2 приказки, якщо відомі.\n"
                  "Лише справжні, відомі українські народні прикмети й звичаї; нічого не вигадуй — якщо для цього дня мало відомо, "
                  "напиши менше. Без вступів і висновків, рядки з емодзі на початку, без markdown-зірочок.")
        text, _model = await self.host.ask_ai(prompt, label="🌤 Погода: прикмети дня")
        text = re.sub(r"[*_#`]+", "", (text or "").strip())[:2500]
        if text:
            c.setdefault("lore", {})[key] = text
            self.save(c)
        return text

    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else ""
        if a in ("", "ua"):
            return None, await self.home(chat)
        if a == "d3":
            try:
                text = await self.ua_text(7)
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ {type(exc).__name__}", await self.home(chat)
            return None, (("📅 <b>Погода в Україні на тиждень</b>\n\n" + text)[:4000], [[("« Погода", self.cb())]] + src_rows())
        if a == "lore":
            now = dt.datetime.now(self.host.tz) if getattr(self.host, "tz", None) else dt.datetime.now()
            try:
                body = await self.lore(now.date())
            except Exception as exc:  # noqa: BLE001
                body = ""
                log.warning("lore: %s", exc)
            if not body:
                body = "Не вдалося зібрати прикмети — моделі зараз недоступні. Спробуйте трохи згодом."
            head = f"📜 <b>Народний календар · {now.day} {MONTHS[now.month - 1]}</b>\n\n"
            note = "\n\n<i>Зібрано ШІ з відомих народних прикмет; у різних регіонах вони можуть відрізнятися.</i>"
            return None, ((head + esc(body) + note)[:4000], [[("🌤 Погода", self.cb())]])
        if a == "my":
            items = self.cities(chat)
            if not items:
                return None, ("📍 Міст ще немає.", [[("➕ Місто", self.cb("add"))], [("« Погода", self.cb())]])
            try:
                text = await self.my_text(chat, full=True)
            except Exception as exc:  # noqa: BLE001
                text = f"⚠️ Не вдалося отримати прогноз: {esc(type(exc).__name__)}"
            rows = [[(f"🗑 {c['name'][:20]}", self.cb("rm", i))] for i, c in enumerate(items)]
            rows += [[("➕ Місто", self.cb("add")), ("« Погода", self.cb())]]
            return None, (("📍 <b>Мої міста</b>\n\n" + text)[:4000], rows)
        if a == "add":
            self.ask(chat, "city")
            return None, ("✍️ Напишіть назву міста чи села (можна будь-якої країни).", [[("✖️ Скасувати", "m:cancel")]])
        if a == "pick":
            pend = (self.cfg().get("pending") or {}).get(str(chat)) or []
            try:
                c = pend[int(x)]
            except (ValueError, IndexError):
                return "Застаріло", await self.home(chat)
            items = [i for i in self.cities(chat) if i["name"] != c["name"]] + [c]
            self.set_cities(chat, items)
            return f"✅ Додано: {c['name']}", (await self.route(update, ctx, chat, ["my"]))[1]
        if a == "rm":
            items = self.cities(chat)
            try:
                gone = items.pop(int(x))
            except (ValueError, IndexError):
                gone = None
            self.set_cities(chat, items)
            return (f"🗑 {gone['name']}" if gone else None), (await self.route(update, ctx, chat, ["my"]))[1]
        if a in ("mo", "mt"):  # 🌅 свій час ранкового звіту або «не надсилати»
            cur = self.morning_time(chat) if self.morning_on(chat) else ""
            tm = [(("✅ " if h == cur else "") + h, self.cb("mts", h.replace(":", ""))) for h in TIMES]
            rows = [tm[:4], tm[4:], [("⌨️ Свій час", self.cb("mti")), ("✖️ Не надсилати", self.cb("mts", "off"))], [("« Погода", self.cb())]]
            return None, ("🌅 <b>Ранковий прогноз</b>\n\nО котрій щодня надсилати вам погоду? Зараз: "
                          + (f"<b>{cur}</b>" if cur else "<b>не надсилати</b>")
                          + "\n\nГучно чи тихо — у 🎛 Поведінка → 🔔 Сповіщення.", rows)
        if a == "mts":
            if x == "off":
                self.set_morning(chat, None)
                return "Вранці не надсилатиму", await self.home(chat)
            hm = f"{x[:2]}:{x[2:4]}"
            self.set_morning(chat, hm)
            return f"🌅 Щодня о {hm}", await self.home(chat)
        if a == "mti":
            self.ask(chat, "mtime")
            return None, ("⌨️ Напишіть час, напр. <code>06:45</code>, або «-», щоб не надсилати.", [[("✖️ Скасувати", "m:cancel")]])
        return None, await self.home(chat)

    async def on_text(self, update, ctx, chat, key, data, text):
        if key == "mtime":
            v = text.strip()
            if v in ("-", "—", "ні", "0"):
                self.set_morning(chat, None)
                return "Вранці не надсилатиму", await self.home(chat)
            m = re.fullmatch(r"(\d{1,2})[:.\s](\d{2})", v)
            if not m or int(m[1]) > 23 or int(m[2]) > 59:
                self.ask(chat, "mtime")
                return None, ("Не зрозумів час. Напишіть як <code>07:15</code> або «-».", [[("✖️ Скасувати", "m:cancel")]])
            hm = f"{int(m[1]):02d}:{m[2]}"
            self.set_morning(chat, hm)
            return f"🌅 Щодня о {hm}", await self.home(chat)
        if key != "city":
            return None, None
        try:
            found = await self.geocode(text.strip()[:60])
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ {type(exc).__name__}", await self.home(chat)
        if not found:
            self.ask(chat, "city")
            return None, (f"🔎 «{esc(text[:40])}» не знайдено. Спробуйте інакше (напр. «Ужгород» чи «Lviv»).", [[("✖️ Скасувати", "m:cancel")]])
        pend = []
        for f in found[:5]:
            label = ", ".join(x for x in (f.get("name"), f.get("admin1"), f.get("country")) if x)
            pend.append({"name": f.get("name") or label, "label": label, "lat": f["latitude"], "lon": f["longitude"]})
        c = self.cfg()
        c.setdefault("pending", {})[str(chat)] = pend
        self.save(c)
        if len(pend) == 1:
            return await self.route(update, ctx, chat, ["pick", "0"])
        rows = [[(f"📍 {p['label'][:40]}", self.cb("pick", i))] for i, p in enumerate(pend)]
        rows.append([("✖️ Скасувати", self.cb())])
        return None, ("Яке саме?", rows)


Plugin = WeatherPlugin
