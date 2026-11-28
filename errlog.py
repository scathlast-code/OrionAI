"""🧯 Журнал помилок: усе, що зламалось, в одному місці — щоб ШІ (бота чи розробника) бачив, де і що виправляти.

Джерела:
  • «журнал» — помилки (ERROR) і важливі попередження з журналу бота: модуль, текст, останній рядок трасування;
  • «сторож» — збої сервісів від 🩺 сторожа здоров'я (розшифровка, Shazam, контент-план…);
  • «людина» — те, що власник сам написав боту («баг …», скриншот із підписом, позначки в «💡 Покращити»).
Однакові записи за годину зливаються (лічильник ×N). Файл JSONL у томі даних, останні 3000 записів.
Кожен запис ще й одним рядком «🧯 ERRLOG …» у журналі сервера — розробницький ШІ знаходить їх фільтром.
Секретів не пишемо: ключі й токени в тексті маскуються.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import datadir

log = logging.getLogger("orion.errlog")
KEEP = 3000
MERGE_S = 3600
_lock = threading.Lock()
SECRET_RE = re.compile(r"(?i)(key|token|secret|password|passwd|api_key|apikey|authorization)([=:\s\"']+)([^\s\"'&]{6,})")
LONG_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
NOISY = re.compile(r"(?i)Unclosed client session|Unclosed connector|Aiohttp retry client was not closed|libssl detected|"
                   r"xing header|shazamio_core|Recognizer created")


def path():
    p = Path(datadir.path("errlog", "ORION_ERRLOG_DIR"))
    p.mkdir(parents=True, exist_ok=True)
    return p / "errlog.jsonl"


def clean(text):
    t = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}…", str(text or ""))
    return LONG_RE.sub("…", t)[:1500]


_cache = {"rows": None, "flushed": 0.0, "dirty": False}


def _read():
    if _cache["rows"] is None:
        p = path()
        rows = []
        if p.is_file():
            for line in p.read_text("utf-8", "replace").splitlines()[-KEEP:]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        _cache["rows"] = rows
    return _cache["rows"]


def _flush(force=False):
    """Запис на диск не частіше разу на 20 с (журнал викликається з основного потоку — без гальм)."""
    if not _cache["dirty"] or (not force and time.time() - _cache["flushed"] < 20):
        return
    p = path()
    tmp = p.with_suffix(".tmp")
    rows = _cache["rows"][-KEEP:]
    _cache["rows"] = rows
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")
    tmp.replace(p)
    _cache["flushed"], _cache["dirty"] = time.time(), False


def add(source, text, where="", level="error", screen=""):
    """Записати збій. source: журнал | сторож | людина."""
    text = clean(text).strip()
    if not text or NOISY.search(text):
        return
    now = time.time()
    key = f"{source}|{where}|{re.sub(r'[0-9]+', '#', text[:160])}"
    new = True
    with _lock:
        try:
            rows = _read()
            for r in reversed(rows[-200:]):
                if r.get("key") == key and now - r.get("last", 0) < MERGE_S:
                    r["n"] = r.get("n", 1) + 1
                    r["last"] = now
                    new = False
                    break
            if new:
                rows.append({"ts": now, "last": now, "n": 1, "source": source, "level": level, "where": where[:80],
                             "screen": screen[:120], "text": text, "key": key})
            _cache["dirty"] = True
            _flush(force=source == "людина")
        except Exception:  # noqa: BLE001 — журнал помилок не має сам ламати бота
            return
    if not new:
        return
    try:  # один рядок у журнал сервера — розробницький ШІ знаходить фільтром «ERRLOG»
        logging.getLogger("orion.errlog.line").warning("🧯 ERRLOG [%s] %s%s %s", source, where[:40],
                                                       f" @{screen[:40]}" if screen else "", text.splitlines()[0][:300])
    except Exception:  # noqa: BLE001
        pass


def recent(limit=50, source=None, since=None):
    with _lock:
        rows = list(_read())
        try:
            _flush(force=True)
        except Exception:  # noqa: BLE001
            pass
    if source:
        rows = [r for r in rows if r.get("source") == source]
    if since:
        rows = [r for r in rows if r.get("last", 0) >= since]
    return rows[-limit:][::-1]


def clear():
    with _lock:
        _cache["rows"], _cache["dirty"] = [], True
        _flush(force=True)


def fmt_time(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def summary_for_ai(hours=72, limit=40):
    """Стислий перелік збоїв для ШІ: що, де, скільки разів, коли востаннє; спершу те, що повідомила людина."""
    rows = recent(500, since=time.time() - hours * 3600)
    if not rows:
        return ""
    rows.sort(key=lambda r: (r.get("source") != "людина", -r.get("last", 0)))
    lines = []
    for r in rows[:limit]:
        first = r["text"].splitlines()[0][:220]
        lines.append(f"- [{r['source']}] {fmt_time(r['last'])}" + (f" ×{r['n']}" if r.get("n", 1) > 1 else "")
                     + (f" · {r['where']}" if r.get("where") else "") + (f" · екран {r['screen']}" if r.get("screen") else "") + f": {first}")
    return "\n".join(lines)


def export_md(hours=24 * 14):
    rows = recent(KEEP, since=time.time() - hours * 3600)
    out = ["# 🧯 Журнал помилок OrionAI", "",
           f"Записів: {len(rows)} за {hours // 24} дн. Джерела: людина — повідомила сама; сторож — збої сервісів; журнал — помилки коду.", ""]
    for r in rows:
        out.append(f"## [{r['source']}] {fmt_time(r['ts'])}–{fmt_time(r['last'])} ×{r.get('n', 1)} · {r.get('where') or '—'}"
                   + (f" · екран {r['screen']}" if r.get("screen") else ""))
        out.append("```")
        out.append(r["text"])
        out.append("```")
        out.append("")
    return "\n".join(out).encode("utf-8")


class Handler(logging.Handler):
    """ERROR (і WARNING з модулів бота) — у журнал помилок; власні рядки ERRLOG і шум бібліотек — ні."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record):
        try:
            if record.name.startswith("orion.errlog"):
                return
            if record.levelno < logging.ERROR and not record.name.startswith(("orion", "media")):
                return
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                msg += f"\n{type(exc).__name__}: {str(exc)[:400]}"
                tb = record.exc_info[2]
                while tb and tb.tb_next:
                    tb = tb.tb_next
                if tb:
                    msg += f"\n  у {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno} ({tb.tb_frame.f_code.co_name})"
            add("журнал", msg, where=record.name, level=record.levelname.lower())
        except Exception:  # noqa: BLE001
            pass


def install():
    root = logging.getLogger()
    if not any(isinstance(h, Handler) for h in root.handlers):
        root.addHandler(Handler())
