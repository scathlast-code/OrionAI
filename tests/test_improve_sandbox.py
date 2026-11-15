import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import improve
import sandbox


def test_marks_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    a = improve.add(1, "chat", "m:pl:news:D", "кнопка «🔥 Гарячі»", "m:pl:news:Dm:hot", "текст", "незрозуміло")
    improve.add(1, "reply", "", "повідомлення бота", "", "уривок", "")
    improve.set_status(a, "done")
    assert improve.counts() == {"done": 1, "new": 1}
    assert "незрозуміло" in improve.export_md().decode() and "Гарячі" in improve.export_csv().decode("utf-8-sig")
    assert improve.clear_done() == 1


def test_webapp_signature():
    token = "123:ABC"
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42}), "query_id": "x"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    init = urlencode(data)
    assert improve.check_webapp(init, token) == 42
    assert improve.check_webapp(init.replace("42", "43"), token) is None
    assert improve.check_webapp(init, "other") is None


def test_sandbox_chat_never_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    snap = sandbox.snapshot()
    assert snap and "m:home" in snap["screens"]
    text, rows = sandbox.screen("m:home")
    assert text.startswith("🧪") and rows[-1][-1] == ("💡", "imp:s")
    flat = [d for r in rows for _t, d in r]
    assert all(d.startswith(("sbx:", "imp:", "url:")) for d in flat)   # жодна кнопка не веде в справжні дії
    kind, _ = sandbox.press("sbx:a:ok:news")
    assert kind == "toast"
    kind, scr = sandbox.press(next(d for d in flat if d.startswith("sbx:s:")))
    assert kind == "screen" and scr
    assert sandbox.stats()["clicks"] == 2 and not (tmp_path / "plugins").exists()
    sandbox.count("click:<script>")   # сміття не рахується
    assert sandbox.stats()["clicks"] == 2
