"""Minimal tests for plugins.media.youtube_pot (no network, no Railway)."""
import asyncio
import re
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.media import youtube_pot as yp


def test_bot_check_detection():
    assert yp.is_bot_check("Sign in to confirm you're not a bot")
    assert yp.is_bot_check("ERROR: [youtube] xxx: Sign in to confirm you\'re not a bot")
    assert yp.is_bot_check("po token required")
    assert not yp.is_bot_check("Video unavailable")
    assert not yp.is_bot_check("HTTP Error 404")


def test_yt_args_no_plugins():
    args = yp.yt_args(allow_plugins=False, client_set="mweb,tv")
    assert "--no-plugin-dirs" in args
    assert "--plugin-dirs" not in args
    assert any("player_client=mweb,tv" in a for a in args)


def test_yt_args_with_plugins_flag():
    # Even if plugin dir is missing in this environment, the flag path must not crash
    args = yp.yt_args(allow_plugins=True, client_set="tv,mweb")
    assert "--no-plugin-dirs" not in args
    assert any("player_client=tv,mweb" in a for a in args)


def test_explain_youtube():
    msg = yp.explain_youtube("Sign in to confirm you're not a bot", has_proxy=False)
    assert "Railway" in msg or "дата-центру" in msg or "бот" in msg
    assert "MEDIA_PROXY" in msg or "проксі" in msg.lower()


def test_client_sets_not_empty():
    assert len(yp.YT_CLIENT_SETS) >= 2
    for s in yp.YT_CLIENT_SETS:
        assert "mweb" in s or "tv" in s or "default" in s


def test_heavy_semaphore():
    async def _run():
        async with yp.HeavyJob():
            async with yp.HeavyJob():
                pass  # sequential is fine; concurrency is limited by semaphore value

    asyncio.run(_run())


if __name__ == "__main__":
    test_bot_check_detection()
    test_yt_args_no_plugins()
    test_yt_args_with_plugins_flag()
    test_explain_youtube()
    test_client_sets_not_empty()
    test_heavy_semaphore()
    print("OK: youtube_pot basic tests passed")
