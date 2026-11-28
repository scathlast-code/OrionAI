"""🎬 Студія для скачаного відео: 🛡 безпечна версія, 🔤 субтитри українською, ✂️ короткі вертикальні ролики.

Працює з тим самим відео, що щойно скачали (бот завантажує його ще раз у тимчасову теку):
  • 🛡 безпечна версія — ШІ прибирає музику (голос і живий звук лишаються, Demucs) і розмиває чужий водяний знак/логотип,
    якщо ШІ бачить його на кадрі (координати рамки — від моделі з зором);
  • 🔤 субтитри — розшифровка з часом (Groq/Gemini) → вшиті субтитри шрифтом Lora + файл .srt;
  • ✂️ ролики — ШІ за розшифровкою обирає 3–5 найцікавіших моментів (15–60 с) → вертикальні 1080×1920 з субтитрами.
"""

import base64
import json
import logging
import re
import tempfile
from pathlib import Path

log = logging.getLogger("orion.plugins.media.studio")

WM_SYSTEM = ("Ти бачиш кадр відео. Знайди чужий водяний знак, логотип телеканалу чи медіа, напис фотобанку (не субтитри й не вивіски в кадрі). "
             'Відповідай лише JSON: {"box":[x,y,w,h],"what":"логотип ТСН"} — частки від ширини й висоти кадру (0–1); '
             'немає — {"box":null}.')
CLIPS_SYSTEM = ("Ти редактор коротких відео для соцмереж новинного медіа. За розшифровкою з часом обери 3–5 найцікавіших, завершених "
                "за змістом моментів по 15–60 секунд, які зрозумілі без решти відео. Відповідай лише JSON: "
                '{"clips":[{"start":12.0,"end":40.5,"title":"коротко українською"}]}. Часи — секунди з розшифровки.')


def parse_box(answer):
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None, ""
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None, ""
    b = d.get("box")
    if not (isinstance(b, list) and len(b) == 4):
        return None, ""
    try:
        x, y, w, h = (max(0.0, min(1.0, float(v))) for v in b)
    except (TypeError, ValueError):
        return None, ""
    if w <= 0.01 or h <= 0.01 or w > 0.6 or h > 0.4:  # завелике — швидше за все не водяний знак
        return None, ""
    return (x, y, w, h), str(d.get("what") or "")[:60]


def delogo_filter(box, width, height):
    """ffmpeg delogo для рамки в частках кадру (з невеликим запасом, у межах кадру)."""
    x, y, w, h = box
    px, py = int(x * width) - 4, int(y * height) - 4
    pw, ph = int(w * width) + 8, int(h * height) + 8
    px, py = max(1, px), max(1, py)
    pw, ph = min(pw, width - px - 1), min(ph, height - py - 1)
    return f"delogo=x={px}:y={py}:w={pw}:h={ph}"


def parse_clips(answer, duration):
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("clips") or []
    except ValueError:
        return []
    out = []
    for c in items[:5]:
        try:
            a, b = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            continue
        a, b = max(0.0, a), min(duration or b, b)
        if b - a < 8:
            continue
        if b - a > 75:
            b = a + 60
        out.append((round(a, 1), round(b, 1), str(c.get("title") or "")[:60]))
    return out


def segs_text(segs, limit=6000):
    lines = [f"[{int(a // 60)}:{int(a % 60):02d}-{int(b // 60)}:{int(b % 60):02d}] {t}" for a, b, t in segs]
    return "\n".join(lines)[:limit]


def shift_segs(segs, start, end):
    """Шматок розшифровки для фрагмента, з часом від початку фрагмента."""
    return [(max(0.0, a - start), min(end, b) - start, t) for a, b, t in segs if b > start and a < end]


LOGO = Path(__file__).resolve().parents[1] / "newsroom" / "brand" / "logo_white.png"


def logo_filter(width):
    """Лого GreenPost у правому нижньому куті: ~22 % ширини кадру, відступ 4 %, легка тінь — читається на будь-якому тлі."""
    lw = max(120, int(width * 0.22))
    pad = max(16, int(width * 0.04))
    return (f"[1:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa=0.92[lg];"
            f"[lg]split[l1][l2];[l2]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.45,boxblur=3[sh];"
            f"[0:v][sh]overlay=W-w-{pad - 2}:H-h-{pad - 2}[v1];[v1][l1]overlay=W-w-{pad}:H-h-{pad}")


