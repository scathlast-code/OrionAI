"""📥 Пошук і завантаження відкритих медіа (з MediaBot): yt-dlp, gallery-dl, прямі файли, дописи Telegram, Open Graph.

Додано для OrionAI: входи через cookies (для Instagram, Facebook, TikTok…), режими MP3 / GIF / без звуку,
короткий запис ефіру. Перевірка адрес (лише публічні HTTP/HTTPS) — як в оригіналі.
"""
import asyncio
import os
import ipaddress
import json
import mimetypes
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from lxml import html

from html import escape as html_escape

from .common import run_stream
from .common import JobError, URLDenied, WEB_LIMIT, run, safe_name

BLOCKED = (
    "line.me", "lin.ee", "line-apps.com", "line-scdn.net",
    "bip.com", "bip.ai", "periscope.tv", "pscp.tv",
    "pornhub.com", "pornhub.org", "pornhub.net", "pornhubpremium.com",
    "phncdn.com", "phprcdn.com",
)
GALLERY = (
    "threads.net", "threads.com", "instagram.com", "x.com", "twitter.com",
    "facebook.com", "tumblr.com", "pinterest.com", "pin.it",
    "tiktok.com", "reddit.com", "snapchat.com", "likee.video",
)
MEDIA = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".heic",
    ".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".mpeg", ".ts", ".flv", ".3gp",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".oga", ".mka",
}
SUBTITLES = {".vtt", ".srt", ".ass", ".ttml", ".srv3", ".json3", ".dfxp"}
YT = (
    "yt-dlp", "--ignore-config", "--no-plugin-dirs", "--no-cache-dir",
    "--no-progress", "--no-warnings", "--socket-timeout", "15",
    "--retries", "1", "--js-runtimes", "node",
)
FF = (
    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
    "-protocol_whitelist", "file,pipe",
)


UA_TITLES = (
    (r"^TikTok video #(\d+)", r"Відео TikTok \1"),
    (r"^Video by (.+)", r"Відео від \1"),
    (r"^Photo by (.+)", r"Фото від \1"),
    (r"^Post by (.+)", r"Допис від \1"),
    (r"^Reel by (.+)", r"Reels від \1"),
    (r"^Story by (.+)", r"Сторіс від \1"),
    (r"^(Instagram|Facebook|Threads|Twitter|X) (video|post|photo)\b", lambda m: {"video": "Відео", "post": "Допис", "photo": "Фото"}[m[2]] + " " + m[1]),
    (r"^Untitled$", "Без назви"),
)


def ua_title(title):
    """Англійські заглушки завантажувачів («TikTok video #…», «Video by …») → українською."""
    title = str(title or "")
    for pattern, repl in UA_TITLES:
        title = re.sub(pattern, repl, title, count=1)
    return title


@dataclass
class Item:
    engine: str
    url: str
    info: dict
    index: int | None = None

    @property
    def title(self):
        return ua_title(self.info.get("title") or self.info.get("filename") or "Медіа")


COOKIES_DIR = None  # тека з cookies.txt за платформами (задає плагін)
COOKIE_SITES = {
    "instagram": ("instagram.com",), "facebook": ("facebook.com", "fb.watch"), "threads": ("threads.net", "threads.com"),
    "tiktok": ("tiktok.com",), "x": ("x.com", "twitter.com"), "youtube": ("youtube.com", "youtu.be"),
    "reddit": ("reddit.com",), "pinterest": ("pinterest.com", "pin.it"), "vk": ("vk.com", "vkvideo.ru"),
}


def cookie_file(url):
    """cookies.txt для сайту посилання, якщо його завантажили в «🍪 Входи»."""
    if not COOKIES_DIR:
        return None
    host = (urlsplit(url).hostname or "").lower()
    for site, domains in COOKIE_SITES.items():
        if domain_matches(host, domains):
            path = Path(COOKIES_DIR) / f"{site}.txt"
            return path if path.is_file() else None
    return None


def proxy_args(url):
    """MEDIA_PROXY у Railway (напр. http://user:pass@host:port) — запити до YouTube йдуть через нього,
    бо YouTube блокує IP серверів. MEDIA_PROXY_ALL=1 — через проксі всі сайти."""
    proxy = os.environ.get("MEDIA_PROXY", "").strip()
    if not proxy:
        return []
    host = (urlsplit(url).hostname or "").lower()
    if os.environ.get("MEDIA_PROXY_ALL", "").strip() in ("1", "true", "yes") or domain_matches(host, ("youtube.com", "youtu.be")):
        return ["--proxy", proxy]
    return []


def cookie_args(url, flag="--cookies"):
    path = cookie_file(url)
    return [flag, str(path)] if path else []


@dataclass
class Options:
    height: int = 0
    mode: str = "video"  # video | audio (MP3) | gif | mute
    live: int = 180      # скільки секунд записувати активний ефір
    audio: str = "auto"
    subs: str = "off"
    cuts: tuple = ()
    join: bool = False
    post: bool = True
    target: str = "tg"


def domain_matches(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


async def check_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        raise URLDenied("Невалідне посилання.") from None
    if (
        parsed.scheme not in {"http", "https"} or not host or
        parsed.username is not None or parsed.password is not None or
        port not in {None, 80, 443} or re.search(r"[\s\\\x00-\x1f]", url)
    ):
        raise URLDenied("Потрібне публічне посилання HTTP або HTTPS.")
    if domain_matches(host, BLOCKED) or "pornhub" in host:
        raise URLDenied("Це джерело вимкнене.")
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=8,
        )
    except (OSError, TimeoutError):
        raise JobError("Сервер джерела не відповідає.") from None
    if not addresses or any(
        not ipaddress.ip_address(row[4][0]).is_global for row in addresses
    ):
        raise URLDenied("Адреса джерела має бути публічною.")


