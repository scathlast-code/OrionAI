"""Versioned UI layouts. Only whitelisted actions; no arbitrary URLs or scripts."""
import json, os, sqlite3, time
from pathlib import Path
DB_DEFAULT=str(Path(__file__).with_name('ui_layouts.sqlite3'))
ACTIONS={'home','work','stickers','settings','more','scenarios','notifications','chat','resources','stats','users','policies','help','builder'}
DEFAULT={'blocks':[{'id':'quick','title':'Швидкий доступ','visible':True,'buttons':[{'id':'work','title':'Робота','action':'work','visible':True},{'id':'settings','title':'Режими','action':'settings','visible':True}]},{'id':'tools','title':'Інструменти','visible':True,'buttons':[{'id':'stats','title':'Статистика','action':'stats','visible':True},{'id':'users','title':'Користувачі','action':'users','visible':True}]}]}
def validate(layout):
 if not isinstance(layout,dict) or set(layout)!={'blocks'}: raise ValueError('Invalid layout')
 blocks=layout['blocks']; ids=set(); count=0
 if not isinstance(blocks,list) or len(blocks)>20: raise ValueError('Too many blocks')
 for b in blocks:
  if not isinstance(b,dict) or set(b)!={'id','title','visible','buttons'}: raise ValueError('Invalid block')
  if not isinstance(b['id'],str) or not b['id'].isalnum() or len(b['id'])>40 or b['id'] in ids: raise ValueError('Invalid block id')
  ids.add(b['id'])
  if not isinstance(b['title'],str) or not 1<=len(b['title'])<=80 or type(b['visible']) is not bool: raise ValueError('Invalid block properties')
  if not isinstance(b['buttons'],list) or len(b['buttons'])>20: raise ValueError('Too many buttons')
  bid=set()
  for item in b['buttons']:
   if not isinstance(item,dict) or set(item)!={'id','title','action','visible'}: raise ValueError('Invalid button')
   if not isinstance(item['id'],str) or not item['id'].isalnum() or len(item['id'])>40 or item['id'] in bid: raise ValueError('Invalid button id')
   bid.add(item['id']);count+=1
   if not isinstance(item['title'],str) or not 1<=len(item['title'])<=70 or item['action'] not in ACTIONS or type(item['visible']) is not bool: raise ValueError('Invalid button properties')
 if count>100: raise ValueError('Too many buttons')
 return layout
def connect(path=None):
 db=sqlite3.connect(path or __import__('datadir').path('ui_layouts.sqlite3','ORION_UI_DB',old=DB_DEFAULT));db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE IF NOT EXISTS layouts (bot TEXT PRIMARY KEY, data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL)');return db
def read(bot,path=None):
 if bot not in ('orion','manager'): raise ValueError('Unknown bot')
 with connect(path) as db:
  row=db.execute('SELECT data,revision FROM layouts WHERE bot=?',(bot,)).fetchone()
 return {'layout':json.loads(row[0]) if row else DEFAULT,'revision':row[1] if row else 0}
def save(bot,layout,revision,path=None):
 if bot not in ('orion','manager'): raise ValueError('Unknown bot')
 validate(layout)
 with connect(path) as db:
  current=db.execute('SELECT revision FROM layouts WHERE bot=?',(bot,)).fetchone();old=current[0] if current else 0
  if revision!=old: raise ValueError('Layout changed elsewhere. Reload before saving')
  db.execute('INSERT INTO layouts(bot,data,revision,updated_at) VALUES(?,?,?,?) ON CONFLICT(bot) DO UPDATE SET data=excluded.data,revision=excluded.revision,updated_at=excluded.updated_at',(bot,json.dumps(layout,ensure_ascii=False),old+1,int(time.time())))
 return {'revision':old+1}
