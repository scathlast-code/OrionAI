"""Read public HTTPS pages, PDFs, and optional JavaScript-rendered pages.

No login bypass, CAPTCHA bypass, private-network access or stored credentials.
"""
import asyncio
import ipaddress
import os
import re
import socket
from io import BytesIO
from urllib.parse import urlparse

import httpx
from websearch import _PageText, _public_host

MAX_BYTES = 4_000_000
MAX_TEXT = 10000

async def _safe_url(url):
    if not _public_host(url):
        return False
    host = urlparse(url).hostname
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(x[4][0]).is_global for x in addresses)
    except (OSError, ValueError):
        return False

def _extract_pdf(data):
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\\n".join(page.get_text() for page in list(doc)[:12])[:MAX_TEXT]
    finally:
        doc.close()

async def _http_read(url):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as c:
        async with c.stream("GET", url, headers={"User-Agent":"OrionAI/1.0", "Accept":"text/html,application/pdf"}) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                return None, "Сторінка перенаправляє на іншу адресу. Відкрий кінцеве HTTPS-посилання окремо."
            r.raise_for_status()
            typ = r.headers.get("content-type", "").lower()
            if "pdf" not in typ and "html" not in typ:
                return None, "Підтримуються лише HTML та PDF."
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    return None, "Файл перевищує ліміт 4 МБ."
            if "pdf" in typ or data[:4] == b"%PDF":
                return _extract_pdf(bytes(data)), None
            parser = _PageText()
            parser.feed(bytes(data).decode("utf-8", "replace"))
            return re.sub(r"\\s+", " ", " ".join(parser.parts)).strip()[:MAX_TEXT], None

async def _javascript_read(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            async def guard(route):
                target = route.request.url
                if await _safe_url(target):
                    await route.continue_()
                else:
                    await route.abort()
            await context.route("**/*", guard)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            text = await page.locator("body").inner_text(timeout=5000)
            links = await page.locator("a[href]").evaluate_all(
                "(els) => els.slice(0, 30).map(a => ({title:(a.innerText||'').trim().slice(0,80), url:a.href}))"
            )
            link_text = "\\n".join(f"- {x['title']}: {x['url']}" for x in links if x["title"] and x["url"].startswith("https://"))
            return (text[:MAX_TEXT] + "\\n\\nПосилання на сторінці:\\n" + link_text[:3500]), None
        finally:
            await browser.close()

async def browse(url):
    url = url.strip()
    if not await _safe_url(url):
        return None, "Дозволені лише публічні HTTPS-адреси. Внутрішні адреси заблоковані."
    try:
        content, err = await _http_read(url)
        if content and len(content) > 250:
            return content, None
        if os.getenv("BROWSER_JS_ENABLED", "1") == "1":
            try:
                js_content, js_error = await _javascript_read(url)
                if js_content:
                    return js_content, None
                return content, js_error or err
            except Exception as exc:
                return content, f"JavaScript-браузер недоступний: {type(exc).__name__}"
        return content, err or "Сторінка не містить достатньо тексту."
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return None, f"Помилка відкриття: {type(exc).__name__}"
