"""🧰 Бекапи розробника: знімок коду окремо для кожного розділу (ядро, мініапп, кожен плагін…).

Знімок робиться, коли власник натискає «Старт» після оновлення бота — лише якщо код розділу змінився
з минулого знімка. Так зламану частину можна повернути саму, не відкочуючи всю програму.
Зберігаються в томі даних (переживають оновлення), по 15 останніх на розділ. Без секретів: лише код.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

import datadir

ROOT = Path(__file__).resolve().parent
KEEP = 15
SKIP_DIRS = {"__pycache__", "data", "logs", "tests", ".git", "node_modules", "history", "simulation", "simulations"}
CODE_EXT = {".py", ".html", ".js", ".css", ".json", ".txt", ".md", ".toml"}


def store():
    p = Path(datadir.path("devbackups", "ORION_DEVBACKUP_DIR"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def sections():
    """Розділ → (назва, [файли]). Кожен плагін і мініапп окремо; головний файл — окремо; решта модулів — разом."""
    out = {"core": ("🧠 Ядро бота (bot.py, меню, кнопки)", [ROOT / f for f in ("bot.py", "uilayout.py", "explain.py", "button_hints.py",
                                                                         "help_texts.py", "ui_layouts.py") if (ROOT / f).exists()])}
    app = [ROOT / "miniapp.py"] + sorted(p for p in (ROOT / "miniapp").rglob("*") if p.is_file() and p.suffix in CODE_EXT)
    out["miniapp"] = ("📱 Мініапп", [p for p in app if p.exists()])
    titles = {"newsroom": "📰 Новинарня (публікації, дні, контент-план)", "editorial": "🗞 Редакція", "weather": "🌤 Погода",
              "media": "⬇️ Завантаження медіа", "stickers": "🏷 Наліпки", "inbox": "📬 Пошта", "hub": "🤖 Мої боти"}
    for d in sorted((ROOT / "plugins").iterdir()):
        if d.is_dir() and d.name not in SKIP_DIRS:
            files = sorted(p for p in d.rglob("*") if p.is_file() and p.suffix in CODE_EXT and "__pycache__" not in p.parts)
            out[f"pl_{d.name}"] = (titles.get(d.name, f"🧩 {d.name}"), files)
    taken = {p for _t, fs in out.values() for p in fs}
    rest = sorted(p for p in ROOT.glob("*.py") if p not in taken and p.name != "__init__.py")
    out["modules"] = ("🧱 Службові модулі (решта .py)", rest)
    extra = [ROOT / f for f in ("requirements.txt", "Procfile", "railway.json", "nixpacks.toml", "Dockerfile", ".gitignore") if (ROOT / f).exists()]
    docs = sorted((ROOT / "docs").glob("*.md")) + sorted((ROOT / "tools").glob("*.py")) if (ROOT / "docs").exists() else []
    out["docs"] = ("📒 Журнал, промти й документація", docs)
    allf = sorted({p for _t, fs in out.values() for p in fs} | set(extra))
    out["full"] = ("📦 Повний знімок (увесь код, журнал, налаштування збирання)", allf)  # повний — поряд із частковими
    return out


def _digest(files):
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def index():
    f = store() / "index.json"
    try:
        return json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(data):
    (store() / "index.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def snapshot(reason="старт"):
    """Знімок розділів, код яких змінився з минулого разу. Повертає список назв знятих розділів."""
    idx, done = index(), []
    commit = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:10]
    for key, (title, files) in sections().items():
        if not files:
            continue
        dg = _digest(files)
        hist = idx.setdefault(key, [])
        if hist and hist[-1]["digest"] == dg:
            continue
        name = f"{key}-{time.strftime('%Y%m%d-%H%M%S')}-{dg[:6]}.zip"
        with zipfile.ZipFile(store() / name, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, str(p.relative_to(ROOT)))
        what = (os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE") or "").split("\n")[0][:300]  # що змінилося в цьому оновленні
        hist.append({"file": name, "digest": dg, "at": time.time(), "commit": commit, "reason": reason, "n": len(files), "what": what})
        for old in hist[:-KEEP]:
            try:
                (store() / old["file"]).unlink()
            except OSError:
                pass
        idx[key] = hist[-KEEP:]
        done.append(title)
    _save_index(idx)
    return done


def restore(key, file):
    """Повертає файли розділу з знімка (до наступного оновлення з GitHub). Перед цим знімає поточний стан."""
    src = store() / file
    if not src.exists() or not file.startswith(key + "-"):
        raise ValueError("Знімок не знайдено")
    snapshot("перед відновленням")
    n = 0
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            dest = (ROOT / info.filename).resolve()
            if ROOT not in dest.parents:
                continue  # нічого поза теками бота
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))
            n += 1
    return n


def zip_bytes(file):
    return (store() / file).read_bytes()


def diff_names(key, file):
    """Які файли відрізняються від поточних — щоб бачити, що саме поверне відновлення."""
    out = []
    with zipfile.ZipFile(store() / file) as z:
        for info in z.infolist():
            cur = ROOT / info.filename
            if not cur.exists() or cur.read_bytes() != z.read(info):
                out.append(info.filename)
    return out
