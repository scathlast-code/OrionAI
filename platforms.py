"""Робота з ботами різних платформ для головного бота: Telegram, WhatsApp (Cloud API), Instagram (Messaging API), HTTP.

Кожна функція повертає звичайні словники, без прив'язки до Telegram-бібліотеки,
тож головний бот однаково керує ботами з будь-якого месенджера.
"""

import os

import httpx


def env(name, default=""):
    return os.environ.get(name, default).strip()


TG_API = env("TELEGRAM_API_URL", "https://api.telegram.org")
GRAPH = env("GRAPH_API_URL", "https://graph.facebook.com")
IG_GRAPH = env("IG_GRAPH_API_URL", "https://graph.instagram.com")
GRAPH_VERSION = env("GRAPH_VERSION", "v25.0")

KINDS = {
    "telegram": ("✈️", "Telegram"),
    "whatsapp": ("🟩", "WhatsApp"),
    "instagram": ("📸", "Instagram"),
    "http": ("🔌", "Інший (HTTP)"),
}


class PlatformError(Exception):
    pass


# ---------------------------------------------------------------- Telegram


async def tg(token, method, **params):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{TG_API}/bot{token}/{method}", json=params)
    try:
        data = r.json()
    except ValueError as e:
        raise PlatformError(f"Telegram відповів {r.status_code}") from e
    if not data.get("ok"):
        desc = data.get("description", "невідома помилка")
        if r.status_code == 401:
            desc = "токен недійсний (його відкликали в @BotFather або він неправильний)"
        raise PlatformError(desc)
    return data["result"]


async def tg_info(token):
    me = await tg(token, "getMe")
    out = {"id": me["id"], "username": me.get("username"), "name": me.get("first_name"),
           "can_join_groups": me.get("can_join_groups"), "inline": me.get("supports_inline_queries")}
    try:
        wh = await tg(token, "getWebhookInfo")
        out["webhook"] = wh.get("url") or ""
        out["pending"] = wh.get("pending_update_count", 0)
        out["last_error"] = wh.get("last_error_message")
        out["last_error_date"] = wh.get("last_error_date")
    except PlatformError:
        pass
    for key, method in (("description", "getMyDescription"), ("short", "getMyShortDescription")):
        try:
            out[key] = (await tg(token, method)).get(key if key == "description" else "short_description", "")
        except PlatformError:
            out[key] = ""
    try:
        out["commands"] = [(c["command"], c["description"]) for c in await tg(token, "getMyCommands")]
    except PlatformError:
        out["commands"] = []
    return out


# ---------------------------------------------------------------- WhatsApp Cloud API


