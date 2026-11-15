# 🧭 OrionAI покроково: GitHub, Railway, Oracle

Тут усе по порядку, від оновлення того, що вже є, до роботи на кількох серверах. Усе можна зробити з iPhone.
Деталі окремих тем — в інших файлах теки `docs` (посилання по ходу).

**Що де буде, коли все готово:**

| Місце | Що там | Навіщо |
|---|---|---|
| **GitHub** (приватний репозиторій OrionAI) | код обох ботів | звідси код беруть Railway та Oracle |
| **Oracle Cloud** (безкоштовно) | 🤖 ШІ-бот + 🧭 головний бот | основний сервер, працює цілодобово |
| **Railway** | ті самі два боти | резерв: підхоплює роботу, якщо Oracle впаде |

Порядок такий: **A** оновити GitHub → **B** оновити ШІ-бот на Railway → **C** головний бот на Railway → **D** зв'язати ботів →
**E** сервер Oracle → **F** Oracle основний, Railway резерв. Зупинитися можна після будь-якого етапу: усе, що зроблено, вже працює.

> **Найпростіший шлях для етапу A:** дайте Claude доступ до репозиторію (підключіть GitHub у налаштуваннях Claude і скажіть «залий оновлення в OrionAI»). Тоді всі файли, разом із прихованими, з'являться в репозиторії самі, і етап A можна пропустити.

---

## Перед початком: що підготувати

1. **Архів.** Збережіть `OrionAI-full.zip` у «Файли» → торкніться його: з'явиться тека `OrionAI`.
2. **Токени ботів** від @BotFather (`docs/01-telegram.md`): ШІ-бот уже має свій; для головного створіть ще одного (`/newbot`).
3. **Ваш Telegram ID** — число. Якщо не знаєте: напишіть ШІ-боту, він покаже.
4. **Довгі випадкові рядки** (секрети, 30+ символів). На iPhone: програма **«Паролі»** → «+» → у полі пароля натисніть запропонований надійний пароль → скопіюйте. Знадобляться окремі для:
   `MCP_SECRET`, `MANAGER_SECRET`, `CLUSTER_KEY`, `MANAGER_CLUSTER_KEY`. Збережіть їх у «Паролях» чи «Нотатках»: на кожному сервері вони мають бути **однакові**.

> Справжні ключі нікуди не пишіть, крім змінних сервера (Railway Variables або файл `.env` на Oracle). Ні в GitHub, ні в чат.

---

## A. Оновити код на GitHub

Проєкт тепер об'єднаний: у ньому і резервні сервери з режимами роботи, і плагіни (ньюзрум, стікери, «Мої боти», Mini App), і UTM. Файлів близько 270, у кількох теках, тому з iPhone зручніше не вантажити їх руками.

**Спосіб 1 (раджу): Claude заливає сам.** У Safari відкрийте **https://github.com/apps/claude** → **Install** (або **Configure**) → акаунт → **Only select repositories → OrionAI** → **Install**. Потім напишіть у чаті «залий OrionAI на GitHub». Усі файли, теки й приховані файли з'являться в репозиторії за один раз.

**Спосіб 2: вручну через Safari.** GitHub вантажить файли в ту теку, яку відкрито, тому робиться по одній теці:

1. Розпакуйте `OrionAI.zip` у «Файлах».
2. **Корінь.** Репозиторій → **Add file → Upload files** → виберіть усі файли верхнього рівня теки `OrionAI` (усі `.py`, `Dockerfile`, `Caddyfile`, `docker-compose.yml`, `requirements.txt`, `README.md`, `CHANGES.md`) → **Commit changes**.
3. **Кожна тека окремо.** Відкрийте в Safari адресу `https://github.com/ВАШ_ЛОГІН/OrionAI/upload/main/НАЗВА_ТЕКИ` — GitHub сам створить теку, якщо її ще немає. Виберіть усі файли з цієї теки → **Commit changes**. Потрібні боту теки:
   `plugins`, `plugins/newsroom`, `plugins/stickers`, `plugins/hub`, `plugins/inbox`, `miniapp`, `webapp`, `sticker_webapp`, `simulations`, `deployment`, `docs`.
   Теки `tests`, `history`, `logs`, `reference`, `simulation` для роботи бота не потрібні — їх можна не вантажити.
