import asyncio
import io
import zipfile

import httpx
import pytest

from plugins.stickers import exporter as X


def test_links():
    t = "https://t.me/addstickers/Cats t.me/addemoji/Emo https://t.me/addstickers/cats"
    assert X.parse_links(t) == [("addstickers", "Cats"), ("addemoji", "Emo")]
    assert X.only_links(t) and X.only_links("/merge " + t)
    assert not X.only_links("Поясни, що за пакет https://t.me/addstickers/Cats і чому він популярний у підлітків")


def fake_transport(packs, sizes=100):
    def handler(req: httpx.Request):
        url = str(req.url)
        if url.endswith("/getStickerSet"):
            import json
            name = json.loads(req.content)["name"]
            if name not in packs:
                return httpx.Response(200, json={"ok": False, "description": "STICKERSET_INVALID"})
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "title": name.upper(), "sticker_type": "regular",
                                  "stickers": [{"file_id": f"{name}-{i}", "file_unique_id": f"u{c}", "emoji": "😀"} for i, c in enumerate(packs[name])]}})
        if url.endswith("/getFile"):
            import json
            fid = json.loads(req.content)["file_id"]
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"stickers/{fid}.webp", "file_size": sizes}})
        if "/file/bot" in url:
            fid = url.rsplit("/", 1)[-1][:-5]
            content_id = packs[fid.split("-")[0]][int(fid.split("-")[1])]
            return httpx.Response(200, content=(content_id * sizes).encode()[:sizes])
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    def apply(transport):
        monkeypatch.setattr(X, "client", lambda: httpx.AsyncClient(transport=transport))
    return apply


def test_export_one_dedups_across_packs(tmp_path, patch_client):
    patch_client(fake_transport({"A": ["a", "b"], "B": ["b", "c"]}))
    seen = []

    async def prog(*a):
        seen.append(a)
    paths, rep = asyncio.run(X.export("T", [("addstickers", "A"), ("addstickers", "B"), ("addstickers", "Gone")], tmp_path, "one", progress=prog))
    assert len(paths) == 1 and rep["files"] == 3 and rep["duplicates"] == 1
    assert rep["errors"][0]["pack"] == "Gone"
    names = zipfile.ZipFile(paths[0]).namelist()
    assert "A/manifest.json" in names and "manifest.json" in names and seen


def test_export_each_and_split(tmp_path, patch_client, monkeypatch):
    monkeypatch.setattr(X, "PART_BYTES", 2500)
    patch_client(fake_transport({"A": list("abcdefgh")}, sizes=1000))
    paths, rep = asyncio.run(X.export("T", [("addstickers", "A")], tmp_path, "each"))
    assert len(paths) > 1 and all(p.name.startswith("A_") for p in paths)
    total = sum(len([n for n in zipfile.ZipFile(p).namelist() if n.endswith(".webp")]) for p in paths)
    assert total == 8
    for p in paths:
        assert zipfile.ZipFile(p).testzip() is None


def test_png_copy(tmp_path, monkeypatch):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, "WEBP")
    assert X.to_png(buf.getvalue()).startswith(b"\x89PNG")
    assert X.to_png(b"not image") is None


def test_nothing_downloaded_raises(tmp_path, patch_client):
    patch_client(fake_transport({}))
    with pytest.raises(X.StickerError):
        asyncio.run(X.export("T", [("addstickers", "Nope")], tmp_path, "one"))
