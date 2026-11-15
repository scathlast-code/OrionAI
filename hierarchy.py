"""OrionAI hierarchy core: a node may manage children and report to a parent.

No network listener is started here. A transport adapter must authenticate peers,
call authorize_command(), and dispatch only explicitly allowed operations.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing

ROLES = frozenset({'standalone', 'root', 'member', 'intermediate'})
PERMISSIONS = frozenset({'status.read', 'tasks.submit', 'tasks.cancel', 'reports.read', 'config.read'})


class HierarchyError(ValueError):
    pass


class Hierarchy:
    def __init__(self, path=None, node_id=None):
        path = path or __import__('datadir').path('hierarchy.db')
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        with self._connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS peers (
                    peer_id TEXT PRIMARY KEY, direction TEXT NOT NULL,
                    secret TEXT NOT NULL, permissions TEXT NOT NULL,
                    ancestors TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nonces (
                    peer_id TEXT NOT NULL, nonce TEXT NOT NULL, seen REAL NOT NULL,
                    PRIMARY KEY(peer_id,nonce)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, peer_id TEXT,
                    action TEXT NOT NULL, result TEXT NOT NULL
                );
            ''')
            if node_id:
                existing = self._get(con, 'node_id')
                if existing and existing != node_id:
                    raise HierarchyError('node_id differs from persisted identity')
                self._set(con, 'node_id', node_id)
            if not self._get(con, 'node_id'):
                self._set(con, 'node_id', secrets.token_hex(12))

    def _connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _get(con, key):
        row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set(con, key, value):
        con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    @property
    def node_id(self):
        with closing(self._connect()) as con:
            return self._get(con, 'node_id')

    def status(self):
        with closing(self._connect()) as con:
            parent = con.execute("SELECT peer_id FROM peers WHERE direction='parent' AND enabled=1").fetchone()
            children = [r[0] for r in con.execute("SELECT peer_id FROM peers WHERE direction='child' AND enabled=1 ORDER BY peer_id")]
            return {'node_id': self.node_id, 'role': 'intermediate' if parent and children else 'member' if parent else 'root' if children else 'standalone', 'parent': parent[0] if parent else None, 'children': children}

    def attach(self, peer_id, direction, secret, permissions=(), ancestors=()):
        if direction not in ('parent', 'child'):
            raise HierarchyError('invalid direction')
        if not peer_id or peer_id == self.node_id or len(secret) < 32:
            raise HierarchyError('invalid peer identity or secret')
        perms = set(permissions)
        if not perms <= PERMISSIONS:
            raise HierarchyError('unknown permission')
        ancestry = list(ancestors)
        if self.node_id in ancestry or peer_id in ancestry or len(set(ancestry)) != len(ancestry):
            raise HierarchyError('cycle in declared ancestry')
        with self._connect() as con:
            if direction == 'parent':
                existing = con.execute("SELECT peer_id FROM peers WHERE direction='parent' AND enabled=1 AND peer_id<>?", (peer_id,)).fetchone()
                if existing:
                    raise HierarchyError('a node can have only one active parent')
                if con.execute("SELECT 1 FROM peers WHERE direction='child' AND enabled=1 AND peer_id=?", (peer_id,)).fetchone():
                    raise HierarchyError('parent is already a child')
            elif con.execute("SELECT 1 FROM peers WHERE direction='parent' AND enabled=1 AND peer_id=?", (peer_id,)).fetchone():
                raise HierarchyError('child is already parent')
            con.execute('''INSERT INTO peers(peer_id,direction,secret,permissions,ancestors,enabled,created)
                VALUES(?,?,?,?,?,1,?) ON CONFLICT(peer_id) DO UPDATE SET
                direction=excluded.direction,secret=excluded.secret,permissions=excluded.permissions,
                ancestors=excluded.ancestors,enabled=1''',
                (peer_id, direction, secret, json.dumps(sorted(perms)), json.dumps(ancestry), time.time()))
            self._audit(con, peer_id, 'attach', 'ok')

    def detach(self, peer_id):
        with self._connect() as con:
            con.execute('UPDATE peers SET enabled=0 WHERE peer_id=?', (peer_id,))
            self._audit(con, peer_id, 'detach', 'ok')

    @staticmethod
    def _audit(con, peer_id, action, result):
        con.execute('INSERT INTO audit(at,peer_id,action,result) VALUES(?,?,?,?)', (time.time(), peer_id, action, result))

    @staticmethod
    def sign(secret, sender, recipient, action, payload=None, timestamp=None, nonce=None):
        message = {'sender': sender, 'recipient': recipient, 'action': action,
                   'payload': payload or {}, 'timestamp': int(timestamp if timestamp is not None else time.time()),
                   'nonce': nonce or secrets.token_hex(16)}
        encoded = json.dumps(message, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        message['signature'] = hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()
        return message

    def authorize_command(self, message, max_age=120):
        """Check signature, freshness, replay and explicit permission; no execution."""
        if not isinstance(message, dict):
            raise HierarchyError('invalid envelope')
        required = {'sender', 'recipient', 'action', 'payload', 'timestamp', 'nonce', 'signature'}
        if set(message) != required or message['recipient'] != self.node_id:
            raise HierarchyError('invalid recipient or fields')
        if not isinstance(message['timestamp'], int) or abs(time.time() - message['timestamp']) > max_age:
            raise HierarchyError('expired command')
        if not isinstance(message['nonce'], str) or len(message['nonce']) < 16:
            raise HierarchyError('invalid nonce')
        if not isinstance(message['payload'], dict):
            raise HierarchyError('invalid payload')
        with self._connect() as con:
            row = con.execute('SELECT direction,secret,permissions FROM peers WHERE peer_id=? AND enabled=1', (message['sender'],)).fetchone()
            if not row or row[0] != 'parent':
                raise HierarchyError('sender is not an authorized parent')
            action = message['action']
            if action not in json.loads(row[2]):
                self._audit(con, message['sender'], str(action), 'denied_permission')
                raise HierarchyError('permission denied')
            unsigned = {k: message[k] for k in required if k != 'signature'}
            encoded = json.dumps(unsigned, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
            expected = hmac.new(row[1].encode(), encoded, hashlib.sha256).hexdigest()
            if not isinstance(message['signature'], str) or not hmac.compare_digest(expected, message['signature']):
                raise HierarchyError('invalid signature')
            con.execute('DELETE FROM nonces WHERE seen<?', (time.time() - max_age * 2,))
            try:
                con.execute('INSERT INTO nonces(peer_id,nonce,seen) VALUES(?,?,?)', (message['sender'], message['nonce'], time.time()))
            except sqlite3.IntegrityError as exc:
                raise HierarchyError('replayed command') from exc
            self._audit(con, message['sender'], action, 'authorized')
            return {'action': action, 'payload': message['payload'], 'sender': message['sender']}
