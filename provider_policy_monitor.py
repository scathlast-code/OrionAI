"""Conservative terms/policy monitor. No AI or automatic acceptance of new terms."""
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

MAX_BYTES=750_000

def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS policy_sources (name TEXT PRIMARY KEY, url TEXT NOT NULL, last_hash TEXT, checked_at INTEGER, last_error TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS policy_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, old_hash TEXT, new_hash TEXT NOT NULL, detected_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', notes TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE TABLE IF NOT EXISTS policy_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, description TEXT NOT NULL, created_at INTEGER NOT NULL, acknowledged_at INTEGER, notified_at INTEGER, UNIQUE(name,kind,description))")
    db.commit()
    return db

def register(path,name,url):
    if not name or not name.replace('-','').replace('_','').isalnum(): raise ValueError('invalid provider name')
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.username or parts.password or not parts.hostname:raise ValueError('HTTPS URL required')
    allowed={h.strip().lower() for h in os.getenv('POLICY_ALLOWED_HOSTS','openai.com,platform.openai.com,help.openai.com,github.com,docs.github.com,railway.com,docs.railway.com,oracle.com,docs.oracle.com,cloud.oracle.com').split(',')}
    if parts.hostname.lower() not in allowed or parts.port not in (None,443):raise ValueError('provider host not allowlisted')
    with connect(path) as db:
        db.execute('INSERT INTO policy_sources(name,url) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET url=excluded.url,last_hash=CASE WHEN url=excluded.url THEN last_hash ELSE NULL END',(name,url))
        db.commit()

def check(path,name,fetch=None):
    with connect(path) as db:
        row=db.execute('SELECT url,last_hash FROM policy_sources WHERE name=?',(name,)).fetchone()
        if not row: raise ValueError('unknown provider')
        url,old=row
        try:
            if fetch is None:
                def fetch(u):
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self,*args,**kwargs):return None
                    opener=urllib.request.build_opener(NoRedirect)
                    with opener.open(urllib.request.Request(u,headers={'User-Agent':'OrionAI-PolicyMonitor/1.0'}),timeout=12) as resp:
                        if resp.url.split('/')[0] != 'https:': raise ValueError('insecure redirect')
                        data=resp.read(MAX_BYTES+1)
                        if len(data)>MAX_BYTES: raise ValueError('policy page too large')
                        return data
            data=fetch(url)
            if isinstance(data,str):data=data.encode()
            if len(data)>MAX_BYTES:raise ValueError('policy page too large')
            digest=hashlib.sha256(data).hexdigest()
            now=int(time.time())
            db.execute('UPDATE policy_sources SET last_hash=?,checked_at=?,last_error=NULL WHERE name=?',(digest,now,name))
            changed=old is not None and old!=digest
            if changed:
                db.execute("INSERT OR IGNORE INTO policy_alerts(name,kind,description,created_at) VALUES(?,?,?,?)",(name,'changed','Офіційна сторінка змінилася; потрібна перевірка змісту',now))
                db.execute('INSERT INTO policy_changes(name,old_hash,new_hash,detected_at) VALUES(?,?,?,?)',(name,old,digest,now))
            db.commit()
            return {'name':name,'changed':changed,'initial_baseline':old is None,'checked_at':now}
        except Exception as e:
            db.execute("INSERT OR IGNORE INTO policy_alerts(name,kind,description,created_at) VALUES(?,?,?,?)",(name,'error','Не вдалося перевірити офіційне джерело',int(time.time())))
            db.execute('UPDATE policy_sources SET checked_at=?,last_error=? WHERE name=?',(int(time.time()),str(e)[:250],name))
            db.commit()
            return {'name':name,'error':str(e)[:250]}

def scan(path,fetch=None):
    with connect(path) as db:
        names=[r[0] for r in db.execute('SELECT name FROM policy_sources ORDER BY name')]
    return [check(path,n,fetch) for n in names]

def changes(path,limit=20):
    with connect(path) as db:
        return [dict(zip(('id','name','detected_at','status','notes'),r)) for r in db.execute('SELECT id,name,detected_at,status,notes FROM policy_changes ORDER BY id DESC LIMIT ?', (min(max(limit,1),100),))]

def acknowledge(path,change_id):
    with connect(path) as db:
        cur=db.execute("UPDATE policy_changes SET status='reviewed' WHERE id=? AND status='pending'",(change_id,))
        db.commit()
        return cur.rowcount>0

def sources(path):
    with connect(path) as db:
        return [dict(zip(('name','url','checked_at','last_error'),r)) for r in db.execute('SELECT name,url,checked_at,last_error FROM policy_sources ORDER BY name')]


def active_alerts(path):
    """Unacknowledged incidents remain visible even after failed scans/restarts."""
    with connect(path) as db:
        return [dict(zip(('id','name','kind','description','created_at','notified_at'),row)) for row in db.execute('SELECT id,name,kind,description,created_at,notified_at FROM policy_alerts WHERE acknowledged_at IS NULL ORDER BY id DESC')]

def acknowledge_alert(path,alert_id):
    with connect(path) as db:
        cur=db.execute('UPDATE policy_alerts SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL',(int(time.time()),alert_id))
        db.commit();return cur.rowcount==1

def mark_notified(path,alert_id):
    with connect(path) as db:
        db.execute('UPDATE policy_alerts SET notified_at=? WHERE id=?',(int(time.time()),alert_id));db.commit()

def alert_sources(path,name):
    with connect(path) as db:
        return [r[0] for r in db.execute('SELECT url FROM policy_sources WHERE name=?',(name,))]
