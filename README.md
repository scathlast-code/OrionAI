# Ваші боти для Telegram — почніть тут

> **Покрокове налаштування всього** — GitHub, Railway, Oracle, зв'язок ботів і оновлення: [`docs/00-pokrokovo.md`](docs/00-pokrokovo.md).

Обидва боти працюють у Telegram: безкоштовно, без вікна 24 години й без ліміту повідомлень.

## Що всередині

**ШІ-бот** — дев'ять безкоштовних моделей одночасно (Gemini, Groq, Mistral, NVIDIA, Ollama Cloud, OpenRouter, Cloudflare, Cohere, своя Ollama) і скільки завгодно власних. Вибір кнопками або «🔁 Авто», сповіщення про ліміти і про те, коли модель знову доступна. Відкладені й заплановані завдання, продовження розмови на іншій моделі з того самого місця, теки, архів, кошик (автоочищення через 30 днів), закріплене, нотатки й медіатека з сортуванням. Картинки, слайдшоу, музика, Canva. Голосові повідомлення й відповіді голосом, розшифровка пересланих голосових. Доступ для інших людей із вашим підтвердженням. Надсилання завдань і нотаток у Todoist, Google Tasks, Microsoft To Do, Trello, Notion, OneNote, Нагадування й Нотатки iPhone, Evernote і будь-які сервіси, що приймають листи. 📒 Журнал запитів і пам'ять проєкту: якщо модель зупинилася, інша продовжує з того самого місця. Скидання до заводських налаштувань без втрати підключень.

**🧭 Головний бот** — керує всіма вашими ботами (Telegram, WhatsApp, Instagram і будь-якими іншими): стан і сповіщення про падіння, розклад перевірок для кожного бота (частота, години, пауза за таймером), назва, опис, команди, вхідні від ботів, пересилання між ботами вручну й за правилами, захист від зациклень.

**В обох:** 🩺 діагностика (що працює й що ні, цифри, поради, суперечності в налаштуваннях, скільки роботи виконано), 🏭 скидання до заводських без втрати підключень, меню налаштовується як завгодно (групи й підменю, будь-які назви кнопок, порядок, що на виду, а що сховано), у кожному меню «◀️ Назад · 🏠 Додому · Вперед ▶️», резервні копії у файл, канал Telegram, Google Диск, Dropbox, OneDrive — для кожного місця свій розклад, пауза чи зупинка, і меню відновлення з хмари.

## 🔌 Плагіни (нове, Stage 21)

У головному меню — категорія **🔌 Плагіни**: **📰 Ньюзрум GreenPost** (збір новин із пересилань, каналів, RSS, WhatsApp і n8n, рерайт за правилами редакції на безкоштовних моделях, точна довжина, погодження, зведення, публікація в канал) і **🧷 Стікери** (архіви пакетів стікерів і емодзі, частини до 45 МБ, без повторів, PNG, стеження за оновленнями). Окремий Oracle-сервер для стікер-бота більше не потрібен. Усе — `docs/17-plugins.md`.

## Порядок налаштування (≈1 година, один раз)

1. **Сервер** — `docs/03-server.md`: безкоштовний Oracle Cloud, безкоштовна адреса DuckDNS, Docker.
2. **Telegram** — `docs/01-telegram.md`: створити ботів, отримати токени.
3. **Моделі ШІ** — `docs/02-modeli.md`: безкоштовні ключі (почніть із Gemini + Groq + Mistral).
4. Заповнити `.env` (зразок — `.env.example`, кожен рядок підписано) → `docker compose up -d --build`.
5. **Резервні копії** — `docs/04-kopii.md`: увімкніть хоча б одну хмару.
6. **Зв'язок ботів** — `docs/05-zvyazok-botiv.md`: додайте ШІ-бота в головного й підключіть свої майбутні боти.
7. **Таск-менеджери й блокноти** (за бажанням) — `docs/06-servisy.md`: Todoist, Google Tasks, Microsoft To Do, Trello, Notion, OneNote, iPhone, Evernote.
8. **Групи, канали, спільноти** (за бажанням) — `docs/07-spilnoty.md`: правила, дозволи за запитом, заявки на вступ, публікації за розкладом, моніторинг; там же — індикатор роботи й паралельні розмови.
9. **Пріоритети й таблиці ботів** — `docs/08-priorytety.md`: що виконувати першим, пауза замість відмови, таблиці ботів і спільнот з посиланнями.
10. **Railway** — `docs/20-railway.md`: обов'язковий диск (Volume) на `/data`, змінні, вартість, режими роботи (завжди / сон / сон + будильник).
11. **Резервні сервери** — `docs/21-rezervni-servery.md`: якщо один сервер впаде, роботу підхопить інший.
12. **Своя модель на сервері** — `docs/22-ollama.md`: Ollama поруч із ботами на Oracle, без лімітів.

