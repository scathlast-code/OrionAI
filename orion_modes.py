"""Conservative policy gate for OrionAI. No claim of air-gapped operation."""
import sqlite3
import os
from pathlib import Path

MODES = {"normal", "incognito", "offline", "online_required", "orion_only"}
DESCRIPTIONS = {
    "normal": "Звичайний режим: доступні дозволені підключені моделі.",
    "incognito": "Недоступно до повного аудиту зберігання та журналювання.",
    "offline": "Тільки локальна модель; зовнішні API заблоковано політикою.",
    "online_required": "Потрібна перевірка джерел у мережі перед відповіддю.",
    "orion_only": "Тільки внутрішній код і локальна модель; сторонні сервіси заборонено.",
}

def _connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS orion_modes (user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL)")
    return c

def get_mode(user_id, path):
    with _connect(path) as c:
        row=c.execute("SELECT mode FROM orion_modes WHERE user_id=?", (int(user_id),)).fetchone()
    return row[0] if row else "normal"

def set_mode(user_id, mode, path):
    if mode not in MODES: raise ValueError("Невідомий режим")
    if mode == "incognito": raise ValueError("Інкогніто поки заблоковано: немає гарантії нульового зберігання")
    with _connect(path) as c:
        c.execute("INSERT INTO orion_modes VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode", (int(user_id),mode))

def preflight(mode, local_model_ready=False, web_verified=False):
    """Fail closed before network/AI calls. Web verification must be attested by caller."""
    if mode == "incognito": return "Інкогніто недоступне до повного аудиту записів, логів і провайдерів."
    if mode in ("offline", "orion_only") and not local_model_ready:
        return "Немає налаштованої локальної моделі. Зовнішній ШІ не викликатиметься."
    if mode == "online_required" and not web_verified:
        return "Потрібна підтверджена перевірка через інтернет. Відповідь без перевірки заборонена."
    return None
