"""🌐 Facebook, Instagram і Threads через офіційні безкоштовні API Meta: публікація з черги, статистика, коментарі.

Ключі — лише у Railway Variables (не в чаті):
  META_PAGE_ID, META_PAGE_TOKEN — сторінка Facebook (довгостроковий токен сторінки з правами pages_manage_posts,
                                  pages_read_engagement, pages_manage_engagement, read_insights);
  META_IG_ID                    — ID бізнес-акаунта Instagram, прив'язаного до цієї сторінки (instagram_content_publish,
                                  instagram_basic, instagram_manage_comments, instagram_manage_insights) — той самий токен сторінки;
  THREADS_TOKEN, THREADS_USER_ID — Threads API (threads_basic, threads_content_publish, threads_read_replies,
                                  threads_manage_replies, threads_manage_insights).
Що вміє: опублікувати текст (і фото за публічним посиланням), забрати останні дописи з охопленням у «🏠 Наші ресурси»
(звідти — статистика тижня, найкращий час, «♻️ Варто повторити»), показати нові коментарі й відповісти на них.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

import httpx

log = logging.getLogger("orion.newsroom.meta")
GRAPH = "https://graph.facebook.com/v21.0"
THREADS = "https://graph.threads.net/v1.0"
NETS = {"fb": "📘 Facebook", "ig": "📸 Instagram", "th": "🧵 Threads"}
NEED = {"fb": ("META_PAGE_ID", "META_PAGE_TOKEN"), "ig": ("META_IG_ID", "META_PAGE_TOKEN"), "th": ("THREADS_TOKEN",)}


class MetaError(Exception):
    pass


def env(k):
    return (os.environ.get(k) or "").strip()


def ready(net):
    return all(env(k) for k in NEED.get(net, ("-",)))


def missing(net):
    return [k for k in NEED.get(net, ()) if not env(k)]


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00").replace("+0000", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def _call(method, url, **params):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await (c.get(url, params=params) if method == "GET" else c.post(url, data=params))
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or "error" in data:
        err = (data.get("error") or {}).get("message") or r.text[:200]
        raise MetaError(f"HTTP {r.status_code}: {err}")
    return data


# ---------------------------------------------------------------- публікація
async def publish(net, text, image_url=None, link=None):
    """-> (ok, посилання на допис або текст помилки)."""
    if not ready(net):
        return False, "не налаштовано: " + ", ".join(missing(net))
    try:
        if net == "fb":
            page, tok = env("META_PAGE_ID"), env("META_PAGE_TOKEN")
            if image_url:
                r = await _call("POST", f"{GRAPH}/{page}/photos", url=image_url, caption=text, access_token=tok)
                pid = r.get("post_id") or r.get("id")
            else:
                r = await _call("POST", f"{GRAPH}/{page}/feed", message=text, access_token=tok, **({"link": link} if link else {}))
                pid = r.get("id")
            info = await _call("GET", f"{GRAPH}/{pid}", fields="permalink_url", access_token=tok)
            return True, info.get("permalink_url") or f"https://facebook.com/{pid}"
        if net == "ig":
            if not image_url:
                return False, "Instagram публікує лише з фото чи відео — додайте картку до новини"
            ig, tok = env("META_IG_ID"), env("META_PAGE_TOKEN")
            r = await _call("POST", f"{GRAPH}/{ig}/media", image_url=image_url, caption=text[:2200], access_token=tok)
            r2 = await _call("POST", f"{GRAPH}/{ig}/media_publish", creation_id=r["id"], access_token=tok)
            info = await _call("GET", f"{GRAPH}/{r2['id']}", fields="permalink", access_token=tok)
            return True, info.get("permalink") or "https://instagram.com/"
        if net == "th":
            uid, tok = env("THREADS_USER_ID") or "me", env("THREADS_TOKEN")
            args = {"media_type": "IMAGE", "image_url": image_url} if image_url else {"media_type": "TEXT"}
            r = await _call("POST", f"{THREADS}/{uid}/threads", text=text[:500], access_token=tok, **args)
            r2 = await _call("POST", f"{THREADS}/{uid}/threads_publish", creation_id=r["id"], access_token=tok)
            info = await _call("GET", f"{THREADS}/{r2['id']}", fields="permalink", access_token=tok)
            return True, info.get("permalink") or "https://www.threads.com/"
    except (MetaError, httpx.HTTPError, KeyError) as exc:
        return False, str(exc)[:300]
    return False, "невідома мережа"


# ---------------------------------------------------------------- статистика
async def recent_posts(net, limit=25):
    """Останні наші дописи з охопленням -> [{id, text, at, url, views, likes, comments, reposts}]."""
    if not ready(net):
        return []
    out = []
    if net == "fb":
        page, tok = env("META_PAGE_ID"), env("META_PAGE_TOKEN")
        r = await _call("GET", f"{GRAPH}/{page}/posts", access_token=tok, limit=limit,
                        fields="id,message,created_time,permalink_url,shares,reactions.summary(true).limit(0),comments.summary(true).limit(0)")
        for p in r.get("data") or []:
            out.append({"id": p["id"], "text": p.get("message") or "", "at": _ts(p.get("created_time")), "url": p.get("permalink_url") or "",
                        "likes": ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0),
                        "comments": ((p.get("comments") or {}).get("summary") or {}).get("total_count", 0),
                        "reposts": (p.get("shares") or {}).get("count", 0), "views": 0})
    elif net == "ig":
        ig, tok = env("META_IG_ID"), env("META_PAGE_TOKEN")
        r = await _call("GET", f"{GRAPH}/{ig}/media", access_token=tok, limit=limit,
                        fields="id,caption,timestamp,permalink,like_count,comments_count")
        for p in r.get("data") or []:
            out.append({"id": p["id"], "text": p.get("caption") or "", "at": _ts(p.get("timestamp")), "url": p.get("permalink") or "",
                        "likes": p.get("like_count", 0), "comments": p.get("comments_count", 0), "reposts": 0, "views": 0})
    elif net == "th":
        uid, tok = env("THREADS_USER_ID") or "me", env("THREADS_TOKEN")
        r = await _call("GET", f"{THREADS}/{uid}/threads", access_token=tok, limit=limit, fields="id,text,timestamp,permalink")
        for p in r.get("data") or []:
            row = {"id": p["id"], "text": p.get("text") or "", "at": _ts(p.get("timestamp")), "url": p.get("permalink") or "",
                   "likes": 0, "comments": 0, "reposts": 0, "views": 0}
            try:
                ins = await _call("GET", f"{THREADS}/{p['id']}/insights", metric="views,likes,replies,reposts", access_token=tok)
                for m in ins.get("data") or []:
                    v = (m.get("values") or [{}])[0].get("value", 0)
                    row[{"replies": "comments"}.get(m.get("name"), m.get("name"))] = v
            except MetaError:
                pass
            out.append(row)
    return out


# ---------------------------------------------------------------- коментарі
async def comments(net, post_id, limit=25):
    """-> [{id, text, who, at}]."""
    if not ready(net):
        return []
    if net == "fb":
        r = await _call("GET", f"{GRAPH}/{post_id}/comments", access_token=env("META_PAGE_TOKEN"), limit=limit,
                        fields="id,message,from,created_time", order="reverse_chronological")
        return [{"id": c["id"], "text": c.get("message") or "", "who": (c.get("from") or {}).get("name") or "", "at": _ts(c.get("created_time"))}
                for c in r.get("data") or []]
    if net == "ig":
        r = await _call("GET", f"{GRAPH}/{post_id}/comments", access_token=env("META_PAGE_TOKEN"), limit=limit, fields="id,text,username,timestamp")
        return [{"id": c["id"], "text": c.get("text") or "", "who": c.get("username") or "", "at": _ts(c.get("timestamp"))}
                for c in r.get("data") or []]
    if net == "th":
        r = await _call("GET", f"{THREADS}/{post_id}/replies", access_token=env("THREADS_TOKEN"), fields="id,text,username,timestamp")
        return [{"id": c["id"], "text": c.get("text") or "", "who": c.get("username") or "", "at": _ts(c.get("timestamp"))}
                for c in r.get("data") or []]
    return []


async def reply(net, comment_id, text):
    if not ready(net):
        return False, "не налаштовано"
    try:
        if net == "fb":
            await _call("POST", f"{GRAPH}/{comment_id}/comments", message=text, access_token=env("META_PAGE_TOKEN"))
        elif net == "ig":
            await _call("POST", f"{GRAPH}/{comment_id}/replies", message=text, access_token=env("META_PAGE_TOKEN"))
        elif net == "th":
            uid, tok = env("THREADS_USER_ID") or "me", env("THREADS_TOKEN")
            r = await _call("POST", f"{THREADS}/{uid}/threads", media_type="TEXT", text=text[:500], reply_to_id=comment_id, access_token=tok)
            await _call("POST", f"{THREADS}/{uid}/threads_publish", creation_id=r["id"], access_token=tok)
        return True, ""
    except (MetaError, httpx.HTTPError, KeyError) as exc:
        return False, str(exc)[:300]


async def hide(net, comment_id, hidden=True):
    """Сховати коментар (спам, образи) — бачить лише автор і він сам."""
    try:
        if net == "fb":
            await _call("POST", f"{GRAPH}/{comment_id}", is_hidden="true" if hidden else "false", access_token=env("META_PAGE_TOKEN"))
        elif net == "ig":
            await _call("POST", f"{GRAPH}/{comment_id}", hide="true" if hidden else "false", access_token=env("META_PAGE_TOKEN"))
        elif net == "th":
            await _call("POST", f"{THREADS}/{comment_id}/manage_reply", hide="true" if hidden else "false", access_token=env("THREADS_TOKEN"))
        return True, ""
    except (MetaError, httpx.HTTPError) as exc:
        return False, str(exc)[:300]
