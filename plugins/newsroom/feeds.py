"""Безпечне читання RSS/Atom: лише публічні HTTPS-адреси, обмеження розміру й редиректів."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 2 * 1024 * 1024
UA = "OrionAI-Newsroom/1.0 (+RSS reader)"


class FeedError(Exception):
    pass


async def public_https(url: str) -> bool:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> bytes:
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": UA}, trust_env=False)
    try:
        for _ in range(4):
            if not await public_https(url):
                raise FeedError("дозволені лише публічні https-адреси")
            async with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise FeedError(f"HTTP {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise FeedError("стрічка більша за 2 МБ")
                return bytes(buf)
        raise FeedError("забагато переадресацій")
    finally:
        if own:
            await client.aclose()


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def parse(data: bytes) -> tuple[str, list[dict]]:
    """-> (назва стрічки, [{guid, title, link, text, published}]) — RSS 2.0, RSS 1.0/RDF, Atom."""
    if b"<!ENTITY" in data[:5000].upper():
        raise FeedError("стрічка містить DTD-сутності — відхилено з міркувань безпеки")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"не XML: {exc}") from exc

    def local(tag):
        return tag.rsplit("}", 1)[-1].lower()

    title, items = "", []
    for el in root.iter():
        name = local(el.tag)
        if name in ("channel", "feed") and not title:
            for ch in el:
                if local(ch.tag) == "title":
                    title = _text(ch)
                    break
        if name in ("item", "entry"):
            rec = {"guid": "", "title": "", "link": "", "text": "", "published": ""}
            for ch in el:
                n = local(ch.tag)
                if n == "title":
                    rec["title"] = strip_html(_text(ch))
                elif n == "link":
                    href = ch.attrib.get("href")
                    if href and ch.attrib.get("rel", "alternate") == "alternate":
                        rec["link"] = href
                    elif not href and _text(ch):
                        rec["link"] = _text(ch)
                elif n in ("guid", "id"):
                    rec["guid"] = _text(ch)
                elif n in ("description", "summary", "content", "encoded") and len(_text(ch)) > len(rec["text"]):
                    rec["text"] = strip_html(_text(ch))
                elif n in ("pubdate", "published", "updated", "date") and not rec["published"]:
                    rec["published"] = _text(ch)
            rec["guid"] = rec["guid"] or rec["link"] or rec["title"]
            if rec["guid"]:
                items.append(rec)
    return title, items
