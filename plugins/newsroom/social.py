"""🌐 Соцмережі як джерела: X (Twitter), Threads, Instagram, Facebook, Tumblr, YouTube, будь-який RSS.

Однаково для чужих ресурсів (беремо новини) і наших (не беремо — лише звіряємо, рахуємо охоплення для 📊 звіту).

Способи, від найнадійнішого:
1. Офіційні API з ключами в .env (ключі ніколи не пишуться в чат і журнали):
   X_BEARER_TOKEN · THREADS_TOKEN · META_TOKEN + IG_USER_ID (Instagram Business/Creator) ·
   FB_PAGE_TOKENS (сторінки, де ви адміністратор) · TUMBLR_API_KEY.
2. Публічні стрічки без ключів: Tumblr RSS, YouTube RSS, будь-який RSS/Atom.
3. RSSHUB_URL — ваш власний RSSHub (docker-compose), запасний шлях для чужих X/Threads/Instagram/Facebook.
   Він читає публічні сторінки; дотримання правил платформ — на власнику сервера.

Що вміє кожен шлях (чесно):
- X API: будь-які публічні акаунти + метрики (перегляди, лайки, репости, відповіді, цитати). Платний тариф X.
- Threads API: ВАШ акаунт + метрики. Чужі — лише через RSSHub.
- Instagram Graph: ваш акаунт + лайки/коментарі; чужі Business/Creator — через business_discovery; особисті — ні.
- Facebook Graph: ваші сторінки + реакції/коментарі/поширення; чужі сторінки — лише з дозволом Page Public Content
  Access у застосунку Meta, інакше RSSHub.
- Tumblr: RSS без ключа; з TUMBLR_API_KEY ще й кількість нотаток.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

UA = "OrionAI-Newsroom/2 (+social)"
GRAPH = os.getenv("META_GRAPH_VERSION", "v21.0")

PLATFORMS = {
    "x": ("𝕏", "X (Twitter)"),
    "threads": ("🧵", "Threads"),
    "ig": ("📸", "Instagram"),
    "fb": ("📘", "Facebook"),
    "tumblr": ("🌀", "Tumblr"),
    "yt": ("▶️", "YouTube"),
    "rss": ("🌐", "RSS"),
}
PREFIX = {"x": "x", "twitter": "x", "tw": "x", "threads": "threads", "th": "threads", "ig": "ig", "instagram": "ig",
          "insta": "ig", "fb": "fb", "facebook": "fb", "tumblr": "tumblr", "tb": "tumblr", "yt": "yt", "youtube": "yt", "rss": "rss"}
HANDLE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
NOT_USER = {"x": {"home", "search", "explore", "i", "intent", "share", "hashtag", "settings"},
            "ig": {"p", "reel", "reels", "explore", "stories", "accounts", "tv"},
            "fb": {"watch", "groups", "events", "sharer", "share", "photo", "story.php", "permalink.php", "marketplace", "reel"},
            "threads": {"search", "t"}}


def _clean(h):
    return (h or "").strip().lstrip("@").strip("/").split("?")[0].split("#")[0]


def parse(raw: str):
    """Посилання або «x:@user» -> ('social', 'x:user') або None (тоді це, можливо, Telegram)."""
    s = (raw or "").strip().strip("<>()[]{}\"'«»,;")
    if not s:
        return None
    m = re.match(r"^([a-z]{2,9}):\s*(.+)$", s, re.I)
    if m and m.group(1).lower() in PREFIX and not m.group(2).startswith("//"):
        p = PREFIX[m.group(1).lower()]
        h = m.group(2).strip()
        if p in ("rss", "yt") and h.startswith("http"):
            return "social", f"{p}:{h}"
        h = _clean(h)
        return ("social", f"{p}:{h.lower() if p != 'yt' else h}") if HANDLE.match(h) else None
    u = re.match(r"(?i)^(?:https?://)?(?:www\.|m\.|mobile\.)?([a-z0-9.\-]+\.[a-z]{2,})(/[^\s]*)?$", s)
    if not u:
        return None
    host, path = u.group(1).lower(), (u.group(2) or "/")
    parts = [x for x in path.split("?")[0].split("/") if x]
    first = parts[0] if parts else ""
    if host in ("x.com", "twitter.com"):
        h = _clean(first)
        return ("social", f"x:{h.lower()}") if h and h.lower() not in NOT_USER["x"] and HANDLE.match(h) else None
    if host in ("threads.net", "threads.com"):
        h = _clean(first)
        return ("social", f"threads:{h.lower()}") if h.startswith("@") or (h and h not in NOT_USER["threads"]) else None
    if host in ("instagram.com",):
        h = _clean(first)
        return ("social", f"ig:{h.lower()}") if h and h.lower() not in NOT_USER["ig"] and HANDLE.match(h) else None
    if host in ("facebook.com", "fb.com", "fb.me"):
        if first == "profile.php":
            q = re.search(r"id=(\d+)", path)
            return ("social", f"fb:{q.group(1)}") if q else None
        if first == "pages" and len(parts) >= 3:
            return "social", f"fb:{parts[-1]}"
        h = _clean(first)
        return ("social", f"fb:{h.lower()}") if h and h.lower() not in NOT_USER["fb"] and HANDLE.match(h) else None
    if host.endswith(".tumblr.com") and host.count(".") == 2:
        return "social", f"tumblr:{host.split('.')[0]}"
    if host == "tumblr.com" and first and HANDLE.match(first):
        return "social", f"tumblr:{first.lower()}"
    if host == "youtube.com":
        if first == "channel" and len(parts) > 1:
            return "social", f"yt:{parts[1]}"
        cid = re.search(r"channel_id=([\w-]+)", path)
        if cid:
            return "social", f"yt:{cid.group(1)}"
        return None
    if re.search(r"(?i)(rss|atom|feed|\.xml)(/|$|\?)", path):
        return "social", "rss:" + (s if s.lower().startswith("http") else "https://" + s)
    return None


def split(ref):
    p, _, h = ref.partition(":")
    return p, h


def display(ref):
    p, h = split(ref)
    icon, name = PLATFORMS.get(p, ("🌐", p))
    if p == "rss":
        return f"{icon} {re.sub(r'^https?://', '', h)[:60]}"
    return f"{icon} {name} · {'@' if p in ('x', 'threads', 'ig') else ''}{h}"


def link(ref):
    p, h = split(ref)
    return {"x": f"https://x.com/{h}", "threads": f"https://www.threads.net/@{h}", "ig": f"https://www.instagram.com/{h}/",
            "fb": f"https://www.facebook.com/{h}", "tumblr": f"https://{h}.tumblr.com", "yt": f"https://www.youtube.com/channel/{h}",
            "rss": h}.get(p, "")


def _ts(s):
    if not s:
        return time.time()
    try:
        s = s.replace("Z", "+00:00")
        if re.match(r".*[+-]\d{4}$", s):
            s = s[:-2] + ":" + s[-2:]
        return dt.datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s).timestamp()
        except Exception:  # noqa: BLE001
            return time.time()


def _strip(t):
    t = re.sub(r"(?is)<br\s*/?>|</p>", "\n", t or "")
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def parse_feed(xml_text: str):
    """RSS/Atom -> (назва, [{id, text, url, at, photo, metrics}])."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
    title = (root.findtext("channel/title") or root.findtext("a:title", namespaces=ns) or "").strip()
    out = []
    for it in root.findall("channel/item"):
        body = _strip(it.findtext("description") or "")
        head = _strip(it.findtext("title") or "")
        url = (it.findtext("link") or "").strip()
        img = re.search(r'<img[^>]+src="([^"]+)"', it.findtext("description") or "")
        out.append({"id": (it.findtext("guid") or url or head)[:300], "url": url, "at": _ts(it.findtext("pubDate")),
                    "text": body if head and body.startswith(head[:40]) else (head + ("\n\n" + body if body else "")).strip(),
                    "photo": img.group(1) if img else "", "metrics": {}})
    for it in root.findall("a:entry", ns):
        ln = it.find("a:link", ns)
        url = ln.get("href", "") if ln is not None else ""
        head = _strip(it.findtext("a:title", namespaces=ns) or "")
        body = _strip(it.findtext("a:content", namespaces=ns) or it.findtext("media:group/media:description", namespaces=ns) or "")
        views = it.find("media:group/media:community/media:statistics", ns)
        out.append({"id": (it.findtext("a:id", namespaces=ns) or url)[:300], "url": url,
                    "at": _ts(it.findtext("a:published", namespaces=ns) or it.findtext("a:updated", namespaces=ns)),
                    "text": (head + ("\n\n" + body if body else "")).strip(), "photo": "",
                    "metrics": {"views": int(views.get("views", 0))} if views is not None else {}})
    out.sort(key=lambda p: p["at"])
    return title, out


