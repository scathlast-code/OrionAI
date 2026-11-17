"""YouTube-specific helpers for OrionAI media downloader.

Goals:
- Prefer on-demand / lightweight PO Token path (bgutil-ytdlp-pot-provider).
- Keep --no-plugin-dirs for all other sites.
- Controlled client sequence + limited retries for datacenter IPs (Railway).
- Semaphore so yt-dlp + FFmpeg do not run in parallel beyond memory budget.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import site
from typing import Sequence

# Max concurrent heavy media jobs (yt-dlp download or FFmpeg post-process).
# 1 is safest under 1 GB Railway RAM.
HEAVY_SLOTS = int(os.environ.get("MEDIA_HEAVY_SLOTS", "1"))
_heavy_sem: asyncio.Semaphore | None = None

BOT_CHECK = re.compile(
    r"sign in to confirm|not a bot|confirm you.?re not|po.?token|login.?required",
    re.I,
)

# Client order tuned for datacenter IPs. First entry is preferred.
YT_CLIENT_SETS: Sequence[str] = (
    "mweb,tv,web_safari,web_embedded",
    "tv,mweb,web_embedded",
    "web_safari,mweb",
    "default,tv",
)


def _plugin_dirs() -> list[str]:
    """Return directories that contain yt_dlp_plugins (bgutil installs here)."""
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
        # Some installs put the package itself as the plugin root
        p2 = pathlib.Path(base)
        if (p2 / "yt_dlp_plugins").is_dir() or any(p2.glob("**/yt_dlp_plugins")):
            dirs.append(str(p2))
    # Deduplicate while preserving order
    seen = set()
    out = []
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
    """Build yt-dlp argument list for a YouTube call.

    When allow_plugins=True we do NOT pass --no-plugin-dirs and we add
    --plugin-dirs pointing at the installed bgutil plugin location so that
    the PO Token provider can be loaded only for YouTube.
    """
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


def is_bot_check(text: str) -> bool:
    return bool(BOT_CHECK.search(str(text or "")))


def explain_youtube(raw: str, has_proxy: bool = False) -> str:
    """User-facing Ukrainian message for YouTube-specific failures."""
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
    """Process-local semaphore for yt-dlp / FFmpeg."""
    global _heavy_sem
    if _heavy_sem is None:
        _heavy_sem = asyncio.Semaphore(max(1, HEAVY_SLOTS))
    return _heavy_sem


class HeavyJob:
    """Async context manager that limits concurrent heavy media work."""

    def __init__(self):
        self._sem = heavy_semaphore()

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False
