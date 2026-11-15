"""Telegram sticker/custom emoji pack exporter using official Bot API.

No user-account login, no scraping, and no token logging.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

PACK_RE = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.me)/(addstickers|addemoji)/([A-Za-z0-9_]{1,64})(?:[/?#][^\s]*)?")
MAX_PACKS = int(os.getenv("STICKER_MAX_PACKS", "10"))
MAX_ITEMS = int(os.getenv("STICKER_MAX_ITEMS", "3000"))
MAX_BYTES = int(os.getenv("STICKER_MAX_BYTES", str(45 * 1024 * 1024)))


class StickerExportError(Exception):
    pass


def parse_links(text: str) -> list[tuple[str, str]]:
    packs = []
    seen = set()
    for kind, name in PACK_RE.findall(text or ""):
        key = name.lower()
        if key not in seen:
            seen.add(key)
            packs.append((kind.lower(), name))
    if len(packs) > MAX_PACKS:
        raise StickerExportError(f"Максимум {MAX_PACKS} пакетів за одне завдання")
    return packs


async def _api(client, token, method, **params):
    response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise StickerExportError(f"Telegram API {method}: {payload.get('description', 'невідома помилка')}")
    return payload["result"]


def _ext(sticker):
    return ".webm" if sticker.get("is_video") else ".tgs" if sticker.get("is_animated") else ".webp"


async def export_packs(token: str, links: str, destination: Path) -> dict:
    """Create a ZIP; return manifest. A failure is reported, never silently skipped."""
    packs = parse_links(links)
    if not packs:
        raise StickerExportError("Не знайдено посилань t.me/addstickers/… або t.me/addemoji/…")
    total_bytes = 0
    total_items = 0
    hashes = {}
    report = {"packs": [], "errors": [], "files": 0, "duplicates": 0}
    timeout = httpx.Timeout(35.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for kind, name in packs:
                try:
                    info = await _api(client, token, "getStickerSet", name=name)
                    stickers = info.get("stickers", [])
                    if total_items + len(stickers) > MAX_ITEMS:
                        raise StickerExportError("Перевищено обмеження кількості елементів")
                    total_items += len(stickers)
                    folder = re.sub(r"[^A-Za-z0-9_-]", "_", name)
                    entry = {"name": name, "kind": kind, "title": info.get("title"), "count": len(stickers), "items": []}
                    for idx, sticker in enumerate(stickers, 1):
                        meta = await _api(client, token, "getFile", file_id=sticker["file_id"])
                        path = meta.get("file_path", "")
                        # Telegram getFile returns a Telegram-controlled path; constrain it.
                        if not path or path.startswith("/") or ".." in Path(path).parts or "://" in path:
                            raise StickerExportError("Небезпечний шлях файлу в Telegram API")
                        url = f"https://api.telegram.org/file/bot{token}/{path}"
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.content
                        total_bytes += len(data)
                        if total_bytes > MAX_BYTES:
                            raise StickerExportError(f"Архів перевищує ліміт {MAX_BYTES // (1024*1024)} МБ")
                        sha = hashlib.sha256(data).hexdigest()
                        filename = f"{folder}/{idx:04d}{_ext(sticker)}"
                        item = {"file": filename, "emoji": sticker.get("emoji"), "file_unique_id": sticker.get("file_unique_id"), "sha256": sha}
                        if sha in hashes:
                            item["duplicate_of"] = hashes[sha]
                            report["duplicates"] += 1
                        else:
                            archive.writestr(filename, data)
                            hashes[sha] = filename
                            report["files"] += 1
                        entry["items"].append(item)
                    report["packs"].append(entry)
                except Exception as exc:
                    report["errors"].append({"pack": name, "error": str(exc)})
            archive.writestr("manifest.json", json.dumps(report, ensure_ascii=False, indent=2))
    if not report["files"]:
        destination.unlink(missing_ok=True)
        raise StickerExportError("Не завантажено жодного елемента: " + json.dumps(report["errors"], ensure_ascii=False)[:800])
    return report
