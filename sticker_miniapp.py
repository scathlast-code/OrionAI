"""Authenticated OrionAI sticker draft Mini App API; launch separately with uvicorn."""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from security_policy import web_access, telegram_access, rate_allowed
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sticker_drafts
import notification_modes as notification_settings

app = FastAPI(title='OrionAI Sticker Mini App')

@app.middleware('http')
async def security_middleware(request: Request, call_next):
    peer = request.client.host if request.client else '127.0.0.1'
    ok, reason = web_access(peer, request.headers.get('x-forwarded-for'))
    if not ok:
        return JSONResponse({'detail': 'Access restricted'}, status_code=403)
    # All business endpoints are disabled during deep sleep. Settings and wake remain available.
    if notification_settings.is_sleeping('orion') and request.url.path.startswith('/api/stickers/'):
        return JSONResponse({'detail':'OrionAI is in deep sleep'}, status_code=423)
    return await call_next(request)

ROOT = Path(__file__).resolve().parent


def verify_init_data(raw: str) -> int:
    token = os.environ.get('TELEGRAM_TOKEN', '')
    if not token or not raw:
        raise HTTPException(401, 'Telegram authorization required')
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    signature = pairs.pop('hash', '')
    try:
        auth_date = int(pairs.get('auth_date', '0'))
        if abs(time.time() - auth_date) > 3600:
            raise ValueError('Expired initData')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('Invalid signature')
        user_id = int(json.loads(pairs['user'])['id'])
        allowed = {int(x) for x in os.getenv('ALLOWED_USERS', '').replace(' ', '').split(',') if x}
        if allowed and user_id not in allowed and __import__('access_grants').status('orion', user_id) != 'granted':
            raise ValueError('User not allowed')
        ok, _ = telegram_access(user_id, scope="orion")
        if not ok or not rate_allowed(user_id, 'miniapp', limit=60):
            raise HTTPException(403, 'Access restricted')
        return user_id
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, 'Invalid or expired Telegram authorization')


class Selection(BaseModel):
    index: int
    included: bool

class Bulk(BaseModel):
    included: bool


def owner(init_data):
    return verify_init_data(init_data or '')


def safe_get(uid, ident):
    try:
        return sticker_drafts.get(uid, ident)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get('/api/stickers/{ident}')
def get_draft(ident: str, x_telegram_init_data: str = Header(default='')):
    return safe_get(owner(x_telegram_init_data), ident)


