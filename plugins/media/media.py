"""📥 Пошук і завантаження відкритих медіа (з MediaBot): yt-dlp, gallery-dl, прямі файли, дописи Telegram, Open Graph.

Додано для OrionAI: входи через cookies (для Instagram, Facebook, TikTok…), режими MP3 / GIF / без звуку,
короткий запис ефіру. Перевірка адрес (лише публічні HTTP/HTTPS) — як в оригіналі.
"""
import asyncio
import logging
import datetime as dt
import time
import os
import ipaddress
import json
import mimetypes
import shutil
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

log = logging.getLogger("orion.media")
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
DOCS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv",
        ".epub", ".fb2", ".djvu", ".zip", ".7z", ".rar", ".json", ".xml", ".apk", ".svg"}
DOC_MIME = {"application/pdf": ".pdf", "application/msword": ".doc", "application/vnd.ms-excel": ".xls",
            "application/vnd.ms-powerpoint": ".ppt", "application/zip": ".zip", "application/x-7z-compressed": ".7z",
            "application/vnd.rar": ".rar", "application/x-rar-compressed": ".rar", "application/epub+zip": ".epub",
            "application/rtf": ".rtf", "text/csv": ".csv", "text/plain": ".txt",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.oasis.opendocument.text": ".odt", "application/vnd.oasis.opendocument.spreadsheet": ".ods",
            "application/vnd.oasis.opendocument.presentation": ".odp"}
# Документ → у що можна перетворити (LibreOffice)
DOC_TO = {".doc": ("pdf", "docx", "odt"), ".docx": ("pdf", "odt", "txt"), ".odt": ("pdf", "docx"), ".rtf": ("pdf", "docx"),
          ".xls": ("xlsx", "pdf", "ods", "csv"), ".xlsx": ("pdf", "ods", "csv"), ".ods": ("xlsx", "pdf", "csv"), ".csv": ("xlsx",),
          ".ppt": ("pptx", "pdf", "odp"), ".pptx": ("pdf", "odp"), ".odp": ("pptx", "pdf"), ".txt": ("pdf", "docx")}
SUBTITLES = {".vtt", ".srt", ".ass", ".ttml", ".srv3", ".json3", ".dfxp"}
YT = (
    "yt-dlp", "--ignore-config", "--no-plugin-dirs", "--no-cache-dir",
    "--no-progress", "--no-warnings", "--socket-timeout", "15",
    "--retries", "1", "--js-runtimes", "node",
)
# ---- YouTube: PO Token (bgutil-ytdlp-pot-provider). Для YouTube дозволяємо плагіни yt-dlp (провайдер стоїть
# через pip у site-packages); для решти сайтів плагіни, як і раніше, вимкнені. Скрипт генерації (Node) лежить у
# /opt/bgutil/server (Dockerfile) і запускається лише на час запиту — постійного сервера немає (ліміт RAM).
POT_HOME = os.environ.get("BGUTIL_SERVER_HOME", "/opt/bgutil/server")
YT_HEAVY = asyncio.Semaphore(int(os.environ.get("MEDIA_HEAVY_SLOTS", "1") or 1))  # YouTube-запити по одному: економимо RAM


import contextvars as _cv

NOTE = _cv.ContextVar("media_note", default=None)  # async fn(text) — показати поточний крок пошуку


async def note(text):
    fn = NOTE.get()
    if fn:
        try:
            await fn(text)
        except Exception:  # noqa: BLE001
            pass


SITE_NAMES = (("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("tiktok.com", "TikTok"), ("instagram.com", "Instagram"),
              ("facebook.com", "Facebook"), ("fb.watch", "Facebook"), ("threads.net", "Threads"), ("threads.com", "Threads"),
              ("x.com", "X"), ("twitter.com", "X"), ("reddit.com", "Reddit"), ("vimeo.com", "Vimeo"), ("tumblr.com", "Tumblr"),
              ("t.me", "Telegram"), ("soundcloud.com", "SoundCloud"), ("pinterest.", "Pinterest"), ("bsky.app", "Bluesky"),
              ("twitch.tv", "Twitch"), ("dailymotion.com", "Dailymotion"), ("vk.com", "VK"), ("linkedin.com", "LinkedIn"))


def site_name(url):
    host = (urlsplit(str(url)).hostname or "").lower()
    for key, name in SITE_NAMES:
        if key in host:
            return name
    return host.removeprefix("www.") or "сайт"


def describe_source(url):
    """«Instagram · сторіс @user», «TikTok · @user», «YouTube» — звідки саме качаємо."""
    url = str(url)
    site = site_name(url)
    path = urlsplit(url).path
    m = re.search(r"/stories/(?:highlights/)?([A-Za-z0-9_.]+)", path)
    if m and m[1] not in ("highlights",):
        return f"{site} · сторіс {'@' if site != 'Facebook' else ''}{m[1]}"
    m = re.search(r"/@([A-Za-z0-9_.-]+)", path)
    if m:
        return f"{site} · @{m[1]}"
    m = re.match(r"/([A-Za-z0-9_.]{3,})/(?:p|reel|reels|status|post|video|videos)/", path)
    if m:
        return f"{site} · @{m[1]}" if site != "Facebook" else f"{site} · {m[1]}"
    if site in ("Telegram",):
        part = path.strip("/").split("/")[0]
        if part and part not in ("s", "c"):
            return f"Telegram · @{part}"
    return site


def is_story(url):
    url = str(url)
    return bool(STORY.match(url) or FB_STORY.match(url) or re.search(r"tiktok\.com/.*/story/|/stories/", url))


def is_youtube(url):
    url = str(url or "")
    if url.startswith(("ytsearch", "ytdl")):
        return True
    return domain_matches((urlsplit(url).hostname or "").lower(), ("youtube.com", "youtu.be"))


def pot_ready():
    return Path(POT_HOME, "build", "generate_once.js").is_file()


def yt_base(url):
    """Базові аргументи yt-dlp: для YouTube — з провайдером PO Token, для інших — без плагінів."""
    if not is_youtube(url):
        return YT
    base = tuple(a for a in YT if a != "--no-plugin-dirs")
    if pot_ready():
        base += ("--extractor-args", f"youtubepot-bgutilscript:server_home={POT_HOME}")
    return base


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
    abr: int = 0       # бітрейт звуку, кбіт/с (0 — 192 для MP3; не вище, ніж в оригіналі)
    fmt: str = ""      # формат файлу: mp4 mov mkv webm · mp3 m4a opus wav flac orig · jpg png webp ("" — як зазвичай)


AUDIO_FMTS = {"mp3": "libmp3lame", "m4a": "aac", "opus": "libopus", "wav": "pcm_s16le", "flac": "flac"}
VIDEO_FMTS = ("mp4", "mov", "mkv", "webm")
IMAGE_FMTS = ("jpg", "png", "webp")
BITRATES = (64, 96, 128, 160, 192, 256, 320)


def audio_choices(info):
    """Бітрейти, які має сенс пропонувати: лише до найкращого звуку в оригіналі (без «роздування»)."""
    fmts = [f for f in (info or {}).get("formats") or [] if isinstance(f, dict)]
    best = max((f.get("abr") or 0 for f in fmts if f.get("acodec") not in (None, "none")), default=0)
    if not best:
        return [], 0
    return [b for b in BITRATES if b <= best + 8], int(best)


def video_heights(info):
    """Висоти, у яких відео справді є на сайті (від найменшої до найбільшої)."""
    fmts = [f for f in (info or {}).get("formats") or [] if isinstance(f, dict)]
    hs = sorted({int(f["height"]) for f in fmts if f.get("height") and f.get("vcodec") not in (None, "none") and f["height"] >= 144})
    if not hs and (info or {}).get("height"):
        hs = [int(info["height"])]
    return hs


ID_PATTERNS = (
    (("youtube.com", "youtu.be"), r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})"),
    (("tiktok.com",), r"/(?:video|photo)/(\d{8,})"),
    (("instagram.com",), r"/(?:p|reel|reels|tv)/([\w-]{5,})"),
    (("vimeo.com",), r"vimeo\.com/(?:video/)?(\d{5,})"),
    (("x.com", "twitter.com"), r"/status/(\d{5,})"),
)


