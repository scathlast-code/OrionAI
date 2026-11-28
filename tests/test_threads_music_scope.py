import asyncio
import subprocess

from plugins.media import Plugin


def test_post_music_not_in_photo_or_silent_video(tmp_path):
    p = Plugin.__new__(Plugin)
    assert asyncio.run(p.post_music_in_file(str(tmp_path / "a.jpg"), "X")) is False
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64", str(v)], check=True)
    assert asyncio.run(p.post_music_in_file(str(v), "X")) is False


def test_ai_risks(tmp_path):
    from plugins.media import related as R
    r = R.parse_risks('{"risks":["ai","ru"],"ai_kinds":["image","text","x"]}')
    assert {"ai", "ru", "ai:image", "ai:text"} <= r and "ai:x" not in r
    assert "створено ШІ (зображення, текст)" in R.risks_line(r)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"\xff\xd8 xmp DigitalSourceType trainedAlgorithmicMedia \xff\xd9")
    assert R.ai_marks_in_file(f)
    g = tmp_path / "b.jpg"
    g.write_bytes(b"\xff\xd8 plain photo \xff\xd9")
    assert not R.ai_marks_in_file(g)
    assert R.ai_marks_in_info({"x": {"aigc_label_type": 1}}) and not R.ai_marks_in_info({"title": "AI talk"})


def test_shazam_windows():
    w = Plugin.shazam_windows
    assert w(10) == [0.0]
    assert w(60)[0] == 0.0 and w(60)[-1] == 40.0 and len(w(60)) == 3
    long = w(600)
    assert len(long) == 6 and long[-1] == 580.0
