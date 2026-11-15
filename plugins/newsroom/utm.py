"""GreenPost UTM builder and persistent ledger. No network verification claims."""
from __future__ import annotations
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from pathlib import Path

STANDARD = {
    'telegram': ('telegram','social'), 'telegram_stories': ('telegram','stories'),
    'tumblr': ('tumblr','social'), 'facebook': ('facebook','social'),
    'facebook_stories': ('facebook','stories'), 'facebook_reels': ('facebook','reels'),
    'threads': ('threads','social'), 'instagram_stories': ('instagram','stories'),
    'twitter': ('twitter','social'),
}
RESERVE = {
    'youtube_video': ('youtube','video'), 'youtube_cards': ('youtube','cards'),
    'instagram_bio': ('instagram','general'), 'tiktok_bio': ('tiktok','general'),
    'telegram_comments': ('telegram','comments'), 'facebook_comments': ('facebook','comments'),
    'twitter_comments': ('twitter','comments'),
}

def build_url(url: str, tag: str, platform: str, *, reserve=False) -> str:
    """Preserve non-UTM query parameters and fragments, replace existing UTM values."""
    url = url.strip().strip('"\'')
    parts = urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.netloc or not tag.strip():
        raise ValueError('Потрібні коректне http(s)-посилання та погоджений utm_content')
    options = {**STANDARD, **(RESERVE if reserve else {})}
    if platform not in options:
        raise ValueError('Невідома платформа або резервний формат не ввімкнено')
    source, medium = options[platform]
    old = [(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True)
           if not k.lower().startswith('utm_')]
    old += [('utm_content',tag.strip()),('utm_medium',medium),('utm_campaign','news'),('utm_source',source)]
    return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(old),parts.fragment))

class Ledger:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.execute('CREATE TABLE IF NOT EXISTS utm_events(id INTEGER PRIMARY KEY, created REAL NOT NULL, original TEXT NOT NULL, generated TEXT NOT NULL, tag TEXT NOT NULL, platform TEXT NOT NULL, duplicate INTEGER NOT NULL DEFAULT 0)')
    def db(self): return sqlite3.connect(self.path)
    def create(self, url, tag, platform, *, reserve=False):
        generated=build_url(url,tag,platform,reserve=reserve)
        with self.db() as con:
            duplicate=bool(con.execute('SELECT 1 FROM utm_events WHERE original=? AND created>=? LIMIT 1',(url,time.time()-259200)).fetchone())
            con.execute('INSERT INTO utm_events(created,original,generated,tag,platform,duplicate) VALUES(?,?,?,?,?,?)',(time.time(),url,generated,tag,platform,int(duplicate)))
        return generated,duplicate
    def recent(self):
        with self.db() as con:
            return con.execute('SELECT created,generated,tag,platform,duplicate FROM utm_events WHERE created>=? ORDER BY created DESC',(time.time()-259200,)).fetchall()
    def statistics(self):
        with self.db() as con:
            return con.execute('SELECT platform,COUNT(*),SUM(duplicate) FROM utm_events GROUP BY platform ORDER BY COUNT(*) DESC').fetchall()


def create_both(ledger: Ledger, url: str, tag: str, platform: str, short_base: str, *, reserve=False):
    """Create both formats; retain original ledger and duplicate warning."""
    from .utm_tracking import ShortLinks
    full, duplicate = ledger.create(url, tag, platform, reserve=reserve)
    tracker = ShortLinks(Path(ledger.path).with_name('utm_tracking.sqlite'), short_base)
    pair = tracker.create(url, tag, platform, reserve=reserve)
    return {'full_url': full, 'short_url': pair['short_url'], 'code': pair['code'], 'duplicate': duplicate}