def url_media_id(url):
    """Ідентифікатор відео з самого посилання (YouTube, TikTok, Instagram, Vimeo, X) — щоб звірити з тим, що віддав сайт."""
    host = (urlsplit(url).hostname or "").lower() if url else ""
    for hosts, pat in ID_PATTERNS:
        if domain_matches(host, hosts):
            m = re.search(pat, url)
            return m.group(1) if m else ""
    return ""


def same_media(url, info):
    """False — сайт віддав не те відео, на яке вказує посилання (буває в інших завантажувачів)."""
    want = url_media_id(url)
    if not want or not info:
        return True
    got = {str(info.get(k) or "") for k in ("id", "display_id")} | {url_media_id(str(info.get("webpage_url") or ""))}
    if info.get("playlist_id") or info.get("_type") == "playlist":
        return True
    return want in got or not any(got - {""})


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
    import linktools
    if linktools.is_bad(host):
        raise URLDenied("Ресурси держави-агресора та її союзників не завантажую.")
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
    if mime in {"", "application/octet-stream"} and url_suffix in MEDIA | DOCS:
        return url_suffix
    if mime in DOC_MIME and mime != "text/plain":  # 📄 документи й архіви — теж завантажуємо як є
        return url_suffix if url_suffix in DOCS else DOC_MIME[mime]
    if mime == "text/plain" and url_suffix in (".txt", ".csv"):
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


def geotag(info):
    """📍 Геотег допису (Instagram і Facebook ставлять його над дописом, Threads і X — під ним; у даних він в одному полі)."""
    info = info or {}
    for key in ("location", "location_name", "place", "poi_name", "geo", "geotag"):
        v = info.get(key)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title") or v.get("full_name") or v.get("short_name") or ""
        if isinstance(v, str) and v.strip() and not v.strip().startswith(("http", "{")):
            return re.sub(r"\s+", " ", v).strip()[:80]
    poi = info.get("poi") or info.get("poi_info")
    if isinstance(poi, dict) and (poi.get("name") or poi.get("poi_name")):
        return str(poi.get("name") or poi.get("poi_name"))[:80]
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
        tried = "з токеном PO Token і кількома клієнтами YouTube" if pot_ready() else "з кількома клієнтами YouTube"
        why = (f"YouTube не пускає сервер бота: просить «підтвердити, що ви не бот» (спроба {tried} не допомогла). "
               "Так YouTube блокує IP-адреси серверів (Railway), посилання тут ні до чого. Інші сайти (TikTok, Instagram, X…) працюють."
               + ("\nПроксі MEDIA_PROXY теж заблоковано — потрібен домашній (residential) проксі." if os.environ.get("MEDIA_PROXY")
                  else "\n🔧 Що ще може допомогти: cookies YouTube у «🍪 Входи на сайти» або домашній проксі (змінна MEDIA_PROXY). Іноді блок знімається сам."))
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
    elif re.search(r"unexpected response|unable to extract|failed to parse|json", low):
        why = ("Сайт відповів серверу бота не так, як очікував завантажувач: найчастіше сайт змінив сторінку або відсікає сервери. "
               "Бот спробував запасні способи — не вийшло. Спробуйте ще раз пізніше.")
    else:
        why = "Не вдалося отримати медіа з цього посилання."
    tech = re.sub(r"^(?:\w[\w-]*: )?(?:[\w-]+ · код \d+\. )?(?:ERROR: )?", "", raw.splitlines()[-1] if raw else "")[:220]
    if tech:  # англійський технічний текст — у журнал для розбору, а людині — лише зрозуміле пояснення
        log.warning("медіа: %s", tech)
    return why


