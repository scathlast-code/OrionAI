"""🖼 Картинки під тему допису з вільних бібліотек (без ключів і без оплати):
- Openverse (openverse.org) — сотні мільйонів зображень із відкритими ліцензіями (CC, суспільне надбання);
- Wikimedia Commons — фото Вікіпедії.
Беремо лише ліцензії, що дозволяють використання (зокрема комерційне), і завжди зберігаємо автора й ліцензію для підпису.
Щоразу — інша картинка: вже показані для новини й нещодавно використані в інших новинах пропускаються."""
from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger("orion.newsroom.stock")
UA = "OrionAI-Newsroom/1 (GreenPost; news cards)"
OK_LICENSES = {"cc0", "pdm", "by", "by-sa"}  # без «nc» (некомерційне) і «nd» (без змін — а ми накладаємо тон і обрізаємо)

QUERY_PROMPT = ("Дай 3 пошукові запити АНГЛІЙСЬКОЮ для пошуку фото до новини в бібліотеці вільних зображень. "
                "Конкретні видимі предмети чи місця (2–4 слова), без абстракцій, без імен політиків і без облич крупним планом. "
                "Від найточнішого до загальнішого. Відповідай ЛИШЕ JSON {\"queries\": [\"…\", \"…\", \"…\"]}.\n\nЗаголовок: ")


def parse_queries(data, headline=""):
    qs = [re.sub(r"[^\w\s-]", " ", str(q)).strip() for q in (data or {}).get("queries") or [] if str(q).strip()]
    return [q for q in qs if q][:3] or [" ".join(re.findall(r"\w{4,}", headline)[:3])]


def openverse_items(js):
    out = []
    for r in (js or {}).get("results") or []:
        lic = (r.get("license") or "").lower()
        if lic not in OK_LICENSES or (r.get("width") or 0) < 900:
            continue
        out.append({"url": r.get("url"), "id": "ov:" + str(r.get("id")), "author": r.get("creator") or "невідомий автор",
                    "license": ("CC " + lic.upper() + " " + (r.get("license_version") or "")).strip() if lic not in ("cc0", "pdm") else
                    ("CC0" if lic == "cc0" else "суспільне надбання"),
                    "page": r.get("foreign_landing_url") or r.get("url"), "src": "Openverse"})
    return [x for x in out if x["url"]]


def commons_items(js):
    out = []
    for p in ((js or {}).get("query") or {}).get("pages", {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = ((meta.get("LicenseShortName") or {}).get("value") or "").strip()
        low = lic.lower()
        if not lic or "nc" in low.split("-") or "nd" in low.split("-") or "fair use" in low or (ii.get("width") or 0) < 900:
            continue
        if not any(k in low for k in ("cc0", "public domain", "cc by", "cc-by", "pd")):
            continue
        author = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or "").strip()[:80] or "невідомий автор"
        out.append({"url": ii.get("thumburl") or ii.get("url"), "id": "wc:" + str(p.get("pageid")), "author": author, "license": lic,
                    "page": ii.get("descriptionurl") or "", "src": "Wikimedia Commons"})
    return [x for x in out if x["url"]]


async def search(query, limit=20):
    """-> список кандидатів з обох бібліотек (спершу Openverse)."""
    items = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as cl:
        try:
            r = await cl.get("https://api.openverse.org/v1/images/", params={"q": query, "page_size": limit, "license_type": "commercial,modification",
                                                                          "mature": "false", "aspect_ratio": "wide"})
            if r.status_code == 200:
                items += openverse_items(r.json())
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            log.info("openverse: %s", exc)
        try:
            r = await cl.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "format": "json", "generator": "search", "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6,
                "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1600})
            if r.status_code == 200:
                items += commons_items(r.json())
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            log.info("commons: %s", exc)
    return items


async def fetch(url):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as cl:
        r = await cl.get(url)
        if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF", b"GIF") and len(r.content) < 25_000_000:
            return r.content
    return None


def credit(it):
    return f"Фото: {it['author']} · {it['license']} · {it['src']}"
