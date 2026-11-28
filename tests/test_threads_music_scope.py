import asyncio
import subprocess

from plugins.media import Plugin


def test_post_music_not_in_photo_or_silent_video(tmp_path):
    p = Plugin.__new__(Plugin)
    assert asyncio.run(p.post_music_in_file(str(tmp_path / "a.jpg"), "X")) is False
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64", str(v)], check=True)
    assert asyncio.run(p.post_music_in_file(str(v), "X")) is False
