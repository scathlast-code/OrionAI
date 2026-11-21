import asyncio
import shutil
import subprocess

import pytest

from plugins.media import media as M


INFO = {"id": "jNQXAC9IVRw", "formats": [
    {"height": 144, "vcodec": "avc1", "acodec": "none"}, {"height": 360, "vcodec": "avc1", "acodec": "none"},
    {"height": 2160, "vcodec": "vp9", "acodec": "none"},
    {"vcodec": "none", "acodec": "opus", "abr": 129.5}, {"vcodec": "none", "acodec": "mp4a", "abr": 48}]}


def test_only_real_heights_and_bitrates():
    assert M.video_heights(INFO) == [144, 360, 2160]
    rates, best = M.audio_choices(INFO)
    assert best == 129 and rates == [64, 96, 128]  # без 192/320 — оригінал лише ~130
    assert M.video_heights({"formats": [], "height": 360}) == [360]


def test_same_media_detects_wrong_video():
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw&si=x"
    assert M.url_media_id(url) == "jNQXAC9IVRw"
    assert M.same_media(url, {"id": "jNQXAC9IVRw"})
    assert not M.same_media(url, {"id": "dQw4w9WgXcQ", "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert M.same_media("https://www.tiktok.com/@a/video/7312345678901234567", {"id": "7312345678901234567"})
    assert not M.same_media("https://www.tiktok.com/@a/video/7312345678901234567", {"id": "7000000000000000000"})
    assert M.same_media("https://example.com/some", {"id": "anything"})  # невідомий сайт — не заважаємо


def test_aggressor_sites_denied():
    for u in ("https://vk.com/video1_2", "https://rutube.ru/video/1/", "https://ok.ru/video/1"):
        with pytest.raises(M.URLDenied):
            asyncio.run(M.check_url(u))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg")
def test_convert_and_thumb(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    out = asyncio.run(M.convert_final(src, M.Options(fmt="mov"), tmp_path))
    assert out.suffix == ".mov" and out.stat().st_size > 0 and not src.exists()
    th = asyncio.run(M.make_thumb(out, tmp_path))
    assert th and th.read_bytes()[:2] == b"\xff\xd8"
    png = tmp_path / "p.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64", "-frames:v", "1", str(png)], check=True)
    jpg = asyncio.run(M.convert_final(png, M.Options(fmt="jpg"), tmp_path))
    assert jpg.suffix == ".jpg"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg")
def test_audio_render_formats(tmp_path):
    src = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:a", "aac", str(src)], check=True)
    for fmt in ("wav", "flac", "mp3"):
        w = tmp_path / fmt
        w.mkdir()
        outs = asyncio.run(M.render(src, M.Options(mode="audio", fmt=fmt, abr=128), w, False))
        assert outs[0].suffix == "." + fmt, fmt
    assert asyncio.run(M.render(src, M.Options(mode="audio", fmt="orig"), tmp_path, False)) == [src]
