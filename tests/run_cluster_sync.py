"""Правило копіювання: копія через 5 хв після роботи бота; якщо 9 хв тиші — сервери «сплять»: ні перевірок, ні копій до наступної роботи."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_cluster.py")).read().split("async def main():")[0].replace('CLUSTER_SYNC="0"', 'CLUSTER_SYNC="300"')
exec(src)
CALLS = []
_h = Net.handle_async_request
async def counting(self, req):
    CALLS.append(req.url.path); return await _h(self, req)
Net.handle_async_request = counting
async def main():
    for _ in range(4):
        await tick("A"); await tick("B")
    A, B = nodes["A"], nodes["B"]
    pulls = lambda: len([e for e in B.events if "скопіював" in e[1]])
    def shift(sec):  # «минуло sec секунд»
        A.activity -= sec; B.peer_activity -= sec; B.last_check -= sec; A.last_check -= sec
        for k in A.pending: A.pending[k] -= sec
    base = pulls()
    A.touch(); await tick("B"); print("робота щойно → копії ще немає:", pulls() - base)
    shift(299); await tick("B"); print("минуло 4:59 → ще ні:", pulls() - base)
    shift(2); await tick("B"); print("минуло 5:01 → копія:", pulls() - base)
    shift(200); await tick("B"); print("8:21 без роботи → нової копії немає:", pulls() - base)
    shift(60); CALLS.clear()
    for _ in range(5): await tick("A"); await tick("B")
    print("9+ хв тиші → сервери сплять, запитів між ними:", len(CALLS), "| копій:", pulls() - base)
    shift(1800); CALLS.clear(); await tick("B"); print("30 хв сну → контрольної перевірки ще немає:", len(CALLS))
    shift(1900); CALLS.clear(); await tick("B"); print("понад годину → одна контрольна перевірка:", CALLS)
    CALLS.clear(); A.touch(); await asyncio.sleep(0.05); print("бот знову працює → A будить резерв:", CALLS)
    await tick("B"); print("резерв знову стежить:", "/cluster/status" in CALLS, "| копія буде через 5 хв, зараз:", pulls() - base)
    shift(301); await tick("B"); print("через 5 хв → копія:", pulls() - base)
asyncio.run(main())
