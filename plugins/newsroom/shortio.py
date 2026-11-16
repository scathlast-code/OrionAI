"""Short.io integration; secrets stay server-side. No synthetic click counts."""
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
import httpx

API = 'https://api.short.io'
STATS = 'https://api-v2.short.io'

class ShortIOError(RuntimeError): pass

class ShortIO:
    def __init__(self, key=None, domain=None, domain_id=None, transport=None):
        self.key = key if key is not None else os.getenv('SHORTIO_API_KEY','')
        self.domain = domain if domain is not None else os.getenv('SHORTIO_DOMAIN','greenpost.s.gy')
        self.domain_id = domain_id if domain_id is not None else os.getenv('SHORTIO_DOMAIN_ID','')
        self.transport = transport
        if not self.key: raise ShortIOError('SHORTIO_API_KEY не налаштований')
        if not self.domain or '/' in self.domain: raise ShortIOError('Некоректний SHORTIO_DOMAIN')
    async def request(self, method, url, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=20,transport=self.transport) as client:
                res = await client.request(method,url,headers={'Authorization':self.key,'Accept':'application/json'},**kwargs)
                res.raise_for_status()
                return res.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ShortIOError('Помилка API Short.io: перевірте доступ, ключ, ліміти та налаштування домену') from exc
    async def shorten(self, full_url):
        parsed=urlsplit(full_url)
        if parsed.scheme not in ('http','https') or not parsed.netloc: raise ValueError('Некоректне посилання')
        data=await self.request('POST',API+'/links',json={'originalURL':full_url,'domain':self.domain,'allowDuplicates':False})
        short=data.get('secureShortURL') or data.get('shortURL')
        if not short or urlsplit(short).hostname != self.domain: raise ShortIOError('Short.io не повернув посилання очікуваного домену')
        return {'short_url':short,'link_id':str(data.get('idString') or data.get('id') or ''),'duplicate':bool(data.get('duplicate',False))}
    async def link_clicks(self, ids):
        if not self.domain_id or not str(self.domain_id).isdigit(): raise ShortIOError('Для статистики потрібен SHORTIO_DOMAIN_ID')
        ids=[str(x) for x in ids if str(x).isdigit()]
        if not ids: return {}
        result=await self.request('GET',f'{STATS}/statistics/domain/{self.domain_id}/link_clicks',params={'ids':','.join(ids)})
        if not isinstance(result,dict): raise ShortIOError('Неочікувана відповідь статистики')
        return {k:int(v) for k,v in result.items() if str(v).isdigit()}
    async def statistics(self, dimension="country", *, period="last30", link_id=None):
        """Raw Short.io statistics. Availability depends on account plan/API permissions."""
        allowed={"country":"country","city":"city","browser":"browser","os":"os","device":"device","referrer":"referrer","date":"date"}
        if dimension not in allowed: raise ValueError("Unsupported statistics dimension")
        if not self.domain_id or not str(self.domain_id).isdigit(): raise ShortIOError("Потрібен SHORTIO_DOMAIN_ID")
        params={"period":period}
        if link_id is not None: params["linkId"]=str(link_id)
        return await self.request("GET",f"{STATS}/statistics/domain/{self.domain_id}/{allowed[dimension]}",params=params)

    async def link_stats(self, link_id):
        """Статистика одного посилання за весь час → {'clicks': {'total': n}, 'country': {...}, 'city': {...}, ...}."""
        data = await self.request('GET', f'{STATS}/statistics/link/{link_id}', params={'period': 'total', 'tz': 'Europe/Kiev'})
        if not isinstance(data, dict): raise ShortIOError('Неочікувана відповідь статистики')
        out = {'clicks': {'total': int(data.get('humanClicks') or data.get('totalClicks') or 0)}}
        for dim, src, keys in (('country', 'country', ('countryName', 'country')), ('city', 'city', ('cityName', 'city', 'name')),
                               ('browser', 'browser', ('browser',)), ('os', 'os', ('os',)), ('referrer', 'referer', ('referer', 'referrer', 'social'))):
            rows = data.get(src) or data.get(dim) or []
            counts = {}
            for r in rows if isinstance(rows, list) else []:
                k = next((r.get(x) for x in keys if isinstance(r, dict) and r.get(x)), None)
                if k: counts[str(k)] = counts.get(str(k), 0) + int(r.get('score') or r.get('count') or 0)
            if counts: out[dim] = counts
        return out

    async def list_links(self, limit=50):
        if not self.domain_id or not str(self.domain_id).isdigit(): raise ShortIOError('Потрібен SHORTIO_DOMAIN_ID')
        return await self.request('GET',API+'/api/links',params={'domain_id':self.domain_id,'limit':max(1,min(limit,150))})

class ShortIORegistry:
    def __init__(self, path):
        self.path=str(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS shortio_links (id INTEGER PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP, full_url TEXT NOT NULL, short_url TEXT NOT NULL, link_id TEXT, platform TEXT NOT NULL, tag TEXT NOT NULL)')
    def save(self,full_url,short_url,link_id,platform,tag):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO shortio_links(full_url,short_url,link_id,platform,tag) VALUES(?,?,?,?,?)',(full_url,short_url,link_id,platform,tag))
    def recent(self,limit=20):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT full_url,short_url,link_id,platform,tag FROM shortio_links ORDER BY id DESC LIMIT ?', (limit,)).fetchall()

async def create_pair(ledger,registry,provider,url,tag,platform):
    """Only record a completed pair after provider success; existing ledger remains intact."""
    from .utm import build_url
    full=build_url(url,tag,platform)
    short=await provider.shorten(full)
    _,duplicate=ledger.create(url,tag,platform)
    registry.save(full,short['short_url'],short['link_id'],platform,tag)
    return {'full_url':full,'short_url':short['short_url'],'link_id':short['link_id'],'duplicate':duplicate}


def normalize_link_rows(payload):
    """Normalize common Short.io list response shapes, reject unknown ones."""
    if isinstance(payload, list): rows=payload
    elif isinstance(payload,dict):
        rows=payload.get('links',payload.get('data'))
        if isinstance(rows,dict): rows=rows.get('links')
    else: rows=None
    if not isinstance(rows,list): raise ShortIOError('Невідомий формат списку Short.io')
    result=[]
    for item in rows:
        if not isinstance(item,dict): continue
        short=item.get('secureShortURL') or item.get('shortURL')
        original=item.get('originalURL')
        if not short or not original: continue
        result.append({'short_url':short,'full_url':original,'link_id':str(item.get('idString') or item.get('id') or ''),'title':str(item.get('title') or '')})
    return result

def import_links(registry, rows):
    """Idempotent import, does not change existing UTM events or delete anything."""
    added=0
    with sqlite3.connect(registry.path) as db:
        for row in rows:
            if db.execute('SELECT 1 FROM shortio_links WHERE short_url=?',(row['short_url'],)).fetchone(): continue
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(row['full_url']).query)
            db.execute('INSERT INTO shortio_links(full_url,short_url,link_id,platform,tag) VALUES(?,?,?,?,?)',
                       (row['full_url'],row['short_url'],row['link_id'],query.get('utm_source',['unknown'])[0],query.get('utm_content',[''])[0]))
            added+=1
    return added
