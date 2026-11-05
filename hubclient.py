"""hubclient — підключення будь-якого вашого бота до 🧭 головного бота. Один файл, без залежностей, крім httpx.

1) Покладіть файл поруч зі своїм ботом.
2) Задайте змінні:  HUB_URL = https://<адреса головного бота>/<MANAGER_SECRET>
                    HUB_KEY = ключ із картки бота в головному боті («🔌 Підключення»)
3) Користуйтеся:

    from hubclient import hub
    await hub.alert("🔴 Сайт не відповідає")        # тривога — завжди зі сповіщенням
    await hub.info("Звіт: 42 нових підписники")       # інформація
    await hub.report("Щоденний звіт …")
    hub.start_heartbeat(cmd_url="https://мій-бот/cmd")  # «я живий» раз на 5 хв (+ адреса, куди слати команди)

    # приймати команди від головного бота (наприклад, у FastAPI/Starlette):
    @app.post("/cmd")
    async def cmd(request):
        body = await request.json()
        if not hub.check(body):                     # перевіряє ключ
            return JSONResponse({"ok": False}, status_code=403)
        answer = do_something(body["text"])
        return JSONResponse({"ok": True, "reply": answer})  # відповідь з'явиться у «📥 Вхідних»

Пріоритети й черга (якщо бот зайнятий):
    • Головний бот передає в команді поле "prio" — наскільки важливий той, хто питає (більше — важливіше).
    • Якщо ваш бот зараз виконує важливіше, НЕ відмовляйте: відповідайте одразу
        {"ok": true, "queued": true, "keep_waiting": true, "do_not_retry": true, "reply": "⏸ У черзі…"}
      а коли виконаєте — надішліть результат: await hub.report("✅ Відповідь: …").
    • Якщо у відповідь ВАМ прийшло queued (hub.is_queued(відповідь)) — не скасовуйте й не повторюйте запит:
      просто чекайте, результат прийде окремим повідомленням.

Без Python: curl -X POST $HUB_URL/in -H 'Content-Type: application/json' -d '{"key":"…","type":"alert","text":"…"}'
"""

import asyncio
import hmac
import logging
import os

import httpx

log = logging.getLogger("hubclient")


