import io

from PIL import Image

from plugins.newsroom import extras as E
from plugins.newsroom import tools as T


def test_translit_official():
    assert E.translit("Згорани") == "zghorany"
    assert E.translit("Юрій Єрмак") == "yurii yermak"
    assert E.slugify("У Карпатах відновили популяцію зубрів!") == "u-karpatakh-vidnovyly-populiatsiiu-zubriv"


def test_poll_limits():
    assert E.poll_ok({"q": "Так?", "options": ["так"]}) is None
    q = E.poll_ok({"q": "Скільки?", "options": ["1", "2", "3"], "correct": 1, "explanation": "бо так"}, quiz=True)
    assert q["correct"] == 1 and len(q["options"]) == 3
    assert E.poll_ok({"q": "x", "options": ["a", "b"], "correct": 5}, quiz=True) is None


def test_srt_and_span():
    srt = T.to_srt([(0.0, 3.5, "Перша фраза"), (3.5, 6.0, "Друга")])
    assert "00:00:00,000 --> 00:00:03,500" in srt and "Друга" in srt
    assert T.parse_span("0:10-0:45") == (10, 45) and T.parse_span("1:05 – 1:30") == (65, 90) and T.parse_span("45-10") is None


def test_photo_facts_and_hash():
    b = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 200, 30)).save(b, "JPEG")
    f = T.photo_facts(b.getvalue())
    assert f["size"] == (64, 48) and len(f["ahash"]) == 64
    assert T.hamming(f["ahash"], f["ahash"]) == 0
