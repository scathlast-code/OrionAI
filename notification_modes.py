"""Persisted notification and sleep settings, isolated per bot and Telegram user.

Deep sleep blocks Telegram updates through an early guard. External workers must
call is_sleeping(scope) before starting jobs; it is not a host shutdown.
"""
import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

DB_DEFAULT = 'data/notification_modes.db'
SOUNDS = ('soft', 'double', 'melody', 'urgent', 'low')
VIBRATIONS = ('short', 'double', 'triple', 'long', 'sos')

def db_path():
    import datadir
    return datadir.path('notification_modes.db', 'NOTIFICATION_DB_PATH')

def connection():
    path=db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
    con=sqlite3.connect(path,timeout=10)
    con.execute('PRAGMA busy_timeout=10000')
    con.execute("""CREATE TABLE IF NOT EXISTS bot_modes (scope TEXT PRIMARY KEY, deep_sleep INTEGER NOT NULL DEFAULT 0, quiet_start TEXT NOT NULL DEFAULT '23:00', quiet_end TEXT NOT NULL DEFAULT '08:00', quiet_enabled INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv')""")
    con.execute("""CREATE TABLE IF NOT EXISTS user_modes (scope TEXT NOT NULL, user_id INTEGER NOT NULL, sound TEXT NOT NULL DEFAULT 'soft', vibration TEXT NOT NULL DEFAULT 'short', PRIMARY KEY(scope,user_id))""")
    return con

def bot_mode(scope):
    with connection() as con:
        con.execute('INSERT OR IGNORE INTO bot_modes(scope) VALUES (?)',(scope,))
        row=con.execute('SELECT deep_sleep,quiet_start,quiet_end,quiet_enabled,timezone FROM bot_modes WHERE scope=?',(scope,)).fetchone()
    return dict(zip(('deep_sleep','quiet_start','quiet_end','quiet_enabled','timezone'),row))

def set_sleep(scope,enabled):
    bot_mode(scope)
    with connection() as con:con.execute('UPDATE bot_modes SET deep_sleep=? WHERE scope=?',(int(bool(enabled)),scope))

def set_quiet(scope,enabled,start='23:00',end='08:00',timezone='Europe/Kyiv'):
    for value in (start,end):
        if len(value)!=5 or value[2]!=':':raise ValueError('Time must be HH:MM')
        time.fromisoformat(value)
    ZoneInfo(timezone)
    bot_mode(scope)
    with connection() as con:con.execute('UPDATE bot_modes SET quiet_enabled=?,quiet_start=?,quiet_end=?,timezone=? WHERE scope=?',(int(bool(enabled)),start,end,timezone,scope))

def is_sleeping(scope):
    return bool(bot_mode(scope)['deep_sleep'])

def is_quiet(scope,now=None):
    mode=bot_mode(scope)
    if not mode['quiet_enabled']:return False
    now=now or datetime.now(ZoneInfo(mode['timezone']))
    current=now.time().replace(tzinfo=None)
    start=time.fromisoformat(mode['quiet_start']);end=time.fromisoformat(mode['quiet_end'])
    if start==end:return True
    return start<=current<end if start<end else (current>=start or current<end)

def user_mode(scope,user_id):
    with connection() as con:
        con.execute('INSERT OR IGNORE INTO user_modes(scope,user_id) VALUES (?,?)',(scope,int(user_id)))
        sound,vibration=con.execute('SELECT sound,vibration FROM user_modes WHERE scope=? AND user_id=?',(scope,int(user_id))).fetchone()
    return {'sound':sound,'vibration':vibration}

def set_user_mode(scope,user_id,kind,value):
    choices={'sound':SOUNDS,'vibration':VIBRATIONS}
    if kind not in choices or value not in choices[kind]:raise ValueError('Unknown notification option')
    user_mode(scope,user_id)
    with connection() as con:con.execute(f'UPDATE user_modes SET {kind}=? WHERE scope=? AND user_id=?',(value,scope,int(user_id)))