async def video_probe(url, work):
    base = (*yt_base(url), *proxy_args(url), *cookie_args(url), "--dump-single-json", "--skip-download", "--flat-playlist", "--yes-playlist")
    extra = ()
    yt = is_youtube(url)
    if yt:
        await YT_HEAVY.acquire()
    try:
        try:
            out = await run(*base, "--", url, cwd=work, timeout=150 if yt else 100)
        except JobError as exc:
            if not (yt and (BOT_CHECK.search(str(exc)) or "403" in str(exc))):
                raise
            extra = YT_RETRY
            out = await run(*base, *extra, "--", url, cwd=work, timeout=150)
    finally:
        if yt:
            YT_HEAVY.release()
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


ICON_RE = re.compile(r"(?i)static\.cdninstagram|rsrc\.php|/static/|\.svg(?:\?|$)|emoji|sprite|/images/|s\d{2,3}x\d{2,3}[/_]")
POST_IMG_RE = re.compile(r"https:(?:\\?/){2}[\w.-]*(?:cdninstagram\.com|fbcdn\.net)(?:\\?/)[^\"'\s<>]+?\.(?:jpe?g|webp|heic)[^\"'\s<>]*")
CDN_ANY_RE = re.compile(r"https:(?:\\?/){2}([\w.-]*(?:cdninstagram\.com|fbcdn\.net))(?:\\?/)[^\"'\s<>]*?(t\d+\.\d+-\d+)")


def _img_census(text):
    """Для журналу: які зображення з CDN є на сторінці (хост · клас → кількість) — щоб бачити, що відсіюється."""
    seen = {}
    for host, cls in CDN_ANY_RE.findall(text or ""):
        k = f"{host.split('.')[0][:14]}:{cls}"
        seen[k] = seen.get(k, 0) + 1
    return ", ".join(f"{k}×{v}" for k, v in sorted(seen.items(), key=lambda kv: -kv[1])[:8]) or "немає"


def _raw_post_images(text):
    """Усі фото допису з сирого тексту сторінки (запасний шлях для каруселей, коли JSON допису неповний):
    лише справжні фото (t51.*-15 / t39.30808-6…), без аватарок і значків; та сама картинка різних розмірів — одна, найбільша."""
    best = {}
    for raw in POST_IMG_RE.findall(text or ""):
        url = raw.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
        if ICON_RE.search(url) or re.search(r"(?i)profile|s150x150|t51\.2885-19", url):  # -19 — аватарки
            continue
        if not re.search(r"t51\.\d+-15|t51\.29350-15|t39\.\d+-6", url):
            continue
        key = urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0]
        m = re.search(r"[ps](\d{3,4})x(\d{3,4})", url)
        size = int(m[1]) * int(m[2]) if m else 10 ** 7  # без позначки розміру — зазвичай оригінал
        if key not in best or size > best[key][0]:
            best[key] = (size, url)
    return [(u, ".jpg") for _, u in best.values()]


def _embed_media(text, base):
    """Сторінка /embed: звичайний HTML з <video> і великими фото (без аватарок)."""
    doc = read_html(text.encode("utf-8"))
    links = []
    for node in doc.xpath("//video[@src] | //video/source[@src]"):
        links.append((urljoin(base, node.get("src")), ".mp4"))
    if not links:
        for node in doc.xpath("//img[@src]"):
            src = node.get("src") or ""
            if ICON_RE.search(src):  # значки сайту (static.cdninstagram.com/rsrc.php…) — не фото допису
                continue
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
    log.info("threads %s: сторінка %d КБ, допис у JSON: %s, карусель: %s з %s, фото в тексті: %d", code, len(text) // 1024,
             bool(post), len((post or {}).get("carousel_media") or []), (post or {}).get("carousel_media_count"),
             len(_raw_post_images(text)))
    if not post:
        log.info("threads %s: CDN на сторінці: %s; вхід: %s", code, _img_census(text), "login" in final or "loginForm" in text)
    if post:
        caption = post.get("caption") or {}
        if isinstance(caption, dict) and caption.get("text"):
            info["description"] = caption["text"]
        loc = post.get("location") or (post.get("text_post_app_info") or {}).get("location")
        if isinstance(loc, dict) and loc.get("name"):  # 📍 геотег
            info["location"] = loc["name"]
        items = []
        for part in post.get("carousel_media") or [post]:
            found = _best_media(part) if isinstance(part, dict) else None
            if found:
                items.append(Item("direct", found[0], dict(info)))
        if (post.get("carousel_media_count") or 0) > len(items):  # карусель у JSON неповна — добираємо з тексту сторінки
            raw = _raw_post_images(text)
            if len(raw) > len(items):
                items = [Item("direct", u, dict(info)) for u, _ in raw]
        if items:
            return items
    embed_note = "немає адреси допису"
    if code and user:
        try:
            efinal, etext = await _grab(f"https://www.threads.com/@{user}/post/{code}/embed", 3_000_000)
            links = []
            epost = _threads_post(etext, code)  # спершу JSON допису (там уся карусель), потім HTML
            if epost:
                links = [f for f in (_best_media(p) for p in (epost.get("carousel_media") or [epost])
                                      if isinstance(p, dict)) if f]
            if not links:
                links = _embed_media(etext, efinal)
            raw = _raw_post_images(etext)
            log.info("threads %s embed: %d КБ, JSON: %s, посилань: %d, фото в тексті: %d; CDN: %s", code, len(etext) // 1024,
                     bool(epost), len(links), len(raw), _img_census(etext))
            if len(raw) > len([x for x in links if x[1] != ".mp4"]) and not any(x[1] == ".mp4" for x in links):
                links = raw  # карусель фото: беремо всі справжні фото, без значків
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
                       "$5 на місяць вистачає приблизно на 300 сторіс).\n\n🔎 " + apify_hint())
    rows = await apify_run(APIFY_ACTOR, {"usernames": [user], "saveFiles": True, "maxItems": 0}, timeout=180)
    items = []
    for row in rows:
        link = row.get("videoUrl") if row.get("mediaType") == "video" else None
        link = row.get("savedFile") or link or row.get("videoUrl") or row.get("imageUrl")
        if link:
            exact = bool(story_id) and str(row.get("storyId") or "").split("_")[0] == story_id
            items.append(Item("direct", link, {"title": f"Сторіс @{user}", "author": user, "when": story_time(row),
                                               "author_url": f"https://www.instagram.com/{user}/", "_exact": exact,
                                               "_fallback": row.get("videoUrl") or row.get("imageUrl"),
                                               "description": " ".join(row.get("links") or []),
                                               "location": row.get("locationName") or row.get("location") or "",
                                               "_ts": story_epoch(row)}))
    if not items:
        raise JobError(f"У @{user} зараз немає доступних сторіс (минули 24 години або акаунт приватний).")
    if all(it.info.get("_ts") for it in items):  # 🕓 у тому ж порядку, у якому їх публікували (від ранішої до пізнішої)
        items.sort(key=lambda it: it.info["_ts"])
    return items


