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


def test_brand_tone_only_brand_palette():
    img = Image.new("RGB", (40, 10))
    for x in range(40):
        for y in range(10):
            img.putpixel((x, y), (x * 6, 255 - x * 6, 120))
    out = C.brand_tone(img)
    for x in range(0, 40, 5):
        r, g, b = out.getpixel((x, 5))
        assert g >= r - 2  # зелено-сині відтінки, без червоних і жовтих


def test_card_pdf_has_real_text():
    import fitz
    pdf = C.card_pdf(None, "Заголовок новини", C.DEFAULTS, "ig")
    page = fitz.open(stream=pdf)[0]
    assert "Заголовок новини" in page.get_text() and (page.rect.width, page.rect.height) == (1170, 1540)


def test_stock_parsers():
    from plugins.newsroom import stock
    ov = {"results": [{"url": "https://a/1.jpg", "id": 1, "creator": "Ann", "license": "by", "license_version": "4.0", "width": 2000,
                       "foreign_landing_url": "https://a/p"}, {"url": "https://a/2.jpg", "id": 2, "license": "by-nc", "width": 2000}]}
    got = stock.openverse_items(ov)
    assert len(got) == 1 and got[0]["license"] == "CC BY 4.0"
    assert stock.parse_queries({"queries": ["european bison", ""]}) == ["european bison"]
