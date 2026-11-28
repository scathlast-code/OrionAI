import io

from PIL import Image

from plugins.newsroom import cards as C


def test_render_sizes_and_no_photo():
    b = io.BytesIO()
    Image.new("RGB", (1600, 900), (120, 90, 60)).save(b, "JPEG")
    for k, (_, wh, _) in C.SIZES.items():
        img = Image.open(io.BytesIO(C.render(b.getvalue(), "Дуже довгий заголовок " * 8, {**C.DEFAULTS, "brand": "GP"}, k, "Погода")))
        assert img.size == wh
    img = Image.open(io.BytesIO(C.render(None, "Без фото", {**C.DEFAULTS, "pos": "top", "upper": True}, "sq")))
    assert img.size == (1080, 1080)


def test_og_image():
    html = '<meta property="og:image" content="https://site.ua/a.jpg?x=1&amp;y=2">'
    assert C.og_image(html) == "https://site.ua/a.jpg?x=1&y=2"
    assert C.og_image("<p>нема</p>") is None