def story_epoch(row):
    """Час публікації сторіс у секундах (для порядку), 0 — якщо сервіс не віддав."""
    for key in ("takenAt", "taken_at", "timestamp", "created_time", "createdAt", "publish_time", "date"):
        v = row.get(key)
        if not v:
            continue
        try:
            if str(v).replace(".", "").isdigit():
                return float(v)
            return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
    return 0


def story_time(row):
    """Час публікації сторіс (як «14:05»), якщо сервіс його віддав."""
    for key in ("takenAt", "taken_at", "timestamp", "created_time", "createdAt", "publish_time", "date"):
        v = row.get(key)
        if not v:
            continue
        try:
            when = dt.datetime.fromtimestamp(float(v), dt.timezone.utc) if str(v).replace(".", "").isdigit() else \
                dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return when.astimezone(dt.timezone(dt.timedelta(hours=2))).strftime("%d.%m %H:%M")
        except (ValueError, OverflowError, OSError):
            continue
    return ""


def apify_auth(url):
    """Записи зі сховища Apify (api.apify.com/…/records/…) віддаються лише з ключем."""
    if "api.apify.com/" in str(url) and "token=" not in str(url) and apify_token():
        return str(url) + ("&" if "?" in str(url) else "?") + "token=" + apify_token()
    return url


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


APIFY_LAST = {}  # пояснення сервісу Apify, коли він нічого не повернув


