"""Друге незалежне джерело прогнозу — MET Norway (yr.no), щоб звіряти Open-Meteo. Безкоштовно, потрібен лише User-Agent."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

API = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
UA = {"User-Agent": "OrionAI-Weather/1.0 github.com/scathlast-code/OrionAI"}
TOL = 3  # °C — розбіжність, у межах якої прогнози вважаємо узгодженими


def page(lat, lon):
    return f"https://www.yr.no/en/forecast/daily-table/{lat:.2f},{lon:.2f}"


def daily(data, tz="Europe/Kyiv"):
    """JSON MET Norway → {"YYYY-MM-DD": (мін, макс)} за місцевим часом."""
    z = ZoneInfo(tz)
    out = {}
    for s in ((data or {}).get("properties") or {}).get("timeseries") or []:
        try:
            t = dt.datetime.fromisoformat(s["time"].replace("Z", "+00:00")).astimezone(z)
            v = float(s["data"]["instant"]["details"]["air_temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        k = t.date().isoformat()
        lo, hi = out.get(k, (v, v))
        out[k] = (min(lo, v), max(hi, v))
    return out


def agree(om, met):
    """(мін, макс) Open-Meteo проти MET Norway → True, якщо обидва відхилення ≤ TOL."""
    return abs(om[0] - met[0]) <= TOL and abs(om[1] - met[1]) <= TOL


# 🔁 Повний запасний прогноз: коли Open-Meteo не відповідає, беремо MET Norway й переводимо у той самий вигляд
SYMBOL_WMO = [  # початок symbol_code MET Norway → код погоди WMO (як в Open-Meteo)
    ("clearsky", 0), ("fair", 1), ("partlycloudy", 2), ("cloudy", 3), ("fog", 45),
    ("lightrainshowersandthunder", 95), ("rainshowersandthunder", 95), ("heavyrainshowersandthunder", 96),
    ("lightrainandthunder", 95), ("rainandthunder", 95), ("heavyrainandthunder", 96),
    ("lightsnowshowersandthunder", 95), ("snowshowersandthunder", 95), ("heavysnowshowersandthunder", 96),
    ("lightsleetshowersandthunder", 95), ("sleetshowersandthunder", 95), ("heavysleetshowersandthunder", 96),
    ("lightsnowandthunder", 95), ("snowandthunder", 95), ("heavysnowandthunder", 96),
    ("lightsleetandthunder", 95), ("sleetandthunder", 95), ("heavysleetandthunder", 96),
    ("lightrainshowers", 80), ("heavyrainshowers", 82), ("rainshowers", 81),
    ("lightsleetshowers", 85), ("heavysleetshowers", 86), ("sleetshowers", 85),
    ("lightsnowshowers", 85), ("heavysnowshowers", 86), ("snowshowers", 85),
    ("lightrain", 61), ("heavyrain", 65), ("rain", 63), ("lightsleet", 66), ("heavysleet", 67), ("sleet", 66),
    ("lightsnow", 71), ("heavysnow", 75), ("snow", 73),
]


def wmo_of(symbol):
    s = (symbol or "").split("_")[0]
    for name, code in SYMBOL_WMO:
        if s == name:
            return code
    return 3


def to_om(data, tz="Europe/Kyiv", days=7):
    """JSON MET Norway → словник у форматі Open-Meteo (current + daily), який розуміє решта плагіна."""
    z = ZoneInfo(tz)
    ts = ((data or {}).get("properties") or {}).get("timeseries") or []
    if not ts:
        return None
    cur = ts[0]["data"]
    det = cur.get("instant", {}).get("details", {})
    sym = ((cur.get("next_1_hours") or cur.get("next_6_hours") or {}).get("summary") or {}).get("symbol_code")
    current = {"temperature_2m": det.get("air_temperature"), "apparent_temperature": det.get("air_temperature"),
               "weather_code": wmo_of(sym), "wind_speed_10m": det.get("wind_speed"), "relative_humidity_2m": det.get("relative_humidity")}
    by = {}
    for s in ts:
        try:
            t_ = dt.datetime.fromisoformat(s["time"].replace("Z", "+00:00")).astimezone(z)
            d = s["data"]
            v = float(d["instant"]["details"]["air_temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        k = t_.date().isoformat()
        b = by.setdefault(k, {"lo": v, "hi": v, "codes": [], "pr": 0.0, "wind": 0.0, "pp": 0})
        b["lo"], b["hi"] = min(b["lo"], v), max(b["hi"], v)
        b["wind"] = max(b["wind"], float(d["instant"]["details"].get("wind_speed") or 0))
        n6 = d.get("next_6_hours") or {}
        if n6 and t_.hour in (0, 6, 12, 18):
            b["codes"].append(wmo_of((n6.get("summary") or {}).get("symbol_code")))
            amt = float((n6.get("details") or {}).get("precipitation_amount") or 0)
            b["pr"] += amt
            b["pp"] = max(b["pp"], 80 if amt >= 1 else 50 if amt >= 0.3 else 20 if amt > 0 else 0)  # MET не дає ймовірності — оцінка за кількістю
    keys = sorted(by)[:days]
    daily = {"time": keys, "weather_code": [], "temperature_2m_max": [], "temperature_2m_min": [],
             "precipitation_probability_max": [], "precipitation_sum": [], "wind_speed_10m_max": []}
    for k in keys:
        b = by[k]
        codes = b["codes"] or [3]
        daily["weather_code"].append(max(codes, key=lambda c: (codes.count(c), c)))
        daily["temperature_2m_max"].append(round(b["hi"], 1))
        daily["temperature_2m_min"].append(round(b["lo"], 1))
        daily["precipitation_probability_max"].append(b["pp"])
        daily["precipitation_sum"].append(round(b["pr"], 1))
        daily["wind_speed_10m_max"].append(round(b["wind"], 1))
    return {"current": current, "daily": daily, "_src": "metno"}
