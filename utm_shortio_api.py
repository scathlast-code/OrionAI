"""Short.io Mini App API. All requests require Telegram signed initData."""
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from plugins.newsroom.utm import Ledger, STANDARD, RESERVE
from plugins.newsroom.shortio import ShortIO, ShortIORegistry, ShortIOError, create_pair, normalize_link_rows, import_links

router=APIRouter(prefix='/api/utm',tags=['utm'])

def authorize(init_data):
    from sticker_miniapp import verify_init_data
    return verify_init_data(init_data or '')

def storage():
    root=Path(__import__('datadir').base())
    return Ledger(root/'greenpost_utm.sqlite3'),ShortIORegistry(os.getenv('ORION_SHORTIO_DB',str(root/'greenpost_shortio.sqlite3')))

class CreateRequest(BaseModel):
    initData: str
    url: str
    tag: str
    platform: str

class Authorized(BaseModel):
    initData: str

@router.post('/create')
async def create(req:CreateRequest):
    authorize(req.initData)
    if req.platform not in {**STANDARD,**RESERVE}: raise HTTPException(422,'Unknown platform')
    try:
        ledger,registry=storage()
        return await create_pair(ledger,registry,ShortIO(),req.url,req.tag,req.platform)
    except (ShortIOError,ValueError) as exc: raise HTTPException(422,str(exc))

@router.post('/import')
async def import_existing(req:Authorized):
    authorize(req.initData)
    try:
        _,registry=storage()
        provider=ShortIO()
        raw=await provider.list_links(150)
        rows=normalize_link_rows(raw)
        return {'imported':import_links(registry,rows),'fetched':len(rows),'note':'One batch only; additional pages may exist.'}
    except ShortIOError as exc: raise HTTPException(502,str(exc))

@router.post('/dashboard')
async def dashboard(req:Authorized):
    authorize(req.initData)
    _,registry=storage()
    rows=registry.recent(500)
    counts={}
    for full,short,ident,platform,tag in rows:
        counts[platform]=counts.get(platform,0)+1
    result={'links':len(rows),'generated_by_platform':counts,'clicks':None,'dimensions':{},'errors':[]}
    try:
        provider=ShortIO()
        ids=[r[2] for r in rows if str(r[2]).isdigit()]
        clicks=await provider.link_clicks(ids)
        result['clicks']=sum(clicks.values())
        result['clicks_by_platform']={}
        for _,_,ident,platform,_ in rows:
            result['clicks_by_platform'][platform]=result['clicks_by_platform'].get(platform,0)+clicks.get(str(ident),0)
        for dim in ('date','country','city','device','browser','os','referrer'):
            try: result['dimensions'][dim]=await provider.statistics(dim)
            except ShortIOError as exc: result['errors'].append(f'{dim}: {exc}')
    except ShortIOError as exc: result['errors'].append(str(exc))
    return result
