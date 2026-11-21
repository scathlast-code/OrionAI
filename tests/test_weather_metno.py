from plugins.weather import REGIONS, WeatherPlugin, metno
from footnotes import Notes


class H:
    tz = None
    def __getattr__(self, k):
        return None


def test_met_cross_check():
    p = WeatherPlugin.__new__(WeatherPlugin)
    data = []
    for cities in REGIONS.values():
        for _n, _la, _lo in cities:
            data.append({"daily": {"time": ["2026-11-21"], "temperature_2m_min": [1], "temperature_2m_max": [5],
                                   "weather_code": [3], "precipitation_probability_max": [0], "wind_speed_10m_max": [3]}})
    (n1, la1, lo1), (n2, la2, lo2) = REGIONS["Захід"][0], REGIONS["Північ"][0]
    p._met = {(la1, lo1): {"2026-11-21": (1.5, 6.0)}, (la2, lo2): {"2026-11-21": (-6.0, -1.0)}}
    txt = p.ua_day(data, 0, Notes())
    assert "MET Norway</a> ✅" in txt and "⚠️" in txt and "−" not in txt and "-6°C…-1°C" in txt
    assert txt.count("MET Norway") == 2  # де даних немає — нічого не пишемо
    assert metno.page(50.45, 30.52).startswith("https://www.yr.no/")


def test_fact_questions_trigger_search():
    import websearch as w
    assert w.wants_search("Коли відбулася битва під Крутами?") and w.wants_search("що сказав мер учора про воду")
    assert not w.wants_search("Привіт") and not w.wants_search("напиши вірш про осінь") and not w.wants_search("переклади hello world")


def test_metno_full_fallback_format():
    from plugins.weather import metno
    ts = []
    for h in range(0, 48, 6):
        ts.append({"time": f"2026-11-{21 + h // 24:02d}T{h % 24:02d}:00:00Z",
                   "data": {"instant": {"details": {"air_temperature": -2 + h / 12, "wind_speed": 5, "relative_humidity": 80}},
                            "next_6_hours": {"summary": {"symbol_code": "lightsnow" if h < 24 else "cloudy"},
                                             "details": {"precipitation_amount": 1.2 if h < 24 else 0}}}})
    f = metno.to_om({"properties": {"timeseries": ts}}, "UTC")
    d = f["daily"]
    assert f["_src"] == "metno" and d["time"][0] == "2026-11-21"
    assert d["weather_code"][0] == 71 and d["precipitation_probability_max"][0] == 80
    assert d["temperature_2m_min"][0] <= d["temperature_2m_max"][0]
    assert f["current"]["weather_code"] == 71
