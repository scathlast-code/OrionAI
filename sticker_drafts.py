"""Persistent OrionAI-only sticker pack drafts and explicit approval workflow.

No Telegram publication occurs until a separate publish action is implemented.
"""
import json
import os
import sqlite3
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__import__('datadir').path('sticker_drafts', 'ORION_STICKER_STORAGE'))


def _db():
    ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ROOT / 'drafts.sqlite3')
    con.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, status TEXT NOT NULL, manifest TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL)')
    return con


def create(owner: int, archive: Path, manifest: dict, mode='all') -> str:
    if mode not in ('all', 'selected'):
        raise ValueError('mode must be all or selected')
    ident = uuid.uuid4().hex[:12]
    destination = ROOT / (ident + '.zip')
    ROOT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        entries = [x for pack in manifest['packs'] for x in pack['items'] if not x.get('duplicate_of')]
        if not all(x['file'] in names for x in entries):
            raise ValueError('Archive does not match manifest')
    destination.write_bytes(Path(archive).read_bytes())
    for pack in manifest['packs']:
        for item in pack['items']:
            item['included'] = mode == 'all' and not item.get('duplicate_of')
    with _db() as con:
        con.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?)', (ident, int(owner), 'editing', json.dumps(manifest, ensure_ascii=False), str(destination), mode))
    return ident


def get(owner: int, ident: str) -> dict:
    with _db() as con:
        row = con.execute('SELECT status,manifest,source,mode FROM drafts WHERE id=? AND owner=?', (ident, int(owner))).fetchone()
    if not row:
        raise ValueError('Чернетку не знайдено або немає доступу')
    return {'id': ident, 'status': row[0], 'manifest': json.loads(row[1]), 'source': row[2], 'mode': row[3]}


def select(owner: int, ident: str, indexes: list[int], included: bool):
    draft = get(owner, ident)
    if draft['status'] != 'editing':
        raise ValueError('Чернетку вже подано на погодження або затверджено')
    items = [item for pack in draft['manifest']['packs'] for item in pack['items']]
    for index in indexes:
        if index < 1 or index > len(items):
            raise ValueError(f'Немає елемента №{index}')
        if included and items[index-1].get('duplicate_of'):
            raise ValueError('Дублікат відсутній у ZIP; вибери оригінальний елемент')
    for index in indexes:
        items[index-1]['included'] = included
    with _db() as con:
        con.execute('UPDATE drafts SET manifest=? WHERE id=? AND owner=?', (json.dumps(draft['manifest'], ensure_ascii=False), ident, int(owner)))
    return draft['manifest']


def change_status(owner: int, ident: str, action: str):
    draft = get(owner, ident)
    transitions = {'submit': ('editing', 'pending'), 'approve': ('pending', 'approved'), 'reject': ('pending', 'editing')}
    if action not in transitions or draft['status'] != transitions[action][0]:
        raise ValueError('Недопустима зміна стану чернетки')
    if action == 'submit' and not any(i.get('included') for p in draft['manifest']['packs'] for i in p['items']):
        raise ValueError('Потрібно обрати хоча б один елемент')
    with _db() as con:
        con.execute('UPDATE drafts SET status=? WHERE id=? AND owner=?', (transitions[action][1], ident, int(owner)))
    return transitions[action][1]


def render(owner: int, ident: str, destination: Path):
    draft = get(owner, ident)
    if draft['status'] not in ('pending', 'approved'):
        raise ValueError('Спочатку надішли чернетку на погодження')
    selected = [i for p in draft['manifest']['packs'] for i in p['items'] if i.get('included')]
    with zipfile.ZipFile(draft['source']) as src, zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as dst:
        for item in selected:
            dst.writestr(item['file'], src.read(item['file']))
        dst.writestr('selection.json', json.dumps({'draft': ident, 'status': draft['status'], 'items': selected}, ensure_ascii=False, indent=2))
    return len(selected)


def overview(owner: int, ident: str) -> str:
    draft = get(owner, ident)
    lines = [f"Чернетка {ident} · {draft['status']} · режим {draft['mode']}"]
    for number, item in enumerate((i for p in draft['manifest']['packs'] for i in p['items']), 1):
        lines.append(f"{number}. {'✅' if item.get('included') else '▫️'} {item['file']} {item.get('emoji') or ''}")
    return '\n'.join(lines)


def page(owner: int, ident: str, offset: int = 0, limit: int = 8):
    draft = get(owner, ident)
    items = [dict(item, number=n) for n, item in enumerate((i for pack in draft['manifest']['packs'] for i in pack['items']), 1)]
    offset = max(0, min(offset, max(0, len(items)-1)))
    return draft, items[offset:offset+limit], len(items), offset


def set_all(owner: int, ident: str, included: bool):
    draft = get(owner, ident)
    if draft['status'] != 'editing':
        raise ValueError('Редагування можливе лише до подання на затвердження')
    for pack in draft['manifest']['packs']:
        for item in pack['items']:
            item['included'] = bool(included and not item.get('duplicate_of'))
    with _db() as con:
        con.execute('UPDATE drafts SET manifest=? WHERE id=? AND owner=?',
                    (json.dumps(draft['manifest'], ensure_ascii=False), ident, int(owner)))
    return draft['manifest']
