"""🦙 Керування власною Ollama з бота: стан, завантаження й видалення моделей.

Ollama працює поруч із ботами (сервіс `ollama` у docker-compose.yml) або на іншому комп'ютері — бот звертається до неї
за адресою OLLAMA_URL. Тут лише рідний API Ollama (/api/tags, /api/ps, /api/pull, /api/delete); саме спілкування з
моделлю йде через сумісний з OpenAI шлях /v1, як і з іншими сервісами.
"""
import json
import time

import httpx

# Моделі, що розумно працюють на 2 процесорах без відеокарти (Oracle Always Free). Розмір — скачування й приблизно стільки ж пам'яті.
RECOMMENDED = [
    ("gemma3:4b", "3,3 ГБ", "найкраща з малих: пише українською, бачить фото; ~3–5 слів/с"),
    ("qwen2.5:3b", "1,9 ГБ", "швидша, вміє інструменти (нотатки, нагадування)"),
    ("llama3.2:3b", "2,0 ГБ", "швидка, краще англійською"),
    ("gemma3:1b", "0,8 ГБ", "найшвидша, але найпростіша"),
]


def gb(n):
    return f"{(n or 0) / 1e9:.1f} ГБ".replace(".", ",")


async def status(url, timeout=8):
    """{'ok', 'error', 'models': [{name,size}], 'loaded': [{name,size,until}], 'version'}"""
    out = {"ok": False, "error": None, "models": [], "loaded": [], "version": None}
    if not url:
        out["error"] = "адресу не задано"
        return out
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{url}/api/tags")
            r.raise_for_status()
            out["models"] = [{"name": m.get("name"), "size": m.get("size", 0)} for m in r.json().get("models", [])]
            try:
                out["loaded"] = [{"name": m.get("name"), "size": m.get("size", 0), "until": m.get("expires_at")}
                                 for m in (await c.get(f"{url}/api/ps")).json().get("models", [])]
                out["version"] = (await c.get(f"{url}/api/version")).json().get("version")
            except (httpx.HTTPError, ValueError):
                pass
        out["ok"] = True
    except httpx.HTTPError as e:
        out["error"] = str(e) or type(e).__name__
    except ValueError:
        out["error"] = "відповідь не схожа на Ollama"
    return out


async def pull(url, model, on_progress=None, every=4.0):
    """Скачати модель. on_progress(text) викликається не частіше ніж раз на `every` с. Повертає (успіх, повідомлення)."""
    last, shown = 0.0, None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as c:
            async with c.stream("POST", f"{url}/api/pull", json={"model": model, "stream": True}) as r:
                if r.status_code >= 400:
                    return False, f"HTTP {r.status_code}: {(await r.aread()).decode(errors='ignore')[:200]}"
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("error"):
                        return False, d["error"]
                    st = d.get("status", "")
                    if d.get("total"):
                        pct = int(100 * (d.get("completed") or 0) / d["total"])
                        st = f"{pct}% з {gb(d['total'])}"
                    if st == "success":
                        return True, "готово"
                    if on_progress and st != shown and time.time() - last >= every:
                        last, shown = time.time(), st
                        await on_progress(st)
    except httpx.HTTPError as e:
        return False, str(e) or type(e).__name__
    return True, "готово"


async def delete(url, model):
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request("DELETE", f"{url}/api/delete", json={"model": model})
        return r.status_code < 400, (r.text[:200] if r.status_code >= 400 else "видалено")
    except httpx.HTTPError as e:
        return False, str(e) or type(e).__name__


async def warm(url, model):
    """Завантажити модель у пам'ять заздалегідь (перша відповідь без очікування). Повертає секунди або None."""
    t = time.time()
    try:
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(f"{url}/api/generate", json={"model": model, "prompt": "", "stream": False})
        return round(time.time() - t) if r.status_code < 400 else None
    except httpx.HTTPError:
        return None
