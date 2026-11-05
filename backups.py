"""Резервні копії бази бота: файлом у чат, у приватний канал Telegram, на Google Диск, Dropbox і OneDrive.

Спільний модуль для ШІ-бота й головного бота. Копія — це стиснута (gzip) база SQLite;
якщо задано BACKUP_PASSWORD, вона ще й шифрується (AES-256-GCM), і без пароля її ніхто не прочитає.

Хмари підключаються через OAuth: бот дає посилання, ви входите у свій акаунт, і бот отримує доступ
лише до власної теки з копіями (Google: drive.file, Dropbox: App folder, OneDrive: AppFolder).
"""

import base64
from datetime import datetime
from html import escape as esc
from pathlib import Path
import gzip
import io
import tarfile
import zipfile
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing
from urllib.parse import urlencode

import httpx

log = logging.getLogger("backups")


def env(name, default=""):
    return os.environ.get(name, default).strip()


MAGIC_ENC = b"AIBK1"  # початок зашифрованої копії
KEEP = int(env("BACKUP_KEEP", "10") or 10)  # скільки останніх копій тримати в кожній хмарі

CLOUDS = {
    "gdrive": {
        "title": "Google Диск", "icon": "🟢", "id_env": "GDRIVE_CLIENT_ID", "secret_env": "GDRIVE_CLIENT_SECRET",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/drive.file", "free": "15 ГБ безкоштовно",
    },
    "dropbox": {
        "title": "Dropbox", "icon": "🔵", "id_env": "DROPBOX_APP_KEY", "secret_env": "DROPBOX_APP_SECRET",
        "auth": "https://www.dropbox.com/oauth2/authorize", "token": "https://api.dropboxapi.com/oauth2/token",
        "scope": "", "free": "2 ГБ безкоштовно",
    },
    "onedrive": {
        "title": "OneDrive", "icon": "🟦", "id_env": "ONEDRIVE_CLIENT_ID", "secret_env": "ONEDRIVE_CLIENT_SECRET",
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "Files.ReadWrite.AppFolder offline_access User.Read", "free": "5 ГБ безкоштовно",
    },
}
URLS = {  # можна підмінити в тестах
    "gdrive_api": "https://www.googleapis.com", "dropbox_api": "https://api.dropboxapi.com",
    "dropbox_content": "https://content.dropboxapi.com", "graph": "https://graph.microsoft.com/v1.0",
}


class BackupError(Exception):
    pass


