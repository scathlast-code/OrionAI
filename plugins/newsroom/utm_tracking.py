"""First-party UTM short links and aggregate click telemetry.

Deploy behind a trusted HTTPS reverse proxy. No external shortening provider.
Geography is optional: GeoIP database is not bundled; never guess a city.
"""
from __future__ import annotations
import hashlib
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit
from .utm import build_url

CODE_RE = re.compile(r'^[A-Za-z0-9_-]{6,32}$')
BOT_RE = re.compile(r'bot|crawler|spider|preview|slackbot|telegrambot|facebookexternalhit|whatsapp', re.I)


def device_info(ua):
    ua = (ua or '')[:512]
    if BOT_RE.search(ua): device = 'bot'
    elif re.search(r'ipad|tablet|android(?!.*mobile)', ua, re.I): device = 'tablet'
    elif re.search(r'iphone|ipod|android.*mobile|mobile', ua, re.I): device = 'mobile'
    else: device = 'desktop' if ua else 'unknown'
    os_name = next((name for pattern,name in [(r'iphone|ipad|ipod','iOS'),(r'android','Android'),(r'windows','Windows'),(r'macintosh|mac os','macOS'),(r'linux','Linux')] if re.search(pattern,ua,re.I)), 'unknown')
    browser = next((name for pattern,name in [(r'edg/','Edge'),(r'firefox/','Firefox'),(r'crios/|chrome/','Chrome'),(r'safari/','Safari')] if re.search(pattern,ua,re.I)), 'other')
    return device,os_name,browser


class ShortLinks:
    def __init__(self, db_path, public_base):
        self.db_path = str(db_path)
        self.public_base = public_base.rstrip('/')
        parts = urlsplit(self.public_base)
        if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('ORION_SHORT_BASE має бути публічною HTTPS-адресою без query/fragment')
        Path(self.db_path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS short_links (
                    code TEXT PRIMARY KEY, created REAL NOT NULL, original TEXT NOT NULL,
                    full_url TEXT NOT NULL, tag TEXT NOT NULL, platform TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS short_clicks (
                    id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES short_links(code),
                    at REAL NOT NULL, device TEXT NOT NULL, os TEXT NOT NULL,
                    browser TEXT NOT NULL, country TEXT, city TEXT, referrer_host TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS short_clicks_code_time ON short_clicks(code,at);
            ''')
    def db(self):
        db = sqlite3.connect(self.db_path,timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        return db
    def create(self, original, tag, platform, *, reserve=False):
        full = build_url(original,tag,platform,reserve=reserve)
        for _ in range(8):
            code = secrets.token_urlsafe(7)
            try:
                with self.db() as db:
                    db.execute('INSERT INTO short_links(code,created,original,full_url,tag,platform) VALUES(?,?,?,?,?,?)',
                               (code,time.time(),original,full,tag,platform))
                return {'full_url':full,'short_url':f'{self.public_base}/r/{code}','code':code}
            except sqlite3.IntegrityError: pass
        raise RuntimeError('Не вдалося створити унікальний короткий код')
    def resolve(self, code):
        if not CODE_RE.fullmatch(code or ''): return None
        with self.db() as db:
            row=db.execute('SELECT full_url FROM short_links WHERE code=? AND enabled=1',(code,)).fetchone()
        return row[0] if row else None
    def record(self, code, user_agent='', referrer='', *, country=None, city=None):
        if not self.resolve(code): return False
        device,os_name,browser=device_info(user_agent)
        ref=urlsplit(referrer or '')
        host=ref.hostname.lower()[:128] if ref.scheme in ('http','https') and ref.hostname else None
        # Country/city are accepted only from an explicitly trusted caller, never directly from HTTP headers.
        with self.db() as db:
            db.execute('INSERT INTO short_clicks(code,at,device,os,browser,country,city,referrer_host,is_bot) VALUES(?,?,?,?,?,?,?,?,?)',
                       (code,time.time(),device,os_name,browser,country,city,host,int(device=='bot')))
        return True
    def report(self, *, code=None, days=30):
        days=max(1,min(int(days),3650))
        with self.db() as db:
            where='WHERE c.at>=? AND c.is_bot=0'
            params=[time.time()-days*86400]
            if code:
                where+=' AND c.code=?';params.append(code)
            total=db.execute(f'SELECT COUNT(*) FROM short_clicks c {where}',params).fetchone()[0]
            def grouped(col):
                return [dict(name=r[0] or 'unknown',clicks=r[1]) for r in db.execute(
                    f'SELECT {col},COUNT(*) FROM short_clicks c JOIN short_links l ON l.code=c.code {where} GROUP BY {col} ORDER BY COUNT(*) DESC',params)]
            return {'clicks':total,'platforms':grouped('l.platform'),'devices':grouped('c.device'),
                    'os':grouped('c.os'),'browsers':grouped('c.browser'),
                    'countries':grouped('c.country'),'cities':grouped('c.city'),
                    'referrers':grouped('c.referrer_host')}
    def disable(self,code):
        with self.db() as db:
            return bool(db.execute('UPDATE short_links SET enabled=0 WHERE code=?',(code,)).rowcount)


def routes(tracker):
    """Attach to the existing Starlette application. Keep stats behind bot authentication."""
    from starlette.responses import RedirectResponse,PlainTextResponse
    from starlette.routing import Route
    async def redirect(request):
        code=request.path_params['code']
        target=tracker.resolve(code)
        if target is None: return PlainTextResponse('Link not found',status_code=404)
        tracker.record(code,request.headers.get('user-agent',''),request.headers.get('referer',''))
        return RedirectResponse(target,status_code=302,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    return [Route('/r/{code}',redirect,methods=['GET'])]
