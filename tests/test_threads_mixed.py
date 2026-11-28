from plugins.media import media as M

CDN = "https://scontent-sjc6-1.cdninstagram.com/v/t51.82787-15/"
HTML = (f'<html><body><div><div><video src="https://scontent.cdninstagram.com/o1/v/t2/f2/abc.mp4"></video>'
        f'<img src="{CDN}POSTER_n.jpg?stp=1"></div></div><div><img src="{CDN}P1_n.jpg?x=1"></div>'
        f'<div><img src="{CDN}P2_n.jpg?x=1"></div><img src="https://scontent.cdninstagram.com/v/t51.82787-19/AV_n.jpg"></body></html>')


def test_mixed_keeps_photos_without_poster():
    vids = [x for x in M._embed_media(HTML, "https://www.threads.com/") if x[1] == ".mp4"]
    posters = M._video_posters(HTML)
    photos = [x for x in M._raw_post_images(HTML) if M._img_key(x[0]) not in posters]
    out = M._in_page_order(vids + photos, HTML)
    assert [x[1] for x in out] == [".mp4", ".jpg", ".jpg"]
    assert all("POSTER" not in u and "AV_" not in u for u, _ in out)


def test_embed_place():
    assert M._embed_place('<html><div><span>Харків • Kharkiv • Харьков</span></div></html>') == "Харків"
    assert M._embed_place('x "location":{"pk":1,"name":"Kyiv, Ukraine"} y') == "Kyiv, Ukraine"
    assert M._embed_place('<p>Сьогодні • гарно</p><p>Ранок • Вечір</p>') == ""


def test_ai_prompt_seen():
    from plugins.media import related as R
    assert "кадр" in R.ai_prompt("Саржин яр", "", [], [], seen=True)
    assert "кадр" not in R.ai_prompt("Саржин яр", "", [], [])


def test_about_and_collage():
    from plugins.media import related as R
    from plugins.media import Plugin
    import io
    from PIL import Image
    assert R.parse_about('{"topics":[],"about":"Відео з качками в Саржиному яру"}') == "Відео з качками в Саржиному яру"
    assert R.parse_about("нема") == ""
    blobs = []
    for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
        b = io.BytesIO(); Image.new("RGB", (800, 600), c).save(b, "JPEG"); blobs.append(b.getvalue())
    assert Plugin._collage(blobs) and Plugin._collage([b"x"]) is None
    p = R.ai_prompt("Саржин яр", "", [], [], seen=True, nimg=3, notes="Музика в дописі: X", site="Threads")
    assert "сітку з 3" in p and "Музика" in p


def test_risks():
    from plugins.media import related as R
    assert R.swear_hits("ну це пиздець, бля") == 2
    assert R.swear_hits("сукня, ребус, хула, страхування, бляшанка, наїбати") <= 1
    assert R.swear_hits("сукня, ребус, хула, страхування, бляшанка") == 0
    assert R.parse_risks('{"risks":["ru","music","x"]}') == {"ru", "music"}
    assert "авторські" in R.risks_line({"music"}, "X — Y") and R.risks_line(set()) == ""
