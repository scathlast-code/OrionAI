"""🏆 Пріоритети: рейтинг (вище/нижче) і черга роботи, де важливіше не відхиляє менш важливе, а ставить його на паузу.

Ranks — порядок елементів одного виду (розмови, завдання, боти, спільноти). У списку є роздільник «звичайний пріоритет»:
що вище за нього — важливіше за звичайне, що нижче — менш важливе. Нові елементи стоять на роздільнику (оцінка 0).

WorkQueue — обмежена кількість одночасних робіт (наприклад, запитів до ШІ). Якщо всі місця зайняті:
• новий важливіший запит забирає місце в найменш важливого: той стає на паузу й продовжиться сам, коли звільниться місце;
• новий менш важливий — чекає в черзі (за пріоритетом), і про це одразу сповіщається той, хто питав.
Ніщо не відхиляється.
"""
import asyncio
import contextvars
import itertools
import json
import time

MARK = "—"


class Ranks:
    def __init__(self, get, set_, prefix="rank:"):
        self.get, self.set, self.prefix = get, set_, prefix

    def order(self, kind):
        try:
            o = json.loads(self.get(self.prefix + kind) or "[]")
        except ValueError:
            o = []
        return o if MARK in o else o + [MARK]

    def save(self, kind, order):
        self.set(self.prefix + kind, json.dumps(order, ensure_ascii=False))

    def score(self, kind, ref):
        o = self.order(kind)
        ref = str(ref)
        if ref not in o:
            return 0
        i, m = o.index(ref), o.index(MARK)
        return m - i if i < m else -(i - m)

    def arrange(self, kind, refs):
        """Ділить елементи на три частини: важливіші за звичайні (згори вниз), звичайні, менш важливі."""
        refs = [str(r) for r in refs]
        o = self.order(kind)
        m = o.index(MARK)
        above = [r for r in o[:m] if r in refs]
        below = [r for r in o[m + 1:] if r in refs]
        normal = [r for r in refs if r not in above and r not in below]
        return above, normal, below

    def move(self, kind, ref, step):
        """step: up, down, top, bottom, reset (звичайний)."""
        o = self.order(kind)
        ref = str(ref)
        if ref not in o:
            if step in ("up", "top"):
                o.insert(0 if step == "top" else o.index(MARK), ref)
            elif step in ("down", "bottom"):
                o.insert(len(o) if step == "bottom" else o.index(MARK) + 1, ref)
        else:
            i = o.index(ref)
            if step == "reset":
                o.pop(i)
            elif step == "top":
                o.pop(i)
                o.insert(0, ref)
            elif step == "bottom":
                o.pop(i)
                o.append(ref)
            elif step == "up" and i > 0:
                if o[i - 1] == MARK:
                    o.pop(i)  # перший серед менш важливих → звичайний
                else:
                    o[i - 1], o[i] = o[i], o[i - 1]
            elif step == "down" and i < len(o) - 1:
                if o[i + 1] == MARK:
                    o.pop(i)  # останній серед важливіших → звичайний
                else:
                    o[i + 1], o[i] = o[i], o[i + 1]
        self.save(kind, o)

    @staticmethod
    def label(score):
        if score > 0:
            return "🔺"
        if score < 0:
            return "🔻"
        return "▫️"


class Job:
    ids = itertools.count(1)

    def __init__(self, prio, label, on_pause=None, on_wait=None, on_resume=None):
        self.id = next(self.ids)
        self.prio, self.label = prio, label
        self.on_pause, self.on_wait, self.on_resume = on_pause, on_wait, on_resume
        self.task = None
        self.preempted = False
        self.since = time.time()


class WorkQueue:
    def __init__(self, slots=2):
        self.slots = max(1, int(slots))
        self.running = []      # [Job]
        self.waiting = []      # [Job]
        self.cond = asyncio.Condition()
        self.paused_total = 0

    def busy_text(self):
        return ", ".join(j.label for j in sorted(self.running, key=lambda j: -j.prio)) or "—"

    def _next_ok(self, job):
        best = max(self.waiting, key=lambda j: (j.prio, -j.since))
        return best is job and len(self.running) < self.slots

    async def _acquire(self, job, first):
        """Чекає місця. Якщо job важливіший за когось із тих, хто працює, — ставить того на паузу."""
        async with self.cond:
            self.waiting.append(job)
            try:
                if len(self.running) >= self.slots:
                    low = min(self.running, key=lambda j: (j.prio, -j.since))
                    if job.prio > low.prio and low.task and not low.task.done():
                        low.preempted = True
                        low.task.cancel()
                    elif first and job.on_wait:
                        asyncio.ensure_future(job.on_wait(self.busy_text()))
                await self.cond.wait_for(lambda: self._next_ok(job))
            finally:
                self.waiting.remove(job)
            self.running.append(job)

    async def _release(self, job):
        async with self.cond:
            if job in self.running:
                self.running.remove(job)
            self.cond.notify_all()

    async def run(self, prio, label, factory, on_pause=None, on_wait=None, on_resume=None, carry=()):
        """factory() — корутина з роботою; її можна перезапустити після паузи.
        carry — контекстні змінні, які робота змінює і які треба передати назад тому, хто викликав."""
        job = Job(prio, label, on_pause, on_wait, on_resume)
        first = True
        while True:
            await self._acquire(job, first)
            if not first and job.on_resume:
                asyncio.ensure_future(job.on_resume())
            first = False
            ctx = contextvars.copy_context()
            job.preempted = False
            job.task = asyncio.get_running_loop().create_task(factory(), context=ctx)
            try:
                result = await asyncio.shield(job.task)
            except asyncio.CancelledError:
                await self._release(job)
                if job.preempted:  # нас поставили на паузу заради важливішого — чекаємо й повторюємо
                    self.paused_total += 1
                    winner = max(self.waiting, key=lambda j: j.prio, default=None)
                    if job.on_pause:
                        asyncio.ensure_future(job.on_pause(winner.label if winner else "важливіше завдання", ctx))
                    job.since = time.time()
                    continue
                if not job.task.done():  # скасували того, хто чекав на результат (кнопка «Зупинити»)
                    job.task.cancel()
                for var in carry:
                    try:
                        var.set(ctx.get(var))
                    except LookupError:
                        pass
                raise
            except BaseException:
                await self._release(job)
                raise
            await self._release(job)
            for var in carry:
                try:
                    var.set(ctx.get(var))
                except LookupError:
                    pass
            return result
