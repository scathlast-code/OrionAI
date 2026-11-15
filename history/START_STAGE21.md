# OrionAI Stage 21 — що в цьому архіві

База — Stage 20 + нове:
- `plugins/` — 📰 Ньюзрум (списки каналів і чатів, підписки службового акаунта, наші ресурси, пульт «Знайти й написати», теги, історії, фільтри) і 🧷 Стікери
- `sandbox.py`, `sandbox_build.py`, `simulations/sandbox.html` — 🧪 пісочниця (чат, Mini App, файл)
- `improve.py` — 💡 «Покращити»
- `plugins/inbox/` — 📮 Скринька (люди пишуть вам у бот)
- `miniapp.py`, `miniapp/index.html` — 🧩 Mini App на тих самих екранах і даних, що й чат
- `docs/17-plugins.md` (плагіни), `docs/18-interface.md` (меню, Mini App, пісочниця, «Покращити»)
- змінені: `bot.py`, `backups.py`, `security_policy.py`, `Dockerfile`, `requirements.txt` (+telethon), `.env.example`, `README.md`
- `tests/` — 86 тестів, усі проходять

Оновлення на сервері: замінити файли → `docker compose up -d --build`. Обов'язкових нових змінних немає;
для підписок акаунта — TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION (docs/17-plugins.md).
Після змін у коді перезберіть пісочницю: `docker compose run --rm ai python sandbox_build.py`.
