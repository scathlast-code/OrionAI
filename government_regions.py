"""Ukrainian territorial coverage for government-position monitoring.
No network collection is implied by selecting a region.
"""
import json
import sqlite3

REGIONS = {
    'UA-00': 'Україна — центральні органи',
    'UA-05': 'Вінницька область', 'UA-07': 'Волинська область',
    'UA-12': 'Дніпропетровська область', 'UA-14': 'Донецька область',
    'UA-18': 'Житомирська область', 'UA-21': 'Закарпатська область',
    'UA-23': 'Запорізька область', 'UA-26': 'Івано-Франківська область',
    'UA-32': 'Київська область', 'UA-35': 'Кіровоградська область',
    'UA-44': 'Луганська область', 'UA-46': 'Львівська область',
    'UA-48': 'Миколаївська область', 'UA-51': 'Одеська область',
    'UA-53': 'Полтавська область', 'UA-56': 'Рівненська область',
    'UA-59': 'Сумська область', 'UA-61': 'Тернопільська область',
    'UA-63': 'Харківська область', 'UA-65': 'Херсонська область',
    'UA-68': 'Хмельницька область', 'UA-71': 'Черкаська область',
    'UA-74': 'Чернігівська область', 'UA-77': 'Чернівецька область',
    'UA-30': 'Київ (місто)', 'UA-43': 'Автономна Республіка Крим',
    'UA-40': 'Севастополь (місто)',
}
ALL_REGIONS = tuple(REGIONS)

class RegionSettings:
    def __init__(self, db_path):
        self.db_path = db_path
        with sqlite3.connect(db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS government_region_settings (profile TEXT PRIMARY KEY, region_codes TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    def get(self, profile='default'):
        with sqlite3.connect(self.db_path) as db:
            row = db.execute('SELECT region_codes FROM government_region_settings WHERE profile=?', (profile,)).fetchone()
        return json.loads(row[0]) if row else list(ALL_REGIONS)
    def set(self, codes, profile='default'):
        if not isinstance(codes, (list, tuple, set)) or not codes:
            raise ValueError('Оберіть хоча б один регіон')
        invalid = set(codes) - REGIONS.keys()
        if invalid: raise ValueError('Невідомі коди регіонів: '+', '.join(sorted(invalid)))
        selected = [code for code in ALL_REGIONS if code in codes]
        with sqlite3.connect(self.db_path) as db:
            db.execute('INSERT INTO government_region_settings(profile,region_codes) VALUES(?,?) ON CONFLICT(profile) DO UPDATE SET region_codes=excluded.region_codes,updated_at=CURRENT_TIMESTAMP', (profile, json.dumps(selected, ensure_ascii=False)))
        return selected
    def reset(self, profile='default'):
        return self.set(ALL_REGIONS, profile)