Спершу можна поклацати симуляції в теці `simulation/`: вони показують ті самі меню, що й справжні боти.

## Файли

- `bot.py` — ШІ-бот: моделі, розмови й теки, завдання, нотатки, медіатека, голос, генератори, Canva, доступ для інших людей;
- `manager.py` — 🧭 головний бот: реєстр ботів, моніторинг, вхідні, пересилання між ботами;
- `uilayout.py` — конструктор меню (групи, перейменування, «Назад / Додому / Вперед») для обох ботів;
- `backups.py` — резервні копії (файл, канал Telegram, Google Диск, Dropbox, OneDrive), шифрування, відновлення;
- `platforms.py` — як головний бот говорить із Telegram, WhatsApp та Instagram;
- `services.py` — таск-менеджери й блокноти;
- `hubclient.py` — підключення ваших майбутніх ботів до головного (один файл);
- `help_texts.py`, `media.py`, `canva.py`, `music_server.py` — довідка, генератори, Canva, необов'язковий музичний сервер;
- `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `requirements.txt`, `.env.example` — запуск на сервері;
- `docs/` — інструкції;
- `simulation/` — вебсимуляції обох ботів: відкрийте в браузері й поклацайте, як усе працює.

## Довідка всередині ботів

У ботах є повна довідка, що працює без звернення до ШІ: «📖 Довідка» (у ШІ-боті — окремо про кожну модель, платні тарифи й порівняння безкоштовних).


## Web search: 10 sources

`/web <query>` or ask "пошукай в інтернеті ...". Sources are attempted in order until one returns results: Tavily, Brave, Serper, SerpAPI, Exa, You.com, SearchAPI (only if respective API keys are configured), then DuckDuckGo HTML, Wikipedia and GDELT News. The last two are specialized sources, not full-web engines. DuckDuckGo HTML is unofficial and may be blocked. Source pages (first two results) are read when accessible; redirects, private IPs, non-HTML, and large pages are skipped. Some JavaScript-rendered/paywalled pages cannot be read. This does not provide unrestricted browser automation, authentication, or access to private websites. Search provider terms, fees, and free quotas vary; adding a key may incur costs depending on your account. IMPORTANT: Having an API key does not guarantee free use; a provider may bill an account with enabled paid billing. This bot does not enforce provider-side spending caps. Keep auto-top-up disabled and set provider-side hard limits where available.

Optional Railway variables: `TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, `SERPER_API_KEY`, `SERPAPI_API_KEY`, `EXA_API_KEY`, `YOU_API_KEY`, `SEARCHAPI_API_KEY`.

Search results are untrusted text. Verify sources before publication. Existing storage, backup, and cleanup behavior has not been modified by this search update.


## ⚠️ OpenAI GPT API: доступ і оплата

У меню «Мої моделі» можна додати OpenAI GPT, але **наявність ключа не гарантує роботу**. Новим акаунтам безкоштовні API-кредити не гарантовані; підписка ChatGPT Plus не оплачує API. Помилка `401` зазвичай означає недійсний ключ, `429 insufficient_quota` — відсутність доступної квоти або кредитів. Див. https://platform.openai.com/settings/organization/billing/. Бот показує це попередження під час додавання OpenAI та на сторінці моделі.

Актуальні умови вебпошуку та посилання: `docs/09-websearch-pricing.md`.
\n\nБраузер і PDF: див. [docs/10-browser.md](docs/10-browser.md). Команда `/browse URL`.\n