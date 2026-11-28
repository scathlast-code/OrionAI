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
