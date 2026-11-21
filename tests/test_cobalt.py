import asyncio

import httpx
import pytest

from plugins.media import media as M


def test_cobalt_skips_locked_and_returns_tunnel(monkeypatch, tmp_path):
    monkeypatch.setenv("COBALT_INSTANCES", "https://a.example,https://b.example")
    monkeypatch.delenv("COBALT_API_URL", raising=False)
    M.COBALT_BAD.clear()

    def handler(req):
        if req.url.host == "a.example":
            return httpx.Response(400, json={"status": "error", "error": {"code": "error.api.auth.jwt.missing"}})
        return httpx.Response(200, json={"status": "tunnel", "url": "https://b.example/tunnel?id=1", "filename": "video.mp4"})
    real = httpx.AsyncClient
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))
    items = asyncio.run(M.cobalt_probe("https://youtu.be/x", tmp_path))
    assert items[0].url == "https://b.example/tunnel?id=1"
    assert "https://a.example" in M.COBALT_BAD  # сервер із ключем більше не смикаємо


def test_cobalt_all_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("COBALT_INSTANCES", "https://c.example")
    M.COBALT_BAD.clear()
    real = httpx.AsyncClient
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "error", "error": {"code": "error.api.youtube.login"}})), **kw))
    with pytest.raises(M.JobError):
        asyncio.run(M.cobalt_probe("https://youtu.be/x", tmp_path))
