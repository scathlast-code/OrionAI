"""🌤 Погода: Україна загалом (сьогодні + 3 дні) і ваші міста. Дані — Open-Meteo (безкоштовно, без ключа).

Щоранку разом із «Темами дня» (той самий час) приходить звіт по Україні й обраних містах.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
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
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


def wmo(code):
    return WMO.get(int(code or 0), ("🌡", "погода"))


def t(v):
    v = round(v or 0)
    return f"+{v}°" if v > 0 else f"{v}°"


def wet(code):
    return int(code or 0) >= 51


class WeatherPlugin(Plugin):
    id = "wx"
    icon = "🌤"
    title = "Погода"
    version = "1.0"
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
            "timezone": "Europe/Kyiv", "forecast_days": 4, "wind_speed_unit": "ms",
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
        head = ("Сьогодні", "Завтра")[i] if i < 2 else WD[d.weekday()].capitalize()
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
            for i in range(1, 4):
                try:
                    de, _ = wmo(d["weather_code"][i])
                    out += f"\n   {self.day_title(d['time'][i], i)}: {de} {t(d['temperature_2m_min'][i])}…{t(d['temperature_2m_max'][i])}"
                except (KeyError, IndexError, TypeError):
                    break
        return out

    async def ua_text(self, days=1):
        data = await self.fetch([(la, lo) for _n, la, lo, _r in UA_POINTS])
        times = (data[0].get("daily") or {}).get("time") or []
        parts = []
        for i in range(min(days, len(times))):
            body = self.ua_day(data, i)
            if body:
                parts.append(f"<b>{self.day_title(times[i], i)}</b>\n{body}")
        return "\n\n".join(parts)

    async def my_text(self, chat, full=False):
        items = self.cities(chat)
        if not items:
            return ""
        data = await self.fetch([(c["lat"], c["lon"]) for c in items])
        return "\n".join(self.city_line(c["name"], f, full) for c, f in zip(items, data))

    async def morning_text(self, chat):
        """Ранковий звіт: Україна сьогодні + наступні 3 дні (згорнуто) і ваші міста."""
        today = await self.ua_text(1)
        later = await self.ua_text(4)
        later = later.split("\n\n", 1)[1] if "\n\n" in later else ""
        text = "🌤 <b>Погода в Україні</b>\n\n" + today
        mine = await self.my_text(chat)
        if mine:
            text += "\n\n📍 <b>Ваші міста</b>\n" + mine
        if later:
            text += "\n\n<b>Наступні 3 дні</b>\n<blockquote expandable>" + later + "</blockquote>"
        return text[:4000]

    async def send_morning(self):
        """Викликається з циклу «Тем дня» одразу після них — тим самим людям."""
        for uid in dict.fromkeys(self.host.admins()):
            if not self.morning_on(uid):
                continue
            try:
                await self.host.notify(uid, await self.morning_text(uid), [[("🌤 Погода", self.cb())]])
            except Exception:  # noqa: BLE001
                log.exception("weather morning")

    # ------------------------------------------------------------ екрани
    async def home(self, chat):
        try:
            ua = await self.ua_text(1)
            mine = await self.my_text(chat)
        except Exception as exc:  # noqa: BLE001
            return (f"🌤 <b>Погода</b>\n\n⚠️ Не вдалося отримати прогноз: {esc(type(exc).__name__)}. Спробуйте за хвилину.",
                    [[("🔄 Оновити прогноз", self.cb())]])
        text = "🌤 <b>Погода · Україна</b>\n\n" + ua
        text += ("\n\n📍 <b>Ваші міста</b>\n" + mine) if mine else "\n\n📍 Додайте свої міста — «➕ Місто»."
        on = self.morning_on(chat)
        rows = [[("📅 3 дні по Україні", self.cb("d3")), ("📍 Мої міста", self.cb("my"))],
                [("➕ Місто", self.cb("add")), (("🌅 Вранці: так" if on else "🌅 Вранці: ні"), self.cb("mo"))],
                [("🔄 Оновити прогноз", self.cb())]]
        return text[:4000] + "\n\n<i>Дані: Open-Meteo</i>", rows

    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else ""
        if a in ("", "ua"):
            return None, await self.home(chat)
        if a == "d3":
            try:
                text = await self.ua_text(4)
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ {type(exc).__name__}", await self.home(chat)
            return None, (("📅 <b>Погода в Україні на 4 дні</b>\n\n" + text)[:4000], [[("« Погода", self.cb())]])
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
        if a == "mo":
            c = self.cfg()
            c.setdefault("morning", {})[str(chat)] = not self.morning_on(chat)
            self.save(c)
            return ("🌅 Вранці надсилатиму" if self.morning_on(chat) else "Вранці не надсилатиму"), await self.home(chat)
        return None, await self.home(chat)

    async def on_text(self, update, ctx, chat, key, data, text):
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
