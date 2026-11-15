"""Резервний сервер копіює не лише основну базу, а й додаткові (плагіни, UTM, статистика)."""
import asyncio, os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_cluster.py")).read().split("async def main():")[0]
exec(src)
def side(node, rel, v):
    p = os.path.join(os.path.dirname(node.db_path), rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p); c.execute("CREATE TABLE IF NOT EXISTS t(v TEXT)"); c.execute("DELETE FROM t"); c.execute("INSERT INTO t VALUES(?)", (v,)); c.commit(); c.close(); return p
def read(node, rel):
    p = os.path.join(os.path.dirname(node.db_path), rel)
    return sqlite3.connect(p).execute("SELECT v FROM t").fetchone()[0] if os.path.exists(p) else None
async def main():
    A, B = nodes["A"], nodes["B"]
    RELS = ["plugins/newsroom/newsroom.db", "greenpost_utm.sqlite3"]
    for n in (A, B):
        n.extras = lambda n=n: [(r, os.path.join(os.path.dirname(n.db_path), r)) for r in RELS if os.path.exists(os.path.join(os.path.dirname(n.db_path), r))]
    side(A, RELS[0], "чернетка на A"); side(A, RELS[1], "мітка на A")
    for _ in range(4):
        await tick("A"); await tick("B")
    got = [read(B, r) for r in RELS]
    print("резерв має додаткові бази:", got)
    assert got == ["чернетка на A", "мітка на A"]
    side(A, RELS[0], "нова чернетка"); A.touch(); await tick("B")
    print("після роботи на A:", read(B, RELS[0])); assert read(B, RELS[0]) == "нова чернетка"
asyncio.run(main())