async def apify_run(actor, payload, timeout=240):
    """Запуск сервісу Apify. Порожньо чи збій — з'ясовуємо чому (стан запуску, повідомлення сервісу), а не вгадуємо;
    людині — зрозуміле пояснення українською, без сирого JSON."""
    token = apify_token()
    if not token:
        raise JobError("Facebook та Instagram показують сторіс лише після входу. Щоб бот їх качав без вашого акаунта, як інші боти, "
                       "додайте в Railway змінну APIFY_TOKEN (apify.com → Settings → API &amp; Integrations).\n\n🔎 " + apify_hint())
    async with httpx.AsyncClient(timeout=timeout + 30, trust_env=False) as client:
        r = await client.post(f"https://api.apify.com/v2/acts/{actor}/runs", params={"token": token, "waitForFinish": timeout}, json=payload)
        if r.status_code in (401, 403):
            raise JobError("Ключ Apify (APIFY_TOKEN) не підходить — перевірте його в Railway.")
        if r.status_code == 402:
            raise JobError("На Apify закінчився баланс цього місяця — сторіс через нього недоступні до поповнення чи нового місяця.")
        if r.status_code >= 400:
            log.warning("Apify %s: HTTP %s %s", actor, r.status_code, r.text[:300])
            raise JobError("Сервіс завантаження сторіс (Apify) відмовив у запуску. Спробуйте за кілька хвилин.")
        run = (r.json() or {}).get("data") or {}
        status, msg = run.get("status") or "", run.get("statusMessage") or ""
        rows = []
        if run.get("defaultDatasetId"):
            d = await client.get(f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items", params={"token": token, "clean": "true"})
            data = d.json() if d.status_code < 400 else []
            rows = [row for row in (data if isinstance(data, list) else []) if isinstance(row, dict)]
    log.warning("Apify %s: стан %s · %s · рядків %d", actor, status, msg[:300], len(rows))
    if rows:
        return rows
    low = msg.lower()
    if re.search(r"limit|free|quota|upgrade|credit|balance|exceed|trial|subscription|rent", low):
        raise JobError("Сервіс завантаження сторіс (Apify) відмовив: схоже, вичерпано безкоштовний ліміт цього сервісу. "
                       "Потрібне поповнення в Apify або інший сервіс — скажіть, і я підключу.")
    if status in ("FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"):
        raise JobError("Сервіс завантаження сторіс (Apify) не впорався з цим посиланням"
                       + (" за відведений час" if "TIM" in status else "") + ". Найчастіше — сторіс уже зникла, видима лише друзям "
                       "або Instagram/Facebook тимчасово не пустив сервіс. Спробуйте ще раз за хвилину.")
    APIFY_LAST[actor] = msg[:200]
    return rows


async def fb_story_probe(url, work):
    """Сторіс Facebook: yt-dlp їх не знає, тож — через сервіс Apify (APIFY_TOKEN), як інші боти-завантажувачі."""
    # Файли просимо зберегти в сховище Apify: пряме посилання Facebook прив'язане до сервера Apify й швидко спливає
    # (бот отримував сторінку замість відео). Зі сховища забираємо за ключем — див. apify_auth().
    m = re.match(r"^https?://(?:(?:www|m|web)\.)?facebook\.com/stories/([^/?#]+)(?:/([^/?#]+))?", url)
    clean = (f"https://www.facebook.com/stories/{m.group(1)}/" + (f"{m.group(2)}/?view_single=1" if m.group(2) else "?view_single=false")) if m else url
    tries = list(dict.fromkeys([clean, url]))  # без зайвих ?mibextid=… — так сервіс упізнає сторіс; не вийде — як є
    reasons = []
    items = []
    for u in tries:
        for attempt in range(2):  # сервіс іноді повертає порожньо з першого разу
            rows = await apify_run(FB_STORY_ACTOR, {"story_urls": [{"url": u}], "saveToKeyValueStore": True})
            log.warning("Facebook-сторіс через Apify: %s → %d рядків %s", u, len(rows),
                        [{k: str(r.get(k))[:120] for k in ("status", "error", "message", "errorMessage", "media_type") if r.get(k)} for r in rows][:3])
            for row in rows:
                if row.get("status") == "failed":
                    why = row.get("error") or row.get("errorMessage") or row.get("message") or ""
                    if why:
                        reasons.append(str(why)[:200])
                    continue
                link = row.get("kv_store_url") or row.get("download_url")
                if not link:
                    continue
                who = row.get("uploader_name") or ""
                info = {"title": f"Сторіс {who}".strip(), "author": who, "when": story_time(row), "_fallback": row.get("download_url")}
                if row.get("media_type") == "video" and row.get("audio_download_url"):
                    info["_audio"] = row["audio_download_url"]  # відео й звук окремо — склеїмо
                items.append(Item("direct", link, info))
            if items:
                return items
            if rows:
                break  # сервіс відповів, але сторіс не віддав — повтор не допоможе
    if reasons:
        raise JobError("Сервіс завантаження не віддав сторіс: " + html_escape(reasons[0]) +
                       "\n\nЯкщо сторіс ще доступна у Facebook — спробуйте ще раз за хвилину або надішліть пряме посилання "
                       "facebook.com/stories/… (відкрийте сторіс → «Поділитися» → «Копіювати посилання»).")
    if APIFY_LAST.get(FB_STORY_ACTOR):
        log.warning("Apify пояснення: %s", APIFY_LAST.pop(FB_STORY_ACTOR))
    raise JobError("Сервіс завантаження не повернув цю сторіс. Причин може бути кілька: сторіс видно лише друзям автора, "
                   "посилання «Поділитися» веде не на саму сторіс, або сервіс тимчасово не впорався. Спробуйте ще раз за хвилину; "
                   "якщо не вийде — надішліть пряме посилання facebook.com/stories/….")


# 🛰 Cobalt — відкритий завантажувач (YouTube, TikTok, Instagram…). Свій сервер (COBALT_API_URL, COBALT_API_KEY) — першим,
# далі спільні сервери з cobalt.directory, де YouTube перевірено як робочий (21.11.2026). Їх можна змінити змінною COBALT_INSTANCES.
COBALT_PUBLIC = ("https://api.qwkuns.me", "https://grapefruit.clxxped.lol", "https://cobalt-alpha.wolfy.love",
                 "https://apicobalt.mgytr.top", "https://rue-cobalt.xenon.zone")
COBALT_BAD = {}  # сервер -> до коли не смикати (вимагає ключ/перевірку чи не відповідає)


def cobalt_servers():
    own = [u.strip().rstrip("/") for u in os.environ.get("COBALT_API_URL", "").split(",") if u.strip()]
    pub = [u.strip().rstrip("/") for u in os.environ.get("COBALT_INSTANCES", ",".join(COBALT_PUBLIC)).split(",") if u.strip()]
    now = time.time()
    return [(u, u in own) for u in dict.fromkeys(own + pub) if COBALT_BAD.get(u, 0) < now]


async def cobalt_probe(url, work):
    """Запасний шлях через Cobalt, коли сайт не пускає сервер бота (YouTube «підтвердіть, що ви не бот» тощо)."""
    notes = []
    body = {"url": url, "videoQuality": "1080", "filenameStyle": "basic"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=8), follow_redirects=True) as cl:
        for server, own in cobalt_servers():
            head = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "OrionAI-bot/1.0"}
            if own and os.environ.get("COBALT_API_KEY"):
                head["Authorization"] = "Api-Key " + os.environ["COBALT_API_KEY"]
            try:
                r = await cl.post(server + "/", json=body, headers=head)
                js = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                COBALT_BAD[server] = time.time() + 900
                notes.append(f"{urlsplit(server).hostname}: {type(exc).__name__}")
                continue
            st = js.get("status")
            if st in ("tunnel", "redirect") and js.get("url"):
                name = js.get("filename") or ""
                ext = (name.rsplit(".", 1)[-1] if "." in name else "mp4")[:5]
                return [Item("direct", js["url"], {"title": name.rsplit(".", 1)[0] or "Відео", "ext": ext, "via": "cobalt"})]
            if st == "picker" and js.get("picker"):
                out = [Item("direct", p["url"], {"title": "Медіа", "ext": "jpg" if p.get("type") == "photo" else "mp4", "via": "cobalt"})
                       for p in js["picker"] if p.get("url")]
                if out:
                    return out
            code = str(((js.get("error") or {}).get("code")) or st or r.status_code)
            if re.search(r"auth|jwt|turnstile|api\.key|rate", code):
                COBALT_BAD[server] = time.time() + 6 * 3600  # потребує ключа чи обмежив — не смикаємо кілька годин
            notes.append(f"{urlsplit(server).hostname}: {code}")
    log.warning("cobalt: %s → %s", url, "; ".join(notes)[:400])
    raise JobError("cobalt · " + "; ".join(notes)[:300])


async def tikwm_probe(url, work):
    """Запасний шлях для TikTok, коли yt-dlp сайт не пускає: відкритий сервіс tikwm.com (відео без водяного знака, фото-каруселі)."""
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": BROWSER}) as cl:
        r = await cl.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1})
        r.raise_for_status()
        js = r.json()
    d = (js or {}).get("data") or {}
    if (js or {}).get("code") != 0 or not d:
        raise JobError("TikTok: запасний сервіс теж не віддав відео (" + str((js or {}).get("msg") or "без пояснення")[:80] + ").")
    who = ((d.get("author") or {}).get("nickname")) or ""
    info = {"title": d.get("title") or "TikTok", "author": who, "description": d.get("title") or ""}
    pics = [p for p in (d.get("images") or []) if isinstance(p, str)]
    if pics:
        return [Item("direct", p, {**info, "ext": "jpg"}) for p in pics]
    link = d.get("hdplay") or d.get("play")
    if not link:
        raise JobError("TikTok: у відповіді немає відео.")
    if link.startswith("/"):
        link = "https://www.tikwm.com" + link
    return [Item("direct", link, {**info, "ext": "mp4"})]