class Hub:
    def __init__(self, url=None, key=None):
        self.configure(url or os.environ.get("HUB_URL", ""), key or os.environ.get("HUB_KEY", ""))
        self._hb = None
        self.peer_plan = None  # план сну головного бота (останній, що він прислав)
        self.on_plan = None    # fn(plan) — зберегти план головного, коли він прийде у відповіді
        self.held = []         # звичайні повідомлення, відкладені, поки головний спить

    def configure(self, url, key):
        """Змінити адресу/ключ головного бота на ходу (наприклад, з меню бота)."""
        self.url = (url or "").strip().rstrip("/")
        for tail in ("/in", "/hb"):  # можна вставити адресу як є, з /in у кінці
            if self.url.endswith(tail):
                self.url = self.url[: -len(tail)]
        self.key = (key or "").strip()

    @property
    def ready(self):
        return bool(self.url and self.key)

    def peer_awake(self, now=None):
        """Чи не спить зараз головний бот (за його планом). Без плану — вважаємо, що не спить."""
        st, _ = plan_state(self.peer_plan, now=now)
        return st != "asleep"

    async def send(self, text, type="info", hop=0, wake=False):
        """Надіслати подію. Поки головний бот спить за своїм планом, звичайні повідомлення відкладаються й підуть,
        щойно він прокинеться; тривоги (alert) і wake=True — будять його одразу. Помилки не валять ваш бот."""
        if not self.ready:
            return False
        if type not in ("alert", "plan") and not wake and not self.peer_awake():
            self.held.append((text, type, hop))
            del self.held[:-50]
            return True
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self.url}/in", json={"key": self.key, "type": type, "text": str(text)[:4000], "hop": hop})
            data = r.json() if r.status_code == 200 else {}
            if data.get("plan") and self.on_plan:
                self.on_plan(data["plan"])
            return r.status_code == 200 and data.get("ok", False)
        except Exception as e:  # noqa: BLE001
            log.warning("головний бот недоступний: %s", e)
            return False

    async def flush_held(self, force=False):
        """Віддати відкладене, коли головний прокинувся (force — щойно розбудили вручну)."""
        if not self.held or (not force and not self.peer_awake()):
            return 0
        items, self.held = self.held, []
        for text, type_, hop in items:
            await self.send(text, type_, hop, wake=True)
        return len(items)

    async def plan(self, plan):
        """Повідомити головному свій план сну/пробуджень (лише коли він змінився). Відповідь містить план головного."""
        if not self.ready:
            return False
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{self.url}/in", json={"key": self.key, "type": "plan", "plan": plan, "text": plan_text(plan)})
            data = r.json() if r.status_code == 200 else {}
            if data.get("plan") and self.on_plan:
                self.on_plan(data["plan"])
            return r.status_code == 200
        except Exception as e:  # noqa: BLE001
            log.warning("план не передано: %s", e)
            return False

    async def wake_peer(self):
        """Різко розбудити головного бота (його адреса пробудження — з його плану)."""
        url = (self.peer_plan or {}).get("wake_url")
        if not url:
            return False
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(url)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def sleeping(self, until=None, reason=""):
        """Перед сном: «не перевіряй мене до until (unix) / до наступного мого сигналу». Головний бот не здійме тривогу."""
        if not self.ready:
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self.url}/in", json={"key": self.key, "type": "sleep", "until": int(until) if until else 0,
                                                         "text": reason or "😴 засинаю"})
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def alert(self, text):
        return await self.send(text, "alert")

    async def info(self, text):
        return await self.send(text, "info")

    async def report(self, text):
        return await self.send(text, "report")

    async def heartbeat(self, cmd_url=None):
        if not self.ready:
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self.url}/hb", json={"key": self.key, **({"cmd_url": cmd_url} if cmd_url else {})})
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def start_heartbeat(self, cmd_url=None, every=300):
        """Фоновий сигнал «я живий». Викликайте, коли вже працює цикл asyncio."""
        async def loop():
            while True:
                await self.heartbeat(cmd_url)
                await asyncio.sleep(every)

        if self.ready and not self._hb:
            self._hb = asyncio.create_task(loop())
        return self._hb

    @staticmethod
    def is_queued(reply):
        """Відповідь «у черзі»: це не відмова. Чекайте результату, не повторюйте й не скасовуйте запит."""
        return isinstance(reply, dict) and bool(reply.get("queued") or reply.get("keep_waiting"))

    @staticmethod
    def queued_reply(text="⏸ Зараз виконую важливіше завдання. Запит у черзі — відповім окремим повідомленням."):
        """Що відповісти головному боту, коли ваш бот зайнятий важливішим (див. вище)."""
        return {"ok": True, "queued": True, "keep_waiting": True, "do_not_retry": True, "do_not_cancel": True, "reply": text}

    def check(self, body):
        """Чи справді команда прийшла від головного бота (ключ збігається)."""
        return bool(self.key) and hmac.compare_digest(str(body.get("key", "")), self.key)

    # ---- розклад від головного бота
    schedule = {}

    def take_schedule(self, body):
        """Викличте у своєму /cmd: якщо прийшов новий розклад — запам'ятає й поверне True."""
        if body.get("type") == "schedule" and isinstance(body.get("schedule"), dict):
            self.schedule = body["schedule"]
            return True
        return False

    def should_run(self, last_run, default_every=300):
        """Чи пора вашому боту робити свою перевірку (з урахуванням розкладу й пауз від головного бота)."""
        s = dict(self.schedule) or {"every": default_every}
        return schedule_due(s, last_run)

    def send_sync(self, text, type="info"):
        """Для звичайних (не async) скриптів."""
        if not self.ready:
            return False
        try:
            r = httpx.post(f"{self.url}/in", json={"key": self.key, "type": type, "text": str(text)[:4000]}, timeout=15)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------- розклад (спільний для головного бота й ваших ботів)
# Розклад — простий словник, який головний бот надсилає вашому боту (або зберігає для своїх перевірок):
#   {"every": 300}                      — кожні N секунд
#   {"daily": "09:00"}                  — раз на день о 09:00
#   {"weekly": [0, "09:00"]}            — раз на тиждень (0 — понеділок … 6 — неділя)
#   "window": ["08:00", "23:00"]        — лише в ці години (можна разом з every)
#   {"times": [{"t": "09:00", "d": [0,2,4]}, {"t": "18:30", "d": []}]} — у задані години (d — дні тижня, порожньо = щодня)
#   {"manual": true}                     — лише за наказом (кнопкою «Перевірити зараз»), автоматично — ніколи
#   "paused": true                      — пауза, доки не знімете
#   "pause_until": 1767225600           — пауза до часу (unix), потім саме відновиться
#   "resume_at": "08:00"                — те саме, але до найближчої такої години
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def _hm(s):
    h, m = (s or "0:0").split(":")
    return int(h) * 60 + int(m)


