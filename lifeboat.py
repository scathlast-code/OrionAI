"""🛟 Нічого не губиться під час оновлень.

1. 🧾 Журнал дій — усе, що роблять у боті й застосунку (кнопки, команди, повідомлення, дії застосунку), пишеться
   в теку даних помісячними файлами JSONL. Секретні введення (ключі, паролі, токени) не записуються.
2. 💤 Стан у пам'яті (на що бот чекає відповіді, хто «підстраховує» мовчазну модель, вичерпані ліміти Gemini…)
   зберігається щохвилини й під час зупинки, а після запуску повертається.
3. 🛟 Рятувальна копія — свіжа копія всіх даних раз на добу приходить власнику в чат із ботом (без звуку) і
   закріплюється там. Якщо після оновлення бот прокинувся з порожньою базою (диск не підключено, його стерли
   чи змінили), він сам бере закріплену копію з чату й відновлює все, а потім пише, що відновив.
"""
from __future__ import annotations

import json
import os
import re
import time

SECRET_HINT = re.compile(r"key|token|secret|pass|pw|парол|ключ", re.I)
KEEP_MONTHS = 6


class Journal:
    def __init__(self, folder):
        self.dir = os.path.join(folder, "activity")
        os.makedirs(self.dir, exist_ok=True)

    def _file(self, t=None):
        return os.path.join(self.dir, time.strftime("%Y-%m", time.localtime(t or time.time())) + ".jsonl")

    def add(self, who, kind, what, detail="", secret=False):
        rec = {"t": round(time.time(), 1), "u": who, "k": kind, "w": str(what)[:200],
               "d": "[приховано]" if secret and detail else str(detail or "")[:300]}
        try:
            with open(self._file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def tail(self, n=40, who=None):
        out = []
        files = sorted(f for f in os.listdir(self.dir) if f.endswith(".jsonl"))[::-1]
        for name in files:
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if who is None or r.get("u") == who:
                    out.append(r)
                if len(out) >= n:
                    return out
        return out

    def size(self):
        return sum(os.path.getsize(os.path.join(self.dir, f)) for f in os.listdir(self.dir) if f.endswith(".jsonl"))

    def prune(self):
        files = sorted(f for f in os.listdir(self.dir) if f.endswith(".jsonl"))
        for f in files[:-KEEP_MONTHS]:
            try:
                os.remove(os.path.join(self.dir, f))
            except OSError:
                pass


class RuntimeState:
    """Пам'ять процесу, яку варто пережити перезапуск. getters: {назва: (отримати(), повернути(значення))}."""

    def __init__(self, folder):
        self.path = os.path.join(folder, "runtime_state.json")
        self.parts = {}

    def register(self, name, get, put):
        self.parts[name] = (get, put)

    def save(self):
        data = {"saved": time.time()}
        for name, (get, _put) in self.parts.items():
            try:
                data[name] = get()
            except Exception:  # noqa: BLE001
                continue
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        n = 0
        for name, (_get, put) in self.parts.items():
            if name in data:
                try:
                    put(data[name])
                    n += 1
                except Exception:  # noqa: BLE001
                    continue
        return n


def db_is_empty(con):
    """Порожня (щойно створена) база: немає ні повідомлень, ні налаштувань, ні учасників."""
    try:
        for table in ("messages", "settings", "members"):
            if con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return False
    except Exception:  # noqa: BLE001
        return False
    return True
