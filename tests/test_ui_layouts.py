import tempfile
from pathlib import Path
import pytest
import ui_layouts as u

def test_layout_roundtrip():
 with tempfile.TemporaryDirectory() as d:
  p=str(Path(d)/'x.db'); assert u.read('orion',p)['revision']==0
  assert u.save('orion',u.DEFAULT,0,p)['revision']==1
  assert u.read('orion',p)['revision']==1
  assert u.read('manager',p)['revision']==0
  with pytest.raises(ValueError): u.save('orion',u.DEFAULT,0,p)

def test_reject_untrusted_action():
 bad={'blocks':[{'id':'x','title':'Bad','visible':True,'buttons':[{'id':'y','title':'Bad','action':'https://evil.test','visible':True}]}]}
 with pytest.raises(ValueError):u.validate(bad)

def test_reject_duplicate_ids():
 b={'blocks':[u.DEFAULT['blocks'][0],u.DEFAULT['blocks'][0]]}
 with pytest.raises(ValueError):u.validate(b)
