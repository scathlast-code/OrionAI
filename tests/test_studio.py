from plugins.media import studio as S


def test_parse_box_and_delogo():
    box, what = S.parse_box('{"box":[0.8,0.02,0.15,0.06],"what":"логотип ТСН"}')
    assert box and what == "логотип ТСН"
    f = S.delogo_filter(box, 1080, 1920)
    assert f.startswith("delogo=x=") and "w=" in f
    assert S.parse_box('{"box":null}')[0] is None
    assert S.parse_box('{"box":[0,0,0.9,0.9]}')[0] is None  # завелике — не водяний знак


def test_parse_clips_and_shift():
    clips = S.parse_clips('{"clips":[{"start":5,"end":40,"title":"a"},{"start":50,"end":52},{"start":60,"end":200}]}', 150)
    assert clips[0] == (5.0, 40.0, "a") and len(clips) == 2 and clips[1][1] - clips[1][0] <= 60
    segs = [(0, 4, "x"), (6, 10, "y"), (38, 45, "z")]
    assert S.shift_segs(segs, 5, 40) == [(1, 5, "y"), (33, 35, "z")]
