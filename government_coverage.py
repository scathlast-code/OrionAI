"""Configurable international monitoring scope. Country codes use ISO 3166-1 alpha-2.
Region identifiers are configured by the operator, not inferred from names.
Selecting a territory does not activate any network monitoring.
"""
import json, re, sqlite3
from government_regions import REGIONS

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
REGION_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,12}$")

class Coverage:
    def __init__(self, path):
        self.path=path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS government_coverage (profile TEXT PRIMARY KEY, config TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")

    @staticmethod
    def validate(data):
        if not isinstance(data,dict): raise ValueError('Невірна конфігурація')
        countries=data.get('countries', ['UA'])
        if not isinstance(countries,list) or not countries or len(countries)>250 or any(not isinstance(c,str) or not COUNTRY_RE.fullmatch(c) for c in countries):
            raise ValueError('Країни мають бути дволітерними кодами ISO (UA, PL, DE...)')
        if len(countries)!=len(set(countries)):raise ValueError('Країни повторюються')
        regions=data.get('regions',{})
        if not isinstance(regions,dict):raise ValueError('Невірні регіони')
        for country, codes in regions.items():
            if country not in countries or not isinstance(codes,list) or len(codes)>500:
                raise ValueError('Регіони не відповідають країнам')
            for code in codes:
                if not isinstance(code,str) or not REGION_RE.fullmatch(code) or not code.startswith(country+'-'):
                    raise ValueError('Невірний код регіону')
                if country=='UA' and code not in REGIONS:raise ValueError('Невідомий регіон України')
        sectors=data.get('sectors',[])
        if not isinstance(sectors,list) or len(sectors)>100 or any(not isinstance(s,str) or not 1<=len(s.strip())<=100 for s in sectors):
            raise ValueError('Невірні галузі')
        return {'countries':countries,'regions':regions,'sectors':sectors,'enabled':bool(data.get('enabled',True))}

    def get(self,profile='default'):
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT config FROM government_coverage WHERE profile=?',(profile,)).fetchone()
        return json.loads(row[0]) if row else {'countries':['UA'],'regions':{'UA':list(REGIONS)},'sectors':[],'enabled':True}

    def set(self,data,profile='default'):
        if not isinstance(profile,str) or not 1<=len(profile)<=80:raise ValueError('Невірний профіль')
        data=self.validate(data)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO government_coverage(profile,config) VALUES(?,?) ON CONFLICT(profile) DO UPDATE SET config=excluded.config,updated_at=CURRENT_TIMESTAMP',(profile,json.dumps(data,ensure_ascii=False)))
        return data

    def set_countries(self,countries,profile='default'):
        cfg=self.get(profile)
        cfg['countries']=countries
        cfg['regions']={c:v for c,v in cfg['regions'].items() if c in countries}
        return self.set(cfg,profile)

    def set_regions(self,country,codes,profile='default'):
        cfg=self.get(profile)
        if country not in cfg['countries']:raise ValueError('Спочатку додайте країну')
        cfg['regions'][country]=codes
        return self.set(cfg,profile)
