"""Одна тека для всіх даних OrionAI.

Усі допоміжні бази (статистика, UTM, держпосади, доступи, режими тощо) лежать поруч з основною базою —
там, де DB_PATH (у Docker і на Railway це диск /data). Раніше частина з них створювалась у теці з кодом
(`data/…` або поруч із .py) і зникала після кожного оновлення сервера, не потрапляла в копії.
"""
import os
import shutil


def base() -> str:
    main = os.environ.get("DB_PATH") or os.environ.get("MANAGER_DB_PATH") or "data/bot.db"
    d = os.environ.get("ORION_DATA_DIR") or os.path.dirname(os.path.abspath(main)) or "."
    os.makedirs(d, exist_ok=True)
    return d


def path(name: str, env: str | None = None, old: str | None = None) -> str:
    """Шлях до файлу даних. env — змінна, якою шлях можна задати вручну; old — старе місце (перенесу один раз)."""
    if env and os.environ.get(env):
        p = os.environ[env]
        os.makedirs(os.path.dirname(os.path.abspath(p)) or ".", exist_ok=True)
        return p
    new = os.path.join(base(), name)
    for cand in (old, os.path.join("data", name)):
        if cand and not os.path.exists(new) and os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(new):
            try:
                (shutil.copytree if os.path.isdir(cand) else shutil.copy2)(cand, new)
            except OSError:
                pass
    return new
