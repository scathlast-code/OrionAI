"""Persistent, bot-scoped access grants. A grant is never erased by geo failures."""
import os
import sqlite3
import time
from pathlib import Path

SCOPES = ('manager', 'orion')

def db_path():
    import datadir
    return Path(datadir.path('access_grants.sqlite3', 'SECURITY_GRANTS_DB'))

def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS grants (scope TEXT NOT NULL, user_id INTEGER NOT NULL, active INTEGER NOT NULL, expires_at INTEGER, updated_at INTEGER NOT NULL, PRIMARY KEY(scope,user_id))')
    db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, user_id INTEGER NOT NULL, action TEXT NOT NULL, actor_id INTEGER, at INTEGER NOT NULL)')
    return db

def _scope(scope):
    if scope not in SCOPES:
        raise ValueError('unknown bot scope')
    return scope

def set_grant(scope, user_id, enabled, actor_id=None, expires_at=None):
    _scope(scope)
    user_id = int(user_id)
    if user_id <= 0: raise ValueError('invalid Telegram ID')
    if expires_at is not None and int(expires_at) <= int(time.time()): raise ValueError('expiry must be in future')
    with connect() as db:
        db.execute('INSERT INTO grants(scope,user_id,active,expires_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(scope,user_id) DO UPDATE SET active=excluded.active, expires_at=excluded.expires_at, updated_at=excluded.updated_at', (scope,user_id,int(bool(enabled)),int(expires_at) if expires_at else None,int(time.time())))
        db.execute('INSERT INTO audit(scope,user_id,action,actor_id,at) VALUES (?,?,?,?,?)', (scope,user_id,'grant' if enabled else 'revoke',actor_id,int(time.time())))

def status(scope,user_id,now=None):
    _scope(scope)
    with connect() as db:
        row=db.execute('SELECT active,expires_at FROM grants WHERE scope=? AND user_id=?',(scope,int(user_id))).fetchone()
    if not row: return 'absent'
    if not row[0]: return 'revoked'
    if row[1] is not None and row[1] <= (int(now) if now is not None else int(time.time())): return 'expired'
    return 'granted'

def list_grants(scope):
    _scope(scope)
    with connect() as db:
        return db.execute('SELECT user_id,active,expires_at,updated_at FROM grants WHERE scope=? ORDER BY user_id',(scope,)).fetchall()
