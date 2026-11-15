"""Persisted per-module sleep and user feedback for OrionAI.

This is a policy/state engine, not a scheduler, network transport or server shutdown.
Workers must call can_run before executing a job. Owner-only mutation must be
validated by the calling API; no Telegram authentication is implemented here.
"""
import json
import sqlite3
import time
from pathlib import Path

MODES = {"active", "light", "deep", "emergency"}
PRIORITIES = {"normal", "important", "critical"}

class RuntimeControl:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS modes (
              node TEXT NOT NULL, module TEXT NOT NULL, mode TEXT NOT NULL,
              updated REAL NOT NULL, PRIMARY KEY(node,module));
            CREATE TABLE IF NOT EXISTS sleep_schedule (
              node TEXT NOT NULL, module TEXT NOT NULL, start_at REAL NOT NULL,
              end_at REAL NOT NULL, mode TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(node,module));
            CREATE TABLE IF NOT EXISTS feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT NOT NULL,
              module TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
              node TEXT NOT NULL, module TEXT NOT NULL, action TEXT NOT NULL,
              details TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def set_mode(self, node, module, mode, *, actor='owner'):
        if not node or not module or mode not in MODES:
            raise ValueError('Invalid node, module or sleep mode')
        if actor != 'owner':
            raise PermissionError('Only the local owner may change runtime policy')
        with self.connect() as db:
            db.execute('INSERT INTO modes VALUES (?,?,?,?) ON CONFLICT(node,module) DO UPDATE SET mode=excluded.mode,updated=excluded.updated', (node,module,mode,time.time()))
            db.execute('INSERT INTO runtime_audit(at,node,module,action,details) VALUES(?,?,?,?,?)', (time.time(),node,module,'set_mode',mode))

    def get_mode(self, node, module):
        with self.connect() as db:
            row=db.execute('SELECT mode FROM modes WHERE node=? AND module=?',(node,module)).fetchone()
        return row[0] if row else 'active'

    def can_run(self, node, module, priority='normal'):
        if priority not in PRIORITIES:raise ValueError('Invalid priority')
        mode=self.get_mode(node,module)
        if mode == 'active':return True
        if mode == 'light':return priority in ('important','critical')
        if mode == 'deep':return priority == 'critical'
        return False  # emergency: owner must manually restore service

    def add_feedback(self, node, module, kind, body):
        if kind not in ('bug','suggestion','rating','result') or not node or not module or not isinstance(body,str) or not body.strip() or len(body)>10000:
            raise ValueError('Invalid feedback')
        with self.connect() as db:
            cur=db.execute('INSERT INTO feedback(node,module,kind,body,created) VALUES(?,?,?,?,?)',(node,module,kind,body.strip(),time.time()))
            return cur.lastrowid

    def list_feedback(self, node, status=None):
        if status is not None and status not in ('new','reviewing','resolved','rejected'):
            raise ValueError('Invalid status')
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            sql='SELECT * FROM feedback WHERE node=?'
            params=[node]
            if status is not None:sql+=' AND status=?';params.append(status)
            return [dict(row) for row in db.execute(sql+' ORDER BY id DESC',params)]

    def update_feedback(self, feedback_id, status, *, actor='owner'):
        if actor != 'owner':raise PermissionError('Only owner can moderate feedback')
        if status not in ('new','reviewing','resolved','rejected'):raise ValueError('Invalid status')
        with self.connect() as db:
            cur=db.execute('UPDATE feedback SET status=? WHERE id=?',(status,int(feedback_id)))
            if not cur.rowcount:raise KeyError('Feedback not found')

    def set_schedule(self, node, module, start_at, end_at, mode='deep', *, actor='owner'):
        if actor != 'owner':
            raise PermissionError('Only local owner can schedule sleep')
        if mode not in ('light', 'deep') or float(end_at) <= float(start_at):
            raise ValueError('Invalid schedule')
        with self.connect() as db:
            db.execute('INSERT INTO sleep_schedule VALUES (?,?,?,?,?,1) ON CONFLICT(node,module) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at,mode=excluded.mode,enabled=1', (node,module,float(start_at),float(end_at),mode))

    def apply_schedules(self, now=None):
        now = time.time() if now is None else float(now)
        changed=[]
        with self.connect() as db:
            rows=db.execute('SELECT node,module,start_at,end_at,mode FROM sleep_schedule WHERE enabled=1').fetchall()
        for node,module,start,end,mode in rows:
            if now >= end:
                if self.get_mode(node,module)==mode:
                    self.set_mode(node,module,'active')
                    changed.append((node,module,'active'))
                with self.connect() as db:
                    db.execute('UPDATE sleep_schedule SET enabled=0 WHERE node=? AND module=?',(node,module))
            elif now >= start and self.get_mode(node,module)=='active':
                self.set_mode(node,module,mode)
                changed.append((node,module,mode))
        return changed
