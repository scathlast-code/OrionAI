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