async def inspect_link(url, work):
    await check_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname.lower().rstrip(".")
    if (
        Path(parsed.path).suffix.lower() in MEDIA
        or domain_matches(host, ("discordapp.com", "discordapp.net", "t.me", "telegram.me"))
    ):
        probes = (page_probe, video_probe, gallery_probe)
    elif YT_POST.match(url):
        probes = (yt_post_probe,)  # 🖼 допис спільноти YouTube: одна чи кілька картинок
    elif STORY.match(url):
        probes = (story_probe,)
    elif FB_STORY.match(url):
        probes = (fb_story_probe,)
    elif domain_matches(host, ("tiktok.com",)):
        probes = (video_probe, tikwm_probe, cobalt_probe, gallery_probe, page_probe)
    elif domain_matches(host, ("youtube.com", "youtu.be")):
        probes = (video_probe, cobalt_probe, gallery_probe, page_probe)  # YouTube блокує сервери — запасний шлях через Cobalt  # yt-dlp; запасний — tikwm; фото-каруселі — gallery-dl
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
    labels = {"video_probe": "завантажувач yt-dlp", "gallery_probe": "альбоми (gallery-dl)", "page_probe": "сторінка допису",
              "threads_probe": "Threads", "tumblr_probe": "Tumblr", "story_probe": "сторіс: напряму, далі через Apify (до 1–2 хв)",
              "fb_story_probe": "сторіс через Apify (до 1–2 хв)", "tikwm_probe": "запасний сервіс TikTok",
              "cobalt_probe": "запасний завантажувач Cobalt"}
    for probe in probes:
        await note(labels.get(probe.__name__, probe.__name__))
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
                       )
    if SHARE.match(url) and any("unsupported url" in e[1].lower() for e in errors):
        raise JobError("Сайт не розкрив серверу це коротке посилання «Поділитися». Спробуйте інше посилання на той самий допис: "
                       "у Facebook — «Поділитися» → «Копіювати посилання» з самого відео (reel/… або watch/?v=…).")
    # Найзмістовніша причина — від yt-dlp (він знає сайти), далі gallery-dl, далі сторінка.
    order = {"story_probe": 0, "fb_story_probe": 0, "threads_probe": 0, "tumblr_probe": 0, "video_probe": 1, "gallery_probe": 2, "page_probe": 3}
    errors.sort(key=lambda e: order.get(e[0], 3))
    if errors and errors[0][0] in ("story_probe", "fb_story_probe"):
        raise JobError(errors[0][1])  # уже зрозуміле пояснення
    raise JobError(explain(errors[0][1] if errors else "Джерело не повернуло медіа або тексту."))


VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


async def has_audio(path):
    """Чи є у відео звукова доріжка (ffprobe)."""
    try:
        data = await probe_media(path)
    except Exception:  # noqa: BLE001
        return True  # не змогли перевірити — не лякаємо даремно
    return any(st.get("codec_type") == "audio" for st in data.get("streams", []))


def source_has_audio(info):
    """Чи був звук в оригіналі: у бодай одному форматі сайту є аудіокодек."""
    info = info or {}
    fmts = info.get("formats") or []
    if any((f.get("acodec") or "none") != "none" for f in fmts if isinstance(f, dict)):
        return True
    return (info.get("acodec") or "none") != "none"


async def fetch_direct(item, source, options, work, progress=None):
    if options.audio != "auto" or options.subs != "off":
        raise JobError("Прямий файл має власну доріжку. Вибери /audio auto і /subs off.")
    urls = [apify_auth(item.url)] + ([item.info["_fallback"]] if item.info.get("_fallback") else [])
    for attempt, address in enumerate(urls):
        try:
            return await _fetch_direct_one(item, address, source, work, progress)
        except (JobError, httpx.HTTPError):
            if attempt == len(urls) - 1:
                raise


async def _fetch_direct_one(item, address, source, work, progress):
    async with open_url(address, referer=None if "api.apify.com" in address else source) as response:
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
            item.info["_no_audio"] = "звукову доріжку не вдалося завантажити (помилка сайту) — спробуйте ще раз"
    elif path.suffix.lower() in VIDEO_EXT and not await has_audio(path):
        item.info["_no_audio"] = "у файлі, який віддав сайт, звуку немає (в оригіналі його могло не бути)"
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
        afmt = options.fmt if options.fmt in AUDIO_FMTS or options.fmt == "orig" else "mp3"
        convert = ["-x"] + ([] if afmt == "orig" else ["--audio-format", afmt]) \
            + (["--audio-quality", f"{options.abr or 192}K"] if afmt in ("mp3", "m4a", "opus") else [])
    else:
        selector = f"bv{height}+ba{language}/b{height}{language}"
        if getattr(options, "need_audio", False):  # 🔁 повтор «зі звуком»: лише формати зі звуком, без «лише відео»
            selector = f"bv{height}+ba/b{height}/bv*+ba/b"
        elif not language:
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
        return (*yt_base(source), *proxy_args(source), *cookie_args(source), *extra, *entry, *convert, *subtitles, *live, "-f", selector,
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

    yt = is_youtube(source)
    if yt:
        await YT_HEAVY.acquire()
    try:
        try:
            await go(extra)
        except JobError as exc:
            if extra or not yt or not (BOT_CHECK.search(str(exc)) or "403" in str(exc)):
                raise
            for leftover in work.iterdir():
                if leftover.is_file():
                    leftover.unlink()
            await go(YT_RETRY)
    finally:
        if yt:
            YT_HEAVY.release()
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
    if item.index is None and not same_media(source, info):
        raise JobError("Сайт віддав не те відео, на яке вказує посилання. Нічого стороннього не надсилаю — "
                       "спробуйте ще раз пізніше або інше посилання на це ж відео.")
    stem = Path(safe_name(ua_title(info.get("title") or item.title)[:70], "media")).stem
    if options.mode == "video" and files[0].suffix.lower() in VIDEO_EXT and not await has_audio(files[0]):
        if source_has_audio(info):  # 🔇 у джерелі звук є, а прийшло німе — не повторюємо самі: людина вирішує (кнопка під файлом)
            info["_no_audio"] = "звук не вдалося отримати з сайту (помилка)"
            info["_audio_retry"] = True
        else:
            info["_no_audio"] = "в оригіналі звукової доріжки немає"
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
    if path.suffix.lower() in DOCS:  # документ/архів — без обробки відео й звуку
        return [path]
    image = (mimetypes.guess_type(path.name)[0] or "").startswith("image/")
    if options.mode in ("gif", "mute") and not image:
        return await render_special(path, options, work)
    needs_scale = direct and options.height and not image and options.mode == "video"
    aext = "." + (options.fmt if options.fmt in AUDIO_FMTS else "mp3")
    keep_orig = options.fmt == "orig"
    needs_audio = options.mode == "audio" and not image and not keep_orig and (direct or path.suffix.lower() != aext)
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
    ext = ".mp4" if video_output else aext
    maps = ["-map", "0:V:0", "-map", "0:a:0?"] if video_output else ["-map", "0:a:0"]
    acodec = AUDIO_FMTS.get(aext[1:], "libmp3lame")
    codec = ["-c:a", "aac", "-b:a", "160k"] if video_output else (["-c:a", acodec] + (["-b:a", f"{options.abr or 192}k"]
                                                                                     if aext in (".mp3", ".m4a", ".opus") else []))
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


# ================================================================ 🎵 музика, накладена соцмережею
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".oga", ".mka"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".heic"}
ORIGINAL_RE = re.compile(r"original sound|original audio|оригінальн\w* звук|оригинальн\w* звук|son original|sonido original|"
                         r"orijinal ses|suara asli|som original|originalton|dźwięk oryginalny", re.I)
