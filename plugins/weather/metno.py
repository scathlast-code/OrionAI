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
