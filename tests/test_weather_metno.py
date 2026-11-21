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
