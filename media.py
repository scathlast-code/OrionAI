"""
Безкоштовні генератори для бота: зображення, відео-слайдшоу, музика.

Зображення (по черзі, перший, що спрацював):
  • Pollinations — без ключа (POLLINATIONS_TOKEN необов'язковий: прибирає водяний знак і обмеження швидкості);
  • Cloudflare Workers AI, FLUX.1 schnell — той самий акаунт, що й текстова модель (CF_API_TOKEN + CF_ACCOUNT_ID);
  • Canva — якщо Canva підключена (див. canva.py); витрачає ліміти вашої підписки Canva.
Відео: слайдшоу з картинок, підписів і музики, збирається на сервері бота через ffmpeg (безкоштовно).
Музика:
  • OpenRouter, безкоштовний варіант Lyria (експериментально: OpenRouter може прибрати його будь-коли);
  • ваш власний музичний сервер (music_server.py з MusicGen) на комп'ютері — MUSIC_LOCAL_URL.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import tempfile
from urllib.parse import quote

import httpx

env = os.environ.get
log = logging.getLogger("media")

POLLINATIONS_TOKEN = env("POLLINATIONS_TOKEN", "")
POLLINATIONS_MODEL = env("POLLINATIONS_MODEL", "flux")
CF_TOKEN, CF_ACCOUNT = env("CF_API_TOKEN", ""), env("CF_ACCOUNT_ID", "")
CF_IMAGE_MODEL = env("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
OPENROUTER_KEY = env("OPENROUTER_API_KEY", "")
OPENROUTER_MUSIC_MODEL = env("OPENROUTER_MUSIC_MODEL", "google/lyria-3-clip-preview")
MUSIC_LOCAL_URL = env("MUSIC_LOCAL_URL", "").rstrip("/")
IMAGE_ORDER = env("IMAGE_ORDER", "pollinations,cloudflare,canva").replace(" ", "").split(",")

IMAGE_PROVIDERS = {
    "pollinations": {"title": "Pollinations", "ready": lambda: True},
    "cloudflare": {"title": "Cloudflare FLUX", "ready": lambda: bool(CF_TOKEN and CF_ACCOUNT)},
    "canva": {"title": "Canva", "ready": lambda: False},  # вмикає canva.py, коли акаунт підключено
}
MUSIC_PROVIDERS = {
    "openrouter_music": {"title": "OpenRouter (Lyria)", "ready": lambda: bool(OPENROUTER_KEY) and env("OPENROUTER_MUSIC", "1") == "1"},
    "music_local": {"title": "Мій музичний сервер", "ready": lambda: bool(MUSIC_LOCAL_URL)},
}


class MediaError(Exception):
    def __init__(self, provider, kind, detail):
        super().__init__(f"{provider}: {kind}: {detail}")
        self.provider, self.kind, self.detail = provider, kind, detail


def ffmpeg_ok():
    return shutil.which("ffmpeg") is not None


def _kind(status, text):
    t = (text or "").lower()
    if status in (401, 403) and ("key" in t or "token" in t or "auth" in t):
        return "auth"
    if status == 402 or "insufficient" in t or "credit" in t:
        return "payment"
    if status == 429 or "rate limit" in t or "quota" in t:
        return "rate"
    if status == 404:
        return "model"
    if status >= 500:
        return "overloaded"
    return "bad_request"


# ---------------------------------------------------------------- зображення


async def image_pollinations(prompt, width, height, seed=None):
    params = f"width={width}&height={height}&nologo=true&model={quote(POLLINATIONS_MODEL)}"
    if seed is not None:
        params += f"&seed={seed}"
    headers = {"Authorization": f"Bearer {POLLINATIONS_TOKEN}"} if POLLINATIONS_TOKEN else {}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        r = await c.get(f"https://image.pollinations.ai/prompt/{quote(prompt)}?{params}", headers=headers)
    if r.status_code >= 400 or not r.headers.get("content-type", "").startswith("image/"):
        raise MediaError("pollinations", _kind(r.status_code, r.text[:300]), f"HTTP {r.status_code}: {r.text[:200]}")
    return r.content


async def image_cloudflare(prompt, width, height, seed=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CF_IMAGE_MODEL}"
    body = {"prompt": prompt, "steps": 4}
    if seed is not None:
        body["seed"] = seed
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, json=body)
    if r.status_code >= 400:
        raise MediaError("cloudflare", _kind(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        return base64.b64decode(r.json()["result"]["image"])
    except (ValueError, KeyError, TypeError):
        raise MediaError("cloudflare", "bad_response", r.text[:200])


CANVA_IMAGE = None  # canva.py підставляє сюди async-функцію (prompt) -> bytes


async def generate_image(prompt, width=1024, height=1024, order=None, seed=None):
    """Повертає (bytes, provider_id, [(provider, MediaError)])."""
    errors = []
    for pid in order or IMAGE_ORDER:
        if pid not in IMAGE_PROVIDERS or not IMAGE_PROVIDERS[pid]["ready"]():
            continue
        try:
            if pid == "pollinations":
                return await image_pollinations(prompt, width, height, seed), pid, errors
            if pid == "cloudflare":
                return await image_cloudflare(prompt, width, height, seed), pid, errors
            if pid == "canva" and CANVA_IMAGE:
                return await CANVA_IMAGE(prompt), pid, errors
        except MediaError as e:
            errors.append((pid, e))
        except httpx.TimeoutException:
            errors.append((pid, MediaError(pid, "timeout", "не відповів вчасно")))
        except httpx.HTTPError as e:
            errors.append((pid, MediaError(pid, "network", str(e) or type(e).__name__)))
    raise MediaError("all", "unavailable", "; ".join(f"{p}: {e.kind}" for p, e in errors) or "немає доступних генераторів")


# ---------------------------------------------------------------- музика


def _find_audio(obj):
    """Шукає в JSON-відповіді аудіо: data:audio/… URL або base64 у полі data поруч з audio-типом."""
    if isinstance(obj, str):
        m = re.match(r"data:audio/[\w.+-]+;base64,(.+)", obj, re.S)
        return base64.b64decode(m.group(1)) if m else None
    if isinstance(obj, dict):
        kind = json.dumps({k: v for k, v in obj.items() if isinstance(v, str) and len(v) < 100}).lower()
        if "audio" in kind and isinstance(obj.get("data"), str) and len(obj["data"]) > 1000:
            try:
                return base64.b64decode(obj["data"])
            except ValueError:
                pass
        for v in obj.values():
            found = _find_audio(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_audio(v)
            if found:
                return found
    return None


async def music_openrouter(prompt, seconds):
    body = {
        "model": OPENROUTER_MUSIC_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["audio", "text"],
    }
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json=body,
        )
    if r.status_code >= 400:
        raise MediaError("openrouter_music", _kind(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
    audio = _find_audio(r.json())
    if not audio:
        raise MediaError("openrouter_music", "bad_response", "у відповіді немає аудіо (можливо, змінився формат або модель)")
    return audio


async def music_local(prompt, seconds):
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{MUSIC_LOCAL_URL}/generate", json={"prompt": prompt, "seconds": seconds})
    if r.status_code >= 400:
        raise MediaError("music_local", _kind(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
    return r.content


async def generate_music(prompt, seconds=30):
    """Повертає (bytes, provider_id, errors)."""
    errors = []
    for pid, fn in (("openrouter_music", music_openrouter), ("music_local", music_local)):
        if not MUSIC_PROVIDERS[pid]["ready"]():
            continue
        try:
            return await fn(prompt, seconds), pid, errors
        except MediaError as e:
            errors.append((pid, e))
        except httpx.TimeoutException:
            errors.append((pid, MediaError(pid, "timeout", "не відповів вчасно")))
        except httpx.HTTPError as e:
            errors.append((pid, MediaError(pid, "network", str(e) or type(e).__name__)))
    raise MediaError("all", "unavailable", "; ".join(f"{p}: {e.kind}" for p, e in errors) or "немає підключених генераторів музики")


# ---------------------------------------------------------------- відео-слайдшоу


def _caption(image_bytes, text, size):
    """Підганяє картинку під розмір кадру і додає підпис унизу."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    w, h = size
    img = ImageOps.fit(Image.open(io.BytesIO(image_bytes)).convert("RGB"), (w, h))
    if text:
        draw = ImageDraw.Draw(img, "RGBA")
        font_size = max(28, w // 22)
        try:
            font = ImageFont.truetype(env("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), font_size)
        except OSError:
            font = ImageFont.load_default()
        words, lines, line = text.split(), [], ""
        for word in words:
            test = (line + " " + word).strip()
            if draw.textlength(test, font=font) > w * 0.86 and line:
                lines.append(line)
                line = word
            else:
                line = test
        if line:
            lines.append(line)
        lh = int(font_size * 1.3)
        box_h = lh * len(lines) + font_size
        draw.rectangle([0, h - box_h, w, h], fill=(0, 0, 0, 150))
        y = h - box_h + font_size // 2
        for ln in lines:
            draw.text(((w - draw.textlength(ln, font=font)) / 2, y), ln, font=font, fill=(255, 255, 255, 255))
            y += lh
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


async def make_slideshow(slides, music=None, size=(1080, 1920), seconds=4):
    """slides — [(image_bytes, caption)]. Повертає mp4 (bytes)."""
    if not ffmpeg_ok():
        raise MediaError("video", "bad_request", "на сервері немає ffmpeg")
    tmp = tempfile.mkdtemp(prefix="slides_")
    try:
        w, h = size
        fps = 25
        frames = seconds * fps
        inputs, filters = [], []
        for i, (img, cap) in enumerate(slides):
            path = os.path.join(tmp, f"s{i}.jpg")
            with open(path, "wb") as f:
                f.write(_caption(img, cap, size))
            inputs += ["-i", path]  # один кадр: zoompan сам робить із нього кліп потрібної довжини
            # повільне наближення (ефект Кена Бернса)
            filters.append(
                f"[{i}:v]scale={w * 2}:{h * 2},zoompan=z='min(zoom+0.0008,1.12)':d={frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},setsar=1[v{i}]"
            )
        n = len(slides)
        filters.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]")
        cmd = ["ffmpeg", "-y", *inputs]
        maps = ["-map", "[v]"]
        if music:
            mpath = os.path.join(tmp, "music.audio")
            with open(mpath, "wb") as f:
                f.write(music)
            cmd += ["-i", mpath]
            maps += ["-map", f"{n}:a", "-shortest", "-c:a", "aac", "-b:a", "160k"]
        out = os.path.join(tmp, "out.mp4")
        cmd += ["-filter_complex", ";".join(filters), *maps, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-movflags", "+faststart", out]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise MediaError("video", "bad_request", err.decode(errors="ignore")[-300:])
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- голос: розшифровка й озвучення

GROQ_KEY = env("GROQ_API_KEY", "")
STT_MODEL_GROQ = env("STT_MODEL_GROQ", "whisper-large-v3")
STT_MODEL_CF = env("STT_MODEL_CF", "@cf/openai/whisper-large-v3-turbo")
GEMINI_KEY = env("GEMINI_API_KEY", "")
GEMINI_STT_MODEL = env("GEMINI_STT_MODEL", "") or env("GEMINI_MODEL", "") or "gemini-flash-latest"
STT_PROVIDERS = {
    "groq_stt": {"title": "Groq Whisper", "ready": lambda: bool(GROQ_KEY)},
    "cloudflare_stt": {"title": "Cloudflare Whisper", "ready": lambda: bool(CF_TOKEN and CF_ACCOUNT)},
    "gemini_stt": {"title": "Gemini (аудіо й відео)", "ready": lambda: bool(GEMINI_KEY)},
}
MIME_BY_EXT = {"ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
               "aac": "audio/aac", "flac": "audio/flac", "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
               "mpeg": "video/mpeg", "3gp": "video/3gpp"}


_GEM_MODELS = {"t": 0, "list": []}


async def _gemini_flash_models():
    """Актуальні моделі Gemini «flash», що вміють generateContent (список від Google, кеш на 6 год)."""
    if _GEM_MODELS["list"] and time.time() - _GEM_MODELS["t"] < 6 * 3600:
        return _GEM_MODELS["list"]
    out = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}&pageSize=200")
        for m in (r.json().get("models") or []) if r.status_code == 200 else []:
            name = str(m.get("name") or "").removeprefix("models/")
            if "generateContent" in (m.get("supportedGenerationMethods") or []) and "flash" in name \
                    and not re.search(r"image|tts|live|embed|thinking|exp", name):
                out.append(name)
    except Exception:  # noqa: BLE001
        log.info("gemini: список моделей не отримано", exc_info=True)
    out.sort(key=lambda n: ("latest" not in n, "lite" in n, n), reverse=False)
    _GEM_MODELS.update(t=time.time(), list=out[:8])
    return _GEM_MODELS["list"]


async def stt_gemini(data, filename, language=None):
    """Розшифровка через Gemini: приймає і голосові, і відео (до ~20 МБ), будь-якою мовою — дослівно, без перекладу."""
    import base64 as _b64
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "ogg").lower()
    mime = MIME_BY_EXT.get(ext, "audio/ogg")
    if len(data) > 19 * 1024 * 1024:
        raise MediaError("gemini_stt", "too_big", "файл завеликий для розшифровки (до 19 МБ)")
    body = {"contents": [{"parts": [
        {"text": "Дослівно розшифруй мовлення з цього запису тією мовою, якою говорять (не перекладай). "
                 "Якщо це відео без мовлення — коротко опиши українською, що на ньому видно. Лише текст, без пояснень."},
        {"inline_data": {"mime_type": mime, "data": _b64.b64encode(data).decode()}}]}]}
    r = None
    # обрана модель може бути перевантажена (503) чи не приймати звук; назви моделей змінюються — беремо живий список від Google
    models = [GEMINI_STT_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"] + await _gemini_flash_models()
    tried = 0
    for model in dict.fromkeys(models):
        if tried >= 5:
            break
        tried += 1
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        for attempt in range(2):  # 503 «перевантажено» — ще раз за 3 с
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(url, json=body)
            if r.status_code != 503:
                break
            await asyncio.sleep(3)
        if r.status_code == 200:
            break
        log.warning("gemini_stt %s: HTTP %s %s", model, r.status_code, r.text[:200].replace(GEMINI_KEY or "§", "…"))
        if r.status_code in (401, 403):  # ключ не підходить — інші моделі не допоможуть
            break
    if r.status_code != 200:
        raise MediaError("gemini_stt", "bad_request" if r.status_code < 500 else "network", f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError):
        raise MediaError("gemini_stt", "empty", "порожня відповідь")
VOICES = {"polina": ("uk-UA-PolinaNeural", "Поліна"), "ostap": ("uk-UA-OstapNeural", "Остап")}


def tts_ready():
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None and ffmpeg_ok() and env("VOICE_TTS", "edge") == "edge"


async def stt_groq(data, filename, language):
    files = {"file": (filename, data)}
    form = {"model": STT_MODEL_GROQ, "response_format": "json"}
    if language:
        form["language"] = language
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            data=form,
            files=files,
        )
    if r.status_code >= 400:
        raise MediaError("groq_stt", _kind(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


async def stt_cloudflare(data, filename, language):
    body = {"audio": base64.b64encode(data).decode()}
    if language:
        body["language"] = language
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{STT_MODEL_CF}",
            headers={"Authorization": f"Bearer {CF_TOKEN}"},
            json=body,
        )
    if r.status_code >= 400:
        raise MediaError("cloudflare_stt", _kind(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        return (r.json()["result"].get("text") or "").strip()
    except (ValueError, KeyError, TypeError):
        raise MediaError("cloudflare_stt", "bad_response", r.text[:200])


async def transcribe(data, filename="voice.ogg", language=None):
    """Розшифровка голосу. Повертає (текст, провайдер, помилки)."""
    errors = []
    for pid, fn in (("groq_stt", stt_groq), ("cloudflare_stt", stt_cloudflare), ("gemini_stt", stt_gemini)):
        if not STT_PROVIDERS[pid]["ready"]():
            continue
        try:
            text = await fn(data, filename, language)
            if text:
                return text, pid, errors
            errors.append((pid, MediaError(pid, "empty", "порожня розшифровка")))
        except MediaError as e:
            errors.append((pid, e))
        except httpx.TimeoutException:
            errors.append((pid, MediaError(pid, "timeout", "не відповів вчасно")))
        except httpx.HTTPError as e:
            errors.append((pid, MediaError(pid, "network", str(e) or type(e).__name__)))
    raise MediaError("all", "unavailable", "; ".join(f"{p}: {e.kind}" for p, e in errors) or "немає сервісу розшифровки: потрібен GROQ_API_KEY, Cloudflare або GEMINI_API_KEY")


TIMED_RE = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:[-–]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?\]?\s*[-–:]?\s*(.+)$")


def _sec(a, b, c):
    if a is None:
        return None
    return int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b)


def parse_timed(text):
    """«[01:23-01:30] текст» / «01:23 текст» → [(початок, кінець, текст)] у секундах; кінець — початок наступного."""
    segs = []
    for ln in (text or "").splitlines():
        m = TIMED_RE.match(ln)
        if not m:
            if segs and ln.strip():
                s0, e0, t0 = segs[-1]
                segs[-1] = (s0, e0, (t0 + " " + ln.strip()).strip())
            continue
        start = _sec(m[1], m[2], m[3])
        end = _sec(m[4], m[5], m[6])
        segs.append((start, end, m[7].strip()))
    out = []
    for i, (s0, e0, t0) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        end = e0 if e0 and e0 > s0 else (nxt if nxt and nxt > s0 else s0 + 4)
        out.append((float(s0), float(end), t0))
    return out


async def transcribe_timed(data, filename="audio.mp3", language=None):
    """Розшифровка з часовими мітками: [(початок_с, кінець_с, текст)], провайдер. Groq — точні сегменти Whisper;
    Gemini — мітки, які модель ставить сама (приблизні, до секунди-двох)."""
    if STT_PROVIDERS["groq_stt"]["ready"]():
        form = {"model": STT_MODEL_GROQ, "response_format": "verbose_json"}
        if language:
            form["language"] = language
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {GROQ_KEY}"},
                             data=form, files={"file": (filename, data)})
        if r.status_code < 400:
            segs = [(float(x["start"]), float(x["end"]), (x.get("text") or "").strip()) for x in r.json().get("segments") or []]
            if segs:
                return [s for s in segs if s[2]], "groq_stt"
    if STT_PROVIDERS["gemini_stt"]["ready"]():
        import base64 as _b64
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "mp3").lower()
        if len(data) > 19 * 1024 * 1024:
            raise MediaError("gemini_stt", "too_big", "файл завеликий для розшифровки (до 19 МБ)")
        body = {"contents": [{"parts": [
            {"text": "Дослівно розшифруй мовлення тією мовою, якою говорять (не перекладай). Кожну фразу — з нового рядка у форматі "
                     "[ММ:СС-ММ:СС] текст (час від початку запису). Якщо говорять кілька людей — на початку фрази «Мовець 1:», «Мовець 2:». "
                     "Лише розшифровка, без пояснень."},
            {"inline_data": {"mime_type": MIME_BY_EXT.get(ext, "audio/mpeg"), "data": _b64.b64encode(data).decode()}}]}]}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_STT_MODEL}:generateContent?key={GEMINI_KEY}"
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(url, json=body)
        if r.status_code == 200:
            try:
                txt = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError, ValueError):
                txt = ""
            segs = parse_timed(txt)
            if segs:
                return segs, "gemini_stt"
    text, prov, _err = await transcribe(data, filename, language)
    return [(0.0, 0.0, text)], prov


async def tts(text, voice="polina", rate=0):
    """Озвучення українським голосом (edge-tts) у форматі голосового повідомлення Telegram (OGG/Opus)."""
    if not tts_ready():
        raise MediaError("tts", "unavailable", "озвучення не налаштоване (потрібні edge-tts і ffmpeg)")
    import edge_tts

    text = re.sub(r"\s+", " ", text)[:3000]
    comm = edge_tts.Communicate(text, VOICES.get(voice, VOICES["polina"])[0], rate=f"{rate:+d}%")
    mp3 = bytearray()
    try:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
    except Exception as e:  # noqa: BLE001
        raise MediaError("tts", "network", str(e)[:200])
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(bytes(mp3))
    if proc.returncode != 0 or not out:
        raise MediaError("tts", "bad_request", err.decode(errors="ignore")[-200:])
    return out
