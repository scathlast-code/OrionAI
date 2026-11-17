"""YouTube-specific helpers for OrionAI media downloader.

Goals:
- Prefer on-demand / lightweight PO Token path (bgutil-ytdlp-pot-provider).
- Keep --no-plugin-dirs for all other sites.
- Controlled client sequence + limited retries for datacenter IPs (Railway).
- Semaphore so yt-dlp + FFmpeg do not run in parallel beyond memory budget.
- Shared argument builder used by both metadata probe and actual download.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import site
from typing import Sequence
from urllib.parse import urlsplit

HEAVY_SLOTS = int(os.environ.get("MEDIA_HEAVY_SLOTS", "1"))
_heavy_sem: asyncio.Semaphore | None = None

BOT_CHECK = re.compile(
    r"sign in to confirm|not a bot|confirm you.?re not|po.?token|login.?required",
    re.I,
)

YT_CLIENT_SETS: Sequence[str] = (
    "mweb,tv,web_safari,web_embedded",
    "tv,mweb,web_embedded",
    "web_safari,mweb",
    "default,tv",
)


def is_youtube(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"


def _plugin_dirs() -> list[str]:
    dirs: list[str] = []
    candidates = []
    try:
        candidates.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        candidates.append(site.getusersitepackages())
    except Exception:
        pass
    for base in candidates:
        if not base:
            continue
        p = pathlib.Path(base) / "yt_dlp_plugins"
        if p.is_dir():
            dirs.append(str(p))
        p2 = pathlib.Path(base)
        if (p2 / "yt_dlp_plugins").is_dir():
            dirs.append(str(p2))
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def yt_args(
    *,
    allow_plugins: bool = True,
    client_set: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    args: list[str] = [
        "yt-dlp",
        "--ignore-config",
        "--no-cache-dir",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--retries",
        "2",
        "--js-runtimes",
        "node",
    ]
    if allow_plugins:
        for d in _plugin_dirs():
            args += ["--plugin-dirs", d]
    else:
        args.insert(2, "--no-plugin-dirs")

    if client_set:
        args += ["--extractor-args", f"youtube:player_client={client_set}"]
    args.extend(extra)
    return args


def probe_command(url: str, client_set: str | None = None, allow_plugins: bool = True) -> list[str]:
    return yt_args(
        allow_plugins=allow_plugins,
        client_set=client_set or YT_CLIENT_SETS[0],
        extra=(
            "--dump-single-json",
            "--skip-download",
            "--flat-playlist",
            "--yes-playlist",
            "--",
            url,
        ),
    )


def download_base_args(
    *,
    client_set: str | None = None,
    allow_plugins: bool = True,
    extra: Sequence[str] = (),
) -> list[str]:
    return yt_args(
        allow_plugins=allow_plugins,
        client_set=client_set or YT_CLIENT_SETS[0],
        extra=extra,
    )


def is_bot_check(text: str) -> bool:
    return bool(BOT_CHECK.search(str(text or "")))


def explain_youtube(raw: str, has_proxy: bool = False) -> str:
    raw = str(raw or "").strip()
    if is_bot_check(raw):
        msg = (
            "YouTube не пускає сервер бота (Railway): просить підтвердити, що ви не бот. "
            "Це блокування IP дата-центру.\n"
            "Спроби з PO Token і іншими клієнтами не допомогли."
        )
        if has_proxy:
            msg += "\nПроксі MEDIA_PROXY теж заблоковано — потрібен residential-проксі."
        else:
            msg += (
                "\n🔧 Найнадійніше рішення зараз — residential-проксі "
                "(змінна MEDIA_PROXY у Railway) або cookies з реального акаунта."
            )
        return msg
    low = raw.lower()
    if "private" in low or "login" in low or "sign in" in low:
        return "Відео приватне або вимагає входу. Додайте cookies YouTube у «🍪 Входи на сайти»."
    if "unavailable" in low or "removed" in low or "404" in low:
        return "Відео недоступне: видалене, заблоковане в регіоні сервера або посилання неповне."
    if "429" in low or "rate" in low:
        return "YouTube тимчасово обмежив запити. Спробуйте через кілька хвилин."
    return "Не вдалося завантажити з YouTube."


def heavy_semaphore() -> asyncio.Semaphore:
    global _heavy_sem
    if _heavy_sem is None:
        _heavy_sem = asyncio.Semaphore(max(1, HEAVY_SLOTS))
    return _heavy_sem


class HeavyJob:
    def __init__(self):
        self._sem = heavy_semaphore()

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False