def configured():
    """-> {платформа: опис способу} — що підключено зараз (без значень ключів)."""
    e = os.getenv
    hub = bool(e("RSSHUB_URL"))
    return {
        "x": "X API" if e("X_BEARER_TOKEN") else ("RSSHub" if hub else ""),
        "threads": ("Threads API (ваш акаунт)" if e("THREADS_TOKEN") else "") + (" + RSSHub (чужі)" if hub else ""),
        "ig": ("Graph API" if e("META_TOKEN") and e("IG_USER_ID") else "") + (" + RSSHub" if hub else ""),
        "fb": ("Graph API (ваші сторінки)" if e("FB_PAGE_TOKENS") or e("META_TOKEN") else "") + (" + RSSHub" if hub else ""),
        "tumblr": "Tumblr API" if e("TUMBLR_API_KEY") else "RSS (без ключа)",
        "yt": "RSS (без ключа)",
        "rss": "RSS (без ключа)",
    }


def _page_tokens():
    """FB_PAGE_TOKENS=сторінка=токен,сторінка2=токен2 -> {сторінка: токен}."""
    out = {}
    for part in (os.getenv("FB_PAGE_TOKENS") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


class SocialCollector:
    def __init__(self, client=None):
        self.client = client
        self._ids = {}

    async def _c(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA})
        return self.client

    async def _json(self, url, params=None, headers=None):
        c = await self._c()
        r = await c.get(url, params=params, headers=headers)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {})
                msg = msg.get("message") if isinstance(msg, dict) else msg
            except Exception:  # noqa: BLE001
                msg = ""
            raise RuntimeError(f"HTTP {r.status_code}" + (f": {str(msg)[:160]}" if msg else ""))
        return r.json()

    async def _feed(self, url):
        c = await self._c()
        r = await c.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"стрічка: HTTP {r.status_code}")
        return parse_feed(r.text)

    async def _rsshub(self, path):
        base = (os.getenv("RSSHUB_URL") or "").rstrip("/")
        if not base:
            raise RuntimeError("потрібен ключ API цієї мережі або власний RSSHub (RSSHUB_URL) — див. 🌐 Соцмережі")
        key = os.getenv("RSSHUB_KEY")
        return await self._feed(f"{base}{path}" + (f"?key={key}" if key else ""))

    # ------------------------------------------------------------ платформи
    async def x(self, h):
        tok = os.getenv("X_BEARER_TOKEN")
        if not tok:
            return await self._rsshub(f"/twitter/user/{h}")
        hd = {"Authorization": f"Bearer {tok}"}
        uid = self._ids.get(("x", h))
        if not uid:
            u = await self._json(f"https://api.x.com/2/users/by/username/{h}", headers=hd)
            uid = self._ids[("x", h)] = u["data"]["id"]
        d = await self._json(f"https://api.x.com/2/users/{uid}/tweets", headers=hd, params={
            "max_results": 20, "exclude": "replies", "tweet.fields": "created_at,public_metrics,referenced_tweets"})
        out = []
        for t in d.get("data", []):
            pm = t.get("public_metrics", {})
            rt = any(r.get("type") == "retweeted" for r in t.get("referenced_tweets", []) or [])
            out.append({"id": t["id"], "text": t.get("text", ""), "url": f"https://x.com/{h}/status/{t['id']}", "at": _ts(t.get("created_at")),
                        "photo": "", "fwd": "repost" if rt else "",
                        "metrics": {"views": pm.get("impression_count", 0), "likes": pm.get("like_count", 0),
                                    "reposts": pm.get("retweet_count", 0) + pm.get("quote_count", 0), "comments": pm.get("reply_count", 0)}})
        out.sort(key=lambda p: p["at"])
        return f"@{h}", out

    async def threads(self, h, ours=False):
        tok = os.getenv("THREADS_TOKEN")
        if tok and (ours or h == (os.getenv("THREADS_USERNAME") or "").lower()):
            d = await self._json("https://graph.threads.net/v1.0/me/threads", params={
                "fields": "id,text,permalink,timestamp,media_type", "limit": 25, "access_token": tok})
            out = []
            for t in d.get("data", []):
                m = {}
                try:
                    ins = await self._json(f"https://graph.threads.net/v1.0/{t['id']}/insights",
                                           params={"metric": "views,likes,replies,reposts,quotes", "access_token": tok})
                    vals = {i["name"]: (i.get("values") or [{}])[0].get("value", 0) for i in ins.get("data", [])}
                    m = {"views": vals.get("views", 0), "likes": vals.get("likes", 0),
                         "reposts": vals.get("reposts", 0) + vals.get("quotes", 0), "comments": vals.get("replies", 0)}
                except Exception:  # noqa: BLE001
                    pass
                out.append({"id": t["id"], "text": t.get("text", ""), "url": t.get("permalink", ""), "at": _ts(t.get("timestamp")),
                            "photo": "", "metrics": m})
            out.sort(key=lambda p: p["at"])
            return f"@{h}", out
        return await self._rsshub(f"/threads/{h}")

    async def ig(self, h, ours=False):
        tok, me = os.getenv("META_TOKEN"), os.getenv("IG_USER_ID")
        if not (tok and me):
            return await self._rsshub(f"/instagram/user/{h}")
        fields = "id,caption,permalink,timestamp,like_count,comments_count,media_type,media_url,thumbnail_url"
        if ours or h == (os.getenv("IG_USERNAME") or "").lower():
            d = await self._json(f"https://graph.facebook.com/{GRAPH}/{me}/media", params={"fields": fields, "limit": 25, "access_token": tok})
            media = d.get("data", [])
        else:  # чужий Business/Creator-акаунт
            d = await self._json(f"https://graph.facebook.com/{GRAPH}/{me}", params={
                "fields": f"business_discovery.username({h}){{name,media.limit(20){{{fields}}}}}", "access_token": tok})
            media = d.get("business_discovery", {}).get("media", {}).get("data", [])
        out = [{"id": m["id"], "text": m.get("caption", ""), "url": m.get("permalink", ""), "at": _ts(m.get("timestamp")),
                "photo": m.get("thumbnail_url") or (m.get("media_url") if m.get("media_type") == "IMAGE" else ""),
                "metrics": {"likes": m.get("like_count", 0), "comments": m.get("comments_count", 0)}} for m in media]
        out.sort(key=lambda p: p["at"])
        return f"@{h}", out

    async def fb(self, h, ours=False):
        tok = _page_tokens().get(h) or os.getenv("META_TOKEN")
        if not tok:
            return await self._rsshub(f"/facebook/page/{h}")
        try:
            d = await self._json(f"https://graph.facebook.com/{GRAPH}/{h}/posts", params={
                "fields": "id,message,permalink_url,created_time,full_picture,shares,reactions.summary(total_count).limit(0),"
                          "comments.summary(total_count).limit(0)", "limit": 25, "access_token": tok})
        except RuntimeError:
            if os.getenv("RSSHUB_URL") and not ours:
                return await self._rsshub(f"/facebook/page/{h}")
            raise
        out = []
        for p in d.get("data", []):
            out.append({"id": p["id"], "text": p.get("message", ""), "url": p.get("permalink_url", ""), "at": _ts(p.get("created_time")),
                        "photo": p.get("full_picture", ""),
                        "metrics": {"likes": p.get("reactions", {}).get("summary", {}).get("total_count", 0),
                                    "comments": p.get("comments", {}).get("summary", {}).get("total_count", 0),
                                    "reposts": p.get("shares", {}).get("count", 0)}})
        out.sort(key=lambda p: p["at"])
        return h, out

    async def tumblr(self, h):
        key = os.getenv("TUMBLR_API_KEY")
        if not key:
            return await self._feed(f"https://{h}.tumblr.com/rss")
        d = await self._json(f"https://api.tumblr.com/v2/blog/{h}.tumblr.com/posts", params={"api_key": key, "limit": 20, "filter": "text"})
        resp = d.get("response", {})
        out = []
        for p in resp.get("posts", []):
            text = "\n\n".join(x for x in (p.get("title"), p.get("body"), p.get("caption"), p.get("summary")) if x)
            out.append({"id": str(p["id"]), "text": _strip(text), "url": p.get("post_url", ""), "at": float(p.get("timestamp", time.time())),
                        "photo": "", "fwd": "repost" if p.get("reblogged_from_id") else "", "metrics": {"likes": p.get("note_count", 0)}})
        out.sort(key=lambda p: p["at"])
        return resp.get("blog", {}).get("title", h), out

    async def fetch(self, ref, ours=False):
        """-> (назва, [дописи від старших до новіших]); кожен допис: id, text, url, at, photo, metrics{views,likes,reposts,comments}."""
        p, h = split(ref)
        if p == "x":
            return await self.x(h)
        if p == "threads":
            return await self.threads(h, ours)
        if p == "ig":
            return await self.ig(h, ours)
        if p == "fb":
            return await self.fb(h, ours)
        if p == "tumblr":
            return await self.tumblr(h)
        if p == "yt":
            return await self._feed(h if h.startswith("http") else f"https://www.youtube.com/feeds/videos.xml?channel_id={h}")
        if p == "rss":
            return await self._feed(h)
        raise RuntimeError(f"невідома мережа: {p}")
