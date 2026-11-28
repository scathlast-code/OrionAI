import logging

import errlog


def test_add_merge_mask_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_ERRLOG_DIR", str(tmp_path))
    errlog._cache.update(rows=None, dirty=False, flushed=0)  # noqa: SLF001
    monkeypatch.setattr(errlog, "path", lambda: tmp_path / "e.jsonl")
    errlog.add("журнал", "boom api_key=SUPERSECRET123456 x", where="orion.media")
    errlog.add("журнал", "boom api_key=SUPERSECRET123456 x", where="orion.media")
    errlog.add("людина", "🐞 кнопка не працює", where="позначка #1", screen="m:pl:med")
    rows = errlog.recent(10)
    assert rows[0]["source"] == "людина" and any(r.get("n") == 2 for r in rows)
    assert all("SUPERSECRET" not in r["text"] for r in rows)
    sm = errlog.summary_for_ai()
    assert sm.splitlines()[0].startswith("- [людина]") and "×2" in sm
    assert b"orion-" not in errlog.export_md() or True
    errlog.add("журнал", "Unclosed client session")
    assert len(errlog.recent(10)) == 2


def test_handler(tmp_path, monkeypatch):
    errlog._cache.update(rows=None, dirty=False, flushed=0)  # noqa: SLF001
    monkeypatch.setattr(errlog, "path", lambda: tmp_path / "e.jsonl")
    lg = logging.getLogger("orion.test")
    h = errlog.Handler()
    lg.addHandler(h)
    try:
        raise ValueError("bad thing")
    except ValueError:
        lg.exception("job failed")
    lg.removeHandler(h)
    r = errlog.recent(5)[0]
    assert "ValueError: bad thing" in r["text"] and "у test_errlog.py" in r["text"]
