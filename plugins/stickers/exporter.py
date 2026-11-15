"""Експорт пакетів стікерів і кастомних емодзі через офіційний Telegram Bot API.

Об'єднує дві попередні версії: Oracle-бот (розбиття ZIP на частини, режим /merge без повторів, manifest у
кожній теці) і модуль Оріона (async, перевірка шляхів, ліміти). Нове: паралельне завантаження, прогрес,
PNG-копії статичних стікерів, інформація про пакет без завантаження, «відбиток» пакета для стеження за оновленнями.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

import httpx

PACK_RE = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(addstickers|addemoji)/([A-Za-z0-9_]{1,64})")
MAX_PACKS = int(os.getenv("STICKER_MAX_PACKS", "30"))
MAX_FILE = int(os.getenv("STICKER_MAX_FILE_MB", "19")) * 1024 * 1024       # Bot API getFile ≈ 20 МБ
PART_BYTES = int(os.getenv("STICKER_PART_MB", "45")) * 1024 * 1024          # sendDocument ≈ 50 МБ
TOTAL_BYTES = int(os.getenv("STICKER_TOTAL_MB", "400")) * 1024 * 1024
PARALLEL = int(os.getenv("STICKER_PARALLEL", "6"))


class StickerError(Exception):
    pass


def parse_links(text: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for kind, name in PACK_RE.findall(text or ""):
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append((kind.lower(), name))
    return out[:MAX_PACKS]


def only_links(text: str) -> bool:
    """Повідомлення складається лише з посилань на пакети (плюс кілька слів)."""
    rest = PACK_RE.sub(" ", text or "")
    rest = re.sub(r"(?i)https?://\S+|/merge|[\s,;.\-–—]+", " ", rest).strip()
    return bool(PACK_RE.search(text or "")) and len(rest) <= 25


def ext(sticker) -> str:
    return ".webm" if sticker.get("is_video") else ".tgs" if sticker.get("is_animated") else ".webp"


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "pack"


class Api:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token, self.c = token, client

    async def call(self, method, **params):
        r = await self.c.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        try:
            payload = r.json()
        except ValueError as exc:
            raise StickerError(f"{method}: HTTP {r.status_code}") from exc
        if not payload.get("ok"):
            raise StickerError(f"{method}: {payload.get('description', 'невідома помилка')}")
        return payload["result"]

    async def download(self, file_id) -> bytes:
        meta = await self.call("getFile", file_id=file_id)
        if int(meta.get("file_size") or 0) > MAX_FILE:
            raise StickerError("файл більший за ліміт Bot API (~20 МБ)")
        path = meta.get("file_path", "")
        pp = PurePosixPath(path)
        if not path or pp.is_absolute() or ".." in pp.parts or "://" in path:
            raise StickerError("небезпечний шлях файлу від Telegram")
        r = await self.c.get(f"https://api.telegram.org/file/bot{self.token}/{path}")
        r.raise_for_status()
        if len(r.content) > MAX_FILE:
            raise StickerError("файл більший за ліміт")
        return r.content


def client():
    return httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0), follow_redirects=False, trust_env=False)


def summarize(info: dict) -> dict:
    st = info.get("stickers") or []
    return {
        "name": info.get("name"),
        "title": info.get("title") or info.get("name"),
        "type": info.get("sticker_type", "regular"),
        "count": len(st),
        "static": sum(1 for s in st if not s.get("is_animated") and not s.get("is_video")),
        "animated": sum(1 for s in st if s.get("is_animated")),
        "video": sum(1 for s in st if s.get("is_video")),
        "signature": hashlib.sha1("|".join(s.get("file_unique_id", "") for s in st).encode()).hexdigest()[:16],
        "ids": [s.get("file_unique_id", "") for s in st],
    }


async def pack_info(token: str, name: str) -> dict:
    async with client() as c:
        return summarize(await Api(token, c).call("getStickerSet", name=name))


def to_png(data: bytes) -> bytes | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            out = io.BytesIO()
            im.save(out, "PNG", optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 — PNG-копія необов'язкова
        return None


class ZipParts:
    """Пише в послідовність ZIP-частин, не перевищуючи PART_BYTES; кожна частина — самостійний архів."""

    def __init__(self, folder: Path, stem: str):
        self.folder, self.stem = folder, stem
        self.paths: list[Path] = []
        self.zf = None
        self.size = 0
        self.count = 0

    def _open(self):
        p = self.folder / f"{self.stem}_{len(self.paths) + 1:02d}.zip"
        self.paths.append(p)
        self.zf = zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self.size, self.count = 0, 0

    def write(self, name: str, data: bytes):
        need = len(data) + 512
        if self.zf is None:
            self._open()
        elif self.count and self.size + need > PART_BYTES:
            self.zf.close()
            self._open()
        self.zf.writestr(name, data)
        self.size += need
        self.count += 1

    def close(self) -> list[Path]:
        if self.zf:
            self.zf.close()
            self.zf = None
        if len(self.paths) == 1:  # одна частина — без суфікса _01
            single = self.folder / f"{self.stem}.zip"
            self.paths[0].rename(single)
            self.paths = [single]
        return self.paths


async def export(token: str, packs: list[tuple[str, str]], folder: Path, mode: str = "one", png: bool = False,
                 progress=None) -> tuple[list[Path], dict]:
    """mode: one — один архів (теки за пакетами, повтори між пакетами прибрано); each — окремий архів на пакет.
    Повертає (шляхи ZIP-частин, звіт). Помилки окремих файлів записуються у звіт, а не замовчуються."""
    if not packs:
        raise StickerError("Не знайдено посилань t.me/addstickers/… або t.me/addemoji/…")
    folder.mkdir(parents=True, exist_ok=True)
    report = {"mode": mode, "packs": [], "errors": [], "files": 0, "duplicates": 0, "png": 0, "bytes": 0}
    hashes: dict[str, str] = {}
    total = 0
    paths: list[Path] = []
    sem = asyncio.Semaphore(PARALLEL)
    writer = ZipParts(folder, "stickers") if mode == "one" else None
    async with client() as c:
        api = Api(token, c)
        for pn, (kind, name) in enumerate(packs, 1):
            try:
                info = await api.call("getStickerSet", name=name)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"pack": name, "error": str(exc)})
                continue
            stickers = info.get("stickers") or []
            sm = summarize(info)
            entry = {"name": name, "kind": kind, "title": sm["title"], "type": sm["type"], "count": len(stickers),
                     "signature": sm["signature"], "items": [], "errors": []}
            w = writer or ZipParts(folder, safe_name(name))
            base = safe_name(name)
            done = 0

            async def grab(i, s):
                async with sem:
                    return i, s, await api.download(s["file_id"])

            tasks = [asyncio.create_task(grab(i, s)) for i, s in enumerate(stickers, 1)]
            results = {}
            for fut in asyncio.as_completed(tasks):
                try:
                    i, s, data = await fut
                    results[i] = (s, data)
                except Exception as exc:  # noqa: BLE001
                    entry["errors"].append(str(exc)[:200])
                done += 1
                if progress and (done % 10 == 0 or done == len(stickers)):
                    await progress(pn, len(packs), sm["title"], done, len(stickers))
            for i in sorted(results):
                s, data = results[i]
                total += len(data)
                if total > TOTAL_BYTES:
                    entry["errors"].append(f"зупинено: перевищено загальний ліміт {TOTAL_BYTES // 1048576} МБ")
                    break
                sha = hashlib.sha256(data).hexdigest()
                fname = f"{base}/{i:04d}{ext(s)}"
                item = {"position": i, "file": fname, "emoji": s.get("emoji", ""), "file_unique_id": s.get("file_unique_id"), "sha256": sha}
                if mode == "one" and sha in hashes:
                    item["duplicate_of"] = hashes[sha]
                    report["duplicates"] += 1
                else:
                    w.write(fname, data)
                    hashes[sha] = fname
                    report["files"] += 1
                    report["bytes"] += len(data)
                    if png and fname.endswith(".webp"):
                        p = to_png(data)
                        if p:
                            w.write(f"{base}/png/{i:04d}.png", p)
                            report["png"] += 1
                entry["items"].append(item)
            w.write(f"{base}/manifest.json", json.dumps(entry, ensure_ascii=False, indent=2).encode())
            report["packs"].append({k: v for k, v in entry.items() if k != "items"} | {"saved": len(entry["items"])})
            if writer is None:
                paths += w.close()
        if writer is not None:
            writer.write("manifest.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
            paths += writer.close()
    if not report["files"]:
        for p in paths:
            p.unlink(missing_ok=True)
        raise StickerError("Не завантажено жодного файлу: " + "; ".join(f"{e['pack']}: {e['error']}" for e in report["errors"])[:600])
    return paths, report
