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
from . import youtube_pot as ytp

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
