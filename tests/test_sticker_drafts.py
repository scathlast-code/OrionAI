import tempfile
import zipfile
from pathlib import Path
import sticker_drafts as sd

def test_draft_flow(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        monkeypatch.setattr(sd, 'ROOT', Path(folder)/'store')
        source = Path(folder)/'source.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('pack/0001.webp', b'one')
            z.writestr('pack/0002.webp', b'two')
        manifest={'packs':[{'name':'pack','items':[{'file':'pack/0001.webp'}, {'file':'pack/0002.webp'}]}]}
        ident=sd.create(123,source,manifest,'all')
        assert sd.get(123,ident)['status']=='editing'
        sd.select(123,ident,[2],False)
        assert '▫️' in sd.overview(123,ident)
        try:
            sd.get(999,ident)
            assert False
        except ValueError:
            pass
        sd.change_status(123,ident,'submit')
        output=Path(folder)/'selected.zip'
        assert sd.render(123,ident,output)==1
        with zipfile.ZipFile(output) as z:
            assert 'pack/0001.webp' in z.namelist()
            assert 'pack/0002.webp' not in z.namelist()
        sd.change_status(123,ident,'reject')
        sd.select(123,ident,[2],True)
        sd.change_status(123,ident,'submit')
        sd.change_status(123,ident,'approve')
        assert sd.get(123,ident)['status']=='approved'
