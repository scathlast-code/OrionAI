"""Verified public-sector appointment register; no inferred successors."""
import sqlite3, os, json
from datetime import datetime, timezone
from government_regions import RegionSettings, REGIONS
from government_coverage import Coverage

SECTORS=('мінекономіки','міндовкілля','агросектор','охорона здоров’я','медицина','інші')
EVENTS=('dismissed','appointed','acting','nominated')
SOURCES=('official','media','unverified')

class Positions:
    def __init__(self,path=None):
        path=path or __import__('datadir').path('orion_positions.sqlite3','ORION_POSITIONS_DB')
        self.path=path
        os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, sector TEXT NOT NULL, organization TEXT NOT NULL, position TEXT NOT NULL, person TEXT NOT NULL, event TEXT NOT NULL, effective_date TEXT, published_date TEXT, source_url TEXT NOT NULL, source_type TEXT NOT NULL, decision_ref TEXT, status TEXT NOT NULL DEFAULT 'reported', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(organization,position,person,event,source_url)); CREATE INDEX IF NOT EXISTS changes_sector ON changes(sector,event);""")
        with self.connect() as db:
            cols = {row[1] for row in db.execute('PRAGMA table_info(changes)')}
            if 'region_code' not in cols:
                db.execute("ALTER TABLE changes ADD COLUMN region_code TEXT NOT NULL DEFAULT 'UA-00'")
            db.execute('CREATE INDEX IF NOT EXISTS changes_region ON changes(region_code)')
            if 'country_code' not in cols:
                db.execute("ALTER TABLE changes ADD COLUMN country_code TEXT NOT NULL DEFAULT 'UA'")
            if 'territory_code' not in cols:
                db.execute("ALTER TABLE changes ADD COLUMN territory_code TEXT")
            db.execute('CREATE INDEX IF NOT EXISTS changes_country ON changes(country_code)')
        self.regions = RegionSettings(path)
        self.coverage = Coverage(path)
    def connect(self):
        return sqlite3.connect(self.path)
    def add(self, *,sector,organization,position,person,event,source_url,source_type='official',effective_date=None,published_date=None,decision_ref=None,status='reported',notes='',region_code='UA-00',country_code='UA',territory_code=None):
        if not isinstance(country_code,str) or len(country_code)!=2 or not country_code.isalpha() or country_code!=country_code.upper(): raise ValueError('Невірна країна')
        if country_code=='UA' and region_code not in REGIONS: raise ValueError('Невідомий регіон України')
        if country_code!='UA' and region_code!='UA-00':raise ValueError('Для інших країн використовуйте territory_code')
        if territory_code is not None and (not isinstance(territory_code,str) or not territory_code.startswith(country_code+'-') or len(territory_code)>16):raise ValueError('Невірний територіальний код')
        if event not in EVENTS or source_type not in SOURCES: raise ValueError('Невідомий тип події або джерела')
        if not all(isinstance(x,str) and x.strip() for x in (sector,organization,position,person,source_url)):raise ValueError('Заповніть обов’язкові поля')
        if not source_url.startswith(('https://','http://')):raise ValueError('Потрібне посилання на джерело')
        with self.connect() as db:
            c=db.execute('INSERT OR IGNORE INTO changes(sector,organization,position,person,event,effective_date,published_date,source_url,source_type,decision_ref,status,notes,created_at,region_code,country_code,territory_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sector,organization,position,person,event,effective_date,published_date,source_url,source_type,decision_ref,status,notes,datetime.now(timezone.utc).isoformat(),region_code,country_code,territory_code))
            return c.rowcount>0
    def list(self,sector=None,event=None,limit=50,region_codes=None,country_codes=None):
        q='SELECT sector,organization,position,person,event,effective_date,published_date,source_url,source_type,decision_ref,status,notes,region_code,country_code,territory_code FROM changes WHERE 1=1';params=[]
        if sector:q+=' AND lower(sector) LIKE ?';params.append('%'+sector.lower()+'%')
        if event:q+=' AND event=?';params.append(event)
        if region_codes is not None:
            codes = list(region_codes)
            if not codes: return []
            if set(codes) - REGIONS.keys(): raise ValueError('Невідомий регіон')
            q += ' AND region_code IN ('+','.join('?' for _ in codes)+')'
            params.extend(codes)
        if country_codes is not None:
            if not country_codes:return []
            q+=' AND country_code IN ('+','.join('?' for _ in country_codes)+')';params.extend(country_codes)
        q+=' ORDER BY COALESCE(effective_date,published_date,created_at) DESC,id DESC LIMIT ?';params.append(min(max(int(limit),1),200))
        with self.connect() as db:return [dict(zip(('sector','organization','position','person','event','effective_date','published_date','source_url','source_type','decision_ref','status','notes','region_code','country_code','territory_code'),r)) for r in db.execute(q,params)]
    def report(self,sector=None,limit=30,region_codes=None,country_codes=None):
        rows=self.list(sector,limit=limit,region_codes=region_codes,country_codes=country_codes)
        output=['🏛️ Держпосади — кадрові зміни','Джерела і статуси наведено для кожного запису. Відсутність призначення не означає, що посада вакантна.']
        for ev,label in [('dismissed','🔴 Звільнені'),('appointed','🟢 Призначені'),('acting','🟡 Виконувачі обов’язків'),('nominated','🔵 Кандидатури')]:
            group=[x for x in rows if x['event']==ev];output.append('\n'+label+' ('+str(len(group))+')')
            for x in group:
                output.append(f"• {x['person']} — {x['position']}, {x['organization']} ({x['sector']}; {REGIONS.get(x['region_code'],x['region_code']) if x['country_code']=='UA' else (x['territory_code'] or x['country_code'])})\n  Дата: {x['effective_date'] or 'не встановлено'} · Джерело: {x['source_type']} · Статус: {x['status']}\n  {x['source_url']}")
        if not rows:output.append('Немає внесених підтверджених записів. Моніторинг джерел ще не налаштований.')
        return '\n'.join(output)