@asynccontextmanager
async def open_url(url, referer=None, agent=None):
    headers = {"User-Agent": agent or "Mozilla/5.0"}
    if referer:
        parsed = urlsplit(referer)
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=False, trust_env=False, headers=headers
    ) as client:
        for _ in range(7):
            await check_url(url)
            async with client.stream("GET", url) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                yield response
                return
    raise JobError("Забагато перенаправлень.")


def media_extension(response):
    mime = response.headers.get("content-type", "").split(";")[0].lower()
    url_suffix = Path(urlsplit(str(response.url)).path).suffix.lower()
    if mime.startswith(("image/", "video/", "audio/")):
        aliases = {
            "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/x-m4a": ".m4a",
            "video/x-matroska": ".mkv", "image/jpeg": ".jpg",
        }
        return aliases.get(mime) or mimetypes.guess_extension(mime) or (
            url_suffix if url_suffix in MEDIA else None
        )
    if mime in {"", "application/octet-stream"} and url_suffix in MEDIA:
        return url_suffix
    return None


def read_html(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        parser = html.HTMLParser(no_network=True, remove_comments=True)
    else:
        parser = html.HTMLParser(encoding="utf-8", no_network=True, remove_comments=True)
    return html.document_fromstring(raw or b"<html></html>", parser=parser)


def post_text(info):
    for key in ("description", "caption", "content", "text", "summary"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            if re.search(r"<(?:p|div|a|br|span)\b", value, re.I):
                return " ".join(text.strip() for text in read_html(value).itertext() if text.strip())
            return value.strip()
    return ""


def author(info):
    name = (
        info.get("uploader") or info.get("channel") or info.get("author")
        or info.get("username") or info.get("creator") or ""
    )
    link = info.get("uploader_url") or info.get("channel_url") or info.get("author_url")
    if isinstance(name, dict):
        link = link or name.get("url")
        name = name.get("name") or name.get("username") or name.get("nick") or ""
    link = link if isinstance(link, str) and link.startswith(("https://", "http://")) else ""
    return str(name), link


def allow_extractor(data):
    extractor = str(data.get("extractor_key") or data.get("ie_key") or "").lower()
    if any(name in extractor for name in ("pornhub", "periscope")):
        raise URLDenied("Це джерело вимкнене.")


# Сайти, де заголовок сторінки без самого відео — це невдача, а не результат.
VIDEO_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "vimeo.com", "twitch.tv", "dailymotion.com", "rumble.com",
               "soundcloud.com", "fb.watch")
# Друга спроба для YouTube, коли він просить «підтвердити, що ви не бот»: інші клієнти плеєра.
YT_RETRY = ("--extractor-args", "youtube:player_client=default,tv,mweb,web_safari,web_embedded")
BOT_CHECK = re.compile(r"sign in to confirm|not a bot|confirm you.re not|po.?token", re.I)


def explain(raw):
    """Сирий текст помилки yt-dlp / gallery-dl → зрозуміла причина + технічний рядок."""
    raw = str(raw or "").strip()
    low = raw.lower()
    if BOT_CHECK.search(raw):
        why = ("YouTube не пускає сервер бота: просить «підтвердити, що ви не бот». Він блокує IP-адреси серверів "
               "(Railway), посилання тут ні до чого. Інші сайти (TikTok, Instagram, X…) працюють."
               + ("\nПроксі MEDIA_PROXY теж заблоковано — потрібен домашній (residential) проксі." if os.environ.get("MEDIA_PROXY")
                  else "\n🔧 Надійне рішення — домашній проксі: змінна MEDIA_PROXY у Railway. Іноді блок знімається сам за кілька годин."))
    elif re.search(r"http error 403|forbidden", low):
        why = "Сайт відмовив серверу бота (403) — часто так блокують сервери; іноді допомагає повтор."
    elif re.search(r"private|log ?in|sign in|authenticat|members.only|age.restrict|inappropriate|cookies (?:are|is) (?:required|needed)", low):
        why = "Сайт віддає це лише після входу (приватне, 18+ чи лише для підписників). 🍪 Додайте cookies цього сайту в «Входи на сайти»."
    elif "unsupported url" in low:
        why = "Цей сайт завантажувачі не підтримують."
    elif re.search(r"unavailable|removed|not available|deleted|does not exist|404", low):
        why = "Відео недоступне: видалене, заблоковане у країні сервера або посилання неповне."
    elif re.search(r"429|too many requests|rate.?limit", low):
        why = "Сайт тимчасово обмежив запити з сервера. Спробуйте за кілька хвилин."
    elif re.search(r"connection aborted|remotedisconnected|connection reset|без медіа", low):
        why = "Сайт обірвав з'єднання з сервером бота або не показав йому медіа (так деякі сайти відсікають сервери)."
    elif "перевищив час" in low:
        why = "Сайт відповідав надто довго."
    else:
        why = "Не вдалося отримати медіа з цього посилання."
    tech = re.sub(r"^(?:\w[\w-]*: )?(?:[\w-]+ · код \d+\. )?(?:ERROR: )?", "", raw.splitlines()[-1] if raw else "")[:220]
    tech = html_escape(tech)
    return why + (f"\n\n<i>Технічно: {tech}</i>" if tech and tech not in why else "")


async def video_probe(url, work):
    base = (*YT, *proxy_args(url), *cookie_args(url), "--dump-single-json", "--skip-download", "--flat-playlist", "--yes-playlist")
    extra = ()
    try:
        out = await run(*base, "--", url, cwd=work, timeout=100)
    except JobError as exc:
        host = (urlsplit(url).hostname or "").lower()
        if not (domain_matches(host, ("youtube.com", "youtu.be")) and BOT_CHECK.search(str(exc))):
            raise
        extra = YT_RETRY
        out = await run(*base, *extra, "--", url, cwd=work, timeout=100)
    data = json.loads(out)
    if extra:
        data["_yt_args"] = list(extra)
    allow_extractor(data)
    if isinstance(data.get("webpage_url"), str):
        await check_url(data["webpage_url"])
    if "entries" in data:
        return [
            Item("yt", url, {**row, "_yt_args": data.get("_yt_args", [])}, position)
            for position, row in enumerate(data["entries"] or [], 1)
            if isinstance(row, dict)
        ]
    if data.get("formats") or data.get("url"):
        return [Item("yt", url, data)]
    return []


async def gallery_probe(url, work):
    data = json.loads(await run(
        "gallery-dl", "--config-ignore", *cookie_args(url, "--cookies"), "--dump-json",
        *proxy_args(url), "--", url, cwd=work, timeout=90,
    ))
    result = []
    shared = {}
    for row in data:
        if not isinstance(row, list) or len(row) != 3 or not isinstance(row[2], dict):
            continue
        kind, address, info = row
        if kind == 2:
            shared = info
        elif kind == 3 and isinstance(address, str) and address.startswith(("https://", "http://")):
            result.append(Item("direct", address, {**shared, **info}))
    return result


async def page_probe(url, work):
    parsed = urlsplit(url)
    telegram = (
        domain_matches((parsed.hostname or "").lower(), ("t.me", "telegram.me"))
        and re.fullmatch(r"/(?:s/)?[A-Za-z0-9_]+/\d+/?", parsed.path)
    )
    if telegram:
        channel, number = parsed.path.strip("/").split("/")[-2:]
        url = f"https://t.me/{channel}/{number}?embed=1"
    async with open_url(url) as response:
        final_url = str(response.url)
        if media_extension(response):
            name = unquote(Path(urlsplit(final_url).path).name)
            return [Item("direct", final_url, {"title": name or "Медіа"})]
        content = bytearray()
        async for chunk in response.aiter_bytes(65536):
            content.extend(chunk)
            if len(content) >= 2_000_000:
                break
    document = read_html(bytes(content))
    meta = {
        (tag.get("property") or tag.get("name") or "").lower(): tag.get("content")
        for tag in document.xpath("//meta[@content]")
    }
    info = {
        "title": meta.get("og:title") or document.xpath("string(//title)"),
        "description": meta.get("og:description") or meta.get("description"),
        "author": meta.get("author"),
        "author_url": meta.get("article:author"),
    }
    links = []
    if telegram:
        texts = document.find_class("tgme_widget_message_text")
        owners = document.find_class("tgme_widget_message_owner_name")
        if texts:
            info["description"] = texts[0].text_content().strip()
        if owners:
            info["author"] = owners[0].text_content().strip()
            if owners[0].get("href"):
                info["author_url"] = urljoin(final_url, owners[0].get("href"))
        nodes = document.xpath("//video[@src] | //video/source[@src]")
        for node in nodes + document.find_class("tgme_widget_message_photo_wrap"):
            address = node.get("src")
            if not address:
                match = re.search(r"url\(['\"]?([^'\")]+)", node.get("style", ""))
                address = match[1] if match else None
            if address:
                links.append(urljoin(final_url, address))
    video_url = meta.get("og:video:secure_url") or meta.get("og:video")
    if video_url and Path(urlsplit(video_url).path).suffix.lower() in MEDIA:
        links.append(urljoin(final_url, video_url))
    if links:
        return [Item("direct", link, info) for link in dict.fromkeys(links)]
    image_url = meta.get("og:image:secure_url") or meta.get("og:image")
    if image_url:
        return [Item("direct", urljoin(final_url, image_url), {**info, "preview": True})]
    return [Item("text", final_url, info)] if post_text(info) else []


BROWSER = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
           "Version/18.0 Mobile/15E148 Safari/604.1")


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _best_media(post):
    """Відео (найкраща версія) або фото (найбільше) з об'єкта допису Threads/Instagram."""
    videos = post.get("video_versions") or []
    if videos and isinstance(videos[0], dict) and videos[0].get("url"):
        return videos[0]["url"], ".mp4"
    candidates = ((post.get("image_versions2") or {}).get("candidates")) or []
    candidates = [c for c in candidates if isinstance(c, dict) and c.get("url")]
    if candidates:
        return max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))["url"], ".jpg"
    return None


