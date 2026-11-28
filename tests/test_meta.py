import asyncio

from plugins.newsroom import meta as MT


def test_ready_and_missing(monkeypatch):
    for k in ("META_PAGE_ID", "META_PAGE_TOKEN", "META_IG_ID", "THREADS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert not MT.ready("fb") and MT.missing("fb") == ["META_PAGE_ID", "META_PAGE_TOKEN"]
    monkeypatch.setenv("THREADS_TOKEN", "x")
    assert MT.ready("th")
    ok, msg = asyncio.run(MT.publish("fb", "t"))
    assert not ok and "META_PAGE_ID" in msg


def test_ts():
    assert MT._ts("2026-11-28T10:00:00+0000") > 0 and MT._ts("bad") == 0


def test_publish_threads(monkeypatch):
    monkeypatch.setenv("THREADS_TOKEN", "tok")
    calls = []

    async def fake(method, url, **p):
        calls.append((method, url.rsplit("/", 1)[-1], p.get("media_type")))
        if url.endswith("/threads"):
            return {"id": "c1"}
        if url.endswith("threads_publish"):
            return {"id": "p1"}
        return {"permalink": "https://www.threads.com/@g/post/X"}
    monkeypatch.setattr(MT, "_call", fake)
    ok, link = asyncio.run(MT.publish("th", "Привіт"))
    assert ok and link.endswith("/X") and calls[0][2] == "TEXT"
