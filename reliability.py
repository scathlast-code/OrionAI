'Scoped diagnostic logging and conservative usage-based advice for OrionAI bots.'
import json, logging, os, re, sqlite3, time, traceback, uuid
from pathlib import Path

LOG = logging.getLogger('orion.reliability')
DOCS = {
 'telegram': 'https://core.telegram.org/bots/api',
 'network': 'https://docs.python-httpx.org/advanced/exceptions/',
 'database': 'https://www.sqlite.org/rescode.html',
 'files': 'https://docs.python.org/3/library/zipfile.html',
 'permissions': 'https://core.telegram.org/bots/features#privacy-mode',
 'general': 'https://docs.python.org/3/library/exceptions.html',
}
SENSITIVE = re.compile(r'(?i)(bot\d{6,}:[A-Za-z0-9_-]{15,}|(?:token|api[_-]?key|password|secret)\s*[=:]\s*\S+)')

def redact(text):
    return SENSITIVE.sub('[REDACTED]', str(text))[:1200]

def diagnose(exc, component='unknown'):
    name = type(exc).__name__
    msg = str(exc).lower()
    if 'sqlite' in name.lower() or 'database' in msg:
        category, reason, fix = 'database', 'Помилка сховища або блокування бази (уточнити за журналом).', 'Перевірте шлях, права запису, вільне місце і стан SQLite.'
    elif 'timeout' in name.lower() or 'connect' in name.lower():
        category, reason, fix = 'network', 'Таймаут або збій мережевого з’єднання.', 'Перевірте DNS, доступ до API, проксі та повторіть запит із затримкою.'
    elif 'telegram' in name.lower() or 'badrequest' in name.lower() or 'forbidden' in name.lower():
        category, reason, fix = 'telegram', 'Telegram API відхилив операцію; точна причина в журналі.', 'Перевірте права бота, ID чату, формат повідомлення та обмеження API.'
    elif 'permission' in name.lower():
        category, reason, fix = 'permissions', 'Операцію заборонено правами доступу.', 'Перевірте дозволи користувача, бота і файлової системи.'
    elif 'zip' in name.lower() or 'archive' in msg:
        category, reason, fix = 'files', 'Не вдалося обробити архів.', 'Перевірте цілісність, пароль і формат файлу.'
    else:
        category, reason, fix = 'general', 'Причина ще не встановлена; потрібен аналіз журналу.', 'Знайдіть запис за ID інциденту, перевірте traceback та конфігурацію.'
    return {'component':component,'error_type':name,'category':category,'reason':reason,'fix':fix,'docs':DOCS[category]}

class Reliability:
    def __init__(self, scope, path=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', scope): raise ValueError('Invalid scope')
        self.scope=scope
        self.path=Path(path or __import__('datadir').path('reliability.sqlite3', 'ORION_RELIABILITY_DB'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS users(scope TEXT, uid INTEGER, first_seen REAL, tips_enabled INTEGER DEFAULT 1, popup_enabled INTEGER DEFAULT 0, PRIMARY KEY(scope,uid));
            CREATE TABLE IF NOT EXISTS usage(scope TEXT,uid INTEGER,feature TEXT,at REAL);
            CREATE INDEX IF NOT EXISTS idx_usage ON usage(scope,uid,at);
            CREATE TABLE IF NOT EXISTS tips(scope TEXT,uid INTEGER,at REAL,feature TEXT);
            CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,scope TEXT,uid INTEGER,at REAL,component TEXT,category TEXT,error_type TEXT,detail TEXT);''')
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        return db
    def record(self,uid,feature,now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO users(scope,uid,first_seen) VALUES(?,?,?)',(self.scope,uid,now))
            db.execute('INSERT INTO usage VALUES(?,?,?,?)',(self.scope,uid,feature,now))
    def set_option(self,uid,option,enabled):
        if option not in ('tips_enabled','popup_enabled'): raise ValueError('Invalid option')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO users(scope,uid,first_seen) VALUES(?,?,?)',(self.scope,uid,time.time()))
            db.execute(f'UPDATE users SET {option}=? WHERE scope=? AND uid=?',(int(bool(enabled)),self.scope,uid))
    def preferences(self,uid):
        with self.connect() as db:
            row=db.execute('SELECT first_seen,tips_enabled,popup_enabled FROM users WHERE scope=? AND uid=?',(self.scope,uid)).fetchone()
        return row
    def suggestion(self,uid,now=None,commit=False):
        now=time.time() if now is None else now
        with self.connect() as db:
            row=db.execute('SELECT first_seen,tips_enabled FROM users WHERE scope=? AND uid=?',(self.scope,uid)).fetchone()
            if not row or not row[1] or now-row[0]<14*86400: return None
            count=db.execute('SELECT count(*) FROM usage WHERE scope=? AND uid=?',(self.scope,uid)).fetchone()[0]
            if count<10:return None
            recent=db.execute('SELECT count(*) FROM tips WHERE scope=? AND uid=? AND at>?',(self.scope,uid,now-7*86400)).fetchone()[0]
            if recent>=2:return None
            last=db.execute('SELECT max(at) FROM tips WHERE scope=? AND uid=?',(self.scope,uid)).fetchone()[0]
            if last and now-last<3*86400:return None
            used={r[0] for r in db.execute('SELECT DISTINCT feature FROM usage WHERE scope=? AND uid=?',(self.scope,uid))}
            choices = [('formats','Спробуйте /formats — довідник підтримуваних файлів.'),('resources','Спробуйте /resources — контроль витрат API.'),('scenarios','Спробуйте /scenarios — налаштування поведінки бота.'),('stickers','Спробуйте /stickers — архівування стікерів.')]
            if self.scope == 'manager':
                choices = [('menu','Спробуйте /menu — керування підключеними ботами.')]
            for feature,tip in choices:
                if feature not in used:
                    if commit: db.execute('INSERT INTO tips VALUES(?,?,?,?)',(self.scope,uid,now,feature))
                    return tip
        return None
    def incident(self,uid,exc,component='unknown'):
        data=diagnose(exc,component)
        incident_id=uuid.uuid4().hex[:12]
        safe_detail=redact(str(exc))
        with self.connect() as db:
            db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)',(incident_id,self.scope,uid,time.time(),component,data['category'],data['error_type'],safe_detail))
        LOG.error('incident=%s scope=%s component=%s type=%s detail=%s',incident_id,self.scope,component,data['error_type'],safe_detail)
        return incident_id,data
    def incident_text(self,incident_id,data):
        return (f'⚠️ Збій #{incident_id}\nКомпонент: {data["component"]}\nТип: {data["error_type"]}\n'
                f'Причина/гіпотеза: {data["reason"]}\nЩо перевірити: {data["fix"]}\n'
                f'Документація: {data["docs"]}\nТочні технічні подробиці доступні адміністратору в журналі.')