async def _grab(url, limit=8_000_000):
    async with open_url(url, agent=BROWSER) as response:
        content = bytearray()
        async for chunk in response.aiter_bytes(65536):
            content.extend(chunk)
            if len(content) >= limit:
                break
        return str(response.url), bytes(content).decode("utf-8", "replace")


POST_URL = re.compile(r"https?:(?:\\?/){2}(?:www\.)?threads\.(?:net|com)(?:\\?/)@([\w.]+)(?:\\?/)post(?:\\?/)([\w-]+)")


def _threads_post(text, code):
    """Об'єкт допису з JSON усередині сторінки (там, де він є)."""
    for blob in re.findall(r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", text, re.S):
        if (code and code not in blob) or ("video_versions" not in blob and "image_versions2" not in blob):
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for node in _walk(data):
            if node.get("code") and (code is None or node.get("code") == code) and (
                    "image_versions2" in node or "video_versions" in node or "carousel_media" in node):
                return node
    return None


def _embed_media(text, base):
    """Сторінка /embed: звичайний HTML з <video> і великими фото (без аватарок)."""
    doc = read_html(text.encode("utf-8"))
    links = []
    for node in doc.xpath("//video[@src] | //video/source[@src]"):
        links.append((urljoin(base, node.get("src")), ".mp4"))
    if not links:
        for node in doc.xpath("//img[@src]"):
            src = node.get("src") or ""
            if re.search(r"cdninstagram|fbcdn", src) and not re.search(r"s150x150|_s\d{2,3}x\d{2,3}|profile", src) \
                    and "Avatar" not in (node.get("class") or "") and "profile" not in (node.get("alt") or "").lower():
                links.append((urljoin(base, src), ".jpg"))
    for address in re.findall(r"\"video_url\":\"([^\"]+)\"", text):
        try:
            links.append((json.loads(f'"{address}"'), ".mp4"))
        except ValueError:
            pass
    return list(dict.fromkeys(links))


async def threads_probe(url, work):
    """Threads: відео/фото з JSON сторінки, а якщо сервер його не отримав — зі сторінки вбудовування /embed."""
    final, text = await _grab(url)  # /share/… перенаправляє на /@автор/post/КОД
    match = POST_URL.search(final) or POST_URL.search(url) or POST_URL.search(text)
    user, code = (match[1], match[2]) if match else ("", None)
    meta = {(t.get("property") or t.get("name") or "").lower(): t.get("content")
            for t in read_html(text.encode("utf-8")).xpath("//meta[@content]")}
    info = {"title": (meta.get("og:title") or f"Threads {code or ''}")[:80],
            "description": meta.get("og:description") or "",
            "author": user, "author_url": f"https://www.threads.com/@{user}" if user else ""}
    post = _threads_post(text, code)
    if post:
        caption = post.get("caption") or {}
        if isinstance(caption, dict) and caption.get("text"):
            info["description"] = caption["text"]
        items = []
        for part in post.get("carousel_media") or [post]:
            found = _best_media(part) if isinstance(part, dict) else None
            if found:
                items.append(Item("direct", found[0], dict(info)))
        if items:
            return items
    embed_note = "немає адреси допису"
    if code and user:
        try:
            efinal, etext = await _grab(f"https://www.threads.com/@{user}/post/{code}/embed", 3_000_000)
            links = _embed_media(etext, efinal)
            if not links:
                epost = _threads_post(etext, code)
                if epost:
                    links = [f for f in (_best_media(p) for p in (epost.get("carousel_media") or [epost])
                                          if isinstance(p, dict)) if f]
            if links:
                return [Item("direct", address, dict(info)) for address, _ in links]
            embed_note = f"embed {len(etext) // 1024} КБ без медіа"
        except (JobError, httpx.HTTPError) as exc:
            embed_note = f"embed: {str(exc)[:80]}"
    raise JobError(f"threads · сторінка {len(text) // 1024} КБ, допис {code or '?'}, {embed_note}")


def _tumblr_media(text):
    """Відео (va.media.tumblr.com) або фото найбільшого розміру з HTML/JSON сторінки Tumblr, без аватарок."""
    text = text.replace("\\/", "/")
    videos = list(dict.fromkeys(re.findall(r"https://(?:va|ve)\.media\.tumblr\.com/[\w./-]+?\.mp4", text)))
    if videos:
        best = {}
        for v in videos:  # однакові відео різної якості: *_480.mp4, *_720.mp4 — беремо більшу
            key = re.sub(r"_\d{3,4}\.mp4$", "", v)
            size = int((re.search(r"_(\d{3,4})\.mp4$", v) or [0, 9999])[1])
            if key not in best or size > best[key][0]:
                best[key] = (size, v)
        return [(v, ".mp4") for _, v in best.values()]
    best = {}
    for img in re.findall(r"https://\d+\.media\.tumblr\.com/[\w./-]+?\.(?:jpe?g|png|gif|webp|pnj|gifv)", text):
        if "avatar" in img:
            continue
        key = img.split("/")[3]
        m = re.search(r"/s(\d+)x(\d+)", img)
        size = int(m[1]) * int(m[2]) if m else 0
        if key not in best or size > best[key][0]:
            best[key] = (size, img)
    return [(v, ".jpg") for _, v in best.values()]


async def tumblr_probe(url, work):
    """Tumblr часто обриває з'єднання завантажувачам — читаємо сторінку як браузер (спершу блог-піддомен, потім www)."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    blog = post_id = None
    if host in ("www.tumblr.com", "tumblr.com") and len(parts) >= 2 and parts[1].isdigit():
        blog, post_id = parts[0], parts[1]
    elif host.endswith(".tumblr.com") and len(parts) >= 2 and parts[0] == "post":
        blog, post_id = host.split(".")[0], parts[1]
    candidates = [url]
    if blog and post_id:
        candidates = [f"https://{blog}.tumblr.com/post/{post_id}", f"https://www.tumblr.com/{blog}/{post_id}", url]
    notes = []
    for address in dict.fromkeys(candidates):
        try:
            final, text = await _grab(address, 6_000_000)
        except (JobError, httpx.HTTPError) as exc:
            notes.append(f"{urlsplit(address).hostname}: {str(exc)[:60] or type(exc).__name__}")
            continue
        links = _tumblr_media(text)
        if links:
            meta = {(t.get("property") or t.get("name") or "").lower(): t.get("content")
                    for t in read_html(text.encode("utf-8")).xpath("//meta[@content]")}
            info = {"title": (meta.get("og:title") or f"Tumblr {post_id or ''}")[:80],
                    "description": meta.get("og:description") or "", "author": blog or "",
                    "author_url": f"https://{blog}.tumblr.com" if blog else ""}
            return [Item("direct", link, dict(info)) for link, _ in links[:40]]
        notes.append(f"{urlsplit(address).hostname}: {len(text) // 1024} КБ без медіа")
    raise JobError("tumblr · " + "; ".join(notes))


SHARE = re.compile(r"^https?://(?:(?:www|m|web)\.)?(?:facebook\.com/(?:share|story\.php|watch/?\?)|fb\.watch/|pin\.it/)")  # короткі TikTok yt-dlp розгортає сам


def _real_target(final):
    """Сторінка входу часто несе справжню адресу в ?next=…"""
    from urllib.parse import parse_qs
    query = parse_qs(urlsplit(final).query)
    for key in ("next", "u", "continue"):
        for value in query.get(key, []):
            if value.startswith(("https://", "http://")) and not SHARE.match(value):
                return value
    return None


async def _peek(url, agent):
    async with open_url(url, agent=agent) as response:
        final = str(response.url)
        content = bytearray()
        async for chunk in response.aiter_bytes(65536):
            content.extend(chunk)
            if len(content) >= 1_500_000:
                break
    return final, bytes(content)


async def resolve_share(url):
    """Короткі посилання «Поділитися» (facebook.com/share/…, fb.watch, vm.tiktok…) → справжня адреса допису,
    бо завантажувачі їх не розпізнають: перенаправлення, ?next= сторінки входу, og:url / canonical / meta refresh."""
    if not SHARE.match(url):
        return url
    for agent in (BROWSER, "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"):
        try:
            final, raw = await _peek(url, agent)
        except (JobError, httpx.HTTPError, OSError):
            continue
        if not SHARE.match(final) and not re.search(r"/login|/checkpoint|login\.php", final):
            return final
        target = _real_target(final)
        if target:
            return target
        try:
            doc = read_html(raw)
        except Exception:  # noqa: BLE001
            continue
        found = (doc.xpath("string(//meta[@property='og:url']/@content)")
                 or doc.xpath("string(//link[@rel='canonical']/@href)"))
        refresh = doc.xpath("string(//meta[translate(@http-equiv,'REFRESH','refresh')='refresh']/@content)")
        if not found and "url=" in refresh.lower():
            found = refresh.split("=", 1)[1].strip(" '\"")
        if found:
            found = urljoin(final, found)
            if not SHARE.match(found) and not re.search(r"/login|login\.php", found):
                return found
    return url


STORY = re.compile(r"^https?://(?:www\.|m\.)?instagram\.com/stories/([A-Za-z0-9_.]+)/?(\d+)?")
APIFY_ACTOR = "seemuapps~instagram-story-downloader"


async def story_probe(url, work):
    """Сторіс Instagram: спершу як звичайно (yt-dlp, з cookies, якщо є), а якщо Instagram вимагає входу —
    через сервіс Apify, який має власні акаунти (як інші боти-завантажувачі). Ключ — APIFY_TOKEN у Railway."""
    match = STORY.match(url)
    user, story_id = match[1], match[2]
    try:
        items = await video_probe(url, work)
        if items:
            return items
    except JobError as exc:
        first = str(exc)
    else:
        first = ""
    if not apify_token() and first:
        raise JobError("Instagram показує сторіс лише після входу. Щоб бот їх качав без вашого акаунта, як інші боти, "
                       "додайте в Railway змінну APIFY_TOKEN (apify.com → Settings → API &amp; Integrations; безкоштовних "
                       "$5 на місяць вистачає приблизно на 300 сторіс).\n\n🔎 " + apify_hint() + f"\n\n<i>Технічно: {html_escape(first[-200:])}</i>")
    rows = await apify_run(APIFY_ACTOR, {"usernames": [user], "saveFiles": False, "maxItems": 0}, timeout=180)
    if story_id:
        exact = [row for row in rows if str(row.get("storyId") or "").split("_")[0] == story_id]
        rows = exact or rows
    items = []
    for row in rows:
        link = row.get("videoUrl") if row.get("mediaType") == "video" else None
        link = link or row.get("videoUrl") or row.get("imageUrl")
        if link:
            items.append(Item("direct", link, {"title": f"Сторіс @{user}", "author": user,
                                               "author_url": f"https://www.instagram.com/{user}/",
                                               "description": " ".join(row.get("links") or [])}))
    if not items:
        raise JobError(f"У @{user} зараз немає доступних сторіс (минули 24 години або акаунт приватний).")
    return items


def apify_token():
    """Ключ Apify: APIFY_TOKEN, а якщо змінну назвали трохи інакше (APIFY_API_TOKEN, з пробілом, малими літерами) —
    будь-яка змінна з «apify» у назві або значення, схоже на ключ (apify_api_…)."""
    exact = os.environ.get("APIFY_TOKEN", "").strip().strip('"\'')
    if exact:
        return exact
    if COOKIES_DIR:  # ключ, вставлений у боті (🛠 Інструменти → 🔑), лежить поруч із cookies
        key = Path(COOKIES_DIR).parent / "apify.key"
        if key.is_file():
            value = key.read_text("utf-8").strip()
            if value:
                return value
    for name, value in os.environ.items():
        value = (value or "").strip().strip('"\'')
        if value and ("APIFY" in name.upper().replace(" ", "") or value.startswith("apify_api_")):
            return value
    return ""


def server_name():
    """Де фізично працює бот: Railway (який сервіс) чи інший сервер кластера."""
    import socket
    svc = os.environ.get("RAILWAY_SERVICE_NAME")
    if svc:
        return f"Railway · сервіс «{svc}» · {os.environ.get('RAILWAY_ENVIRONMENT_NAME') or os.environ.get('RAILWAY_ENVIRONMENT') or ''}".rstrip(" ·")
    return f"НЕ Railway · вузол «{os.environ.get('ORION_NODE_ID', 'primary')}» · {socket.gethostname()}"


def apify_hint():
    names = [n for n in os.environ if "APIFY" in n.upper()]
    where = server_name()
    if not where.startswith("Railway"):
        return (f"Зараз відповідає не Railway, а інший сервер кластера ({where}) — у нього свої змінні. "
                "Додайте APIFY_TOKEN і туди, або вставте ключ у боті кнопкою «🔑».")
    return ("Сервер бачить змінні з «APIFY»: " + ", ".join(f"«{n}»" for n in names) + " — але без значення."
            if names else "Сервер бота не бачить жодної змінної з «APIFY» — у Railway вона не застосована до цього сервісу "
            "(натисніть «Deploy» після додавання) або додана до іншого сервісу.")


FB_STORY = re.compile(r"^https?://(?:(?:www|m|web)\.)?facebook\.com/stories/")
FB_STORY_ACTOR = "codenest~facebook-story-downloader"


async def apify_run(actor, payload, timeout=240):
    token = apify_token()
    if not token:
        raise JobError("Facebook та Instagram показують сторіс лише після входу. Щоб бот їх качав без вашого акаунта, як інші боти, "
                       "додайте в Railway змінну APIFY_TOKEN (apify.com → Settings → API &amp; Integrations).\n\n🔎 " + apify_hint())
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        r = await client.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                              params={"token": token}, json=payload)
    if r.status_code in (401, 403):
        raise JobError("APIFY_TOKEN не підходить — перевірте ключ у Railway.")
    if r.status_code == 402:
        raise JobError("На Apify закінчився баланс цього місяця.")
    if r.status_code >= 400:
        raise JobError(f"Apify відповів помилкою {r.status_code}: {r.text[:150]}")
    data = r.json()
    return [row for row in (data if isinstance(data, list) else []) if isinstance(row, dict)]


async def fb_story_probe(url, work):
    """Сторіс Facebook: yt-dlp їх не знає, тож — через сервіс Apify (APIFY_TOKEN), як інші боти-завантажувачі."""
    rows = await apify_run(FB_STORY_ACTOR, {"story_urls": [{"url": url}], "saveToKeyValueStore": False})
    items = []
    for row in rows:
        if row.get("status") == "failed":
            continue
        link = row.get("download_url") or row.get("kv_store_url")
        if not link:
            continue
        who = row.get("uploader_name") or ""
        info = {"title": f"Сторіс {who}".strip(), "author": who}
        if row.get("media_type") == "video" and row.get("audio_download_url"):
            info["_audio"] = row["audio_download_url"]  # відео й звук окремо — склеїмо
        items.append(Item("direct", link, info))
    if not items:
        raise JobError("Сторіс не знайдено: минули 24 години, вона приватна або посилання неповне.")
    return items


async def inspect_link(url, work):
    await check_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname.lower().rstrip(".")
    if (
        Path(parsed.path).suffix.lower() in MEDIA
        or domain_matches(host, ("discordapp.com", "discordapp.net", "t.me", "telegram.me"))
    ):
        probes = (page_probe, video_probe, gallery_probe)
    elif STORY.match(url):
        probes = (story_probe,)
    elif FB_STORY.match(url):
        probes = (fb_story_probe,)
    elif domain_matches(host, ("tiktok.com",)):
        probes = (video_probe, gallery_probe, page_probe)  # відео — yt-dlp; фото-каруселі — gallery-dl
    elif domain_matches(host, ("tumblr.com",)):
        probes = (tumblr_probe, gallery_probe, video_probe, page_probe)
    elif domain_matches(host, ("threads.net", "threads.com")):
        probes = (threads_probe, gallery_probe, video_probe, page_probe)
    elif domain_matches(host, GALLERY):
        probes = (gallery_probe, video_probe, page_probe)
    else:
        probes = (video_probe, gallery_probe, page_probe)

    video_site = domain_matches(host, VIDEO_HOSTS)
    errors = []
    for probe in probes:
        try:
            items = await probe(url, work)
        except URLDenied:
            raise
        except (JobError, httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            errors.append((probe.__name__, str(exc) or type(exc).__name__))
            continue
        if items and (video_site or domain_matches(host, GALLERY)) and probe is page_probe and all(
            it.engine == "text" or it.info.get("preview") for it in items
        ):
            continue  # лише заголовок і обкладинка сторінки — для відеосайту це не результат
        if items:
            return items
    if domain_matches(host, ("threads.net", "threads.com")):
        detail = next((e[1] for e in errors if e[0] == "threads_probe"), "")
        raise JobError("Threads не віддав серверу саме відео, лише обкладинку."
                       + ("" if cookie_file(url) else " Якщо допис відкривається лише після входу — 🍪 додайте cookies threads у «Входи на сайти».")
                       + (f"\n\n<i>Технічно: {html_escape(detail)}</i>" if detail else ""))
    if SHARE.match(url) and any("unsupported url" in e[1].lower() for e in errors):
        raise JobError("Сайт не розкрив серверу це коротке посилання «Поділитися». Спробуйте інше посилання на той самий допис: "
                       "у Facebook — «Поділитися» → «Копіювати посилання» з самого відео (reel/… або watch/?v=…).")
    # Найзмістовніша причина — від yt-dlp (він знає сайти), далі gallery-dl, далі сторінка.
    order = {"story_probe": 0, "fb_story_probe": 0, "threads_probe": 0, "tumblr_probe": 0, "video_probe": 1, "gallery_probe": 2, "page_probe": 3}
    errors.sort(key=lambda e: order.get(e[0], 3))
    if errors and errors[0][0] in ("story_probe", "fb_story_probe"):
        raise JobError(errors[0][1])  # уже зрозуміле пояснення
    raise JobError(explain(errors[0][1] if errors else "Джерело не повернуло медіа або тексту."))


async def fetch_direct(item, source, options, work, progress=None):
    if options.audio != "auto" or options.subs != "off":
        raise JobError("Прямий файл має власну доріжку. Вибери /audio auto і /subs off.")
    async with open_url(item.url, referer=source) as response:
        extension = media_extension(response)
        if not extension:
            raise JobError("За адресою відкрилася сторінка замість медіафайла.")
        candidate = unquote(Path(urlsplit(str(response.url)).path).name)
        stem = Path(safe_name(candidate or item.title, "media")).stem
        path = work / (stem + extension)
        total = 0
        size = int(response.headers.get("content-length") or 0)
        started = asyncio.get_running_loop().time()
        with path.open("wb") as output:
            async for chunk in response.aiter_bytes(65536):
                total += len(chunk)
                if progress:
                    spent = max(asyncio.get_running_loop().time() - started, 0.001)
                    speed = total / spent
                    await progress(total, size, speed, (size - total) / speed if size and speed else 0)
                if total > WEB_LIMIT:
                    raise JobError("Розмір перевищує ліміт завантаження.")
                output.write(chunk)
        if not total:
            raise JobError("Джерело повернуло порожній файл.")
    audio = item.info.get("_audio")
    if audio:  # окрема звукова доріжка (сторіс Facebook) — склеюємо без перекодування
        sound = work / "audio.m4a"
        try:
            async with open_url(audio, referer=source) as response:
                with sound.open("wb") as output:
                    async for chunk in response.aiter_bytes(65536):
                        output.write(chunk)
            joined = work / (path.stem + "-av.mp4")
            await run(*FF, "-i", path, "-i", sound, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                      "-movflags", "+faststart", joined, timeout=300)
            path.unlink(missing_ok=True)
            path = joined.rename(path.with_suffix(".mp4"))
        except (JobError, httpx.HTTPError, OSError):
            pass  # без звуку краще, ніж нічого
    return path, [], item.info


PROGRESS = ("--newline", "--progress-template",
            "download:ORP %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s "
            "%(progress.speed)s %(progress.eta)s")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def fetch(item, source, options, work, progress=None):
    """progress(done_bytes, total_bytes, speed_bytes_s, eta_s) — для живих відсотків."""
    if item.engine == "text":
        return None, [], item.info
    if item.engine == "direct":
        return await fetch_direct(item, source, options, work, progress)

    allow_extractor(item.info)
    entry_url = item.info.get("webpage_url") or item.info.get("url")
    if isinstance(entry_url, str) and entry_url.startswith(("https://", "http://")):
        await check_url(entry_url)
    height = f"[height<={options.height}]" if options.height else ""
    language = f"[language^={options.audio}]" if options.audio != "auto" else ""
    if options.mode == "audio":
        selector = f"ba{language}/b{language}"
        convert = ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    else:
        selector = f"bv{height}+ba{language}/b{height}{language}"
        if not language:
            selector += f"/bv{height}"
        if height:  # у Facebook та ін. формати часто без висоти — тоді фільтр відсіює все; беремо найкраще, що є
            selector += f"/bv*+ba{language}/b{language}" + ("" if language else "/bv*")
        convert = []
    entry = (
        ["--yes-playlist", "--playlist-items", str(item.index)]
        if item.index is not None else ["--no-playlist"]
    )
    subtitles = (
        ["--write-subs", "--write-auto-subs", "--sub-langs", re.escape(options.subs) + ".*"]
        if options.subs != "off" else []
    )
    live = []
    if item.info.get("is_live") or item.info.get("live_status") == "is_live":  # 🔴 ефір — записуємо лише перші хвилини
        live = ["--downloader", "ffmpeg", "--downloader-args", f"ffmpeg:-t {int(options.live)}"]
    def args(extra):
        return (*YT, *proxy_args(source), *cookie_args(source), *extra, *entry, *convert, *subtitles, *live, "-f", selector,
                "--max-filesize", str(WEB_LIMIT), "--write-info-json",
                "--no-write-playlist-metafiles", "-o", "media.%(ext)s", "--", source)
    limit = 2400 + (options.live if live else 0)
    extra = tuple(item.info.get("_yt_args") or ())
    async def on_line(line):
        if progress and line.startswith("ORP "):
            parts = line.split()[1:]
            done, total, estimate, speed, eta = (_num(v) for v in (parts + ["0"] * 5)[:5])
            await progress(done, total or estimate, speed, eta)

    async def go(extra_args):
        if progress:
            plain = tuple(a for a in args(extra_args) if a != "--no-progress")
            cut = plain.index("--")
            await run_stream(*plain[:cut], *PROGRESS, *plain[cut:], on_line=on_line, cwd=work, timeout=limit)
        else:
            await run(*args(extra_args), cwd=work, timeout=limit)

    try:
        await go(extra)
    except JobError as exc:
        if extra or not (BOT_CHECK.search(str(exc)) or "403" in str(exc)) or not domain_matches(
                (urlsplit(source).hostname or "").lower(), ("youtube.com", "youtu.be")):
            raise
        for leftover in work.iterdir():
            if leftover.is_file():
                leftover.unlink()
        await go(YT_RETRY)
    outputs = [path for path in work.iterdir() if path.is_file()]
    files = [
        path for path in outputs
        if path.suffix.lower() in MEDIA and path.stat().st_size > 0
    ]
    if len(files) != 1:
        raise JobError("Джерело не віддало один файл у вибраній якості або мові.")
    if files[0].stat().st_size > WEB_LIMIT:
        raise JobError("Розмір перевищує ліміт завантаження.")
    info_path = work / "media.info.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else item.info
    allow_extractor(info)
    stem = Path(safe_name(ua_title(info.get("title") or item.title)[:70], "media")).stem
    path = files[0].rename(work / (stem + files[0].suffix.lower()))
    tracks = [
        track.rename(work / (stem + track.name.removeprefix("media")))
        for track in outputs if track.suffix.lower() in SUBTITLES
    ]
    return path, tracks, info


def parse_cuts(value):
    segments = []
    for section in value.split(","):
        match = re.fullmatch(
            r"\s*(\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?)\s*[-–—]\s*"
            r"(\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?)\s*", section
        )
        if not match:
            raise JobError("Приклад · /cuts 00:12-00:30,01:05-01:25")
        times = []
        for clock in match.groups():
            parts = list(map(float, clock.split(":")))
            if any(part >= 60 for part in parts[1:]):
                raise JobError("У форматі з двокрапкою останні значення мають бути до 60.")
            times.append(sum(part * 60**i for i, part in enumerate(reversed(parts))))
        start, end = times
        if end <= start or end > 1_000_000:
            raise JobError("Перевір початок і кінець фрагмента.")
        segments.append((start, end))
    return tuple(segments)


async def probe_media(path):
    return json.loads(await run(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_format", "-show_streams", "-of", "json", path, timeout=30,
    ))


async def render(path, options, work, direct):
    image = (mimetypes.guess_type(path.name)[0] or "").startswith("image/")
    if options.mode in ("gif", "mute") and not image:
        return await render_special(path, options, work)
    needs_scale = direct and options.height and not image and options.mode == "video"
    needs_audio = (direct or path.suffix.lower() != ".mp3") and options.mode == "audio" and not image
    if not (options.cuts or needs_scale or needs_audio):
        return [path]
    data = await probe_media(path)
    video = next((
        stream for stream in data.get("streams", [])
        if stream["codec_type"] == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ), None)
    audio = any(stream["codec_type"] == "audio" for stream in data.get("streams", []))
    video_output = video is not None and options.mode != "audio"
    if not video_output and not audio:
        raise JobError("У цьому файлі немає доріжки для вибраної операції.")
    if not options.cuts and not needs_audio:
        if not video_output or (video.get("height") or 0) <= options.height:
            return [path]
    duration = data.get("format", {}).get("duration")
    if options.cuts and duration not in {None, "N/A"}:
        if float(duration) + 0.25 < max(end for _, end in options.cuts):
            raise JobError("Таймкод виходить за тривалість файлу.")
    ext = ".mp4" if video_output else ".mp3"
    maps = ["-map", "0:V:0", "-map", "0:a:0?"] if video_output else ["-map", "0:a:0"]
    codec = ["-c:a", "aac", "-b:a", "160k"] if video_output else ["-c:a", "libmp3lame", "-b:a", "192k"]
    if video_output:
        height = f"min(ih,{options.height})" if needs_scale else "ih"
        codec += [
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-vf", f"scale=-2:'trunc({height}/2)*2'", "-movflags", "+faststart",
        ]
    clips = work / "segments"
    clips.mkdir()
    chunks = []
    for number, (start, end) in enumerate(options.cuts or ((0, None),), 1):
        output = clips / f"part{number:03d}{ext}"
        seek = ["-ss", str(start)] if start else []
        length = ["-t", str(end - start)] if end is not None else []
        await run(
            *FF, *seek, "-i", path, *length, *maps, "-map_metadata", "0",
            *codec, output, timeout=450,
        )
        chunks.append(output)
    if options.join and len(chunks) > 1:
        listing = clips / "segments.txt"
        listing.write_text("".join(f"file '{part.name}'\n" for part in chunks), encoding="utf-8")
        joined = work / f"{path.stem} — фрагменти{ext}"
        await run(
            *FF, "-f", "concat", "-safe", "1", "-i", listing, "-i", path,
            "-map", "0", "-map_metadata", "1", "-c", "copy", joined, timeout=120,
        )
        return [joined]
    return [
        part.rename(work / f"{path.stem} — {index:02d}{ext}")
        for index, part in enumerate(chunks, 1)
    ]


async def render_special(path, options, work):
    """🎞 GIF (до 15 с, 480 px, 12 кадр/с) або 🔇 відео без звуку; таймкоди теж враховуються."""
    cuts = options.cuts or ((0, None),)
    out = []
    for number, (start, end) in enumerate(cuts, 1):
        seek = ["-ss", str(start)] if start else []
        if options.mode == "gif":
            length = ["-t", str(min(15, (end - start) if end is not None else 15))]
            dest = work / f"{path.stem}{'' if len(cuts) == 1 else f' — {number:02d}'}.gif"
            await run(*FF, *seek, *length, "-i", path, "-vf",
                      "fps=12,scale='min(480,iw)':-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
                      "-loop", "0", dest, timeout=300)
        else:
            length = ["-t", str(end - start)] if end is not None else []
            dest = work / f"{path.stem} — без звуку{'' if len(cuts) == 1 else f' {number:02d}'}{path.suffix if path.suffix.lower() in ('.mp4', '.mov', '.mkv', '.webm') else '.mp4'}"
            await run(*FF, *seek, "-i", path, *length, "-map", "0:V:0", "-an", "-c", "copy" if not cuts[0][0] and cuts[0][1] is None else "libx264",
                      *([] if not cuts[0][0] and cuts[0][1] is None else ["-preset", "fast", "-pix_fmt", "yuv420p"]), dest, timeout=450)
        out.append(dest)
    return out
