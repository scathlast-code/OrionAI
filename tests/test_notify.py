import sqlite3

import notify
from backups import Backups


def test_modes_cycle_and_apply():
    store = {}
    notify.setup(lambda c: store.get(c, {}), lambda c, v: store.__setitem__(c, v))
    assert notify.mode(5, "backup") == "silent"
    assert notify.mode(5, "morning") == "silent"  # ранкове зведення — тихо за замовчуванням
    assert notify.cycle(5, "morning") == "off"
    assert notify.cycle(5, "morning") == "loud"
    assert notify.cycle(5, "morning") == "silent"
    assert notify.cycle(5, "tasks") == "silent"
    assert notify.cycle(5, "tasks") == "loud"  # не вимикається
    with notify.kind("tasks"):
        notify.cycle(5, "tasks")
        assert notify.apply(5, {}, True) == {"disable_notification": True}
        assert notify.apply(5, {}, False) == {}
    assert notify.apply(5, {}, True) == {}  # «інше» гучно


def test_fingerprint_ignores_volatile(tmp_path):
    db = tmp_path / "b.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT); CREATE TABLE usage(a); CREATE TABLE notes(t);")
    con.commit()
    bm = Backups.__new__(Backups)
    bm.db_path, bm.extra_dbs = str(db), None
    a = bm.fingerprint()
    con.execute("INSERT INTO kv VALUES('lifeboat_t','1')"); con.execute("INSERT INTO usage VALUES(1)"); con.commit()
    assert bm.fingerprint() == a
    con.execute("INSERT INTO notes VALUES('x')"); con.commit()
    assert bm.fingerprint() != a