4. **Приховані файли** (`.dockerignore`, `.gitignore`, `.env.example`) iPhone у «Файлах» не показує. Якщо їх немає в репозиторії — **Add file → Create new file**, назва `.dockerignore`, вміст:
   ```
   .git
   .env
   .env.*
   !.env.example
   __pycache__/
   *.pyc
   *.db
   *.sqlite
   *.sqlite3
   *.log
   *.zip
   tests/
   simulation/
   history/
   logs/
   reference/
   ```
5. Перевірка: у репозиторії є `cluster.py`, `utm_flow.py`, `datadir.py`, тека `plugins/newsroom` і `docs/00-pokrokovo.md`.

---

## B. Оновити ШІ-бот на Railway

Railway сам бере код із GitHub після кожного Commit.

1. Safari → **railway.com** → проєкт **supportive-charisma** → сервіс **OrionAI** → вкладка **Deployments**: має з'явитися нове розгортання → дочекайтесь **Active** (2–4 хв).
   Якщо нічого не з'явилося: **Settings → Source** → перевірте, що підключено репозиторій OrionAI, гілку `main`, і ввімкнено автодеплой. Можна натиснути **Deploy** вручну.
2. **Диск (обов'язково).** На полотні проєкту біля сервісу має бути Volume із шляхом `/data`. Якщо немає: **+ Create → Volume** → сервіс OrionAI → Mount path `/data` (`docs/20-railway.md`, крок 1).
3. **Variables** сервісу OrionAI — перевірте:
   - `TELEGRAM_TOKEN` — токен ШІ-бота;
   - `ALLOWED_USERS` — ваш ID;
   - `MCP_SECRET` — довгий рядок;
   - хоча б один ключ моделі, наприклад `GEMINI_API_KEY` або `GROQ_API_KEY` (як отримати — `docs/02-modeli.md`);
   - **видаліть замінники** (`123456789:AAAA...`, `example.com`, `zminit-...`). Порожня змінна краща за замінник: з нею бот просто вимкне функцію.
4. **Адреса.** **Settings → Networking → Generate Domain** (якщо ще немає). Скопіюйте адресу, наприклад `orionai-production.up.railway.app`: вона знадобиться для етапів D і F.
5. Перевірка в Telegram: ШІ-бот → `/start` → ⚙️ Налаштування → 🩺 Стан бота. Там не має бути попередження про Volume.

---

## C. Головний бот на Railway

Це окремий сервіс у тому ж проєкті, з тим самим кодом.

1. Railway → проєкт → **+ Create → GitHub Repo** → **OrionAI**.
2. Новий сервіс → **Settings**:
   - **Service Name**: `hub`;
   - **Deploy → Custom Start Command**: `python manager.py`;
   - **Networking → Generate Domain** → скопіюйте адресу, наприклад `hub-production.up.railway.app`.
3. **Volume** для нього: **+ Create → Volume** → сервіс `hub` → Mount path `/data`. Свій диск, не спільний із ШІ-ботом.
4. **Variables** сервісу `hub`:
   - `MANAGER_TOKEN` — токен головного бота;
   - `MANAGER_SECRET` — свій довгий рядок (не той, що в MCP_SECRET);
   - `ALLOWED_USERS` — ваш ID.
5. Дочекайтесь **Active** → у Telegram напишіть головному боту `/start`.
6. У ШІ-бота вже є вбудований розділ «🧭 Мої боти». Коли головний бот працює окремо, у сервісі **OrionAI** додайте змінну `ORION_PLUGINS_OFF=hub`, щоб не було двох різних списків ботів.

> **Простіший варіант без окремого сервісу:** пропустіть етап C і користуйтесь вбудованим «🧭 Мої боти» в ШІ-боті. Окремий головний бот потрібен, якщо хочете, щоб він стежив за ШІ-ботом іззовні й надсилав нагадування, навіть коли ШІ-бот впав.

---

## D. Зв'язати ШІ-бот із головним

1. Головний бот → **🤖 Боти → ➕ Додати бота → 🔌 Інший (HTTP)** → назва `ШІ-бот`.
2. Картка бота → **🔌 Підключення** → кнопки **📋 Адреса** і **📋 Ключ** копіюють їх.
3. ШІ-бот → ⚙️ Налаштування → 🩺 Стан бота → **🔗 Зв'язки → ✏️ Головний бот** → надішліть адресу й ключ через пробіл.
   Або Railway → OrionAI → Variables: `HUB_URL` = адреса, `HUB_KEY` = ключ.
4. Перевірка: у головному боті біля ШІ-бота 🟢; в 🔗 Зв'язках ШІ-бота — «зв'язок є». Деталі — `docs/05-zvyazok-botiv.md`.

---

## E. Сервер Oracle: розмістити й запустити обидва боти

Повністю з картинками — `docs/03-server.md`. Коротко:

1. **Акаунт:** cloud.oracle.com → Sign up → регіон у Європі (змінити не можна) → картка лише для перевірки.
2. **Сервер:** Compute → Instances → Create → Ubuntu 24.04 → Ampere **VM.Standard.A1.Flex** (2 OCPU, 12 ГБ) → **Generate a key pair → Save private key** (збережіть у «Файли») → Create → скопіюйте **Public IP**.
3. **Порти 80 і 443:** сторінка сервера → Subnet → Security Lists → Default → **Add Ingress Rules**: `0.0.0.0/0`, TCP, `80,443`.
4. **Адреса:** duckdns.org → ім'я, наприклад `orion` → у current ip вставте Public IP → update. Вийде `orion.duckdns.org`.
5. **Підключитися з iPhone:** застосунок **Termius** → New Host: адреса = Public IP, користувач `ubuntu`, ключ = файл із кроку 2.
6. **Docker і порти на сервері** — команди з `docs/03-server.md`, крок 4 (скопіюйте по черзі). Потім вийдіть (`exit`) і підключіться знову.
7. **Код із GitHub.** Репозиторій приватний, тож потрібен ключ лише на читання:
   GitHub → фото профілю → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token** →
   Repository access: **Only select repositories → OrionAI** → Permissions: **Contents → Read-only** → Generate → скопіюйте.
   На сервері:
   ```bash
   git clone https://github.com/ВАШ_ЛОГІН/OrionAI.git bots
   ```
   Username — ваш логін GitHub, Password — щойно створений токен (символи не видно, це нормально).
8. **Змінні:**
   ```bash
   cd bots
   cp .env.example .env
   nano .env
   ```
   Заповніть: `DOMAIN=orion.duckdns.org`, `PUBLIC_URL=https://orion.duckdns.org/ai`, `MANAGER_PUBLIC_URL=https://orion.duckdns.org/hub`,
   `TELEGRAM_TOKEN`, `MANAGER_TOKEN`, `ALLOWED_USERS`, `MCP_SECRET`, `MANAGER_SECRET`, ключі моделей — **ті самі значення, що на Railway**.
   Замінники в інших рядках зітріть (лишіть після `=` порожньо). Зберегти: Ctrl+O, Enter; вийти: Ctrl+X (у Termius — кнопка Ctrl над клавіатурою).
9. **Не запускайте ще!** Той самий токен не може працювати на Oracle і Railway одночасно. Спершу етап F, потім запуск.

---

## F. Oracle основний, Railway резерв

Сервери домовляються самі: працює один, інший тримає копію бази й підхоплює роботу, якщо перший впаде (`docs/21-rezervni-servery.md`).

1. **На Oracle** допишіть у `.env` (`nano .env`):
   ```
   CLUSTER_NODES=oracle=https://orion.duckdns.org/ai,railway=https://ВАША-АДРЕСА-ШІ-БОТА.up.railway.app
   CLUSTER_NAME=oracle
   CLUSTER_KEY=довгий-рядок-1
   MANAGER_CLUSTER_NODES=oracle=https://orion.duckdns.org/hub,railway=https://ВАША-АДРЕСА-HUB.up.railway.app
   MANAGER_CLUSTER_NAME=oracle
   MANAGER_CLUSTER_KEY=довгий-рядок-2
   ```
2. **На Railway**, сервіс OrionAI → Variables: ті самі `CLUSTER_NODES` і `CLUSTER_KEY`, але `CLUSTER_NAME=railway`.
   Сервіс `hub` → Variables: ті самі `MANAGER_CLUSTER_NODES` і `MANAGER_CLUSTER_KEY`, `MANAGER_CLUSTER_NAME=railway`.
3. **Запуск на Oracle:**
   ```bash
   docker compose up -d --build
   docker compose logs -f        # журнал; вихід — Ctrl+C
   ```
   Перевірка в Safari: `https://orion.duckdns.org` → «Боти працюють.».
4. **Перевірка в ботах:** ⚙️ Налаштування → ⚡ Режим роботи → **🛰 Сервери**. Має бути: oracle 🟢 працює, railway — резерв.
   Через 2–3 хв після першого запуску Oracle забирає свіжу базу з Railway, тож розмови й налаштування не губляться.
5. **Режими:** у кожному боті ⚡ Режим роботи → **🤖 Автоматично за сервером**. На Oracle бот завжди на зв'язку, на Railway резерв спить і майже не витрачає кредит.
   Для сну на Railway: обидва сервіси → **Settings → Deploy → Serverless** → увімкнути.

---

## Як оновлювати надалі

1. **GitHub:** Upload files → Commit (етап A). Що міняти — у `CHANGES.md` нового архіву.
2. **Railway** оновиться сам за 2–4 хв.
3. **Oracle:** Termius →
   ```bash
   cd bots
   git pull
   docker compose up -d --build
   ```
   `git pull` знову спитає логін і токен. Якщо строк токена скінчився — створіть новий (етап E, крок 7).

Дані (розмови, нотатки, налаштування) при оновленні не губляться: на Railway вони на Volume, на Oracle — у томах Docker.

---

## Корисне після запуску

- **Посилання** на Railway, GitHub і Oracle: ⚙️ Налаштування → 🔗 Посилання. Адресу репозиторію задайте там кнопкою «✏️ Репозиторій».
- **Нагадування зайти в Oracle** приходить від головного бота раз на 14 днів (частота — ⚡ Режим роботи).
- **Своя модель на Oracle** (безкоштовно, без лімітів): `docs/22-ollama.md`.
- **Резервні копії в хмару:** `docs/04-kopii.md`. Ключі й входи в копію не потрапляють.
- **Щось не працює:** 🩺 Стан бота (у головному — 🩺 Діагностика) показує, що саме і що робити. На Oracle — журнал `docker compose logs -f ai` (ШІ-бот) або `docker compose logs -f hub` (головний).

## Часті помилки

| Що бачите | Причина | Що зробити |
|---|---|---|
| Бот відповідає через раз, повідомлення губляться | той самий токен працює у двох місцях без `CLUSTER_*` | перевірте етап F: однакові `CLUSTER_NODES` і `CLUSTER_KEY`, різні `CLUSTER_NAME` |
| На Railway після оновлення все «з нуля» | немає Volume на `/data` | етап B, крок 2 (і C, крок 3) |
| `https://orion.duckdns.org` не відкривається | порти 80/443 закриті або в DuckDNS старий IP | етап E, кроки 3–4 і команди iptables із `docs/03-server.md` |
| `git clone` пише «Authentication failed» | замість пароля потрібен токен | етап E, крок 7 |
| Railway пише про ліміт / сервіс зупинено | скінчився кредит $1 | дані не губляться; працюватиме Oracle, Railway оживе з новим кредитом |
