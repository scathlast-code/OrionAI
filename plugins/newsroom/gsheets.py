"""✏️ Запис у Google Таблицю через сервісний акаунт (Sheets API). Ключ — лише в змінній Railway GOOGLE_SERVICE_ACCOUNT_JSON.
Бот змінює тільки ту клітинку, яку людина відредагувала й підтвердила; ШІ сюди нічого не пише."""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import quote

import httpx

API = "https://sheets.googleapis.com/v4/spreadsheets"
_tok = {"v": "", "exp": 0}


def creds():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw if raw.startswith("{") else base64.b64decode(raw).decode())
    except Exception:  # noqa: BLE001
        return None


def email():
    c = creds()
    return (c or {}).get("client_email", "")


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


async def token(client):
    if _tok["v"] and _tok["exp"] > time.time() + 60:
        return _tok["v"]
    c = creds()
    if not c:
        raise RuntimeError("немає GOOGLE_SERVICE_ACCOUNT_JSON у змінних Railway")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": c["client_email"], "scope": "https://www.googleapis.com/auth/spreadsheets",
                            "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(c["private_key"].encode(), password=None)
    sig = _b64(key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    r = await client.post("https://oauth2.googleapis.com/token",
                          data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{body}.{sig}"})
    r.raise_for_status()
    _tok.update(v=r.json()["access_token"], exp=now + 3500)
    return _tok["v"]


def a1(sheet, row, col):
    from .contentplan import col_letter
    return f"'{sheet.replace(chr(39), chr(39) * 2)}'!{col_letter(col)}{row}"


async def get_cell(sheet_id, sheet, row, col):
    async with httpx.AsyncClient(timeout=20) as cl:
        t = await token(cl)
        r = await cl.get(f"{API}/{sheet_id}/values/{quote(a1(sheet, row, col))}", headers={"Authorization": f"Bearer {t}"})
        r.raise_for_status()
        v = r.json().get("values") or [[""]]
        return str(v[0][0]) if v and v[0] else ""


async def set_cell(sheet_id, sheet, row, col, value):
    async with httpx.AsyncClient(timeout=20) as cl:
        t = await token(cl)
        r = await cl.put(f"{API}/{sheet_id}/values/{quote(a1(sheet, row, col))}", params={"valueInputOption": "RAW"},
                         headers={"Authorization": f"Bearer {t}"}, json={"values": [[value]]})
        if r.status_code == 400 and "not supported for this document" in r.text:
            raise RuntimeError("це Excel-файл, а не Google Таблиця — у Диску: Файл → Зберегти як Google Таблицю")
        r.raise_for_status()
