import os, sys, sqlite3, json, zipfile, io, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contextlib import closing
from backups import Backups
D = tempfile.mkdtemp(); db = f"{D}/bot.db"
with closing(sqlite3.connect(db)) as c, c:
    c.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT)"); c.execute("CREATE TABLE custom_providers(id TEXT, api_key TEXT)")
    c.execute("INSERT INTO kv VALUES('backup_cfg', ?)", (json.dumps({"days": 3, "tokens": {"gdrive": {"refresh": "RTOKEN-SECRET"}}}),))
    c.execute("INSERT INTO kv VALUES('canva_tokens', 'CANVA-SECRET')"); c.execute("INSERT INTO kv VALUES('note', 'привіт')")
    c.execute("INSERT INTO custom_providers VALUES('x', 'APIKEY-SECRET')")
def kg(k):
    with closing(sqlite3.connect(db)) as c: r = c.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone(); return r[0] if r else None
def ks(k, v):
    with closing(sqlite3.connect(db)) as c, c: c.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
BK = Backups(db, kg, ks, "", secret_kv=[("canva_tokens", None)], secret_cols=[("custom_providers", "api_key")])
name, data = BK.make_format("zip", "full")
raw = zipfile.ZipFile(io.BytesIO(data)).read("data/bot.db")
print("секрети в копії:", [s for s in (b"RTOKEN-SECRET", b"CANVA-SECRET", b"APIKEY-SECRET") if s in raw], "| дані є:", "привіт".encode() in raw,
      "| days збережено:", b'"days": 3' in raw)
print("файли project/:", [n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.startswith("project/data") or "node" in n][:5])
BK.restore(data)
print("після відновлення:", kg("canva_tokens"), json.loads(kg("backup_cfg"))["tokens"], sqlite3.connect(db).execute("SELECT api_key FROM custom_providers").fetchone())

# додаткові бази (плагіни, UTM, статистика): потрапляють у копію і відновлюються
os.makedirs(f"{D}/plugins/newsroom")
for rel, val in (("plugins/newsroom/newsroom.db", "чернетка"), ("plugins/newsroom/utm_tracking.sqlite", "клік"), ("greenpost_utm.sqlite3", "мітка")):
    with closing(sqlite3.connect(f"{D}/{rel}")) as c, c:
        c.execute("CREATE TABLE t(v TEXT)"); c.execute("INSERT INTO t VALUES(?)", (val,))
BK.extra_dbs = BK.scan_extras
name, data = BK.make_format("zip", "data")
names = sorted(n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.startswith("data/"))
print("у копії:", names)
for rel in ("plugins/newsroom/newsroom.db", "greenpost_utm.sqlite3"):
    with closing(sqlite3.connect(f"{D}/{rel}")) as c, c: c.execute("UPDATE t SET v='зіпсовано'")
os.remove(f"{D}/plugins/newsroom/utm_tracking.sqlite")
BK.restore(data)
got = {rel: sqlite3.connect(f"{D}/{rel}").execute("SELECT v FROM t").fetchone()[0] for rel in ("plugins/newsroom/newsroom.db", "plugins/newsroom/utm_tracking.sqlite", "greenpost_utm.sqlite3")}
print("відновлено:", sorted(BK.last_extras), got)
assert got == {"plugins/newsroom/newsroom.db": "чернетка", "plugins/newsroom/utm_tracking.sqlite": "клік", "greenpost_utm.sqlite3": "мітка"}
# шлях за межі теки даних ігнорується
evil = io.BytesIO()
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("data/bot.db", zipfile.ZipFile(io.BytesIO(data)).read("data/bot.db")); z.writestr("data/../../evil.db", zipfile.ZipFile(io.BytesIO(data)).read("data/greenpost_utm.sqlite3"))
print("небезпечний шлях:", BK._extras_from_container(evil.getvalue()))
assert BK._extras_from_container(evil.getvalue()) == {}