_TITLE_KEYS = ("title", "song_name", "track", "name", "audio_title", "music_title")
_ARTIST_KEYS = ("display_artist", "artist", "authorName", "author_name", "author", "artist_name", "ig_artist", "owner")
_URL_KEYS = ("playUrl", "play_url", "progressive_download_url", "download_url", "audio_url", "url", "fast_start_progressive_download_url")
_MUSIC_KEYS = ("music_asset_info", "music_info", "music_metadata", "original_sound_info", "music", "sound", "audio", "song",
               "clips_metadata", "audio_info")


def _s(v):
    if isinstance(v, dict):
        v = v.get("username") or v.get("name") or v.get("full_name") or ""
    if isinstance(v, list):
        v = ", ".join(_s(x) for x in v if _s(x))
    return str(v or "").strip()


def _music_from(d):
    title = next((_s(d.get(k)) for k in _TITLE_KEYS if _s(d.get(k))), "")
    if not title or len(title) > 200 or title.startswith("http"):
        return None
    artist = next((_s(d.get(k)) for k in _ARTIST_KEYS if _s(d.get(k))), "")
    url = next((d.get(k) for k in _URL_KEYS if isinstance(d.get(k), str) and d.get(k).startswith("http")), "")
    if isinstance(d.get("playUrl"), list) and d["playUrl"]:
        url = url or d["playUrl"][0]
    orig = bool(d.get("original") or d.get("is_original") or d.get("original_sound_info") or ORIGINAL_RE.search(title))
    dur = d.get("duration")
    if not dur and isinstance(d.get("duration_in_ms"), (int, float)):
        dur = d["duration_in_ms"] / 1000
    return {"title": title, "artist": artist, "url": url, "original": orig, "duration": dur or 0}


def music_of(info, _depth=0):
    """Яка музика накладена на допис: {title, artist, url (фрагмент з допису, якщо сайт його віддає), original}.
    Шукає у відомих полях yt-dlp (track/artist), TikTok (music.title/authorName/playUrl), Instagram
    (music_asset_info.title/display_artist), Facebook (music/sound) — на будь-якій глибині метаданих."""
    if not isinstance(info, dict) or _depth > 6:
        return None
    if _depth == 0:
        track = _s(info.get("track"))
        if track:
            artist = _s(info.get("artist") or info.get("artists"))
            return {"title": track, "artist": artist, "url": "", "original": bool(ORIGINAL_RE.search(track)), "duration": 0}
    for k in _MUSIC_KEYS:
        v = info.get(k)
        if isinstance(v, dict):
            inner = v.get("music_asset_info") or v.get("music_info") or v.get("original_sound_info")
            if isinstance(inner, dict):
                got = music_of({"music_asset_info": inner} if "music_asset_info" not in v else v, _depth + 1) or _music_from(inner)
                if got:
                    if v.get("original_sound_info") and not v.get("music_info"):
                        got["original"] = True
                    return got
            got = _music_from(v)
            if got:
                return got
            got = music_of(v, _depth + 1)
            if got:
                return got
    for v in info.values():
        if isinstance(v, dict):
            got = music_of(v, _depth + 1) if any(k in v for k in _MUSIC_KEYS) else None
            if got:
                return got
        elif isinstance(v, list) and v and isinstance(v[0], dict) and _depth < 3:
            for x in v[:5]:
                got = music_of(x, _depth + 1) if any(k in x for k in _MUSIC_KEYS) else None
                if got:
                    return got
    return None


def is_audio_item(item):
    ext = (item.info.get("extension") or Path(urlsplit(str(item.url)).path).suffix.lstrip(".")).lower()
    return ("." + ext) in AUDIO_EXT


def is_image_item(item):
    ext = (item.info.get("extension") or Path(urlsplit(str(item.url)).path).suffix.lstrip(".")).lower()
    return ("." + ext) in IMAGE_EXT


# ================================================================ 🖼 YouTube: дописи спільноти (каруселі картинок)
YT_POST = re.compile(r"https?://(?:www\.|m\.)?youtube\.com/(?:post/[\w-]+|channel/[\w-]+/community\?.*lb=|@[\w.-]+/community\?.*lb=)")


def _yt_initial(text):
    m = re.search(r"ytInitialData\s*=\s*(\{.+?\});\s*</script>", text, re.S) or re.search(r"var ytInitialData = (\{.+?\});", text, re.S)
    return json.loads(m.group(1)) if m else {}


