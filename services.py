"""🔗 Хмарні таск-менеджери й блокноти для ШІ-бота.

Завдання: Todoist, Google Tasks, Microsoft To Do, Trello.  Нотатки: Notion, OneNote.
Що вміє бот: додати завдання чи нотатку (кнопкою «📤» або словами в розмові з ШІ), показати список,
позначити завдання виконаним, автоматично копіювати нові нотатки/завдання бота в обраний сервіс.

Підключення:
- токеном, який ви вставляєте в бот (Todoist, Notion, Trello);
- входом через Google / Microsoft (Google Tasks, Microsoft To Do, OneNote) — ті самі застосунки,
  що й для резервних копій (GDRIVE_*, ONEDRIVE_*), лише з ще однією адресою повернення.

Без офіційного API для особистих акаунтів (і тому не підключаються): Google Keep, Apple Нотатки й Нагадування, Evernote.
"""

import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

log = logging.getLogger("services")


def env(name, default=""):
    return os.environ.get(name, default).strip()


URLS = {  # можна підмінити в тестах
    "todoist": "https://api.todoist.com/api/v1", "notion": "https://api.notion.com/v1", "gtasks": "https://tasks.googleapis.com/tasks/v1",
    "graph": "https://graph.microsoft.com/v1.0", "trello": "https://api.trello.com/1",
}
NOTION_VERSION = "2025-09-03"

SERVICES = {
    "todoist": {"title": "Todoist", "icon": "🔴", "kinds": ("task",), "auth": "token",
                "how": "Todoist → Налаштування → Інтеграції → Розробник → скопіюйте API-токен і надішліть мені."},
    "gtasks": {"title": "Google Tasks", "icon": "🟢", "kinds": ("task",), "auth": "google", "scope": "https://www.googleapis.com/auth/tasks",
               "how": "Натисніть «Увійти» й дозвольте доступ до Google Tasks."},
    "mstodo": {"title": "Microsoft To Do", "icon": "🔷", "kinds": ("task",), "auth": "microsoft", "scope": "Tasks.ReadWrite offline_access User.Read",
               "how": "Натисніть «Увійти» й дозвольте доступ до To Do."},
    "trello": {"title": "Trello", "icon": "🟦", "kinds": ("task",), "auth": "token",
               "how": "trello.com/power-ups/admin → New → створіть Power-Up → API key → поруч «Token» → дозвольте.\nНадішліть мені: <code>КЛЮЧ ТОКЕН</code> через пробіл."},
    "notion": {"title": "Notion", "icon": "⬛️", "kinds": ("note", "task"), "auth": "token",
               "how": "notion.so/my-integrations → New integration → скопіюйте ключ (ntn_…).\nПотім у Notion відкрийте сторінку, куди складати нотатки → ⋯ → Connections → додайте інтеграцію.\nНадішліть мені ключ."},
    "onenote": {"title": "OneNote", "icon": "🟪", "kinds": ("note",), "auth": "microsoft", "scope": "Notes.ReadWrite offline_access User.Read",
                "how": "Натисніть «Увійти» й дозвольте доступ до OneNote. Нотатки потраплятимуть у ваш розділ за замовчуванням."},
    "iphone": {"title": "iPhone: Нагадування й Нотатки", "btn": "iPhone", "icon": "📱", "kinds": ("task", "note"), "auth": "shortcut",
               "how": "Apple не дає інтернет-доступу до Нагадувань і Нотаток, тому їх з'єднує застосунок «Команди» (Shortcuts) на вашому iPhone: "
                      "він сам забирає з бота нові нагадування й нотатки (наприклад, щогодини) і може надсилати в бот ваші. Натисніть «Підключити» — я дам адресу й покрокову інструкцію."},
    "evernote": {"title": "Evernote (через пошту)", "icon": "🐘", "kinds": ("note",), "auth": "email",
                 "how": "Evernote більше не видає ключі API, але приймає нотатки поштою (платні тарифи). У Evernote: Налаштування → «Пересилання листів» (Email notes) → скопіюйте свою адресу …@m.evernote.com і надішліть мені."},
    "mail": {"title": "Пошта (TickTick, Things тощо)", "btn": "Пошта", "icon": "✉️", "kinds": ("task", "note"), "auth": "email",
             "how": "Багато застосунків приймають завдання й нотатки листом: TickTick, Things («Mail to Things»), Todoist, OmniFocus тощо. Знайдіть у застосунку свою адресу для листів і надішліть мені."},
}
NOT_SUPPORTED = ("Google Keep і ColorNote не мають відкритого API для особистих акаунтів, а неофіційні способи можуть призвести до блокування вашого акаунта Google, "
                 "тому бот їх не підключає. Замінники: Google Tasks (той самий акаунт Google), «📱 iPhone» або Notion; для ColorNote — «📋 Копіювати» й вставити.")


