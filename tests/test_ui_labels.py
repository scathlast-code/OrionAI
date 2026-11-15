"""Анти-хаос: одна назва кнопки = одна дія. Перевіряє всі екрани, зняті пісочницею зі справжнього коду."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def action(d):
    p = d.split(":")
    if d.startswith("m:pl:") and len(p) > 3:
        return ":".join(p[2:4])
    if d.startswith("m:pl:"):
        return ":".join(p[:3])
    if d.startswith(("m:grp:", "m:grpx:")):
        return "grp:" + p[2]
    return ":".join(p[:2])


def norm(label):
    t = re.sub(r"[·:]\s*\d+.*$", "", label)          # лічильники «· 12»
    t = re.sub(r"^\d+[.\s]", "", t)
    return t.replace("●", "").replace("▼", "").replace("▲", "").strip()


# Одне слово — одна дія; виняток — та сама дія для різних об'єктів (написати, сортувати, інтервал, видалити).
SAME = [{"news:Dgo", "news:Fw", "news:obw"}, {"news:Tgs", "news:Fl"}, {"news:Tgiv", "news:Liv", "hub:mge"}, {"news:Edel", "news:Fdel", "news:act", "news:acd", "news:Tpdel", "news:delok", "news:Ldel", "news:sodel", "news:Tgdel", "news:csd", "news:ctd"}]
EDITORS = (":btn", ":bi", ":brl", ":bg")   # конструктор меню показує чужі підписи як елементи списку


def same(a, b):
    return any(a in g and b in g for g in SAME)


def test_one_label_one_action(tmp_path):
    env = dict(os.environ, SANDBOX_OUT=str(tmp_path))
    r = subprocess.run([sys.executable, "sandbox_build.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    data = json.loads((tmp_path / "sandbox_screens.json").read_text("utf-8"))
    seen, clashes = {}, []
    for key, scr in data["screens"].items():
        if not key.startswith(("m:pl", "m:grp:plugins")) or any(e in key for e in EDITORS):
            continue
        for row in scr["rows"]:
            for label, d in row:
                if not d or d.startswith(("m:nav", "url:", "app:", "m:cancel", "m:pl:hub:~")) or label.startswith("«") or "Скасувати" in label or label in ("◀️ Назад", "🏠 Додому", "Вперед ▶️"):  # навігація: одна назва, ціль — попередній екран
                    continue
                n = norm(label)
                if not n or re.fullmatch(r"[\d ⋯⬅️➡️⏫⬆️⬇️⏬🗑]+", n) or n.startswith(("🟢", "⚪️", "⏸", "▶️", "📁 ", "📋 ", "🏠 #", "#")):
                    continue  # елементи списків, номери, перемикачі стану
                a = action(d)
                if n in seen and seen[n][0] != a and not same(seen[n][0], a):
                    clashes.append(f"«{n}»: {seen[n][1]} → {seen[n][0]}  vs  {key} → {a}")
                seen.setdefault(n, (a, key))
    assert not clashes, "Однакові назви для різних дій:\n" + "\n".join(sorted(set(clashes)))