class Backups:
    def __init__(self, db_path, kv_get, kv_set, public_url, name="bot", route_prefix="/backup", project_root=None):
        self.db_path = db_path
        self.kv_get = kv_get
        self.kv_set = kv_set
        self.public_url = (public_url or "").rstrip("/")
        self.name = name  # префікс у назві файлу: bot / manager
        self.route_prefix = route_prefix
        self.project_root = os.path.abspath(project_root or os.path.dirname(__file__))
        self.on_connected = None  # async fn(cloud, ok, detail) — повідомити власника після входу

    # ------------------------------------------------------------ створення й перевірка копії

    def _snapshot_db(self):
        """Консистентний знімок SQLite у байтах без зупинки бота."""
        tmp = tempfile.mktemp(suffix=".db")
        try:
            with closing(sqlite3.connect(self.db_path)) as src, closing(sqlite3.connect(tmp)) as dst:
                src.backup(dst)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def formats(self):
        allowed = ("zip", "targz", "dbgz")
        vals = [x for x in self.cfg().get("formats", ["zip"]) if x in allowed]
        return vals or ["zip"]

    def set_formats(self, formats):
        allowed = ("zip", "targz", "dbgz")
        vals = []
        for x in formats:
            if x in allowed and x not in vals:
                vals.append(x)
        c = self.cfg()
        c["formats"] = vals or ["zip"]
        self.save_cfg(c)

    def toggle_format(self, fmt):
        vals = self.formats()
        if fmt in vals:
            if len(vals) == 1:
                return False
            vals.remove(fmt)
        else:
            vals.append(fmt)
        self.set_formats(vals)
        return True

    def scope(self):
        return self.cfg().get("scope", "data") if self.cfg().get("scope") in ("data", "full") else "data"

    def set_scope(self, scope):
        c = self.cfg()
        c["scope"] = scope if scope in ("data", "full") else "data"
        self.save_cfg(c)

    def set_auto(self, enabled):
        c = self.cfg()
        c["auto_enabled"] = bool(enabled)
        self.save_cfg(c)

    def _safe_project_files(self):
        """Файли коду/документації без секретів, БД, кешів і старих копій."""
        allowed_ext = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini"}
        allowed_names = {"Dockerfile", "Caddyfile", "requirements.txt", ".env.example"}
        out = []
        root = self.project_root
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "node_modules"}]
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                low = fn.lower()
                if fn == ".env" or (fn.startswith(".env.") and fn != ".env.example"):
                    continue
                if low.endswith((".db", ".sqlite", ".sqlite3", ".pyc", ".log", ".enc")) or "-backup-" in low:
                    continue
                if fn not in allowed_names and Path(fn).suffix.lower() not in allowed_ext:
                    continue
                try:
                    if os.path.getsize(full) > 5 * 1024 * 1024:
                        continue
                    with open(full, "rb") as f:
                        out.append((f"project/{rel}", f.read()))
                except OSError:
                    continue
        return out

    def _archive_entries(self, db_raw, scope):
        manifest = {
            "backup_version": 2,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "bot": self.name,
            "scope": scope,
            "contains_secrets": False,
            "restore_note": "Автоматичне відновлення ботом відновлює базу даних. Файли project/ у повній копії призначені для ручного відновлення коду.",
        }
        entries = [(f"data/{self.name}.db", db_raw), ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())]
        if scope == "full":
            entries.extend(self._safe_project_files())
        return entries

    def make_format(self, fmt="zip", scope=None):
        """Створює одну копію у вибраному форматі: zip / tar.gz / db.gz."""
        scope = scope or self.scope()
        db_raw = self._snapshot_db()
        stamp = time.strftime("%Y-%m-%d_%H-%M")
        if fmt == "dbgz":
            data = gzip.compress(db_raw, 9)
            ext = "db.gz"
        elif fmt == "targz":
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w:gz") as tf:
                for arcname, body in self._archive_entries(db_raw, scope):
                    info = tarfile.TarInfo(arcname)
                    info.size = len(body)
                    info.mtime = int(time.time())
                    tf.addfile(info, io.BytesIO(body))
            data = bio.getvalue()
            ext = "tar.gz"
        else:
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                for arcname, body in self._archive_entries(db_raw, scope):
                    zf.writestr(arcname, body)
            data = bio.getvalue()
            ext = "zip"
        name = f"{self.name}-backup-{stamp}.{ext}"
        pw = env("BACKUP_PASSWORD")
        if pw:
            data = encrypt(data, pw)
            name += ".enc"
        return name, data

    def make(self):
        """Зворотна сумісність: створює перший із вибраних форматів."""
        return self.make_format(self.formats()[0], self.scope())

    def make_all(self):
        return [self.make_format(fmt, self.scope()) for fmt in self.formats()]

    def _db_from_container(self, blob):
        """Дістає SQLite із ZIP/TAR.GZ/DB.GZ або сирої БД."""
        if blob[:16] == b"SQLite format 3\x00":
            return blob
        bio = io.BytesIO(blob)
        if zipfile.is_zipfile(bio):
            bio.seek(0)
            with zipfile.ZipFile(bio) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".db")]
                if not names:
                    raise BackupError("у ZIP немає бази .db")
                preferred = f"data/{self.name}.db"
                return zf.read(preferred if preferred in names else names[0])
        bio.seek(0)
        try:
            with tarfile.open(fileobj=bio, mode="r:*") as tf:
                members = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(".db")]
                if not members:
                    raise BackupError("у TAR.GZ немає бази .db")
                preferred = next((m for m in members if m.name == f"data/{self.name}.db"), members[0])
                f = tf.extractfile(preferred)
                if not f:
                    raise BackupError("не вдалося прочитати базу з TAR.GZ")
                return f.read()
        except tarfile.ReadError:
            pass
        if blob[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(blob)
            except OSError as e:
                raise BackupError("GZIP-файл пошкоджений") from e
            if raw[:16] == b"SQLite format 3\x00":
                return raw
        raise BackupError("невідомий формат копії. Підтримуються ZIP, TAR.GZ, DB.GZ та їх .enc")

    def unpack(self, blob):
        """Байти копії -> тимчасова SQLite БД; підтримує ZIP/TAR.GZ/DB.GZ і шифровані .enc."""
        if blob[: len(MAGIC_ENC)] == MAGIC_ENC:
            pw = env("BACKUP_PASSWORD")
            if not pw:
                raise BackupError("копія зашифрована, а BACKUP_PASSWORD не задано")
            blob = decrypt(blob, pw)
        raw = self._db_from_container(blob)
        if raw[:16] != b"SQLite format 3\x00":
            raise BackupError("це не база бота (не SQLite)")
        tmp = tempfile.mktemp(suffix=".db")
        with open(tmp, "wb") as f:
            f.write(raw)
        try:
            with closing(sqlite3.connect(tmp)) as con:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("база в копії пошкоджена")
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "kv" not in tables:
                raise BackupError("у копії немає потрібних таблиць — це копія іншої програми?")
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return tmp, tables

    def restore(self, blob):
        """Відновлює дані з будь-якого підтриманого формату; файли project/ не перезаписує автоматично."""
        tmp, tables = self.unpack(blob)
        safe = f"{self.db_path}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        with closing(sqlite3.connect(self.db_path)) as live, closing(sqlite3.connect(safe)) as keep:
            live.backup(keep)
        with closing(sqlite3.connect(tmp)) as src, closing(sqlite3.connect(self.db_path)) as live:
            src.backup(live)
        os.remove(tmp)
        return safe, tables

    # ------------------------------------------------------------ налаштування

    def cfg(self):
        raw = self.kv_get("backup_cfg")
        # Без явної дії користувача автоматичні копії НЕ запускаються.
        c = {"days": 0, "auto_enabled": False, "targets": {"file": True}, "channel": None,
             "tokens": {}, "log": [], "formats": ["zip"], "scope": "data"}
        if raw:
            saved = json.loads(raw)
            c.update(saved)
            # Міграція старої версії: навіть якщо там було days=7, це не вважаємо явною згодою на автоархівацію.
            if "auto_enabled" not in saved:
                c["auto_enabled"] = False
            if not saved.get("formats"):
                c["formats"] = ["zip"]
            if saved.get("scope") not in ("data", "full"):
                c["scope"] = "data"
        return c

    def save_cfg(self, c):
        self.kv_set("backup_cfg", json.dumps(c))

    def configured(self, cloud):
        m = CLOUDS[cloud]
        return bool(env(m["id_env"]) and env(m["secret_env"]) and self.public_url)

    def connected(self, cloud):
        return bool(self.cfg()["tokens"].get(cloud, {}).get("refresh"))

    def enabled(self, target):
        c = self.cfg()
        if target == "channel":
            return bool(c.get("channel")) and c["targets"].get("channel", True)
        if target in CLOUDS:
            return self.connected(target) and c["targets"].get(target, True)
        return c["targets"].get(target, True)

    def toggle(self, target):
        c = self.cfg()
        c["targets"][target] = not c["targets"].get(target, True)
        self.save_cfg(c)

    def set_days(self, days):
        c = self.cfg()
        c["days"] = max(0, min(30, int(days)))
        c["auto_enabled"] = bool(c["days"])
        self.save_cfg(c)

    def set_channel(self, chat_id, title):
        c = self.cfg()
        c["channel"] = {"id": chat_id, "title": title} if chat_id else None
        c["targets"]["channel"] = True
        self.save_cfg(c)

    def note(self, target, name, ok, detail=""):
        c = self.cfg()
        c["log"] = ([{"t": time.time(), "target": target, "name": name, "ok": ok, "detail": str(detail)[:200]}] + c.get("log", []))[:40]
        if ok:
            c["last"] = time.time()
            c.setdefault("lastt", {})[target] = time.time()
        self.save_cfg(c)

    # ---- розклад: для кожного місця свій (як часто, о котрій, пауза, зупинка)
    def sched(self, target):
        c = self.cfg()
        if not c.get("auto_enabled", False):
            return {"paused": True}
        own = (c.get("sched") or {}).get(target)
        if own is not None:
            return own
        return {"every": c["days"] * 86400} if c["days"] else {"paused": True}

    def has_own_sched(self, target):
        return target in (self.cfg().get("sched") or {})

    def set_sched(self, target, s):
        c = self.cfg()
        c.setdefault("sched", {})
        if s is None:
            c["sched"].pop(target, None)
        else:
            c["sched"][target] = s
            if not s.get("paused"):
                c["auto_enabled"] = True
        self.save_cfg(c)

    def sched_text(self, target):
        from hubclient import schedule_text

        s = self.sched(target)
        if target == "file" and not self.has_own_sched("file") and any(self.enabled(t) for t in ("channel", *CLOUDS)):
            return "вручну (автокопії йдуть у хмару; можна задати й сюди)"
        if s.get("paused"):
            return "⏹ автоматичні копії зупинено"
        if s.get("every") and not (s.get("daily") or s.get("weekly")):
            d = int(s["every"]) // 86400
            base = f"кожні {d} дн" if d != 1 else "щодня"
            if s.get("pause_until") and s["pause_until"] > time.time():
                return "⏸ пауза до " + time.strftime("%d.%m", time.localtime(s["pause_until"])) + " · потім " + base
            return base
        return schedule_text(s)

    def due_targets(self):
        """Місця, куди пора робити автоматичну копію за їхнім розкладом."""
        from hubclient import schedule_due

        if not self.cfg().get("auto_enabled", False):
            return []
        last = self.cfg().get("lastt") or {}
        others = any(self.enabled(t) for t in ("channel", *CLOUDS))
        out = []
        for t in ("file", "channel", *CLOUDS):
            if not self.enabled(t):
                continue
            if t == "file" and others and not self.has_own_sched("file"):
                continue  # файл у чат автоматично — лише якщо немає іншого місця або ви задали йому свій розклад
            if schedule_due(self.sched(t), last.get(t, self.cfg().get("last", 0))):
                out.append(t)
        return out

    def due(self):
        return bool(self.due_targets())

    # ------------------------------------------------------------ OAuth

    def redirect(self, cloud):
        return f"{self.public_url}{self.route_prefix}/{cloud}/callback"

    def login_url(self, cloud):
        if not self.configured(cloud):
            return None
        m = CLOUDS[cloud]
        state = secrets.token_urlsafe(16)
        self.kv_set(f"bk_state:{state}", json.dumps({"cloud": cloud, "t": time.time()}))
        q = {"client_id": env(m["id_env"]), "redirect_uri": self.redirect(cloud), "response_type": "code", "state": state}
        if cloud == "gdrive":
            q.update(scope=m["scope"], access_type="offline", prompt="consent")
        elif cloud == "dropbox":
            q.update(token_access_type="offline")
        else:
            q.update(scope=m["scope"])
        return f"{m['auth']}?{urlencode(q)}"

    async def callback(self, cloud, code, state):
        """Обробляє повернення з OAuth. Повертає (ok, текст для сторінки)."""
        raw = self.kv_get(f"bk_state:{state}") if state else None
        if not raw or json.loads(raw)["cloud"] != cloud or time.time() - json.loads(raw)["t"] > 900:
            return False, "Посилання для входу застаріло. Відкрийте його в боті ще раз."
        self.kv_set(f"bk_state:{state}", "")
        m = CLOUDS[cloud]
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect(cloud),
                "client_id": env(m["id_env"]), "client_secret": env(m["secret_env"])}
        if cloud == "onedrive":
            data["scope"] = m["scope"]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(m["token"], data=data)
        if r.status_code != 200:
            return False, f"Сервіс відмовив у доступі: {r.text[:200]}"
        tok = r.json()
        if not tok.get("refresh_token"):
            return False, "Сервіс не видав постійного доступу (refresh token). Спробуйте ще раз."
        c = self.cfg()
        c["tokens"][cloud] = {"refresh": tok["refresh_token"], "access": tok.get("access_token"), "exp": time.time() + int(tok.get("expires_in", 3000)) - 60}
        c["targets"][cloud] = True
        self.save_cfg(c)
        return True, f"{m['title']} підключено. Можна повертатися в Telegram."

    def disconnect(self, cloud):
        c = self.cfg()
        c["tokens"].pop(cloud, None)
        self.save_cfg(c)

    async def access(self, cloud, client):
        c = self.cfg()
        t = c["tokens"].get(cloud)
        if not t:
            raise BackupError(f"{CLOUDS[cloud]['title']} не підключено")
        if t.get("access") and t.get("exp", 0) > time.time():
            return t["access"]
        m = CLOUDS[cloud]
        data = {"grant_type": "refresh_token", "refresh_token": t["refresh"], "client_id": env(m["id_env"]), "client_secret": env(m["secret_env"])}
        if cloud == "onedrive":
            data["scope"] = m["scope"]
        r = await client.post(m["token"], data=data)
        if r.status_code != 200:
            raise BackupError(f"доступ до {m['title']} відкликано або прострочено — підключіть знову ({r.status_code})")
        tok = r.json()
        t.update(access=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3000)) - 60)
        if tok.get("refresh_token"):
            t["refresh"] = tok["refresh_token"]
        c["tokens"][cloud] = t
        self.save_cfg(c)
        return t["access"]

    # ------------------------------------------------------------ хмари: завантажити, список, скачати, видалити

    async def upload(self, cloud, name, data):
        async with httpx.AsyncClient(timeout=120) as c:
            h = {"Authorization": f"Bearer {await self.access(cloud, c)}"}
            if cloud == "gdrive":
                folder = await self._gdrive_folder(c, h)
                meta = {"name": name, "parents": [folder]}
                r = await c.post(f"{URLS['gdrive_api']}/upload/drive/v3/files?uploadType=resumable", headers={**h, "Content-Type": "application/json"}, json=meta)
                r.raise_for_status()
                r = await c.put(r.headers["Location"], content=data, headers={"Content-Type": "application/octet-stream"})
            elif cloud == "dropbox":
                arg = json.dumps({"path": f"/{name}", "mode": "add", "autorename": True})
                r = await c.post(f"{URLS['dropbox_content']}/2/files/upload", headers={**h, "Dropbox-API-Arg": arg, "Content-Type": "application/octet-stream"}, content=data)
            else:
                r = await c.post(f"{URLS['graph']}/me/drive/special/approot:/{name}:/createUploadSession", headers=h, json={"item": {"@microsoft.graph.conflictBehavior": "rename"}})
                r.raise_for_status()
                url, step, total = r.json()["uploadUrl"], 3_276_800, len(data)
                for start in range(0, total, step):
                    chunk = data[start : start + step]
                    r = await c.put(url, content=chunk, headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"})
                    if r.status_code not in (200, 201, 202):
                        break
            if r.status_code not in (200, 201):
                raise BackupError(f"{CLOUDS[cloud]['title']}: {r.status_code} {r.text[:150]}")
        await self.prune(cloud)

    async def _gdrive_folder(self, c, h):
        cfg = self.cfg()
        fid = cfg["tokens"]["gdrive"].get("folder")
        if fid:
            return fid
        r = await c.post(f"{URLS['gdrive_api']}/drive/v3/files", headers=h, json={"name": f"Резервні копії {self.name}", "mimeType": "application/vnd.google-apps.folder"})
        r.raise_for_status()
        fid = r.json()["id"]
        cfg = self.cfg()
        cfg["tokens"]["gdrive"]["folder"] = fid
        self.save_cfg(cfg)
        return fid

    async def list(self, cloud):
        """[{id, name, size, t}] від нових до старих."""
        async with httpx.AsyncClient(timeout=60) as c:
            h = {"Authorization": f"Bearer {await self.access(cloud, c)}"}
            if cloud == "gdrive":
                folder = await self._gdrive_folder(c, h)
                r = await c.get(f"{URLS['gdrive_api']}/drive/v3/files", headers=h, params={
                    "q": f"'{folder}' in parents and trashed=false", "fields": "files(id,name,size,createdTime)", "orderBy": "createdTime desc", "pageSize": 100})
                r.raise_for_status()
                out = [{"id": f["id"], "name": f["name"], "size": int(f.get("size", 0)), "t": f.get("createdTime", "")} for f in r.json().get("files", [])]
            elif cloud == "dropbox":
                r = await c.post(f"{URLS['dropbox_api']}/2/files/list_folder", headers=h, json={"path": ""})
                r.raise_for_status()
                out = [{"id": e["path_lower"], "name": e["name"], "size": e.get("size", 0), "t": e.get("server_modified", "")}
                       for e in r.json().get("entries", []) if e.get(".tag") == "file"]
            else:
                r = await c.get(f"{URLS['graph']}/me/drive/special/approot/children", headers=h)
                r.raise_for_status()
                out = [{"id": e["id"], "name": e["name"], "size": e.get("size", 0), "t": e.get("createdDateTime", "")}
                       for e in r.json().get("value", []) if "file" in e]
        out = [x for x in out if "-backup-" in x["name"]]
        out.sort(key=lambda x: (x["t"], x["name"]), reverse=True)
        return out

    async def download(self, cloud, file_id):
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            h = {"Authorization": f"Bearer {await self.access(cloud, c)}"}
            if cloud == "gdrive":
                r = await c.get(f"{URLS['gdrive_api']}/drive/v3/files/{file_id}", headers=h, params={"alt": "media"})
            elif cloud == "dropbox":
                r = await c.post(f"{URLS['dropbox_content']}/2/files/download", headers={**h, "Dropbox-API-Arg": json.dumps({"path": file_id})})
            else:
                r = await c.get(f"{URLS['graph']}/me/drive/items/{file_id}/content", headers=h)
            if r.status_code != 200:
                raise BackupError(f"не вдалося скачати копію: {r.status_code}")
            return r.content

    async def delete(self, cloud, file_id):
        async with httpx.AsyncClient(timeout=60) as c:
            h = {"Authorization": f"Bearer {await self.access(cloud, c)}"}
            if cloud == "gdrive":
                await c.delete(f"{URLS['gdrive_api']}/drive/v3/files/{file_id}", headers=h)
            elif cloud == "dropbox":
                await c.post(f"{URLS['dropbox_api']}/2/files/delete_v2", headers=h, json={"path": file_id})
            else:
                await c.delete(f"{URLS['graph']}/me/drive/items/{file_id}", headers=h)

    async def prune(self, cloud):
        """Лишає KEEP останніх копій, старіші видаляє."""
        try:
            files = await self.list(cloud)
            for f in files[KEEP:]:
                await self.delete(cloud, f["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("не вдалося прибрати старі копії в %s: %s", cloud, e)

    # ------------------------------------------------------------ усе разом

    async def run(self, send_file, send_channel, only=None):
        """Створює всі вибрані формати й надсилає у вибрані місця.
        Якщо only задано — це ручна явна дія, тому місце не мусить бути увімкнене для авто."""
        artifacts = self.make_all()
        results = []
        targets = only or ["file", "channel", *CLOUDS]
        forced = only is not None
        for t in targets:
            if not forced and not self.enabled(t):
                continue
            try:
                sent_names = []
                for name, data in artifacts:
                    if t == "file":
                        await send_file(name, data)
                    elif t == "channel":
                        ch = self.cfg().get("channel")
                        if not ch:
                            raise BackupError("приватний канал не підключено")
                        fid = await send_channel(ch["id"], name, data)
                        c = self.cfg()
                        c.setdefault("channel_files", []).insert(0, {"name": name, "file_id": fid, "t": time.time(), "size": len(data)})
                        c["channel_files"] = c["channel_files"][:50]
                        self.save_cfg(c)
                    else:
                        if not self.connected(t):
                            raise BackupError(f"{CLOUDS[t]['title']} не підключено")
                        await self.upload(t, name, data)
                    sent_names.append(name)
                summary = ", ".join(sent_names)
                self.note(t, summary, True)
                results.append((t, True, f"{len(sent_names)} файл(и)"))
            except Exception as e:  # noqa: BLE001
                log.warning("копія в %s не вдалася: %s", t, e)
                self.note(t, "backup", False, e)
                results.append((t, False, str(e)[:200]))
        summary_name = ", ".join(name for name, _ in artifacts)
        total_size = sum(len(data) for _, data in artifacts)
        return summary_name, total_size, results


TARGET_TITLES = {"file": ("📱", "Файлом у чат"), "channel": ("📢", "Приватний канал Telegram")} | {k: (v["icon"], v["title"]) for k, v in CLOUDS.items()}


# ------------------------------------------------------------ шифрування


def _key(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)


def encrypt(data, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC_ENC + salt + nonce + AESGCM(_key(pw, salt)).encrypt(nonce, data, None)


def decrypt(blob, pw):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    n = len(MAGIC_ENC)
    salt, nonce, body = blob[n : n + 16], blob[n + 16 : n + 28], blob[n + 28 :]
    try:
        return AESGCM(_key(pw, salt)).decrypt(nonce, body, None)
    except InvalidTag as e:
        raise BackupError("неправильний BACKUP_PASSWORD — копію не розшифрувати") from e


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# ------------------------------------------------------------ екрани (спільні для обох ботів): (текст, кнопки)


FORMAT_LABELS = {"zip": "ZIP", "targz": "TAR.GZ", "dbgz": "DB.GZ"}


def scr_backups(BK, TZ):
    c = BK.cfg()
    last = c.get("last")
    formats = " + ".join(FORMAT_LABELS.get(x, x.upper()) for x in BK.formats())
    scope = "Повна копія OrionAI" if BK.scope() == "full" else "Лише дані бота"
    auto = "✅ увімкнено" if c.get("auto_enabled") else "⏹ вимкнено"
    lines = [
        "<b>💾 Резервні копії</b>", "",
        "За замовчуванням бот <b>нічого не архівує автоматично</b>. Автокопії почнуться лише після того, як ви самі задасте розклад.",
        f"Автокопії: <b>{auto}</b>",
        f"Формати: <b>{formats}</b>",
        f"Вміст: <b>{scope}</b>",
        f"Остання копія: <b>{datetime.fromtimestamp(last, TZ).strftime('%d.%m %H:%M') if last else 'ще не було'}</b>",
        f"Шифрування: <b>{'✅ BACKUP_PASSWORD' if env('BACKUP_PASSWORD') else '▫️ без пароля'}</b>",
        "",
        "<b>Куди зберігати:</b>",
    ]
    rows = [[("💾 Зробити копію зараз", "m:bkgo")],
            [("📦 Формати", "m:bkfmt"), ("🗂 Вміст копії", "m:bkscope")]]
    for t in ("file", "channel", *CLOUDS):
        icon, title = TARGET_TITLES[t]
        if t == "file":
            state = "✅" if BK.enabled(t) else "▫️"
            lines.append(f"{state} {icon} {title} · {BK.sched_text(t)}")
        elif t == "channel":
            ch = c.get("channel")
            state = (("✅" if BK.enabled(t) else "▫️") + f" {esc(ch['title'])} · {BK.sched_text(t)}") if ch else "▫️ не підключено"
            lines.append(f"{icon} {title}: {state}")
        else:
            if BK.connected(t):
                state = ("✅ підключено" if BK.enabled(t) else "▫️ вимкнено") + f" · {BK.sched_text(t)}"
            elif BK.configured(t):
                state = "▫️ можна підключити"
            else:
                state = f"▫️ потрібні {CLOUDS[t]['id_env']} і {CLOUDS[t]['secret_env']}" + ("" if BK.public_url else " та PUBLIC_URL")
            lines.append(f"{icon} {title}: {state}")
        rows.append([(f"{icon} {title}", f"m:bkt:{t}")])
    rows.append([("♻️ Відновити", "m:bkrs"), ("ℹ️ Довідка", "help:g:safety")])
    return "\n".join(lines), rows


def scr_backup_formats(BK):
    active = set(BK.formats())
    lines = ["<b>📦 Формати резервної копії</b>", "",
             "Можна вибрати <b>кілька форматів одночасно</b>. При створенні копії бот надішле окремий файл кожного вибраного формату.", "",
             "• <b>ZIP</b> — найзручніше для iPhone і ручного перегляду.",
             "• <b>TAR.GZ</b> — універсальний архів для серверів/Linux.",
             "• <b>DB.GZ</b> — компактна технічна копія лише SQLite-бази."]
    rows = [[(("✅ " if fmt in active else "▫️ ") + label, f"m:bkfmtog:{fmt}")] for fmt, label in FORMAT_LABELS.items()]
    rows.append([("« Копії", "m:bk")])
    return "\n".join(lines), rows


def scr_backup_scope(BK):
    cur = BK.scope()
    lines = ["<b>🗂 Що включати в копію</b>", "",
             "<b>Дані бота</b> — розмови, теки, завдання, нотатки, медіа-метадані та налаштування з SQLite.", "",
             "<b>Повна копія OrionAI</b> — база + доступні на сервері файли коду та документації. <b>Секрети, .env, токени й ключі не додаються.</b>", "",
             "Під час автоматичного відновлення бот відновлює базу даних. Файли коду з повної копії призначені для ручного відновлення через GitHub."]
    rows = [[(("✅ " if cur == "data" else "▫️ ") + "Лише дані", "m:bkscopev:data")],
            [(("✅ " if cur == "full" else "▫️ ") + "Повна копія OrionAI", "m:bkscopev:full")],
            [("« Копії", "m:bk")]]
    return "\n".join(lines), rows

def scr_backup_target(BK, t, ask_channel):
    icon, title = TARGET_TITLES[t]
    c = BK.cfg()
    lines = [f"<b>{icon} {title}</b>", "", f"⏱ Автокопії сюди: <b>{BK.sched_text(t)}</b>", ""]
    rows = [[("⏱ Розклад копій", f"m:bks:{t}")]]
    if t == "file":
        lines.append("Бот надсилає копію файлом сюди, у чат. Збережіть її у «Файли» на телефоні — тоді копія буде у вас на пристрої.")
        rows.append([("✅ Місце ввімкнено" if BK.enabled(t) else "▫️ Місце вимкнено", "m:bktog:file")])
        rows.append([("📱 Надіслати файл зараз", "m:bkgo:file")])
    elif t == "channel":
        ch = c.get("channel")
        if ch:
            lines.append(f"Канал: <b>{esc(ch['title'])}</b> (<code>{ch['id']}</code>)\nКопій у каналі: {len(c.get('channel_files', []))}")
            rows.append([("✅ Місце ввімкнено" if BK.enabled(t) else "▫️ Місце вимкнено", "m:bktog:channel"), ("✖️ Відключити", "m:bkchx")])
            rows += [[(f"♻️ {f['name'][-26:]}", f"m:bkrc:channel:{n}")] for n, f in enumerate(c.get("channel_files", [])[:5])]
        else:
            lines.append("1. Створіть <b>приватний</b> канал у Telegram (наприклад, «Копії бота»).\n"
                         "2. Додайте цього бота в канал адміністратором з правом публікувати.\n"
                         "3. Перешліть сюди будь-який допис із каналу — і я його запам'ятаю.\n\n"
                         "Плюси: без лімітів місця, нічого не треба підключати. Файл до 20 МБ бот зможе й відновити сам.")
            ask_channel()
            rows.append([("✖️ Скасувати", "m:bk")])
    else:
        m = CLOUDS[t]
        if BK.connected(t):
            lines.append(f"✅ Підключено. Копії лежать в окремій теці бота ({m['free']}). Зберігаються останні {KEEP}.")
            rows.append([("📂 Копії й відновлення", f"m:bkls:{t}"), ("☁️ Копію зараз", f"m:bkgo:{t}")])
            rows.append([("✅ Місце ввімкнено" if BK.enabled(t) else "▫️ Місце вимкнено", f"m:bktog:{t}"), ("✖️ Відключити", f"m:bkdis:{t}")])
        elif BK.configured(t):
            lines.append(f"Натисніть «Увійти», оберіть свій акаунт і дозвольте доступ. Бот бачитиме лише власну теку з копіями. {m['free']}.")
            rows.append([(f"🔗 Увійти в {title}", "url:" + BK.login_url(t))])
        else:
            lines.append(f"Щоб підключити {title}, власник бота має один раз створити безкоштовний ключ застосунку й додати "
                         f"<code>{m['id_env']}</code> і <code>{m['secret_env']}</code> у налаштування хостингу"
                         + ("" if BK.public_url else ", а також <code>PUBLIC_URL</code>") + ". Покроково — в інструкції «Резервні копії» в архіві з ботом.")
    rows.append([("« Копії", "m:bk")])
    return "\n".join(lines), rows


async def scr_backup_list(BK, chat_id, t):
    icon, title = TARGET_TITLES[t]
    try:
        files = await BK.list(t)
    except Exception as e:  # noqa: BLE001
        return f"<b>{icon} {title}</b>\n\n⚠️ {esc(e)}", [[("« Назад", f"m:bkt:{t}")]]
    lines = [f"<b>{icon} Копії в {title}</b> · {len(files)}", ""]
    lines += [f"{n}. {esc(f['name'])} · {human_size(f['size'])}" for n, f in enumerate(files[:10], 1)] or ["<i>Поки порожньо.</i>"]
    BK.kv_set(f"bkls:{chat_id}:{t}", json.dumps([f["id"] for f in files[:10]]))
    rows = [[(f"♻️ {n}. Відновити", f"m:bkrc:{t}:{n - 1}")] for n in range(1, min(10, len(files)) + 1)]
    rows.append([("« Назад", f"m:bkt:{t}")])
    return "\n".join(lines), rows


def scr_restore(BK):
    """Меню відновлення: звідки взяти копію."""
    c = BK.cfg()
    lines = ["<b>♻️ Відновлення з копії</b>", "", "Звідки взяти копію?", "",
             "📱 <b>З телефона:</b> просто надішліть файл копії: <code>.zip</code>, <code>.tar.gz</code>, <code>.db.gz</code> або зашифрований <code>.enc</code>.",
             "Перед відновленням я покажу, що в копії, і збережу поточну базу поруч — помилку можна відкотити."]
    rows = []
    for t in CLOUDS:
        if BK.connected(t):
            icon, title = TARGET_TITLES[t]
            rows.append([(f"{icon} {title}: обрати копію", f"m:bkls:{t}")])
    files = c.get("channel_files", [])
    if c.get("channel") and files:
        rows.append([(f"📢 Канал · копій: {len(files)}", "m:bkt:channel")])
    if not rows:
        lines += ["", "<i>Хмари ще не підключені — підключіть Google Диск, Dropbox або OneDrive у «💾 Резервні копії».</i>"]
    return "\n".join(lines), rows


BK_INTERVALS = [(86400, "щодня"), (3 * 86400, "3 дні"), (7 * 86400, "тиждень"), (30 * 86400, "місяць")]
BK_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def scr_bk_sched(BK, t):
    icon, title = TARGET_TITLES[t]
    s = BK.sched(t)
    cur = s.get("every") if not (s.get("daily") or s.get("weekly") or s.get("paused")) else None
    lines = [f"<b>⏱ Розклад копій: {icon} {title}</b>", "", f"Зараз: <b>{BK.sched_text(t)}</b>" + ("" if BK.has_own_sched(t) else " (типовий)")]
    rows = [[(("● " if cur == sec else "") + label, f"m:bkse:{t}:{sec}") for sec, label in BK_INTERVALS],
            [(("● " if s.get("daily") else "") + "📅 Щодня о…", f"m:bksd:{t}"), (("● " if s.get("weekly") else "") + "🗓 Раз на тиждень…", f"m:bksw:{t}")]]
    if s.get("paused"):
        rows.append([("▶️ Відновити автокопії", f"m:bkstart:{t}")])
    elif s.get("pause_until") and s["pause_until"] > time.time():
        rows.append([("▶️ Зняти паузу", f"m:bkstart:{t}")])
    else:
        rows.append([("⏸ Пауза на тиждень", f"m:bkspause:{t}"), ("⏹ Зупинити", f"m:bkstop:{t}")])
    rows.append([("↺ Типовий розклад", f"m:bksx:{t}"), ("« Назад", f"m:bkt:{t}")])
    return "\n".join(lines), rows


def handle_bk_sched(BK, parts, ask):
    """Дії розкладу копій для обох ботів. Повертає (екран, підказка) або None."""
    a, t = parts[1], parts[2] if len(parts) > 2 else None
    if a not in ("bks", "bkse", "bksd", "bksw", "bksw2", "bkspause", "bkstop", "bkstart", "bksx") or t not in TARGET_TITLES:
        return None
    s = dict(BK.sched(t))
    tip = None
    if a == "bkse":
        BK.set_sched(t, {"every": int(parts[3])})
        tip = "⏱ Збережено"
    elif a == "bksd":
        ask("bkd", t, "📅 О котрій робити копію щодня? Надішліть час, наприклад <code>03:00</code>.")
        return None, None
    elif a == "bksw":
        return ("🗓 У який день тижня?", [[(d, f"m:bksw2:{t}:{n}") for n, d in enumerate(BK_WEEKDAYS)]]), None
    elif a == "bksw2":
        ask("bkw", (t, int(parts[3])), "🗓 О котрій? Надішліть час, наприклад <code>03:00</code>.")
        return None, None
    elif a == "bkspause":
        s["pause_until"] = time.time() + 7 * 86400
        BK.set_sched(t, s)
        tip = "⏸ Пауза на тиждень — потім відновлю сам"
    elif a == "bkstop":
        s["paused"] = True
        BK.set_sched(t, s)
        tip = "⏹ Автокопії сюди зупинено. Вручну — як і раніше"
    elif a == "bkstart":
        s.pop("paused", None)
        s.pop("pause_until", None)
        if not s or s == {}:
            s = {"every": (BK.cfg()["days"] or 7) * 86400}
        BK.set_sched(t, s)
        tip = "▶️ Автокопії відновлено"
    elif a == "bksx":
        BK.set_sched(t, None)
        tip = "↺ Типовий розклад"
    return scr_bk_sched(BK, t), tip


