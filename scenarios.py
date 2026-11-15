"""Persistent, opt-in per-chat behavior profiles; no automatic paid-provider enablement."""
import json,sqlite3
from pathlib import Path
DEFAULTS={'ask_delivery':False,'show_sources':True,'auto_model_switch':False,'quality_check':True,'file_operations':True}

def get(db,chat_id):
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE IF NOT EXISTS scenario_preferences(chat_id INTEGER PRIMARY KEY, config TEXT NOT NULL)')
        row=c.execute('SELECT config FROM scenario_preferences WHERE chat_id=?',(chat_id,)).fetchone()
    return {**DEFAULTS,**(json.loads(row[0]) if row else {})}

def set_option(db,chat_id,key,value):
    if key not in DEFAULTS or not isinstance(value,bool): raise ValueError('Невідомий параметр')
    settings=get(db,chat_id);settings[key]=value
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO scenario_preferences(chat_id,config) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET config=excluded.config',(chat_id,json.dumps(settings)))
    return settings


# Stage 13: named scenarios, scoped per bot and per Telegram user.
import time
SCENARIO_PRESETS = {
 'dialog': {'title':'Діалог', 'description':'Звичайні розмови, джерела й самоперевірка'},
 'research': {'title':'Дослідження', 'description':'Пошук, порівняння джерел і критичний аналіз'},
 'files': {'title':'Файли', 'description':'Документи, архіви та перетворення'},
 'stickers': {'title':'Стікери', 'description':'Завантаження та погодження добірок'},
 'economy': {'title':'Економія ресурсів', 'description':'Без автоматичного використання платних API'},
}
def _init_profiles(c):
 c.execute("CREATE TABLE IF NOT EXISTS scenario_profiles(bot TEXT NOT NULL, user_id INTEGER NOT NULL, slug TEXT NOT NULL, enabled INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(bot,user_id,slug))")
def list_profiles(db, bot, user_id):
 if bot not in ('orion','manager'): raise ValueError('Unknown bot')
 with sqlite3.connect(str(db)) as c:
  _init_profiles(c)
  rows=dict(c.execute('SELECT slug,enabled FROM scenario_profiles WHERE bot=? AND user_id=?',(bot,int(user_id))).fetchall())
 return [{'id':slug,**preset,'enabled':bool(rows.get(slug, slug in ('dialog','files','economy')))} for slug,preset in SCENARIO_PRESETS.items()]
def update_profile(db,bot,user_id,slug,enabled):
 if slug not in SCENARIO_PRESETS or type(enabled) is not bool or bot not in ('orion','manager'): raise ValueError('Invalid scenario')
 with sqlite3.connect(str(db)) as c:
  _init_profiles(c)
  c.execute('INSERT INTO scenario_profiles(bot,user_id,slug,enabled,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(bot,user_id,slug) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at',(bot,int(user_id),slug,int(enabled),int(time.time())))
 return list_profiles(db,bot,user_id)
