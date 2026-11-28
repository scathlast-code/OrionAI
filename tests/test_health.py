import health


def test_threshold_and_recovery():
    sent = []
    store = {}
    health.setup(None, store.get, store.__setitem__)
    health._send = lambda t: sent.append(t)  # noqa: SLF001
    health._fails.clear(); health._alerted.clear()
    health.fail("shazam", "x"); health.fail("shazam", "x")
    assert not sent
    health.fail("shazam", "x")
    assert sent and "Shazam" in sent[-1]
    health.fail("shazam", "x")
    assert len(sent) == 1  # тиша 6 год
    health.ok("shazam")
    assert "знову працює" in sent[-1]


def test_quota_immediate():
    sent = []
    store = {}
    health.setup(None, store.get, store.__setitem__)
    health._send = lambda t: sent.append(t)  # noqa: SLF001
    health._fails.clear(); health._alerted.clear()
    health.fail("stt", "gemini_stt: HTTP 429 You exceeded your current quota")
    assert sent and "ліміт" in sent[0] and "GROQ_API_KEY" in sent[0]


def test_disk_report(tmp_path):
    db = tmp_path / "b.db"
    db.write_bytes(b"x" * 10)
    pct, used, total, size, folder = health.disk_report(str(db))
    assert 0 <= pct <= 100 and size == 10


def test_timed_even_split(monkeypatch):
    import asyncio
    import media

    async def tr(data, filename, language=None):
        return "Перше речення. Друге довше речення!", "x", []
    async def secs(data, filename):
        return 10.0
    monkeypatch.setattr(media, "transcribe", tr)
    monkeypatch.setattr(media, "_audio_seconds", secs)
    for k in ("groq_stt", "gemini_stt", "openrouter_stt"):
        monkeypatch.setitem(media.STT_PROVIDERS, k, {"title": k, "ready": lambda: False})
    segs, prov = asyncio.run(media.transcribe_timed(b"x", "a.mp3"))
    assert len(segs) == 2 and segs[0][0] == 0 and abs(segs[-1][1] - 10) < 0.01
