"""🔗 Інструменти для посилання: куди насправді веде, чи безпечне, чисте посилання без стеження, QR-код.

Нічого не відкриває в браузері й не виконує сторінку — лише читає заголовки переадресацій.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Параметри стеження, які можна прибрати без шкоди для сторінки
TRACK_PARAMS = {"fbclid", "gclid", "dclid", "gbraid", "wbraid", "yclid", "igsh", "igshid", "mibextid", "si", "xmt",
                "mc_cid", "mc_eid", "_hsenc", "_hsmi", "ref_src", "ref_url", "s", "t", "share_id", "rdid", "rdt"}
TRACK_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")
# Параметри, які на деяких сайтах — частина адреси (не чіпаємо там)
KEEP_ON = {"youtube.com": {"si"}, "x.com": {"s", "t"}, "twitter.com": {"s", "t"}}
KEEP_ALWAYS = {"youtu.be": set()}

SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy", "short.io",
              "shorturl.at", "tiny.cc", "lnkd.in", "fb.me", "vk.cc", "clck.ru", "s.id", "rebrand.ly", "t.ly", "bl.ink"}
# Ресурси держав-агресорів і їхніх союзників — попереджаємо, не відкриваємо
BAD_TLDS = (".ru", ".su", ".by", ".ir", ".xn--p1ai", ".xn--90ais", ".xn--mgba3a4f16a")
BAD_HOSTS = ("vk.com", "vk.ru", "ok.ru", "mail.ru", "yandex.", "ya.ru", "dzen.ru", "rutube.ru", "vkvideo.ru", "vk.cc", "clck.ru")


def host_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_bad(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return h.endswith(BAD_TLDS) or any(h == b or h.endswith("." + b) or (b.endswith(".") and (h.startswith(b) or ("." + b) in h))
                                       for b in BAD_HOSTS)


REDIRECTORS = {"l.instagram.com": "u", "l.facebook.com": "u", "lm.facebook.com": "u", "l.threads.net": "u", "l.threads.com": "u",
               "l.messenger.com": "u", "out.reddit.com": "url", "away.vk.com": "to"}


def unwrap(url: str) -> str:
    """Посилання-обгортки соцмереж (l.instagram.com/?u=…&e=…, l.facebook.com/l.php?u=…) → справжня адреса без стеження.
    Instagram так «загортає» кожне посилання зі сторіс і підписів, щоб рахувати переходи."""
    for _ in range(3):
        try:
            p = urlsplit(url)
        except ValueError:
            return url
        key = REDIRECTORS.get(host_of(url))
        inner = dict(parse_qsl(p.query)).get(key) if key else None
        if not inner or not inner.startswith(("http://", "https://")):
            break
        url = inner
    return clean_url(url)


def clean_url(url: str) -> str:
    """Прибирає параметри стеження (utm_*, fbclid, igsh…), решту адреси залишає як є."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    host = host_of(url)
    keep = set()
    for dom, ks in KEEP_ON.items():
        if host == dom or host.endswith("." + dom):
            keep |= ks
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k in keep or not (k.lower() in TRACK_PARAMS or k.lower().startswith(TRACK_PREFIXES))]
    frag = "" if p.fragment.lower().startswith(("utm_", "xtor=")) else p.fragment
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q, doseq=True), frag))


def verdict(chain: list[str], status: int | None = None, error: str = "") -> dict:
    """Оцінка за ланцюжком переадресацій (перший — те, що надіслали, останній — куди веде)."""
    first, last = chain[0], chain[-1]
    fh, lh = host_of(first), host_of(last)
    warn, info = [], []
    hosts = [host_of(u) for u in chain]
    if any(is_bad(h) for h in hosts):
        warn.append("веде на ресурс держави-агресора чи її союзника — краще не відкривати")
    try:
        ipaddress.ip_address(lh)
        warn.append("замість назви сайту — голі цифри адреси (так часто маскують підробки)")
    except ValueError:
        pass
    if any(part.startswith("xn--") for part in lh.split(".")):
        warn.append("у назві сайту літери з інших абеток — може підробляти відомий сайт")
    if urlsplit(last).scheme == "http":
        warn.append("сторінка без шифрування (http) — не вводьте там паролі й дані картки")
    if "@" in (urlsplit(first).netloc or ""):
        warn.append("у посиланні є «@» перед назвою сайту — класичний прийом підробки")
    if len(chain) > 4:
        warn.append(f"дуже довгий ланцюжок переадресацій ({len(chain) - 1})")
    if fh in SHORTENERS or fh.endswith(".short.gy"):
        info.append("це скорочене посилання — справжня адреса нижче")
    if fh != lh:
        info.append(f"насправді веде на {lh or 'інший сайт'}")
    if status and status >= 400:
        info.append(f"сторінка відповідає помилкою {status}" + (" (не знайдено)" if status == 404 else ""))
    if error:
        info.append(error)
    level = "bad" if warn else "ok"
    return {"first": first, "last": last, "chain": chain, "warn": warn, "info": info, "level": level,
            "clean": clean_url(last), "status": status}


async def expand(url: str, client=None, hops: int = 10) -> dict:
    """Іде по переадресаціях вручну (HEAD, за потреби GET без тіла) і повертає verdict()."""
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
    chain, status, error = [url], None, ""
    try:
        cur = url
        for _ in range(hops):
            if is_bad(host_of(cur)):  # туди не звертаємося зовсім
                break
            try:
                r = await client.head(cur, follow_redirects=False)
                if r.status_code in (403, 405, 501) or (r.status_code >= 400 and r.status_code != 404):
                    async with client.stream("GET", cur, follow_redirects=False) as r2:
                        r = r2
            except httpx.HTTPError as e:
                error = "сайт не відповів" if isinstance(e, httpx.TimeoutException) else "сайт недоступний"
                break
            status = r.status_code
            loc = r.headers.get("location")
            if r.is_redirect and loc:
                nxt = str(httpx.URL(cur).join(loc))
                if nxt in chain:
                    error = "переадресація по колу"
                    break
                chain.append(nxt)
                cur = nxt
                continue
            break
    finally:
        if own:
            await client.aclose()
    return verdict(chain, status, error)


def report_html(v: dict, esc) -> str:
    icon = "⛔️" if v["level"] == "bad" else "✅"
    lines = [f"{icon} <b>Перевірка посилання</b>"]
    if v["level"] == "ok":
        lines.append("Явних ознак підробки чи небезпеки не видно.")
    for w in v["warn"]:
        lines.append(f"⚠️ {esc(w)}")
    for i in v["info"]:
        lines.append(f"• {esc(i)}")
    if len(v["chain"]) > 1:
        lines.append("\n<b>Маршрут:</b>")
        for n, u in enumerate(v["chain"]):
            lines.append(f"{n}. <code>{esc(u[:200])}</code>")
    if v["clean"] != v["chain"][0]:
        lines.append(f"\n🧹 <b>Чисте посилання</b> (без стеження):\n<code>{esc(v['clean'][:900])}</code>")
    return "\n".join(lines)


def qr_png(url: str, scale: int = 10) -> bytes:
    import segno
    from io import BytesIO
    buf = BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()
