"""🗓 Контент-план редакції (таблиця Google / .xlsx у Google Диску) — лише ЧИТАННЯ: ШІ нічого в нього не пише.

Формат — календар: аркуш на місяць (рядок 1 — рік, рядок 2 — місяць), далі тижні: рядок із числами дат (пн…нд),
під ним — теми дня («Назва - Автор», кілька в клітинці через новий рядок), рядки «Сайт» і «smm» — що й де публікуємо."""
from __future__ import annotations

import datetime as dt
import io
import re

MONTHS = {"січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6, "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листоп": 11, "груд": 12}
WHERE = {"сайт": "Сайт", "smm": "SMM", "смм": "SMM", "соцмережі": "SMM"}


def file_id(url):
    m = re.search(r"/d/([\w-]{20,})", url or "") or re.search(r"[?&]id=([\w-]{20,})", url or "")
    return m.group(1) if m else ""


def download_urls(url):
    fid = file_id(url)
    return [f"https://docs.google.com/spreadsheets/d/{fid}/export?format=xlsx",
            f"https://drive.google.com/uc?export=download&id={fid}"] if fid else []


def _month_of(text):
    t = str(text or "").strip().lower()
    for k, v in MONTHS.items():
        if t.startswith(k):
            return v
    return None


def _split(cell):
    """Клітинка → [(назва, автор)]: «Всесвітній день медузи - Валентин»; кілька тем — через новий рядок."""
    out = []
    for line in re.split(r"[\n\r]+", str(cell or "")):
        line = line.strip(" •·-—\t")
        if len(line) < 3:
            continue
        m = re.match(r"^(.*\S)\s+[-–—]\s+([А-ЯІЇЄҐ][а-яіїєґ'’]+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'’]+)?)$", line)
        out.append((m.group(1), m.group(2)) if m else (line, ""))
    return out


def parse(data: bytes, report: list | None = None):
    """→ {"YYYY-MM-DD": [{"title", "author", "where"}]} з усіх аркушів-місяців. report — сюди підсумок по кожному аркушу."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    plan = {}
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        year = month = None
        for r in rows[:6]:  # рік і місяць — у перших рядках (або в назві аркуша)
            for c in r:
                if isinstance(c, (int, float)) and 2000 < int(c) < 2100:
                    year = year or int(c)
                elif isinstance(c, str):
                    if re.fullmatch(r"\s*20\d\d\s*", c):
                        year = year or int(c)
                    month = month or _month_of(c)
        title_m = _month_of(ws.title)
        month = month or title_m
        ym = re.search(r"20\d\d", ws.title or "")
        year = year or (int(ym.group(0)) if ym else None)
        if not (year and month):
            if report is not None:
                report.append(f"{ws.title}: не знайшов рік/місяць — пропущено")
            continue
        before = {k: len(v) for k, v in plan.items()}
        cols = {}  # колонка → дата поточного тижня
        prev_day = 0
        for r in rows:
            nums = {i: int(c) for i, c in enumerate(r) if isinstance(c, (int, float)) and 1 <= int(c) <= 31 and float(c).is_integer()}
            nums.update({i: int(c) for i, c in enumerate(r) if isinstance(c, str) and re.fullmatch(r"\s*\d{1,2}\s*", c) and 1 <= int(c) <= 31})
            filled = [c for c in r if c not in (None, "")]
            if nums and len(nums) >= max(1, len(filled) - 1):  # рядок дат тижня
                cols = {}
                for i, d in sorted(nums.items()):
                    m_ = month
                    if d < 8 and prev_day > 20:  # дні наступного місяця в останньому тижні
                        m_ = month % 12 + 1
                    elif d > 20 and not cols and prev_day == 0 and d > 24:  # дні попереднього місяця в першому тижні
                        m_ = (month - 2) % 12 + 1
                    y_ = year + (1 if m_ < month and month == 12 else 0) - (1 if m_ > month and month == 1 else 0)
                    try:
                        cols[i] = dt.date(y_, m_, d)
                    except ValueError:
                        continue
                    if m_ == month:
                        prev_day = d
                continue
            if not cols:
                continue
            label = str(r[0] or "").strip().lower() if r else ""
            where = next((v for k, v in WHERE.items() if label.startswith(k)), "")
            for i, c in enumerate(r):
                if i not in cols or c in (None, ""):
                    continue
                txt = str(c)
                if i == 0 and where:
                    txt = re.sub(r"(?i)^\s*(?:сайт|smm|смм)\s*", "", txt)
                for title, author in _split(txt):
                    if title.lower() in WHERE:
                        continue
                    day = cols[i]
                    if abs((day.year * 12 + day.month) - (year * 12 + month)) > 1:  # дата далеко від місяця аркуша — помилка розбору
                        continue
                    plan.setdefault(day.isoformat(), []).append({"title": title, "author": author, "where": where})
        if report is not None:
            got = {k: len(v) - before.get(k, 0) for k, v in plan.items() if len(v) > before.get(k, 0)}
            ks = sorted(got)
            report.append(f"{ws.title}: {month:02d}.{year} · днів {len(ks)}, тем {sum(got.values())}"
                          + (f" ({ks[0][8:]}.{ks[0][5:7]}–{ks[-1][8:]}.{ks[-1][5:7]}.{ks[-1][:4]})" if ks else ""))
    return plan