class ServiceError(Exception):
    pass


class Services:
    def __init__(self, kv_get, kv_set, public_url):
        self.kv_get, self.kv_set = kv_get, kv_set
        self.public_url = (public_url or "").rstrip("/")

    # ------------------------------------------------------------ налаштування
    def cfg(self):
        raw = self.kv_get("services_cfg")
        c = {"acc": {}, "default": {"note": None, "task": None}, "auto": {"note": False, "task": False}}
        if raw:
            c.update(json.loads(raw))
        return c

    def save(self, c):
        self.kv_set("services_cfg", json.dumps(c, ensure_ascii=False))

    def connected(self, sid):
        return bool(self.cfg()["acc"].get(sid))

    def connected_for(self, kind):
        return [s for s in SERVICES if kind in SERVICES[s]["kinds"] and self.connected(s)]

    def oauth_ready(self, sid):
        a = SERVICES[sid]["auth"]
        if a == "email":
            return bool(env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASSWORD"))
        if a == "shortcut":
            return bool(self.public_url)
        if a == "google":
            return bool(env("GDRIVE_CLIENT_ID") and env("GDRIVE_CLIENT_SECRET") and self.public_url)
        if a == "microsoft":
            return bool(env("ONEDRIVE_CLIENT_ID") and env("ONEDRIVE_CLIENT_SECRET") and self.public_url)
        return True

    def disconnect(self, sid):
        c = self.cfg()
        c["acc"].pop(sid, None)
        for k in ("note", "task"):
            if c["default"].get(k) == sid:
                c["default"][k] = None
        self.save(c)

    def set_default(self, kind, sid):
        c = self.cfg()
        c["default"][kind] = sid
        self.save(c)

    def toggle_auto(self, kind):
        c = self.cfg()
        c["auto"][kind] = not c["auto"].get(kind)
        self.save(c)

    def default(self, kind):
        c = self.cfg()
        sid = c["default"].get(kind)
        if sid and self.connected(sid):
            return sid
        conn = self.connected_for(kind)
        return conn[0] if conn else None

    # ------------------------------------------------------------ підключення токеном
    async def connect_token(self, sid, text):
        text = text.strip()
        if sid == "todoist":
            acc = {"token": text}
            await self._todoist(acc, "GET", "projects")
        elif sid == "trello":
            parts = text.split()
            if len(parts) != 2:
                raise ServiceError("потрібні ключ і токен через пробіл")
            acc = {"key": parts[0], "token": parts[1]}
            boards = await self._trello(acc, "GET", "members/me/boards", params={"fields": "name", "filter": "open"})
            if not boards:
                raise ServiceError("у вас немає жодної дошки Trello — створіть її спершу")
            acc["board"], acc["board_name"] = boards[0]["id"], boards[0]["name"]
            lists = await self._trello(acc, "GET", f"boards/{acc['board']}/lists", params={"filter": "open"})
            if not lists:
                raise ServiceError("на дошці немає жодного списку")
            acc["list"], acc["list_name"] = lists[0]["id"], lists[0]["name"]
        elif sid in ("evernote", "mail"):
            import re as _re

            if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise ServiceError("це не схоже на адресу пошти")
            acc = {"to": text}
        elif sid == "notion":
            acc = {"token": text}
            pages = await self.notion_pages(acc)
            if not pages:
                raise ServiceError("ключ працює, але жодна сторінка не надана інтеграції. У Notion: сторінка → ⋯ → Connections → додайте інтеграцію, потім надішліть ключ ще раз")
            acc["page"], acc["page_name"] = pages[0]
        else:
            raise ServiceError("цей сервіс підключається входом, а не токеном")
        c = self.cfg()
        c["acc"][sid] = acc
        if not c["default"].get(SERVICES[sid]["kinds"][0]):
            c["default"][SERVICES[sid]["kinds"][0]] = sid
        self.save(c)
        return acc

    # ------------------------------------------------------------ iPhone через «Команди»
    def connect_iphone(self):
        c = self.cfg()
        c["acc"]["iphone"] = {"key": secrets.token_urlsafe(18)}
        for k in ("task", "note"):
            if not c["default"].get(k):
                c["default"][k] = "iphone"
        self.save(c)
        return c["acc"]["iphone"]

    def iphone_queue(self):
        return json.loads(self.kv_get("iphone_queue") or "[]")

    def iphone_take(self, key):
        """Для «Команд» на iPhone: віддає нове й позначає як доставлене."""
        acc = self.cfg()["acc"].get("iphone")
        if not acc or not secrets.compare_digest(str(key or ""), acc["key"]):
            return None
        q = self.iphone_queue()
        self.kv_set("iphone_queue", "[]")
        log_ = json.loads(self.kv_get("iphone_sent") or "[]")
        self.kv_set("iphone_sent", json.dumps((q + log_)[:50], ensure_ascii=False))
        return q

    def iphone_check(self, key):
        acc = self.cfg()["acc"].get("iphone")
        return bool(acc) and secrets.compare_digest(str(key or ""), acc["key"])

    # ------------------------------------------------------------ вхід Google / Microsoft
    def redirect(self, sid):
        return f"{self.public_url}/connect/{sid}/callback"

    def login_url(self, sid):
        if not self.oauth_ready(sid):
            return None
        s = SERVICES[sid]
        state = secrets.token_urlsafe(16)
        self.kv_set(f"svc_state:{state}", json.dumps({"sid": sid, "t": time.time()}))
        if s["auth"] == "google":
            q = {"client_id": env("GDRIVE_CLIENT_ID"), "redirect_uri": self.redirect(sid), "response_type": "code", "scope": s["scope"],
                 "access_type": "offline", "prompt": "consent", "state": state}
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q)
        q = {"client_id": env("ONEDRIVE_CLIENT_ID"), "redirect_uri": self.redirect(sid), "response_type": "code", "scope": s["scope"], "state": state}
        return "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?" + urlencode(q)

    def _token_url(self, sid):
        return "https://oauth2.googleapis.com/token" if SERVICES[sid]["auth"] == "google" else "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    def _client(self, sid):
        p = "GDRIVE" if SERVICES[sid]["auth"] == "google" else "ONEDRIVE"
        return env(f"{p}_CLIENT_ID"), env(f"{p}_CLIENT_SECRET")

    async def callback(self, sid, code, state):
        raw = self.kv_get(f"svc_state:{state}") if state else None
        if not raw or json.loads(raw)["sid"] != sid or time.time() - json.loads(raw)["t"] > 900:
            return False, "Посилання для входу застаріло. Відкрийте його в боті ще раз."
        self.kv_set(f"svc_state:{state}", "")
        cid, sec = self._client(sid)
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect(sid), "client_id": cid, "client_secret": sec}
        if SERVICES[sid]["auth"] == "microsoft":
            data["scope"] = SERVICES[sid]["scope"]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(self._token_url(sid), data=data)
        if r.status_code != 200 or not r.json().get("refresh_token"):
            return False, f"Сервіс не дав доступу: {r.text[:200]}"
        tok = r.json()
        c = self.cfg()
        c["acc"][sid] = {"refresh": tok["refresh_token"], "access": tok.get("access_token"), "exp": time.time() + int(tok.get("expires_in", 3000)) - 60}
        kind = SERVICES[sid]["kinds"][0]
        if not c["default"].get(kind):
            c["default"][kind] = sid
        self.save(c)
        return True, f"{SERVICES[sid]['title']} підключено. Можна повертатися в бот."

    async def _access(self, sid, client):
        c = self.cfg()
        acc = c["acc"].get(sid)
        if not acc:
            raise ServiceError(f"{SERVICES[sid]['title']} не підключено")
        if acc.get("access") and acc.get("exp", 0) > time.time():
            return acc["access"]
        cid, sec = self._client(sid)
        data = {"grant_type": "refresh_token", "refresh_token": acc["refresh"], "client_id": cid, "client_secret": sec}
        if SERVICES[sid]["auth"] == "microsoft":
            data["scope"] = SERVICES[sid]["scope"]
        r = await client.post(self._token_url(sid), data=data)
        if r.status_code != 200:
            raise ServiceError(f"доступ до {SERVICES[sid]['title']} відкликано — підключіть знову")
        tok = r.json()
        acc.update(access=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3000)) - 60)
        if tok.get("refresh_token"):
            acc["refresh"] = tok["refresh_token"]
        c["acc"][sid] = acc
        self.save(c)
        return acc["access"]

    # ------------------------------------------------------------ запити до сервісів
    async def _req(self, method, url, headers=None, **kw):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, url, headers=headers or {}, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json()
                msg = msg.get("error", {}).get("message") if isinstance(msg.get("error"), dict) else msg.get("message") or msg.get("error") or r.text
            except ValueError:
                msg = r.text
            raise ServiceError(f"{r.status_code}: {str(msg)[:160]}")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"text": r.text}

    async def _todoist(self, acc, method, path, **kw):
        return await self._req(method, f"{URLS['todoist']}/{path}", {"Authorization": f"Bearer {acc['token']}"}, **kw)

    async def _trello(self, acc, method, path, params=None, **kw):
        return await self._req(method, f"{URLS['trello']}/{path}", params={**(params or {}), "key": acc["key"], "token": acc["token"]}, **kw)

    async def _notion(self, acc, method, path, **kw):
        return await self._req(method, f"{URLS['notion']}/{path}", {"Authorization": f"Bearer {acc['token']}", "Notion-Version": NOTION_VERSION}, **kw)

    async def _oauth(self, sid, method, url, **kw):
        async with httpx.AsyncClient(timeout=30) as c:
            tok = await self._access(sid, c)
        return await self._req(method, url, {"Authorization": f"Bearer {tok}"}, **kw)

    async def notion_pages(self, acc):
        r = await self._notion(acc, "POST", "search", json={"filter": {"property": "object", "value": "page"}, "page_size": 20})
        out = []
        for p in r.get("results", []):
            title = "Без назви"
            for prop in (p.get("properties") or {}).values():
                if prop.get("type") == "title" and prop.get("title"):
                    title = "".join(t.get("plain_text", "") for t in prop["title"]) or title
            out.append((p["id"], title))
        return out

    async def _mstodo_list(self):
        r = await self._oauth("mstodo", "GET", f"{URLS['graph']}/me/todo/lists")
        lists = r.get("value", [])
        main = next((l for l in lists if l.get("wellknownListName") == "defaultList"), lists[0] if lists else None)
        if not main:
            raise ServiceError("у To Do немає жодного списку")
        return main["id"]

    # ------------------------------------------------------------ дії
    async def add(self, sid, kind, title, details="", due=None):
        """Додати завдання чи нотатку. due — природною мовою для Todoist або ISO-дата (2026-11-06)."""
        title = (title or "").strip()[:500] or "Без назви"
        acc = self.cfg()["acc"].get(sid)
        if not acc:
            raise ServiceError(f"{SERVICES[sid]['title']} не підключено")
        if sid == "iphone":
            item = {"id": secrets.token_hex(4), "kind": kind, "title": title, "notes": details or "", "due": None, "added": time.time()}
            if due and due[:4].isdigit():
                item["due"] = due[:10] + ("T" + due[11:16] if len(due) >= 16 else "T09:00")
            q = self.iphone_queue()
            q.append(item)
            self.kv_set("iphone_queue", json.dumps(q[-200:], ensure_ascii=False))
            return "iphone"
        if sid in ("evernote", "mail"):
            await send_mail(acc["to"], title, (("☐ " + title + "\n\n") if kind == "task" and sid == "evernote" else "") + (details or title))
            return "mail"
        if sid == "todoist":
            body = {"content": title, "description": details or ""}
            if due:
                body["due_string" if not due[:4].isdigit() else "due_date"] = due
            r = await self._todoist(acc, "POST", "tasks", json=body)
            return r.get("url") or "https://app.todoist.com"
        if sid == "gtasks":
            body = {"title": title, "notes": details or ""}
            if due and due[:4].isdigit():
                body["due"] = due[:10] + "T00:00:00.000Z"
            await self._oauth(sid, "POST", f"{URLS['gtasks']}/lists/@default/tasks", json=body)
            return "https://tasks.google.com"
        if sid == "mstodo":
            lid = await self._mstodo_list()
            body = {"title": title, "body": {"content": details or "", "contentType": "text"}}
            if due and due[:4].isdigit():
                body["dueDateTime"] = {"dateTime": due[:10] + "T09:00:00", "timeZone": env("BOT_TZ", "Europe/Kyiv")}
            await self._oauth(sid, "POST", f"{URLS['graph']}/me/todo/lists/{lid}/tasks", json=body)
            return "https://to-do.live.com"
        if sid == "trello":
            params = {"idList": acc["list"], "name": title, "desc": details or ""}
            if due and due[:4].isdigit():
                params["due"] = due[:10] + "T09:00:00.000Z"
            r = await self._trello(acc, "POST", "cards", params=params)
            return r.get("shortUrl") or "https://trello.com"
        if sid == "notion":
            children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}}
                        for chunk in _chunks(details or "", 1900)][:90]
            if kind == "task":
                children.insert(0, {"object": "block", "type": "to_do", "to_do": {"rich_text": [{"type": "text", "text": {"content": title[:1900]}}], "checked": False}})
            r = await self._notion(acc, "POST", "pages", json={"parent": {"page_id": acc["page"]},
                                                              "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:200]}}]}},
                                                              "children": children})
            return r.get("url") or "https://notion.so"
        if sid == "onenote":
            from html import escape

            page = f"<!DOCTYPE html><html><head><title>{escape(title[:200])}</title></head><body>" + "".join(f"<p>{escape(p)}</p>" for p in (details or "").split("\n")) + "</body></html>"
            async with httpx.AsyncClient(timeout=30) as c:
                tok = await self._access(sid, c)
                r = await c.post(f"{URLS['graph']}/me/onenote/pages", headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/html"}, content=page.encode())
            if r.status_code >= 400:
                raise ServiceError(f"{r.status_code}: {r.text[:160]}")
            return (r.json().get("links", {}).get("oneNoteWebUrl", {}) or {}).get("href") or "https://onenote.com"
        raise ServiceError("невідомий сервіс")

    async def items(self, sid, limit=15):
        """Список відкритих завдань або останніх нотаток: [(id, текст, чи можна позначити виконаним)]."""
        acc = self.cfg()["acc"].get(sid)
        if not acc:
            raise ServiceError(f"{SERVICES[sid]['title']} не підключено")
        if sid == "iphone":
            q = self.iphone_queue()
            sent = json.loads(self.kv_get("iphone_sent") or "[]")
            return [(i["id"], ("⏳ " if n < len(q) else "📲 ") + ("☐ " if i["kind"] == "task" else "") + i["title"], False) for n, i in enumerate(q + sent)][:limit]
        if sid in ("evernote", "mail"):
            return []
        if sid == "todoist":
            r = await self._todoist(acc, "GET", "tasks", params={"limit": limit})
            res = r.get("results", r if isinstance(r, list) else [])
            return [(t["id"], t["content"] + (f" · {t['due']['string']}" if t.get("due") else ""), True) for t in res[:limit]]
        if sid == "gtasks":
            r = await self._oauth(sid, "GET", f"{URLS['gtasks']}/lists/@default/tasks", params={"showCompleted": "false", "maxResults": limit})
            return [(t["id"], t.get("title", ""), True) for t in r.get("items", [])]
        if sid == "mstodo":
            lid = await self._mstodo_list()
            r = await self._oauth(sid, "GET", f"{URLS['graph']}/me/todo/lists/{lid}/tasks", params={"$filter": "status ne 'completed'", "$top": limit})
            return [(f"{lid}|{t['id']}", t.get("title", ""), True) for t in r.get("value", [])]
        if sid == "trello":
            r = await self._trello(acc, "GET", f"lists/{acc['list']}/cards", params={"fields": "name,dueComplete"})
            return [(t["id"], t["name"], True) for t in r[:limit]]
        if sid == "notion":
            r = await self._notion(acc, "GET", f"blocks/{acc['page']}/children", params={"page_size": 50})
            pages = [(b["id"], b.get("child_page", {}).get("title", ""), False) for b in r.get("results", []) if b.get("type") == "child_page"]
            return pages[-limit:][::-1]
        if sid == "onenote":
            r = await self._oauth(sid, "GET", f"{URLS['graph']}/me/onenote/pages", params={"$top": limit, "$orderby": "lastModifiedDateTime desc", "$select": "id,title"})
            return [(p["id"], p.get("title", ""), False) for p in r.get("value", [])]
        return []

    async def complete(self, sid, item_id):
        acc = self.cfg()["acc"].get(sid)
        if sid == "todoist":
            await self._todoist(acc, "POST", f"tasks/{item_id}/close")
        elif sid == "gtasks":
            await self._oauth(sid, "PATCH", f"{URLS['gtasks']}/lists/@default/tasks/{item_id}", json={"status": "completed"})
        elif sid == "mstodo":
            lid, tid = item_id.split("|", 1)
            await self._oauth(sid, "PATCH", f"{URLS['graph']}/me/todo/lists/{lid}/tasks/{tid}", json={"status": "completed"})
        elif sid == "trello":
            await self._trello(acc, "PUT", f"cards/{item_id}", params={"dueComplete": "true", "closed": "true"})
        else:
            raise ServiceError("нотатки не позначаються виконаними")

    async def choose_targets(self, sid):
        """Куди складати: Notion — сторінки, Trello — списки на дошках."""
        acc = self.cfg()["acc"].get(sid)
        if sid == "notion":
            return await self.notion_pages(acc)
        if sid == "trello":
            out = []
            for b in (await self._trello(acc, "GET", "members/me/boards", params={"fields": "name", "filter": "open"}))[:8]:
                for l in (await self._trello(acc, "GET", f"boards/{b['id']}/lists", params={"filter": "open"}))[:6]:
                    out.append((f"{b['id']}|{l['id']}", f"{b['name']} → {l['name']}"))
            return out
        return []

    def set_target(self, sid, target_id, name):
        c = self.cfg()
        acc = c["acc"][sid]
        if sid == "notion":
            acc["page"], acc["page_name"] = target_id, name
        elif sid == "trello":
            acc["board"], acc["list"] = target_id.split("|", 1)
            acc["board_name"], _, acc["list_name"] = name.partition(" → ")
        self.save(c)


async def send_mail(to, subject, body):
    """Лист через вашу пошту (SMTP_*). Для Gmail — пароль застосунку (myaccount.google.com/apppasswords)."""
    import asyncio
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = env("SMTP_FROM") or env("SMTP_USER")
    msg["To"] = to
    msg["Subject"] = subject[:200]
    msg.set_content(body)

    def go():
        port = int(env("SMTP_PORT", "587") or 587)
        if port == 465:
            with smtplib.SMTP_SSL(env("SMTP_HOST"), port, timeout=30) as s:
                s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
                s.send_message(msg)
        else:
            with smtplib.SMTP(env("SMTP_HOST"), port, timeout=30) as s:
                s.starttls()
                s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
                s.send_message(msg)

    try:
        await asyncio.to_thread(go)
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"лист не надіслано: {e}") from e


def _chunks(text, n):
    return [text[i : i + n] for i in range(0, len(text), n)] or []
