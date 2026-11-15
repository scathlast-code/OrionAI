# Stage 19 — конструктор UI
- Додано `ui_layouts.py`: перевірка структури, SQLite, ревізії, незалежні конфігурації OrionAI/manager.
- Захищені Telegram initData та owner-only API GET/PUT `/api/ui-layout/{bot}` в `sticker_miniapp.py`.
- `sticker_webapp/builder.html`: редактор блоків і кнопок, порядок, видимість, вибір безпечних дій, попередній перегляд, збереження.
- У dashboard OrionAI додано посилання на конструктор.
- `simulations/builder_examples.html`: чотири окремі локальні вебсимуляції.

## Межі реалізації
- Конструктор зберігає структуру, але НЕ змінює автоматично Telegram inline keyboard, материнський Mini App чи маршрути Telegram-ботів. Потрібен окремий renderer/dispatch для кожної поверхні.
- Для роботи серверного API необхідні `TELEGRAM_TOKEN`, `ORION_OWNER_IDS` і постійне сховище SQLite.
- Демоверсії використовують localStorage, не змінюють робочий бот.