async def graph(token, method, path, base=None, **kw):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{base or GRAPH}/{GRAPH_VERSION}/{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
    try:
        data = r.json()
    except ValueError as e:
        raise PlatformError(f"Meta відповіла {r.status_code}") from e
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        msg = err.get("message", str(data)[:200])
        if err.get("code") == 190:
            msg = "токен Meta недійсний або прострочений — створіть постійний токен системного користувача"
        raise PlatformError(msg)
    return data


async def wa_info(token, phone_id):
    d = await graph(token, "GET", phone_id, params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,status,name_status"})
    out = {"name": d.get("verified_name"), "username": d.get("display_phone_number"), "quality": d.get("quality_rating"),
           "tier": d.get("messaging_limit_tier"), "status_text": d.get("status")}
    try:
        p = (await graph(token, "GET", f"{phone_id}/whatsapp_business_profile", params={"fields": "about,description,email,websites"}))["data"][0]
        out["description"] = p.get("description", "")
        out["short"] = p.get("about", "")
    except (PlatformError, KeyError, IndexError):
        pass
    try:
        ca = await graph(token, "GET", f"{phone_id}/conversational_automation")
        out["commands"] = [(c["command_name"], c["command_description"]) for c in ca.get("commands", [])]
    except PlatformError:
        out["commands"] = []
    return out


# ---------------------------------------------------------------- Instagram


async def ig_info(token, ig_id):
    d = await graph(token, "GET", "me", base=IG_GRAPH, params={"fields": "user_id,username,name,account_type,followers_count"})
    out = {"name": d.get("name"), "username": d.get("username"), "followers": d.get("followers_count"), "commands": []}
    try:
        mp = await graph(token, "GET", "me/messenger_profile", base=IG_GRAPH, params={"fields": "ice_breakers"})
        ib = (mp.get("data") or [{}])[0].get("ice_breakers") or []
        if ib and "call_to_actions" in ib[0]:
            ib = ib[0]["call_to_actions"]
        out["commands"] = [(x.get("payload", ""), x.get("question", "")) for x in ib]
    except PlatformError:
        pass
    return out


# ---------------------------------------------------------------- спільне


async def info(kind, s):
    """s — секрети бота: {"token": …, "phone_id": …, "ig_id": …}."""
    if kind == "telegram":
        return await tg_info(s["token"])
    if kind == "whatsapp":
        return await wa_info(s["token"], s["phone_id"])
    if kind == "instagram":
        return await ig_info(s["token"], s.get("ig_id"))
    return {}


PROFILE_FIELDS = {
    "telegram": [("name", "Назва"), ("description", "Опис (видно до першого повідомлення)"), ("short", "Короткий опис (у профілі)")],
    "whatsapp": [("description", "Опис компанії"), ("short", "Статус «Про мене»")],
    "instagram": [],
}


async def set_profile(kind, s, field, value):
    if kind == "telegram":
        method, key = {"name": ("setMyName", "name"), "description": ("setMyDescription", "description"), "short": ("setMyShortDescription", "short_description")}[field]
        return await tg(s["token"], method, **{key: value})
    if kind == "whatsapp":
        body = {"messaging_product": "whatsapp", {"description": "description", "short": "about"}[field]: value}
        return await graph(s["token"], "POST", f"{s['phone_id']}/whatsapp_business_profile", json=body)
    raise PlatformError("Instagram не дозволяє змінювати профіль через API — змініть його в застосунку Instagram")


COMMANDS_NAME = {"telegram": "Команди (меню «/»)", "whatsapp": "Команди («/» у чаті)", "instagram": "Швидкі питання (Ice Breakers)"}


async def set_commands(kind, s, commands):
    """commands — [(команда, опис)]; для Instagram — [(payload, питання)]."""
    if kind == "telegram":
        return await tg(s["token"], "setMyCommands", commands=[{"command": c.lstrip("/").lower()[:32], "description": d[:256]} for c, d in commands])
    if kind == "whatsapp":
        body = {"enable_welcome_message": True, "commands": [{"command_name": c.lstrip("/")[:32], "command_description": d[:256]} for c, d in commands]}
        return await graph(s["token"], "POST", f"{s['phone_id']}/conversational_automation", json=body)
    if kind == "instagram":
        body = {"platform": "instagram", "ice_breakers": [{"call_to_actions": [{"question": q[:80], "payload": (p or q)[:1000]} for p, q in commands[:4]], "locale": "default"}]}
        return await graph(s["token"], "POST", "me/messenger_profile", base=IG_GRAPH, json=body)
    raise PlatformError("для цього бота команд немає")


async def send_as(kind, s, to, text):
    """Надіслати повідомлення від імені бота (Telegram — у чат to; WhatsApp — на номер to; Instagram — IGSID to)."""
    if kind == "telegram":
        return await tg(s["token"], "sendMessage", chat_id=to, text=text[:4096])
    if kind == "whatsapp":
        body = {"messaging_product": "whatsapp", "to": str(to), "type": "text", "text": {"body": text[:4096]}}
        return await graph(s["token"], "POST", f"{s['phone_id']}/messages", json=body)
    if kind == "instagram":
        body = {"recipient": {"id": str(to)}, "message": {"text": text[:1000]}}
        return await graph(s["token"], "POST", "me/messages", base=IG_GRAPH, json=body)
    raise PlatformError("цей тип бота не надсилає повідомлення сам")


async def health(kind, s, info_cache=None):
    """(стан, деталі): стан up | down | warn."""
    try:
        i = await info(kind, s)
    except PlatformError as e:
        return "down", str(e), {}
    except httpx.HTTPError as e:
        return "warn", f"немає зв'язку з сервером платформи: {e.__class__.__name__}", {}
    if kind == "telegram":
        if i.get("webhook") and i.get("pending", 0) > 50:
            return "warn", f"бот не встигає: у черзі {i['pending']} повідомлень" + (f", остання помилка: {i['last_error']}" if i.get("last_error") else ""), i
        if i.get("last_error"):
            return "warn", f"остання помилка вебхука: {i['last_error']}", i
    if kind == "whatsapp":
        if i.get("quality") in ("RED", "YELLOW"):
            return "warn", f"якість номера: {i['quality']} — менше розсилайте й перевірте скарги", i
        if i.get("status_text") and i["status_text"] not in ("CONNECTED", "connected"):
            return "warn", f"стан номера: {i['status_text']}", i
    return "up", "", i
