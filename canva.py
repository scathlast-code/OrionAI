"""
Canva для бота: вхід у ваш акаунт Canva (OAuth) і робота з офіційним MCP-сервером Canva.

Що дає:
  • моделі бота отримують інструменти Canva (створити дизайн з опису, згенерувати зображення,
    знайти дизайни, експортувати, змінити розмір тощо) — пишете «зроби в Canva пост про…»;
  • прямі команди без ШІ: /canva (вхід і стан), /canva_designs (пошук дизайнів);
  • Canva як ще один генератор зображень для /image.

Потрібно (див. README): інтеграція в Canva Developers з Client ID і Secret,
адреса повернення https://ВАШ-ДОМЕН/canva/callback і змінна PUBLIC_URL.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from starlette.requests import Request
from starlette.responses import HTMLResponse

env = os.environ.get
log = logging.getLogger("canva")

CLIENT_ID = env("CANVA_CLIENT_ID", "")
CLIENT_SECRET = env("CANVA_CLIENT_SECRET", "")
PUBLIC_URL = env("PUBLIC_URL", "").rstrip("/")
AUTH_URL = env("CANVA_AUTH_URL", "https://www.canva.com/api/oauth/authorize")
TOKEN_URL = env("CANVA_TOKEN_URL", "https://api.canva.com/rest/v1/oauth/token")
MCP_URL = env("CANVA_MCP_URL", "https://api.canva.com/connect/v1/mcp")
SCOPES = env(
    "CANVA_SCOPES",
    "profile:read design:meta:read design:content:read design:content:write asset:read asset:write "
    "folder:read folder:write comment:read comment:write brandtemplate:meta:read brandtemplate:content:read",
)
REDIRECT_URI = f"{PUBLIC_URL}/canva/callback"
PREFIX = "canva__"

# сховище ключ-значення бота (bot.py підставляє функції роботи з базою)
kv_get = None
kv_set = None
notify = None  # async (chat_id, text) — повідомити в Telegram про результат входу

PENDING = {}  # state -> (verifier, chat_id, created)
_tools_cache = {"ts": 0, "tools": []}


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET and PUBLIC_URL)


def tokens():
    raw = kv_get("canva_tokens") if kv_get else None
    return json.loads(raw) if raw else None


def connected():
    return configured() and tokens() is not None


def disconnect():
    kv_set("canva_tokens", None)
    _tools_cache.update(ts=0, tools=[])


def login_url(chat_id):
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    PENDING[state] = (verifier, chat_id, time.time())
    q = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    }
    return f"{AUTH_URL}?{urlencode(q)}"


async def _token_request(data):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data, auth=(CLIENT_ID, CLIENT_SECRET))
    if r.status_code >= 400:
        raise RuntimeError(f"Canva відмовила у вході: HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    old = tokens() or {}
    t = {
        "access": j["access_token"],
        "refresh": j.get("refresh_token") or old.get("refresh"),
        "expires_at": time.time() + int(j.get("expires_in", 3600)),
    }
    kv_set("canva_tokens", json.dumps(t))
    return t


async def access_token():
    t = tokens()
    if not t:
        raise RuntimeError("Canva не підключена. Надішліть /canva, щоб увійти.")
    if t["expires_at"] - time.time() < 60:
        if not t.get("refresh"):
            raise RuntimeError("Вхід у Canva застарів. Надішліть /canva і увійдіть знову.")
        try:
            t = await _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh"]})
        except RuntimeError:
            disconnect()
            raise RuntimeError("Не вдалося оновити вхід у Canva. Надішліть /canva і увійдіть знову.")
    return t["access"]


async def callback(request: Request):
    """Сюди Canva повертає користувача після входу."""
    state, code = request.query_params.get("state"), request.query_params.get("code")
    err = request.query_params.get("error")
    pend = PENDING.pop(state, None) if state else None
    if err or not code or not pend or time.time() - pend[2] > 900:
        msg = "Вхід не завершено: посилання застаріло або вхід скасовано. Надішліть боту /canva ще раз."
        return HTMLResponse(_page("Canva не підключено", msg), status_code=400)
    verifier, chat_id, _ = pend
    try:
        await _token_request(
            {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT_URI}
        )
        tools = await list_tools(force=True)
        text = f"🎨 Canva підключено! Доступно інструментів: {len(tools)}.\nНапишіть, наприклад: «зроби в Canva пост для Instagram про весняне прибирання парку»."
    except Exception as e:  # noqa: BLE001
        log.exception("canva callback")
        text = f"⚠️ Вхід у Canva пройшов, але підключитися до інструментів не вдалося: {e}"
    if notify:
        await notify(chat_id, text)
    return HTMLResponse(_page("Готово", "Можна закрити цю сторінку й повернутися в Telegram."))


def _page(title, text):
    return (
        "<!DOCTYPE html><html lang='uk'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{title}</title><body style='font:18px system-ui;padding:40px;max-width:520px;margin:auto'>"
        f"<h1>{title}</h1><p>{text}</p></body></html>"
    )


# ---------------------------------------------------------------- MCP-клієнт


async def _session_call(fn):
    token = await access_token()
    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with client:
        async with streamable_http_client(MCP_URL, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return await fn(s)


async def list_tools(force=False):
    if not force and time.time() - _tools_cache["ts"] < 600 and _tools_cache["tools"]:
        return _tools_cache["tools"]

    async def go(s):
        return (await s.list_tools()).tools

    tools = await _session_call(go)
    _tools_cache.update(ts=time.time(), tools=tools)
    return tools


URL_RE = re.compile(r"https://[^\s\"'<>\\]+")


async def call_tool(name, args):
    """Повертає (текст результату, [посилання на файли й зображення])."""

    async def go(s):
        return await s.call_tool(name, args)

    res = await _session_call(go)
    parts = []
    for c in res.content or []:
        if getattr(c, "type", "") == "text":
            parts.append(c.text)
        else:
            parts.append(str(c.model_dump(exclude_none=True))[:500])
    if res.structured_content:
        parts.append(json.dumps(res.structured_content, ensure_ascii=False)[:4000])
    text = "\n".join(parts)[:12000]
    urls = [u for u in dict.fromkeys(URL_RE.findall(text)) if _is_media(u)]
    if res.is_error:
        text = "ПОМИЛКА CANVA: " + text
    return text, urls


def _is_media(u):
    u = u.lower()
    return any(x in u for x in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".mp4", "export", "thumbnail", "media.canva.com", "design.canva.ai"))


def _clean_schema(s):
    """Прибирає з JSON-схеми поля, які не приймають деякі моделі (Gemini тощо)."""
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if k not in ("$schema", "additionalProperties", "examples", "title", "$id")}
    if isinstance(s, list):
        return [_clean_schema(v) for v in s]
    return s


async def openai_tools():
    """Інструменти Canva у форматі function calling для моделей бота."""
    if not connected():
        return []
    try:
        tools = await list_tools()
    except Exception as e:  # noqa: BLE001
        log.warning("Canva tools недоступні: %s", e)
        return []
    out = []
    for t in tools:
        schema = _clean_schema(t.input_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        out.append(
            {
                "type": "function",
                "function": {
                    "name": (PREFIX + t.name)[:64],
                    "description": ("Canva: " + (t.description or t.name))[:1000],
                    "parameters": schema,
                },
            }
        )
    return out


def tool_name(prefixed):
    return prefixed[len(PREFIX):] if prefixed.startswith(PREFIX) else None


async def generate_image(prompt):
    """Генерація зображення через Canva (для /image). Повертає bytes першого зображення."""
    tools = {t.name: t for t in await list_tools()}
    tool = tools.get("generate-image")
    if not tool:
        raise RuntimeError("у Canva немає інструмента generate-image")
    props = (tool.input_schema or {}).get("properties", {})
    key = next((k for k in ("prompt", "query", "description", "text") if k in props), None) or next(iter(props), "prompt")
    text, urls = await call_tool("generate-image", {key: prompt})
    imgs = [u for u in urls if any(x in u.lower() for x in (".png", ".jpg", ".jpeg", ".webp", "media.canva.com", "thumbnail"))]
    if not imgs:
        raise RuntimeError("Canva не повернула зображення: " + text[:200])
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(imgs[0])
    r.raise_for_status()
    return r.content
