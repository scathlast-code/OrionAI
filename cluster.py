"""⚡ Режими роботи й 🛰 резервні сервери.

Режими (кожен сервер свій, зберігається в /data/node.json, а не в базі — бо база між серверами копіюється):
• always — завжди активний: бот постійно слухає Telegram (polling). Найшвидші відповіді, працює весь час.
• sleep  — сон між розмовами: Telegram надсилає повідомлення вебхуком, тож хостинг (Railway Serverless) може
           присипляти бота після 10 хв тиші; кожне повідомлення його будить.
• timer  — сон + пробудження за розкладом: як sleep, але бота ще й будять у задані години (і, за бажанням,
           до кожного запланованого завдання), він виконує все, що назбиралося, і знову засинає.
           Будильник — безкоштовний сервіс cron-job.org: з ключем CRONJOB_API_KEY бот сам ставить і прибирає
           пробудження; без ключа — показує, що вписати вручну.

Резервні сервери (CLUSTER_NODES): той самий бот на кількох серверах. Працює (активний) лише один — найвищий у списку
з тих, що живі. Інші — у резерві: не відповідають у Telegram і не виконують завдань, а лише регулярно копіюють
собі базу з активного. Якщо активний впав (або закінчився ліміт хостингу), наступний за списком перебирає роботу.
Коли головний повертається, він забирає свіжу базу з того, хто працював, просить його відступити й працює далі.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta

import httpx

log = logging.getLogger("cluster")
MODES = {
    "always": ("🟢 Завжди активний", "Бот постійно на зв'язку, відповідає миттєво, усе за розкладом спрацьовує вчасно. Хостинг рахує кожну хвилину."),
    "sleep": ("😴 Сон між розмовами", "Бот засинає після 10 хв тиші й прокидається від вашого повідомлення (перша відповідь — на кілька секунд довша). "
                                     "Поки спить — не коштує нічого, але й завдання за розкладом чекають, доки ви напишете."),
    "timer": ("⏰ Сон + будильник", "Як «Сон», але бот ще й прокидається в задані години та до запланованих завдань, виконує все, що назбиралося, і знову засинає."),
    "auto": ("🤖 Автоматично за сервером", "На безкоштовному сервері (Oracle тощо) — «🟢 Завжди активний»: сон там нічого не заощаджує, а простій навіть ризикує "
                                           "тим, що Oracle забере сервер. На платному за хвилини (Railway) — сон: «⏰ Сон + будильник», якщо будильник задано, інакше «😴 Сон»."),
}
SELECT = {
    "auto": ("🤖 Автоматично", "першим працює безкоштовний сервер; платний — лише коли безкоштовні недоступні"),
    "list": ("📋 За списком", "перший живий у порядку CLUSTER_NODES"),
    "manual": ("👆 Вручну", "працює обраний вами, поки живий; якщо впаде — автоматично інший"),
}
WEEK = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


class Node:
    def __init__(self, data_dir, db_path, tz, fname="node.json", prefix=""):
        """prefix — для головного бота «MANAGER_»: його сервери задаються окремо (MANAGER_CLUSTER_NODES тощо)."""
        self.path = os.path.join(data_dir, fname)  # головний бот — свій файл (node-manager.json), бо тека /data може бути спільною
        self.db_path = db_path
        self.extras = None  # fn() -> [(відносна назва, шлях)] додаткових баз (плагіни, UTM, статистика) — їдуть на резерв разом з основною
        self.tz = tz
        # кластер
        self.prefix = prefix
        self.key = os.environ.get(prefix + "CLUSTER_KEY", "").strip()
        self.name = os.environ.get(prefix + "CLUSTER_NAME", "").strip() or "main"
        self.nodes = []  # [(назва, адреса)] — порядок = пріоритет
        for part in os.environ.get(prefix + "CLUSTER_NODES", "").split(","):
            if "=" in part:
                n, u = part.split("=", 1)
                self.nodes.append((n.strip(), u.strip().rstrip("/")))
        self.enabled = bool(self.nodes and self.key and len(self.key) >= 16 and any(n == self.name for n, _ in self.nodes))
        self.active = not self.enabled  # один сервер — завжди активний; у кластері вирішує перевірка
        self.since = time.time()
        self.activity = time.time()  # коли бот востаннє щось робив (повідомлення, команда, пробудження)
        self.pending = {}            # сервер-резерв -> перша робота бота після його останньої копії (на активному сервері)
        self.peer_activity = 0       # (на резерві) коли бот востаннє працював на активному сервері
        self.last_check = 0          # коли востаннє перевіряли інші сервери
        self.kick = False            # активний повідомив «бот знову працює» — перевірити одразу
        self.peers = {}   # назва -> {"ok": bool, "fails": int, "oks": int, "status": {...}, "t": час}
        self.events = []  # останні події кластера для екрана

    # ------------------------------------------------------------ локальні налаштування цього сервера
    def cfg(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, c):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, k, default=None):
        return self.cfg().get(k, default)

    def set(self, k, v):
        c = self.cfg()
        c[k] = v
        self.save(c)

    def cost(self):
        """free — безкоштовний сервер з фіксованою ціною (Oracle, домашній ПК…); paid — платний за хвилини роботи (Railway)."""
        v = os.environ.get("SERVER_COST", "").strip().lower()
        if v in ("free", "paid"):
            return v
        return "paid" if any(os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")) else "free"

    def raw_mode(self, default="always"):
        m = self.get("mode")
        return m if m in MODES else default

    def mode(self, default="always"):
        """Режим, що діє зараз. «auto» залежить від сервера: безкоштовний — завжди активний, платний — сон."""
        m = self.raw_mode(default)
        if m != "auto":
            return m
        if self.cost() == "free":
            return "always"
        return "timer" if (self.wake_times() and self.cron_key()) else "sleep"

    # ------------------------------------------------------------ будильник (режим timer)
    def wake_times(self):
        """[{"t": "09:00", "d": [0..6]}] — порожній d означає щодня."""
        return self.get("wake", [])

    def wake_text(self):
        w = self.wake_times()
        if not w:
            return "не задано"
        return ", ".join(x["t"] + (f" ({','.join(WEEK[d] for d in x['d'])})" if x.get("d") else "") for x in w)

    def next_wakes(self, now=None, limit=3):
        now = now or datetime.now(self.tz)
        out = []
        for day in range(8):
            base = (now + timedelta(days=day)).date()
            for x in self.wake_times():
                hh, mm = map(int, x["t"].split(":"))
                dt = datetime(base.year, base.month, base.day, hh, mm, tzinfo=self.tz)
                if dt > now and (not x.get("d") or dt.weekday() in x["d"]):
                    out.append(dt)
        return sorted(set(out))[:limit]

    # ------------------------------------------------------------ cron-job.org
    def cron_key(self):
        return os.environ.get("CRONJOB_API_KEY", "").strip()

    async def cron_api(self, method, path, body=None):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, f"https://api.cron-job.org{path}", json=body,
                                headers={"Authorization": f"Bearer {self.cron_key()}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"cron-job.org {r.status_code}: {r.text[:150]}")
        try:
            return r.json()
        except ValueError:
            return {}

    def desired_wakes(self, task_times):
        """Ключ -> розклад для cron-job.org. Регулярні пробудження + одноразові до завдань (task_times: [(id, datetime)])."""
        tzname = getattr(self.tz, "key", "Europe/Kyiv")
        out = {}
        for x in self.wake_times():
            hh, mm = map(int, x["t"].split(":"))
            wd = [(d + 1) % 7 for d in x.get("d", [])] or [-1]  # у cron-job.org 0 — неділя
            out[f"w:{x['t']}:{','.join(map(str, x.get('d', [])))}"] = {"timezone": tzname, "expiresAt": 0, "hours": [hh], "minutes": [mm],
                                                                      "mdays": [-1], "months": [-1], "wdays": wd}
        if self.get("wake_tasks", True):
            for tid, dt in task_times:
                dt = dt.astimezone(self.tz)
                out[f"t:{tid}:{int(dt.timestamp())}"] = {"timezone": tzname, "expiresAt": 0, "hours": [dt.hour], "minutes": [dt.minute],
                                                         "mdays": [dt.day], "months": [dt.month], "wdays": [-1]}
        return out

    async def sync_cron(self, wake_url, task_times):
        """Ставить/прибирає пробудження на cron-job.org так, щоб вони збігалися з розкладом. Повертає (додано, прибрано)."""
        if not self.cron_key() or not wake_url:
            return None
        want = self.desired_wakes(task_times) if self.mode() == "timer" else {}
        have = dict(self.get("cron_jobs", {}))
        added = removed = 0
        for k in list(have):
            if k not in want:
                try:
                    await self.cron_api("DELETE", f"/jobs/{have[k]}")
                except RuntimeError as e:
                    if " 404" not in str(e):
                        log.warning("cron-job.org: %s", e)
                        continue
                have.pop(k)
                removed += 1
        for k, sched in want.items():
            if k in have:
                continue
            res = await self.cron_api("PUT", "/jobs", {"job": {"url": wake_url, "enabled": True, "title": f"OrionAI wake {k}"[:60],
                                                              "saveResponses": False, "schedule": sched}})
            if res.get("jobId"):
                have[k] = res["jobId"]
                added += 1
        self.set("cron_jobs", have)
        self.set("cron_synced", time.time())
        return added, removed

    # ------------------------------------------------------------ кластер: перевірки й рішення
    def note(self, text):
        self.events.insert(0, (time.time(), text))
        del self.events[20:]
        log.info("кластер: %s", text)

    def peers_list(self):
        return [(n, u) for n, u in self.nodes if n != self.name]

    def env_int(self, name, default):
        return int(os.environ.get(self.prefix + name, os.environ.get(name, str(default))) or default)

    def quiet(self, now=None):
        """Сервери «сплять»: бот ніде не працював понад CLUSTER_IDLE (9 хв) — не перевіряємо одне одного й не копіюємо."""
        now = now or time.time()
        last = max(self.activity if self.active else 0, self.peer_activity)
        return now - last > self.env_int("CLUSTER_IDLE", 540)

    def touch(self):
        """Бот щось зробив. На активному сервері: запам'ятати, що резервам є що копіювати; якщо сервери «спали» —
        одразу повідомити їм, що бот знову працює (це їх і будить)."""
        now = time.time()
        was_quiet = self.quiet(now)
        self.activity = now
        if not (self.enabled and self.active):
            return
        for n, _ in self.peers_list():
            self.pending.setdefault(n, now)
        if was_quiet:
            try:
                asyncio.get_running_loop().create_task(self.announce())
            except RuntimeError:
                pass

    async def announce(self):
        for n, u in self.peers_list():
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    await c.post(f"{u}/cluster/activity", json={"t": self.activity, "from": self.name}, headers={"X-Cluster-Key": self.key})
            except httpx.HTTPError:
                pass

    def status(self):
        try:
            mtime = os.path.getmtime(self.db_path)
        except OSError:
            mtime = 0
        return {"name": self.name, "active": self.active, "since": self.since, "db_mtime": mtime, "activity": self.activity, "t": time.time(),
                "cost": self.cost(), "mode": self.mode()}

    async def _get(self, url, path, timeout=10):
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.get(f"{url}{path}", headers={"X-Cluster-Key": self.key, "X-Cluster-Node": self.name})

    async def check_peers(self):
        for n, u in self.peers_list():
            p = self.peers.setdefault(n, {"ok": False, "fails": 0, "oks": 0, "status": {}, "t": 0})
            try:
                r = await self._get(u, "/cluster/status")
                st = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                st = None
            p["t"] = time.time()
            if st:
                p.update(status=st, fails=0, oks=p["oks"] + 1)
                if st.get("active"):
                    self.peer_activity = max(self.peer_activity, st.get("activity") or 0)
                if not p["ok"]:
                    p["ok"] = True
            else:
                p.update(fails=p["fails"] + 1, oks=0)
                if p["fails"] >= 2:  # дві невдалі перевірки поспіль — вважаємо сервер недоступним
                    p["ok"] = False

    def leader(self):
        """Хто має працювати: обраний вручну (якщо живий), інакше перший у списку з живих.
        Повернення головного — лише коли він стабільно живий 3 перевірки."""
        pool = self.get("pool") or [n for n, _ in self.nodes]  # які сервери взагалі можуть працювати
        sel = self.get("select") or "auto"
        pref = self.get("prefer")
        if sel == "manual" and pref in pool:
            if pref == self.name:
                return self.name
            if self.peers.get(pref, {}).get("ok"):
                return pref

        def alive(n):
            if n == self.name:
                return True
            p = self.peers.get(n)
            return bool(p and p["ok"] and (p["status"].get("active") or p["oks"] >= 3))

        order = [n for n, _ in self.nodes if n in pool] or [self.name]
        if sel == "auto":  # безкоштовні — першими
            cost = lambda n: self.cost() if n == self.name else (self.peers.get(n, {}).get("status", {}).get("cost") or "free")  # noqa: E731
            order.sort(key=lambda n: (cost(n) != "free", [x for x, _ in self.nodes].index(n)))
        for n in order:
            if alive(n):
                return n
        return self.name

    async def pull_db(self, url):
        """Забирає знімок бази з іншого сервера й підміняє свою (знімком SQLite — безпечно на ходу)."""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(f"{url}/cluster/db", headers={"X-Cluster-Key": self.key, "X-Cluster-Node": self.name})
        if r.status_code != 200 or r.content[:16] != b"SQLite format 3\x00":
            raise RuntimeError(f"не вдалося отримати базу ({r.status_code})")
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(r.content)
            with closing(sqlite3.connect(tmp)) as src, closing(sqlite3.connect(self.db_path)) as dst:
                src.backup(dst)
        finally:
            os.remove(tmp)
        self.set("last_pull", time.time())
        try:
            await self.pull_extras(url)
        except Exception:  # noqa: BLE001 — старіша версія на тому сервері або збій: основна база вже є
            log.warning("кластер: додаткові бази не скопійовано", exc_info=True)
        return len(r.content)

    def snapshot_extras(self):
        """TAR зі знімками додаткових баз."""
        import io
        import tarfile
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w") as tf:
            for rel, path in (self.extras() if self.extras else []):
                fd, tmp = tempfile.mkstemp(suffix=".db")
                os.close(fd)
                try:
                    with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(tmp)) as dst:
                        src.backup(dst)
                    tf.add(tmp, arcname=rel)
                except (OSError, sqlite3.Error):
                    continue
                finally:
                    os.remove(tmp)
        return bio.getvalue()

    async def pull_extras(self, url):
        import io
        import tarfile
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.get(f"{url}/cluster/extras", headers={"X-Cluster-Key": self.key, "X-Cluster-Node": self.name})
        if r.status_code != 200:
            return 0
        base = os.path.dirname(os.path.abspath(self.db_path))
        n = 0
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:") as tf:
            for m in tf.getmembers():
                parts = m.name.replace("\\", "/").split("/")
                if not m.isfile() or m.name.startswith("/") or ".." in parts:
                    continue
                raw = tf.extractfile(m).read()
                if raw[:16] != b"SQLite format 3\x00":
                    continue
                dest = os.path.join(base, *parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                fd, tmp = tempfile.mkstemp(suffix=".db")
                os.close(fd)
                try:
                    with open(tmp, "wb") as f:
                        f.write(raw)
                    with closing(sqlite3.connect(tmp)) as src, closing(sqlite3.connect(dest)) as dst:
                        src.backup(dst)
                    n += 1
                finally:
                    os.remove(tmp)
        return n

    def snapshot(self):
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with closing(sqlite3.connect(self.db_path)) as src, closing(sqlite3.connect(tmp)) as dst:
                src.backup(dst)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            os.remove(tmp)

    async def settings(self, **kw):
        """Спосіб вибору сервера / увімкнені сервери — на всі сервери одразу."""
        for k, v in kw.items():
            self.set(k, v)
        for n, u in self.peers_list():
            try:
                async with httpx.AsyncClient(timeout=20) as cl:
                    await cl.post(f"{u}/cluster/settings", json=kw, headers={"X-Cluster-Key": self.key})
            except httpx.HTTPError:
                pass
        self.note("змінено вибір серверів: " + ", ".join(f"{k}={v}" for k, v in kw.items()))

    async def prefer(self, name):
        """Вручну зробити активним інший сервер (або «-» — знову автоматично за списком). Повідомляє всім серверам."""
        name = None if name in (None, "", "-") else name
        self.set("prefer", name)
        self.set("select", "manual" if name else "auto")
        for n, u in self.peers_list():
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.post(f"{u}/cluster/settings", json={"prefer": name, "select": "manual" if name else "auto"}, headers={"X-Cluster-Key": self.key})
            except httpx.HTTPError:
                pass
        self.note(f"вручну обрано сервер «{name}»" if name else "знову автоматично за списком")

    async def ask_yield(self, url):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{url}/cluster/yield", headers={"X-Cluster-Key": self.key})
        return r.status_code == 200

    async def tick(self, activate, deactivate, notify):
        """Одна перевірка кластера. activate()/deactivate() — увімкнути/вимкнути роботу в Telegram і завдання."""
        if not self.enabled:
            return
        now = time.time()
        # Сон серверів: якщо бот ніде не працює понад 9 хв, перевірки й копіювання припиняються до його наступної роботи
        # (активний сервер сам повідомить резервам). Лише рідка контрольна перевірка CLUSTER_WATCH (типово раз на годину),
        # щоб помітити, якщо активний сервер упав, поки все спало; 0 — не перевіряти взагалі.
        watch = self.env_int("CLUSTER_WATCH", 3600)
        if self.last_check and self.quiet(now) and not self.kick and (not watch or now - self.last_check < watch):
            return
        self.kick = False
        self.last_check = now
        await self.check_peers()
        lead = self.leader()
        url = dict(self.nodes)
        active_peers = [n for n, p in self.peers.items() if p["ok"] and p["status"].get("active")]
        if lead == self.name:
            if self.active and active_peers:  # двоє активних (був розрив зв'язку): свіжіші дані в того, хто став активним пізніше
                newer = [n for n in active_peers if self.peers[n]["status"].get("since", 0) > self.since]
                if newer:
                    try:
                        await self.pull_db(url[newer[0]])
                        self.note(f"забрав свіжішу базу з «{newer[0]}» після розриву зв'язку")
                    except Exception as e:  # noqa: BLE001
                        self.note(f"не вдалося забрати базу з «{newer[0]}»: {e}")
                for n in active_peers:
                    try:
                        await self.ask_yield(url[n])
                    except httpx.HTTPError:
                        pass
                await activate()  # переконатися, що Telegram знову надсилає повідомлення сюди
                await notify(f"🛰 Сервер «{self.name}» знову на зв'язку й працює далі" + (f" (база з «{newer[0]}»)" if newer else "") + ".")
            elif not self.active:
                src = active_peers[0] if active_peers else None
                if not src:  # активних немає — беремо найсвіжішу базу з живих резервних, якщо вона новіша за нашу
                    mine = self.status()["db_mtime"]
                    fresh = [(p["status"].get("db_mtime", 0), n) for n, p in self.peers.items() if p["ok"]]
                    src = max(fresh)[1] if fresh and max(fresh)[0] > mine + 5 else None
                if src:
                    try:
                        await self.pull_db(url[src])
                    except Exception as e:  # noqa: BLE001
                        self.note(f"не вдалося забрати базу з «{src}»: {e}")
                for n in active_peers:
                    try:
                        await self.ask_yield(url[n])
                    except httpx.HTTPError:
                        pass
                self.active, self.since = True, time.time()
                await activate()
                down = [n for n, _ in self.peers_list() if not self.peers.get(n, {}).get("ok")]
                self.note(f"цей сервер («{self.name}») тепер активний" + (f"; недоступні: {', '.join(down)}" if down else ""))
                await notify(f"🛰 Працює сервер «{self.name}»" + (f" — «{', '.join(down)}» недоступний, я підхопив роботу." if down else ".")
                             + (" База скопійована з «" + src + "»." if src else ""))
        else:
            if self.active and self.peers.get(lead, {}).get("status", {}).get("active"):
                self.active = False
                await deactivate()
                self.note(f"повернувся «{lead}» — цей сервер у резерві")
            elif not self.active and active_peers:
                # Копія — через CLUSTER_SYNC (5 хв) після першої роботи бота, яку ми ще не скопіювали.
                # Далі, якщо бот мовчить, — нічого: наступна копія лише після його наступної роботи.
                src = active_peers[0]
                first = self.peers[src]["status"].get("pending")
                never = not self.get("last_pull")  # новий резервний сервер — одна початкова копія
                if never or (first and time.time() - first >= self.env_int("CLUSTER_SYNC", 300)):
                    try:
                        size = await self.pull_db(url[src])
                        self.note(f"скопіював базу з «{src}» ({size // 1024} КБ)")
                    except Exception as e:  # noqa: BLE001
                        self.note(f"не вдалося скопіювати базу з «{src}»: {e}")

    # ------------------------------------------------------------ HTTP-маршрути для інших серверів
    def routes(self, on_yield):
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        def ok(request):
            return self.enabled and request.headers.get("X-Cluster-Key") == self.key

        async def status(request):
            if not ok(request):
                return JSONResponse({}, status_code=403)
            st = self.status()
            st["pending"] = self.pending.get(request.headers.get("X-Cluster-Node", ""))  # що ще не скопіював саме цей сервер
            return JSONResponse(st)

        async def db(request):
            if not ok(request):
                return Response(status_code=403)
            data = await asyncio.to_thread(self.snapshot)
            self.pending.pop(request.headers.get("X-Cluster-Node", ""), None)  # цей резерв тепер має все
            return Response(data, media_type="application/octet-stream")

        async def extras_(request):
            if not ok(request):
                return Response(status_code=403)
            return Response(await asyncio.to_thread(self.snapshot_extras), media_type="application/x-tar")

        async def settings_(request):
            if not ok(request):
                return Response(status_code=403)
            try:
                kw = await request.json()
            except ValueError:
                kw = {}
            for k in ("select", "pool", "prefer"):
                if k in kw:
                    self.set(k, kw[k])
            self.kick = True
            return JSONResponse({"ok": True})

        async def activity(request):
            if not ok(request):
                return Response(status_code=403)
            try:
                self.peer_activity = max(self.peer_activity, float((await request.json()).get("t") or time.time()))
            except ValueError:
                self.peer_activity = time.time()
            self.kick = True  # бот знову працює — резерв прокидається й стежить
            return JSONResponse({"ok": True})

        async def yield_(request):
            if not ok(request):
                return Response(status_code=403)
            if self.active:
                self.active = False
                await on_yield()
                self.note("інший сервер забрав роботу — цей у резерві")
            return JSONResponse({"ok": True})

        async def prefer_(request):
            if not ok(request):
                return Response(status_code=403)
            try:
                name = (await request.json()).get("name")
            except ValueError:
                name = None
            self.set("prefer", name)
            self.note(f"інший сервер обрав активним «{name}»" if name else "знову автоматично за списком")
            return JSONResponse({"ok": True})

        return [Route("/cluster/status", status), Route("/cluster/db", db), Route("/cluster/extras", extras_), Route("/cluster/yield", yield_, methods=["POST"]),
                Route("/cluster/prefer", prefer_, methods=["POST"]), Route("/cluster/activity", activity, methods=["POST"]),
                Route("/cluster/settings", settings_, methods=["POST"])]


# 🆓 Безкоштовні сервери для бота (перевірено 21.11.2026; умови змінюються — звіряйте за посиланням)
FREE_HOSTS = [
    ("☁️ Oracle Cloud Always Free", "найкращий варіант: безкоштовні віртуальні сервери, що працюють постійно; бот уже вміє тут працювати "
     "(план переїзду — у «Сервері»); потрібна картка для перевірки особи.", "https://www.oracle.com/cloud/free/"),
    ("🟦 Google Cloud — e2-micro", "один постійний сервер e2-micro безкоштовно лише в регіонах США (Орегон, Айова, Південна Кароліна), "
     "30 ГБ диска й 1 ГБ трафіку на місяць; реєстрація з платіжним акаунтом.", "https://cloud.google.com/free/docs/compute-getting-started"),
    ("🟩 Koyeb — безкоштовний екземпляр", "один на акаунт: 512 МБ пам'яті, 0,1 ядра, 2 ГБ диска; засинає після години без запитів — "
     "для бота потрібен режим вебхука (TG_WEBHOOK=1), тоді Telegram сам будить. З 2026 належить Mistral AI; "
     "чи дають його новим акаунтам — перевірте при реєстрації.", "https://www.koyeb.com/docs/reference/instances"),
    ("🟪 Render — безкоштовний вебсервіс", "засинає через 15 хв без запитів (прокидається ~хвилину), файли зникають при кожному перезапуску — "
     "годиться лише як запасний сервер у режимі вебхука, базу тут не зберегти.", "https://render.com/docs/free"),
    ("🚂 Railway Free", "після пробного місяця — $1 на місяць безкоштовно, але сервіси засинають; для постійної роботи потрібен Hobby ($5).",
     "https://docs.railway.com/pricing/free-plan"),
    ("✖️ Fly.io", "безкоштовного плану для нових акаунтів більше немає — лише коротка проба.", "https://fly.io/docs/about/discontinued-plans/"),
]


def free_hosts_screen(esc, back="m:cl"):
    lines = ["🆓 <b>Безкоштовні сервери для бота</b>",
             "Куди можна перенести бота чи додати запасний сервер без оплати. Посилання — одразу після кожного рядка.", ""]
    for name, about, url in FREE_HOSTS:
        lines.append(f"<b>{esc(name)}</b> — {esc(about)} <a href=\"{esc(url)}\">джерело</a>")
    lines += ["", "<i>Порада: основний сервер — Oracle Always Free або Railway Hobby; Koyeb/Render — запасні у режимі вебхука. "
              "Як підключити запасний — «🛰 Сервери».</i>"]
    rows = [[(name.split(" ", 1)[1][:28] + " ↗", "url:" + url)] for name, _a, url in FREE_HOSTS[:5]]
    rows.append([("« Сервери", back)])
    return "\n".join(lines), rows


def cluster_screen(node, esc, tz, back="m:rm"):
    """Екран «🛰 Сервери» — спільний для ШІ-бота й головного (дані кнопок m:cl…)."""
    from datetime import datetime as _dt
    lines = ["🛰 <b>Сервери</b>",
             "Той самий бот на кількох серверах. Працює один, інші в резерві: копіюють базу через 5 хв після роботи бота; "
             "після 9 хв тиші сервери «сплять» до наступної роботи (лише контрольна перевірка раз на годину).", ""]
    if not node.enabled:
        p = node.prefix
        lines += [f"Зараз сервер один ({'🆓 безкоштовний' if node.cost() == 'free' else '💳 платний за хвилини'}).", "",
                  "<b>Як додати ще сервери</b> (на кожному ті самі змінні, крім NAME):",
                  f"<code>{p}CLUSTER_NODES=oracle=https://адреса-1,railway=https://адреса-2</code>",
                  f"<code>{p}CLUSTER_NAME=</code> назва саме цього сервера зі списку",
                  f"<code>{p}CLUSTER_KEY=</code> однаковий секрет (від 16 символів)",
                  "Докладно — docs/21-rezervni-servery.md"]
        return "\n".join(lines), [[("🆓 Безкоштовні сервери", "m:clfree")], [("« Режим роботи", back)]]
    sel = node.get("select") or "auto"
    pool = node.get("pool") or [n for n, _ in node.nodes]
    lines.append(f"Як обирати: <b>{SELECT[sel][0]}</b> — {SELECT[sel][1]}")
    for n, _ in node.nodes:
        if n == node.name:
            cost, st = node.cost(), "цей сервер · " + ("<b>працює</b>" if node.active else "у резерві")
        else:
            p = node.peers.get(n) or {}
            cost = p.get("status", {}).get("cost") or "?"
            st = (("✅ живий · " + ("<b>працює</b>" if p["status"].get("active") else "у резерві")) if p.get("ok")
                  else ("❌ недоступний" if p.get("t") else "▫️ ще не перевірявся"))
        tag = {"free": "🆓", "paid": "💳"}.get(cost, "▫️")
        lines.append(f"{'✅' if n in pool else '🚫'} {tag} <b>{esc(n)}</b> — {st}" + (" · 📌 обраний" if sel == "manual" and node.get("prefer") == n else ""))
    lines.append("\n✅ — може працювати · 🚫 — вимкнений (лише копіює базу) · 🆓 безкоштовний · 💳 платний за хвилини")
    if node.get("last_pull"):
        lines.append(f"Остання копія бази сюди: {_dt.fromtimestamp(node.get('last_pull'), tz).strftime('%d.%m %H:%M')}")
    if node.events:
        lines += ["", "<b>Події:</b>"] + [f"{_dt.fromtimestamp(t, tz).strftime('%d.%m %H:%M')} {esc(x)}" for t, x in node.events[:5]]
    rows = [[(("● " if k == sel else "") + v[0], f"m:clsel:{k}") for k, v in SELECT.items()]]
    for n, _ in node.nodes:
        rows.append([(("✅ " if n in pool else "🚫 ") + n, f"m:clpool:{n}"), ("👆 Працюй ти", f"m:clpref:{n}")])
    rows.append([("🆓 Безкоштовні сервери", "m:clfree")])
    rows.append([("🔄 Перевірити зараз", "m:clrun"), ("« Режим роботи", back)])
    return "\n".join(lines), rows


async def cluster_route(node, a, arg):
    """Дії екрана «🛰 Сервери». Повертає підказку."""
    if a == "clsel" and arg in SELECT:
        await node.settings(select=arg, **({"prefer": None} if arg != "manual" else {}))
        return SELECT[arg][0]
    if a == "clpool":
        pool = node.get("pool") or [n for n, _ in node.nodes]
        if arg in pool:
            if len(pool) == 1:
                return "Хоча б один сервер має лишатися увімкненим"
            pool.remove(arg)
        elif arg in [n for n, _ in node.nodes]:
            pool.append(arg)
        await node.settings(pool=pool)
        return "Сервер " + ("увімкнено" if arg in pool else "вимкнено")
    if a == "clpref":
        await node.prefer(arg)
        return f"Працюватиме «{arg}»" if arg != "-" else "Автоматично"
    return None


# ---------------------------------------------------------------- 🔔 нагадування зайти в акаунт Oracle
# Oracle може вважати покинутим акаунт, у якому довго нічого не відбувається; що саме рахується активністю, офіційно
# не описано — тож найнадійніше раз на 2 тижні просто зайти в консоль чи застосунок.
ORACLE_URL = "https://cloud.oracle.com/"
ORACLE_APP = "https://apps.apple.com/app/id1534250806"


def oracle_on(node, need=False):
    v = node.get("oracle_remind")
    if v is not None:
        return bool(v)
    return need or node.cost() == "free" or any("oracle" in n.lower() for n, _ in node.nodes)


def oracle_due(node, now=None, need=False):
    now = now or time.time()
    if not oracle_on(node, need):
        return False
    nxt = node.get("oracle_next")
    if not nxt:  # перше нагадування — через інтервал від увімкнення
        node.set("oracle_next", now + int(node.get("oracle_every") or 14) * 86400)
        return False
    return now >= nxt


def oracle_message(node):
    every = int(node.get("oracle_every") or 14)
    text = ("🔔 <b>Час зайти в Oracle Cloud</b>\n"
            "Oracle може зупинити або закрити акаунт, у якому довго нічого не відбувається. Достатньо увійти й побути хвилину "
            "(на iPhone — у застосунку Oracle Cloud Infrastructure). Заодно гляньте, чи немає листів від Oracle про «idle» сервер "
            f"і чи дійсна картка в акаунті.\nНаступне нагадування — через {every} дн.")
    rows = [[("🌐 Відкрити Oracle Cloud", "url:" + ORACLE_URL)], [("📱 Застосунок для iPhone", "url:" + ORACLE_APP)],
            [("✅ Зайшов", "m:orok"), ("⏰ Нагадати завтра", "m:orlater")]]
    return text, rows


def oracle_route(node, a, arg):
    """Дії кнопок нагадування. Повертає підказку."""
    now = time.time()
    if a in ("orok", "orlater", "orset"):
        node.set("oracle_ts", now)  # спільна позначка: найновіша дія з будь-якого бота перемагає
    if a == "orok":
        node.set("oracle_last", now)
        node.set("oracle_next", now + int(node.get("oracle_every") or 14) * 86400)
        return "Дякую! Наступне нагадування — через " + str(int(node.get("oracle_every") or 14)) + " дн."
    if a == "orlater":
        node.set("oracle_next", now + 86400)
        return "Нагадаю завтра"
    if a == "orset":
        if arg == "off":
            node.set("oracle_remind", False)
            return "Нагадування вимкнено"
        node.set("oracle_remind", True)
        node.set("oracle_every", int(arg))
        node.set("oracle_next", now + int(arg) * 86400)
        return f"Нагадуватиму раз на {arg} дн."
    return None


def oracle_rows(node, need=False):
    on = oracle_on(node, need)
    every = int(node.get("oracle_every") or 14)
    # підпис коротко, щоб чотири кнопки в ряду не обрізались до «🔔 Oracl…»; що це — у заголовку ряду
    return [[("🔔 Нагадувати зайти в Oracle:", "m:noop")],
            [(("● " if on and every == d else "") + f"{d} дн", f"m:orset:{d}") for d in (7, 14, 21)] + [(("● " if not on else "") + "🔕 Ні", "m:orset:off")]]


def oracle_line(node, tz, need=False):
    from datetime import datetime as _dt
    if not oracle_on(node, need):
        return "🔕 Нагадування зайти в Oracle Cloud вимкнено."
    nxt = node.get("oracle_next")
    return (f"🔔 Нагадування зайти в Oracle Cloud: раз на {int(node.get('oracle_every') or 14)} дн"
            + (f", наступне {_dt.fromtimestamp(nxt, tz).strftime('%d.%m')}" if nxt else ""))


# ---------------------------------------------------------------- 🔗 корисні посилання (Railway, GitHub, Oracle)
RAILWAY_URL = "https://railway.com/dashboard"
RAILWAY_PLANS = "https://railway.com/workspace/plans"
GITHUB_URL = "https://github.com/"
GITHUB_APP = "https://apps.apple.com/app/id1477376905"


def github_repo(node):
    """Адреса репозиторію: задана в боті → змінна GITHUB_REPO → None. «owner/repo» розгортається в повну адресу."""
    v = (node.get("github") or os.environ.get("GITHUB_REPO") or "").strip().rstrip("/")
    if not v:
        return None
    if not v.startswith("http"):
        v = "https://github.com/" + v.removeprefix("github.com/")
    return v


def github_set(node, text):
    """Повертає (успіх, повідомлення). «-» — як у змінних сервера."""
    t = (text or "").strip()
    if t == "-":
        node.set("github", None)
        return True, "Репозиторій: " + (github_repo(node) or "не задано")
    t = t.removeprefix("https://").removeprefix("http://").removeprefix("www.").removeprefix("github.com/").strip("/")
    if t.endswith(".git"):
        t = t[:-4]
    parts = t.split("/")
    ok = len(parts) >= 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) for x in parts[:2])
    if not ok:
        return False, "Потрібно: <code>власник/назва</code> або адреса github.com/власник/назва"
    node.set("github", "https://github.com/" + "/".join(parts[:2]))
    return True, "Репозиторій: " + github_repo(node)


def links_screen(node, esc, back="m:rm"):
    repo = github_repo(node)
    on_railway = bool(os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT"))
    lines = ["🔗 <b>Корисні посилання</b>", "Railway відкривається в Safari (окремого застосунку немає), GitHub і Oracle мають застосунки для iPhone.", "",
             "🚂 <b>Railway</b> — сервер бота" + (" (цей)" if on_railway else "") + ". Заходити для «активності» не потрібно: "
             "Railway нічого не забирає через простій. Важливо інше:",
             "• закінчився кредит — сервіси зупиняються, дані лишаються; на Free оживуть, коли нарахується новий $1;",
             "• після пробного періоду (30 днів) томи з даними видаляються через 30 днів, якщо не перейти на Free чи Hobby "
             "(перед цим приходить лист);",
             "• на Free кожному сервісу потрібен Serverless (сон), інакше він не запуститься.",
             "У «Plans» видно залишок кредиту й витрати.", "",
             "🐙 <b>GitHub</b> — код бота. Активності теж не вимагає. Railway бере код звідти й оновлює бота після кожної зміни.",
             "Репозиторій: " + (f"<code>{esc(repo)}</code>" if repo else "не задано (кнопка нижче відкриє головну GitHub)"), "",
             "☁️ <b>Oracle Cloud</b> — безкоштовний сервер; там заходити раз на 2–3 тижні варто (є нагадування)."]
    rows = [[("🚂 Railway: проєкти", "url:" + RAILWAY_URL), ("💰 Кредит і план", "url:" + RAILWAY_PLANS)],
            [("🐙 " + ("Репозиторій" if repo else "GitHub"), "url:" + (repo or GITHUB_URL)), ("✏️ Репозиторій", "m:lnkgh")],
            [("☁️ Oracle Cloud", "url:" + ORACLE_URL), ("🆓 Безкоштовні сервери", "m:clfree")],
            [("📱 Застосунок GitHub", "url:" + GITHUB_APP), ("📱 Застосунок Oracle", "url:" + ORACLE_APP)],
            [("« Режим роботи", back)]]
    return "\n".join(lines), rows


GITHUB_PROMPT = ("✏️ <b>Репозиторій GitHub</b>\nНадішліть <code>власник/назва</code> або адресу, наприклад "
                 "<code>https://github.com/ivan/OrionAI</code>. Ні токенів, ні паролів не потрібно — лише адреса.\n«-» — як у змінній GITHUB_REPO.")


# Нагадування надсилає лише головний бот; дочірні показують ті самі налаштування й кнопки. Стан (частота, коли наступне,
# «✅ Зайшов») їздить разом із планами сну — тобто лише при змінах, — і найновіша дія з будь-якого бота стає спільною.
ORACLE_FIELDS = ("oracle_remind", "oracle_every", "oracle_next", "oracle_last")


def oracle_state(node):
    st = {k: node.get(k) for k in ORACLE_FIELDS}
    st["ts"] = node.get("oracle_ts") or 0
    st["need"] = node.cost() == "free" or any("oracle" in n.lower() for n, _ in node.nodes)  # чи цей сервер сам на Oracle — головний нагадує, навіть якщо він на Railway
    return st


def oracle_merge(node, st):
    """Застосувати стан з іншого бота, якщо він новіший. True — щось змінилось."""
    if not isinstance(st, dict) or (st.get("ts") or 0) <= (node.get("oracle_ts") or 0):
        return False
    for k in ORACLE_FIELDS:
        node.set(k, st.get(k))
    node.set("oracle_ts", st["ts"])
    return True
