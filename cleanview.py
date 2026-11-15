"""Reversible, presentation-only clean text. Originals never overwritten."""
import re
import sqlite3
from html import unescape
from contextlib import contextmanager

SOURCE_HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:джерела|sources|посилання на джерела)\s*:?\s*$', re.I)
TECH_HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:технічний звіт|використані ресурси|usage report)\s*:?\s*$', re.I)

def clean(text):
    """Remove presentation clutter without rewording claims or inventing evidence."""
    lines=[]; hiding=False
    for line in text.splitlines():
        if SOURCE_HEADING.match(line) or TECH_HEADING.match(line):
            hiding=True; continue
        if hiding:
            # Preserve later sections if the source block is not at the end.
            if re.match(r'^#{1,6}\s+\S',line): hiding=False
            else: continue
        line=re.sub(r'^\s*(?:[-*•]\s+|\d+[.)]\s+)', '',line)
        line=re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)',r'\1',line)
        line=re.sub(r'\*\*(.*?)\*\*',r'\1',line)
        line=re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)',r'\1',line)
        line=re.sub(r'^\s*#{1,6}\s*','',line)
        lines.append(line)
    return unescape('\n'.join(lines)).strip()

@contextmanager
def _db(db):
    c=sqlite3.connect(db,timeout=15)
    try:
        c.execute('CREATE TABLE IF NOT EXISTS answer_views (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, original TEXT NOT NULL, clean TEXT NOT NULL, is_clean INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,message_id))')
        yield c
        c.commit()
    finally:
        c.close()

def store(db,chat_id,message_id,original):
    with _db(db) as c:
        c.execute('INSERT OR REPLACE INTO answer_views(chat_id,message_id,original,clean,is_clean) VALUES(?,?,?,?,0)',(chat_id,message_id,original,clean(original)))

def toggle(db,chat_id,message_id):
    with _db(db) as c:
        row=c.execute('SELECT original,clean,is_clean FROM answer_views WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
        if row is None:return None
        new=int(not row[2]); c.execute('UPDATE answer_views SET is_clean=? WHERE chat_id=? AND message_id=?',(new,chat_id,message_id))
        return (row[1] if new else row[0]),bool(new)