class StudioUI:
    """Міксин для плагіна «Медіа»: кнопки й завдання студії."""

    def studio_rows(self, job, risks=()):
        vis, aud, imgs = self.parts(job)
        if len(vis) - len(imgs) <= 0:
            return []
        tok = job.token
        row = [("🔤 Субтитри", self.cb("stu", tok, "subs")), ("✂️ Короткі ролики", self.cb("stu", tok, "clips"))]
        brand = [("🏷 Лого GreenPost", self.cb("stu", tok, "logo"))]
        if set(risks) & {"music", "music_post", "rights"} or any(str(r).startswith("why:") for r in risks):
            return [[("🛡 Безпечна версія", self.cb("stu", tok, "safe"))] + brand, row]
        return [row, [("🛡 Без музики й водяних знаків", self.cb("stu", tok, "safe"))] + brand]

    def brand_on(self, chat):
        return bool(self.pref(chat, "studio_logo", False))

    async def _studio_src(self, job, work):
        from dataclasses import replace
        from . import media as M
        vis, aud, imgs = self.parts(job)
        vids = [k for k in vis if k not in imgs]
        item = job.items[vids[0] - 1]
        opts = replace(job.options, mode="video", cuts=(), height=min(job.options.height or 1080, 1080))
        path, _t, info = await M.fetch(item, job.url, opts, work)
        return Path(path), item

    async def _timed(self, path, work):
        from .common import run
        import media as core
        aud = work / "a.mp3"
        await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
                  str(aud), timeout=600)
        segs, _prov = await core.transcribe_timed(aud.read_bytes(), "a.mp3")
        return [s for s in (segs or []) if s[1] > s[0]]

    async def _brand(self, src, out):
        from .common import run
        from . import media as M
        w, _h = await self._probe_wh(src)
        await run(*M.FF, "-i", src, "-i", str(LOGO), "-filter_complex", logo_filter(w or 1080), "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out, timeout=1800)
        return out

    async def _maybe_brand(self, chat, path):
        """«🏷 Лого на всіх результатах студії» — накладає лого на готовий файл перед надсиланням."""
        if not self.brand_on(chat):
            return path
        out = path.with_name(path.stem + " · GreenPost" + path.suffix)
        try:
            return await self._brand(path, out)
        except Exception:  # noqa: BLE001
            log.info("studio: лого не накладено", exc_info=True)
            return path

    async def _probe_wh(self, path):
        import asyncio
        proc = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                                    "-of", "csv=p=0", str(path), stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        try:
            w, h = (int(v) for v in out.decode().strip().split(",")[:2])
            return w, h
        except ValueError:
            return 0, 0

    async def studio_go(self, chat, job, what):
        import asyncio
        from .common import JobError, run
        from . import media as M
        from plugins.newsroom.tools import VERT_BLUR, sub_filter, to_srt
        esc = self._esc
        stop = [[("⏹ Зупинити", self.cb("stop"))]]
        title = {"safe": "🛡 Безпечна версія", "subs": "🔤 Субтитри", "clips": "✂️ Короткі ролики", "logo": "🏷 Лого GreenPost"}[what]
        status = await self._status(chat, None, f"{title}: завантажую відео…", stop)

        async def say(t):
            await self._status(chat, status, f"{title}: {t}", stop)
        try:
            with tempfile.TemporaryDirectory(prefix="orion-studio-") as work:
                work = Path(work)
                src, item = await self._studio_src(job, work)
                stem = Path(item.title[:50] or "video").stem.replace("/", "_")
                if what == "logo":
                    await say("накладаю лого…")
                    out = work / f"{stem} · GreenPost.mp4"
                    await self._brand(src, out)
                    await self.send_media(chat, out, esc("🏷 Відео з лого GreenPost"))
                elif what == "safe":
                    done = []
                    cur = src
                    if await self._has_audio(src):
                        await say("прибираю музику зі звуку (ШІ, ≈1–2 хв на хвилину відео)…")
                        voice, _music = await M.separate(src, work, progress=say)
                        nxt = work / "nomusic.mp4"
                        await run(*M.FF, "-i", cur, "-i", voice, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                                  "-b:a", "160k", "-shortest", "-movflags", "+faststart", nxt, timeout=900)
                        cur = nxt
                        done.append("музику прибрано")
                    await say("шукаю чужі водяні знаки…")
                    frames = await self._frames(cur, 1)
                    box, what_ = (None, "")
                    if frames:
                        try:
                            ans, _m = await asyncio.wait_for(self.host.ask_ai("Чи є на кадрі чужий водяний знак чи логотип?", system=WM_SYSTEM,
                                                                              label="🛡 Водяні знаки", image=base64.b64encode(frames[0]).decode()), 60)
                            box, what_ = parse_box(ans)
                        except Exception:  # noqa: BLE001
                            log.info("studio: водяний знак не перевірено", exc_info=True)
                    if box:
                        w, h = await self._probe_wh(cur)
                        if w and h:
                            nxt = work / "clean.mp4"
                            await run(*M.FF, "-i", cur, "-vf", delogo_filter(box, w, h), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                                      "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", nxt, timeout=1800)
                            cur = nxt
                            done.append(f"розмито: {what_ or 'водяний знак'}")
                    if not done:
                        await self._status(chat, status, f"{title}: музики й чужих водяних знаків не знайшов — відео вже чисте.",
                                           [[("« Медіа", self.cb())]])
                        return
                    out = work / f"{stem} · безпечна версія.mp4"
                    cur.rename(out)
                    await say("надсилаю…")
                    out = await self._maybe_brand(chat, out)
                    await self.send_media(chat, out, esc("🛡 Безпечна версія: " + ", ".join(done)
                                                          + ". Права на саме відео це не дає — потрібен дозвіл автора."))
                elif what == "subs":
                    await say("розшифровую мову…")
                    segs = await self._timed(src, work)
                    if not segs:
                        raise JobError("мову не розпізнано (немає мовлення або сервіс розшифровки недоступний)")
                    srt = work / f"{stem}.srt"
                    srt.write_text(to_srt(segs), "utf-8")
                    out = work / f"{stem} · субтитри.mp4"
                    await say("вшиваю субтитри…")
                    await run(*M.FF, "-i", src, "-vf", sub_filter(srt), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                              "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out, timeout=1800)
                    out = await self._maybe_brand(chat, out)
                    await self.send_media(chat, out, esc("🔤 Відео із субтитрами (шрифт Lora)"))
                    await self.send_doc(chat, srt, "📄 Субтитри окремо (.srt)")
                else:  # clips
                    await say("розшифровую мову…")
                    segs = await self._timed(src, work)
                    dur = await self._duration(src)
                    if not segs:
                        raise JobError("мову не розпізнано — без розшифровки ШІ не обере моменти")
                    await say("ШІ обирає найцікавіші моменти…")
                    ans, _m = await asyncio.wait_for(self.host.ask_ai(segs_text(segs), system=CLIPS_SYSTEM, label="✂️ Моменти"), 90)
                    clips = parse_clips(ans, dur)
                    if not clips:
                        raise JobError("ШІ не знайшов завершених моментів — спробуйте довше відео")
                    for i, (a, b, t) in enumerate(clips, 1):
                        await say(f"ролик {i} з {len(clips)}…")
                        part = shift_segs(segs, a, b)
                        vf = VERT_BLUR
                        if part:
                            srt = work / f"c{i}.srt"
                            srt.write_text(to_srt(part), "utf-8")
                            vf = vf + "[v0];[v0]" + sub_filter(srt)
                        out = work / f"{stem} · ролик {i}.mp4"
                        await run(*M.FF, "-ss", str(a), "-to", str(b), "-i", src, "-filter_complex", vf, "-c:v", "libx264", "-preset", "veryfast",
                                  "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-r", "30", "-movflags", "+faststart",
                                  out, timeout=1800)
                        out = await self._maybe_brand(chat, out)
                        await self.send_media(chat, out, esc(f"✂️ {i}/{len(clips)} · {int(a // 60)}:{int(a % 60):02d}–{int(b // 60)}:{int(b % 60):02d}"
                                                             + (f" · {t}" if t else "") + " · 1080×1920"))
            await self._status(chat, status, f"{title}: ✅ готово",
                               [[(("🏷 Лого на всіх результатах: так" if self.brand_on(chat) else "🏷 Лого на всіх результатах: ні"),
                                  self.cb("stulogo"))], [("« Медіа", self.cb())]])
        except JobError as exc:
            await self._status(chat, status, f"{title}: ⚠️ {esc(str(exc)[:300])}", [[("« Медіа", self.cb())]])
        except Exception as exc:  # noqa: BLE001
            log.exception("studio failed")
            await self._status(chat, status, f"{title}: ⚠️ не вдалося ({esc(type(exc).__name__)})", [[("« Медіа", self.cb())]])