@app.post('/api/stickers/{ident}/select')
def select(ident: str, body: Selection, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    try:
        sticker_drafts.select(uid, ident, [body.index], body.included)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return safe_get(uid, ident)


@app.post('/api/stickers/{ident}/all')
def select_all(ident: str, body: Bulk, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    try:
        sticker_drafts.set_all(uid, ident, body.included)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return safe_get(uid, ident)


@app.post('/api/stickers/{ident}/{action}')
def status(ident: str, action: str, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    if action not in ('submit', 'approve', 'reject'):
        raise HTTPException(404, 'Unknown action')
    try:
        sticker_drafts.change_status(uid, ident, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return safe_get(uid, ident)


@app.get('/api/stickers/{ident}/download')
def download(ident: str, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    draft = safe_get(uid, ident)
    if draft['status'] not in ('pending', 'approved'):
        raise HTTPException(400, 'Submit draft first')
    import tempfile
    # FileResponse needs persistent file until request completes.
    path = sticker_drafts.ROOT / f'{ident}_selection.zip'
    sticker_drafts.render(uid, ident, path)
    return FileResponse(path, filename=f'OrionAI_stickers_{ident}.zip')


class NotificationChoice(BaseModel):
    kind: str
    value: str

class QuietChoice(BaseModel):
    enabled: bool
    start: str = '23:00'
    end: str = '08:00'
    timezone: str = 'Europe/Kyiv'

class SleepChoice(BaseModel):
    enabled: bool


def _admin(uid):
    ids = {int(v) for v in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if v}
    ids |= {int(v) for v in os.getenv('OWNER_IDS', '').replace(' ', '').split(',') if v}
    if uid not in ids:
        raise HTTPException(403, 'Administrator required')


@app.get('/api/notifications')
def notification_status(x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    return {'user': notification_settings.user_mode('orion', uid),
            'bot': notification_settings.bot_mode('orion'),
            'sounds': list(notification_settings.SOUNDS),
            'vibrations': list(notification_settings.VIBRATIONS),
            'admin': uid in {int(v) for k in ('ADMIN_IDS', 'OWNER_IDS') for v in os.getenv(k, '').replace(' ', '').split(',') if v}}


@app.post('/api/notifications/choice')
def notification_choice(body: NotificationChoice, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    try:
        notification_settings.set_user_mode('orion', uid, body.kind, body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return notification_settings.user_mode('orion', uid)


@app.post('/api/notifications/quiet')
def notification_quiet(body: QuietChoice, x_telegram_init_data: str = Header(default='')):
    _admin(owner(x_telegram_init_data))
    try:
        notification_settings.set_quiet('orion', body.enabled, body.start, body.end, body.timezone)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc))
    return notification_settings.bot_mode('orion')


@app.post('/api/notifications/sleep')
def notification_sleep(body: SleepChoice, x_telegram_init_data: str = Header(default='')):
    _admin(owner(x_telegram_init_data))
    notification_settings.set_sleep('orion', body.enabled)
    return notification_settings.bot_mode('orion')



# Stage 13: authenticated scenario preferences for OrionAI Mini App.
import scenarios as scenario_store
class ScenarioChoice(BaseModel):
    enabled: bool

def _scenario_db():
    import datadir
    return datadir.path('orion_scenarios.sqlite3', 'ORION_SCENARIO_DB', old=str(ROOT / 'orion_scenarios.sqlite3'))

@app.get('/api/scenarios')
def scenario_list(x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    return {'bot':'orion','items':scenario_store.list_profiles(_scenario_db(),'orion',uid)}

@app.post('/api/scenarios/{slug}')
def scenario_update(slug: str, body: ScenarioChoice, x_telegram_init_data: str = Header(default='')):
    uid = owner(x_telegram_init_data)
    try:
        return {'bot':'orion','items':scenario_store.update_profile(_scenario_db(),'orion',uid,slug,body.enabled)}
    except ValueError as exc:
        raise HTTPException(400,str(exc))

# Stage 19: owner-only UI builder API. Manager layouts are edited by the same trusted owner.
import ui_layouts
class LayoutUpdate(BaseModel):
    layout: dict
    revision: int

def _layout_admin(raw):
    uid=owner(raw)
    owners={int(v) for v in os.getenv('ORION_OWNER_IDS',os.getenv('ALLOWED_USERS','')).replace(' ','').split(',') if v}
    if not owners or uid not in owners:
        raise HTTPException(403,'Owner permission required; configure ORION_OWNER_IDS')
    return uid

@app.get('/api/ui-layout/{bot}')
def get_ui_layout(bot: str, x_telegram_init_data: str = Header(default='')):
    _layout_admin(x_telegram_init_data)
    try: return ui_layouts.read(bot)
    except ValueError as exc: raise HTTPException(400,str(exc))

@app.put('/api/ui-layout/{bot}')
def put_ui_layout(bot: str, body: LayoutUpdate, x_telegram_init_data: str = Header(default='')):
    _layout_admin(x_telegram_init_data)
    try: return ui_layouts.save(bot,body.layout,body.revision)
    except ValueError as exc: raise HTTPException(409,str(exc))

from utm_shortio_api import router as utm_router
app.include_router(utm_router)

app.mount('/', StaticFiles(directory=ROOT / 'sticker_webapp', html=True), name='sticker_webapp')