def _yt_walk(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _yt_walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _yt_walk(v, key)


def yt_post_images(data):
    """Усі картинки першого допису спільноти в найвищій якості (=s0 — оригінальний розмір)."""
    post = next(_yt_walk(data, "backstagePostRenderer"), None) or next(_yt_walk(data, "sharedPostRenderer"), None) or data
    urls = []
    for img in _yt_walk(post, "backstageImageRenderer"):
        thumbs = ((img or {}).get("image") or {}).get("thumbnails") or []
        if thumbs:
            u = thumbs[-1].get("url", "")
            u = re.sub(r"=s\d+[^/?#]*$|=w\d+-h\d+[^/?#]*$", "=s0", u)
            if u.startswith("//"):
                u = "https:" + u
            if u and u not in urls:
                urls.append(u)
    text = "".join(r.get("text", "") for t in _yt_walk(post, "contentText") if isinstance(t, dict) for r in t.get("runs") or [])
    author = next((r.get("text", "") for a in _yt_walk(post, "authorText") if isinstance(a, dict) for r in a.get("runs") or []), "")
    return urls, text, author


async def yt_post_probe(url, work):
    if not YT_POST.match(url):
        return []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "uk,en;q=0.8"}, cookies={"CONSENT": "YES+1"},
            proxy=os.environ.get("MEDIA_PROXY", "").strip() or None) as c:
        r = await c.get(url)
        r.raise_for_status()
    urls, text, author = yt_post_images(_yt_initial(r.text))
    if not urls:
        raise JobError("У цьому дописі YouTube немає картинок (або він доступний лише після входу).")
    base = {"description": text, "uploader": author, "webpage_url": url, "title": (text[:60] or "Допис YouTube")}
    return [Item("direct", u, {**base, "extension": "jpg", "filename": f"youtube-post-{i}"}) for i, u in enumerate(urls, 1)]


# ================================================================ 🗣 відокремити оригінальний звук від музики (ШІ)
async def separate(path, work, progress=None):
    """Demucs (htdemucs, лише процесор): голос/оригінальний звук окремо від музики → (vocals.wav, music.wav).
    Перший запуск після оновлення сервера ставить Demucs (≈3–5 хв); далі — ≈1–2 хв на хвилину звуку."""
    import importlib
    import sys
    try:
        importlib.import_module("demucs")
    except ImportError:
        if progress:
            await progress("📦 Перший запуск: встановлюю розділювач звуку Demucs (≈3–5 хв)…")
        await run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                  "--extra-index-url", "https://download.pytorch.org/whl/cpu", "torch", "torchaudio", "demucs", timeout=1500)
        importlib.invalidate_caches()
    wav = work / "sep-in.wav"
    await run(*FF, "-i", path, "-vn", "-ac", "2", "-ar", "44100", wav, timeout=600)
    if progress:
        await progress("🗣 Відокремлюю оригінальний звук від музики (ШІ на процесорі, ≈1–2 хв на хвилину звуку)…")
    out = work / "sep"
    await run(sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "--segment", "7", "-j", "1",
              "-o", out, wav, timeout=3600)
    found = list(out.rglob("vocals.wav")), list(out.rglob("no_vocals.wav"))
    if not found[0] or not found[1]:
        raise JobError("Розділювач звуку не повернув результат.")
    return found[0][0], found[1][0]


async def convert_final(path, options, work):
    """Формат файлу на вибір: відео mp4/mov/mkv/webm, фото jpg/png/webp. Без втрат, де можна (перепакування)."""
    fmt = options.fmt
    ext = path.suffix.lower().lstrip(".")
    image = (mimetypes.guess_type(path.name)[0] or "").startswith("image/")
    if not fmt or fmt == ext or (fmt == "jpg" and ext == "jpeg"):
        return path
    out = work / (path.stem + "." + fmt)
    if path.suffix.lower() in DOC_TO and fmt in DOC_TO[path.suffix.lower()]:
        office = shutil.which("libreoffice") or shutil.which("soffice")
        if not office:
            raise JobError("Перетворення документів недоступне: на сервері немає LibreOffice.")
        conv = work / "converted"
        conv.mkdir(exist_ok=True)
        await run(office, "--headless", "--convert-to", fmt, "--outdir", str(conv), str(path), timeout=300)
        got = conv / (path.stem + "." + fmt)
        if not got.is_file():
            raise JobError(f"Не вдалося перетворити в {fmt.upper()}.")
        out = got.rename(work / got.name) if not (work / got.name).exists() else got
        path.unlink(missing_ok=True)
        return out
    if image and fmt in IMAGE_FMTS:
        await run(*FF, "-i", path, *(["-q:v", "2"] if fmt == "jpg" else []), out, timeout=120)
    elif not image and fmt in VIDEO_FMTS and options.mode not in ("audio", "gif"):
        if fmt == "webm":
            codec = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "0", "-crf", "33", "-c:a", "libopus"]
        else:
            codec = ["-c", "copy"] + (["-movflags", "+faststart"] if fmt == "mov" else [])
        await run(*FF, "-i", path, "-map", "0", *codec, out, timeout=1800)
    else:
        return path
    if out.is_file() and out.stat().st_size > 0:
        path.unlink(missing_ok=True)
        return out
    return path


async def make_thumb(path, work):
    """Картинка-передперегляд для файлу (кадр відео, обкладинка треку, перша сторінка PDF) — JPEG до 320 px."""
    ext = path.suffix.lower()
    out = work / (path.stem + ".thumb.jpg")
    try:
        if ext == ".pdf" and shutil.which("pdftoppm"):
            await run("pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to", "320", "-singlefile", path, str(out.with_suffix("")), timeout=60)
        elif ext in MEDIA or ext in (".mkv", ".webm", ".wav", ".flac", ".opus", ".m4a", ".mp3", ".mov"):
            seek = ["-ss", "1"] if ext in (".mp4", ".mov", ".m4v", ".mkv", ".webm") else []
            try:
                await run(*FF, *seek, "-i", path, "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", out, timeout=60)
            except JobError:
                if not seek:
                    raise
                await run(*FF, "-i", path, "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", out, timeout=60)
        else:
            return None
    except (JobError, OSError):
        return None
    return out if out.is_file() and 0 < out.stat().st_size < 200_000 else None
