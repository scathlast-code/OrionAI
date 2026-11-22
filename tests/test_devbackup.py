import devbackup as D


def test_snapshot_per_section_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_DEVBACKUP_DIR", str(tmp_path / "bk"))
    root = tmp_path / "app"
    (root / "plugins" / "weather").mkdir(parents=True)
    (root / "miniapp").mkdir()
    (root / "bot.py").write_text("v1")
    (root / "miniapp.py").write_text("m1")
    (root / "plugins" / "weather" / "__init__.py").write_text("w1")
    monkeypatch.setattr(D, "ROOT", root)
    first = D.snapshot()
    assert len(first) >= 3
    assert D.snapshot() == []  # нічого не змінилось — нових знімків немає
    (root / "plugins" / "weather" / "__init__.py").write_text("w2-broken")
    again = D.snapshot()
    assert again[0] == "🌤 Погода" and again[1:] == [D.sections()["full"][0]]  # змінений розділ + повний знімок поряд
    old = D.index()["pl_weather"][0]["file"]
    assert D.diff_names("pl_weather", old) == ["plugins/weather/__init__.py"]
    D.restore("pl_weather", old)
    assert (root / "plugins" / "weather" / "__init__.py").read_text() == "w1"
    assert (root / "bot.py").read_text() == "v1"
