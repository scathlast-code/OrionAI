"""Numeric-only lifetime usage ledger. Never store prompts, filenames, or message IDs.

Append-only via application API. DB and backups must live on persistent storage.
"""
import sqlite3
import time
from pathlib import Path

MODES = frozenset(('normal','incognito','offline','online_required','orion_only'))

def _db(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=20)
    db.execute('PRAGMA busy_timeout=20000')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute("""CREATE TABLE IF NOT EXISTS lifetime_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot TEXT NOT NULL, user_id INTEGER NOT NULL, mode TEXT NOT NULL,
        occurred_at INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, cost_microunits INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        CHECK(requests>=0 AND duration_ms>=0 AND input_tokens>=0 AND output_tokens>=0 AND cost_microunits>=0 AND failures>=0)
    )""")
    db.execute('CREATE INDEX IF NOT EXISTS lifetime_by_user ON lifetime_events(bot,user_id,mode)')
    db.execute("""CREATE TRIGGER IF NOT EXISTS lifetime_no_update BEFORE UPDATE ON lifetime_events
       BEGIN SELECT RAISE(ABORT,'lifetime events are immutable'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS lifetime_no_delete BEFORE DELETE ON lifetime_events
       BEGIN SELECT RAISE(ABORT,'lifetime events cannot be deleted'); END""")
    return db

def record(path, *, bot, user_id, mode='normal', requests=0, duration_ms=0,
           input_tokens=0, output_tokens=0, cost_microunits=0, failures=0):
    if mode not in MODES: raise ValueError('unknown mode')
    if not bot or len(bot)>40 or not bot.replace('_','').isalnum(): raise ValueError('invalid bot')
    numbers=(requests,duration_ms,input_tokens,output_tokens,cost_microunits,failures)
    if any(type(v) is not int or v<0 for v in numbers): raise ValueError('metrics must be nonnegative integers')
    with _db(path) as db:
        db.execute("""INSERT INTO lifetime_events(bot,user_id,mode,occurred_at,requests,duration_ms,
          input_tokens,output_tokens,cost_microunits,failures) VALUES (?,?,?,?,?,?,?,?,?,?)""",
          (bot,int(user_id),mode,int(time.time()),*numbers))

def totals(path, *, bot=None, user_id=None, mode=None):
    if mode is not None and mode not in MODES: raise ValueError('unknown mode')
    where=[]; args=[]
    for key,val in [('bot',bot),('user_id',user_id),('mode',mode)]:
        if val is not None: where.append(key+'=?'); args.append(val)
    sql="""SELECT COUNT(*),COALESCE(SUM(requests),0),COALESCE(SUM(duration_ms),0),
        COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),
        COALESCE(SUM(cost_microunits),0),COALESCE(SUM(failures),0) FROM lifetime_events"""
    if where: sql+=' WHERE '+' AND '.join(where)
    with _db(path) as db: result=db.execute(sql,args).fetchone()
    return dict(zip(('events','requests','duration_ms','input_tokens','output_tokens','cost_microunits','failures'),result))


def by_mode(path, *, bot=None, user_id=None):
    """Numeric totals grouped by mode; never returns message contents."""
    filters=[]; args=[]
    if bot is not None: filters.append('bot=?'); args.append(bot)
    if user_id is not None: filters.append('user_id=?'); args.append(int(user_id))
    sql="""SELECT mode, COUNT(*), COALESCE(SUM(requests),0), COALESCE(SUM(duration_ms),0),
       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
       COALESCE(SUM(cost_microunits),0), COALESCE(SUM(failures),0)
       FROM lifetime_events"""
    if filters: sql+=' WHERE '+' AND '.join(filters)
    sql+=' GROUP BY mode ORDER BY mode'
    with _db(path) as db: rows=db.execute(sql,args).fetchall()
    keys=('events','requests','duration_ms','input_tokens','output_tokens','cost_microunits','failures')
    return {r[0]:dict(zip(keys,r[1:])) for r in rows}


def by_user(path, *, bot=None, limit=100):
    """Aggregate per-user numbers. Caller must enforce administrator authorization."""
    if type(limit) is not int or not 1<=limit<=1000: raise ValueError('invalid limit')
    sql="""SELECT user_id, COUNT(*), COALESCE(SUM(requests),0),
        COALESCE(SUM(duration_ms),0), COALESCE(SUM(input_tokens),0),
        COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_microunits),0),
        COALESCE(SUM(failures),0) FROM lifetime_events"""
    args=[]
    if bot is not None: sql+=' WHERE bot=?'; args.append(bot)
    sql+=' GROUP BY user_id ORDER BY user_id LIMIT ?'; args.append(limit)
    with _db(path) as db: rows=db.execute(sql,args).fetchall()
    keys=('user_id','events','requests','duration_ms','input_tokens','output_tokens','cost_microunits','failures')
    return [dict(zip(keys,r)) for r in rows]


def backup(path, destination):
    """SQLite consistent snapshot. Destination must be protected persistent storage."""
    source=Path(path)
    if not source.exists(): raise FileNotFoundError(path)
    dest=Path(destination)
    if source.resolve()==dest.resolve(): raise ValueError('destination equals source')
    dest.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(str(source)) as original, sqlite3.connect(str(dest)) as snapshot:
        original.backup(snapshot)
        if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('backup integrity check failed')
    return dest