def schedule_due(sched, last_run, now=None, tz=None):
    """Чи пора запускати перевірку/роботу. sched — словник вище; last_run — коли запускали востаннє (unix)."""
    import datetime as _dt
    import time as _t

    sched = sched or {}
    now = now or _t.time()
    if sched.get("paused") or sched.get("manual"):
        return False
    if sched.get("pause_until") and now < sched["pause_until"]:
        return False
    dt = _dt.datetime.fromtimestamp(now, tz)
    minute = dt.hour * 60 + dt.minute
    win = sched.get("window")
    if win:
        a, b = _hm(win[0]), _hm(win[1])
        inside = a <= minute < b if a <= b else (minute >= a or minute < b)  # вікно через північ: 22:00–06:00
        if not inside:
            return False
    last = last_run or 0
    if sched.get("times"):  # найближчий минулий слот (за останні 8 днів) ще не відпрацьований
        for back in range(8):
            day = dt - _dt.timedelta(days=back)
            for x in sched["times"]:
                at = _hm(x["t"])
                slot = day.replace(hour=at // 60, minute=at % 60, second=0, microsecond=0)
                if x.get("d") and slot.weekday() not in x["d"]:
                    continue
                if last < slot.timestamp() <= now:
                    return True
        return False
    if sched.get("daily"):
        at = _hm(sched["daily"])
        today = dt.replace(hour=at // 60, minute=at % 60, second=0, microsecond=0).timestamp()
        return now >= today and last < today
    if sched.get("weekly"):
        dow, at = sched["weekly"][0], _hm(sched["weekly"][1])
        start = (dt - _dt.timedelta(days=(dt.weekday() - dow) % 7)).replace(hour=at // 60, minute=at % 60, second=0, microsecond=0)
        if start.timestamp() > now:
            start -= _dt.timedelta(days=7)
        return last < start.timestamp() <= now
    return now - last >= int(sched.get("every") or 300)


def schedule_text(sched, default_every=300):
    """Розклад людською мовою."""
    import time as _t

    sched = sched or {}
    if sched.get("paused"):
        base = "⏸ на паузі, доки не відновите"
    elif sched.get("pause_until") and sched["pause_until"] > _t.time():
        base = "⏸ на паузі до " + _t.strftime("%d.%m %H:%M", _t.localtime(sched["pause_until"]))
    else:
        base = ""
    if sched.get("manual"):
        how = "лише за наказом"
    elif sched.get("times"):
        how = "о " + ", ".join(x["t"] + (f" ({','.join(WEEKDAYS[d] for d in x['d'])})" if x.get("d") else "") for x in sched["times"])
    elif sched.get("daily"):
        how = f"раз на день о {sched['daily']}"
    elif sched.get("weekly"):
        how = f"раз на тиждень: {WEEKDAYS[sched['weekly'][0]]} о {sched['weekly'][1]}"
    else:
        e = int(sched.get("every") or default_every)
        how = f"кожні {e // 3600} год" if e >= 3600 and e % 3600 == 0 else f"кожні {e // 60} хв"
    if sched.get("window"):
        how += f", лише {sched['window'][0]}–{sched['window'][1]}"
    return (base + " · потім " + how) if base else how


# ---------------------------------------------------------------- план сну: обмін лише при змінах
# {"v": версія, "name": "…", "mode": "always"|"sleep"|"timer", "wake": [{"t": "09:00", "d": []}], "window": 15,
#  "wake_url": "https://…/wake/…", "tz": "Europe/Kyiv"}
# always — не спить; sleep — спить, доки його не покличуть (не спить ~10 хв після останньої активності);
# timer — не спить у вікна «час пробудження + window хв», решту часу спить.
def plan_state(plan, now=None, last_seen=None):
    """("awake"|"asleep", до_коли_unix_або_None) — стан бота за його планом, без жодного запиту до нього."""
    import datetime as _dt
    import time as _t
    from zoneinfo import ZoneInfo

    if not plan or plan.get("mode") in (None, "always"):
        return "awake", None
    now = now or _t.time()
    if last_seen and now - last_seen < 600:  # щойно працював — ще не заснув
        return "awake", None
    if plan["mode"] == "sleep":
        return "asleep", None
    tz = ZoneInfo(plan.get("tz") or "Europe/Kyiv")
    win = int(plan.get("window") or 15) * 60
    dt = _dt.datetime.fromtimestamp(now, tz)
    nxt = None
    for back in range(-1, 8):
        day = dt - _dt.timedelta(days=back)
        for x in plan.get("wake") or []:
            at = _hm(x["t"])
            slot = day.replace(hour=at // 60, minute=at % 60, second=0, microsecond=0)
            if x.get("d") and slot.weekday() not in x["d"]:
                continue
            s = slot.timestamp()
            if s <= now < s + win:
                return "awake", s + win
            if s > now and (nxt is None or s < nxt):
                nxt = s
    return "asleep", nxt


def plan_text(plan):
    if not plan or plan.get("mode") in (None, "always"):
        return "🟢 завжди на зв'язку"
    if plan["mode"] == "sleep":
        return "😴 спить, доки не покличуть"
    w = ", ".join(x["t"] + (f" ({','.join(WEEKDAYS[d] for d in x['d'])})" if x.get("d") else "") for x in plan.get("wake") or [])
    return f"⏰ прокидається о {w or '—'} на ~{plan.get('window') or 15} хв"


hub = Hub()
