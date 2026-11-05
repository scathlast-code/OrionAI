# Безкоштовний сервер для ботів

Ботам потрібен комп'ютер, який працює цілодобово, і адреса з HTTPS. Адреса потрібна для вебхуків WhatsApp та Instagram, входу в Google Диск / Dropbox / OneDrive / Canva і «скриньки» головного бота. Найкращий безкоштовний варіант зараз — **Oracle Cloud Always Free**: віртуальний сервер, який не треба оплачувати. На ньому вміщаються всі боти разом.

> Railway та подібні сервіси тепер дають лише кілька доларів кредиту: цього не вистачає, щоб два боти працювали цілодобово.

## Що отримаєте безкоштовно

- **Сервер Ampere A1** (ARM): разом до 2 процесорів і 12 ГБ пам'яті. Ботам із запасом вистачає 1 процесора й 6 ГБ.
- Або, якщо A1 у вашому регіоні зайнятий, **VM.Standard.E2.1.Micro**: 1 ГБ пам'яті, вистачить для одного-двох ботів.
- 200 ГБ диска, постійна публічна IP-адреса.

## Крок 1. Акаунт Oracle Cloud

1. **cloud.oracle.com** → **Sign up**.
2. **Home region** обирайте уважно, змінити його не можна. Підійде Frankfurt, Amsterdam, Stockholm або інший у Європі.
3. Картка потрібна лише для перевірки особи. Може з'явитися тимчасове блокування ~1 $, яке потім повертається.

**Важливо: Oracle може забрати «простий» безкоштовний сервер.** Якщо тиждень сервер майже не навантажений (а боти навантажують мало), Oracle може його зупинити. Щоб цього не сталося:

- **Upgrade to Pay As You Go**: Billing → Upgrade. Поки ви в межах Always Free, оплата не нараховується, а сервер більше не вважається «покинутим».
- Одразу після переходу: Billing → **Budgets → Create Budget** на 1 $ зі сповіщенням на пошту. Якщо раптом щось почне коштувати гроші, ви дізнаєтесь першим.
- І завжди тримайте резервні копії в хмарі (`04-kopii.md`): тоді перенесення на новий сервер займе 10 хвилин.

## Крок 2. Сервер

1. Menu → **Compute → Instances → Create instance**.
2. **Image:** Canonical **Ubuntu 24.04**.
3. **Shape → Ampere → VM.Standard.A1.Flex**: 1–2 OCPU, 6–12 ГБ пам'яті.
   Якщо пише «Out of capacity», спробуйте пізніше або оберіть **VM.Standard.E2.1.Micro**.
4. **Add SSH keys → Generate a key pair for me → Save private key**. Збережіть файл: це ключ від сервера.
5. **Create**. За хвилину буде **Public IP address**: скопіюйте його.

### Відкрийте порти 80 і 443

1. На сторінці сервера: **Subnet** → **Security Lists** → **Default Security List** → **Add Ingress Rules**:
   Source CIDR `0.0.0.0/0`, IP Protocol TCP, Destination Port `80,443`.
2. Решта — на самому сервері (крок 4).

## Крок 3. Безкоштовна адреса (домен)

1. **duckdns.org** → увійдіть через Google або GitHub.
2. Придумайте ім'я, наприклад `mybots` → **add domain**. Вийде `mybots.duckdns.org`.
3. У полі **current ip** вставте Public IP сервера → **update ip**.
4. У `.env`: `DOMAIN=mybots.duckdns.org`, `PUBLIC_URL=https://mybots.duckdns.org/ai`, `MANAGER_PUBLIC_URL=https://mybots.duckdns.org/hub`.

Сертифікат HTTPS сервер отримає сам (Caddy + Let's Encrypt, безкоштовно).

## Крок 4. Підключіться до сервера і встановіть Docker

**З iPhone або Android:** застосунок **Termius** (безкоштовний) → New Host → адреса = Public IP, користувач `ubuntu`, ключ — файл із кроку 2.
**З комп'ютера:** `ssh -i шлях/до/ключа ubuntu@PUBLIC_IP`.

Далі на сервері, по черзі:

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# дозволити вхідні з'єднання на 80 і 443 (образи Oracle блокують їх за замовчуванням)
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
# swap — запас пам'яті (особливо для E2.1.Micro з 1 ГБ)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
exit
```

Підключіться знову (щоб застосувалися права Docker).

## Крок 5. Завантажте ботів і запустіть

**Як закинути архів на сервер:**
- Termius → вкладка **SFTP** → перетягніть zip у `/home/ubuntu`;
- або завантажте файли в приватний репозиторій GitHub і на сервері виконайте `git clone https://github.com/ВИ/РЕПОЗИТОРІЙ.git bots`.

Потім:

```bash
sudo apt install -y unzip
unzip ai-bots-telegram.zip -d bots && cd bots      # або whatsapp / instagram
cp .env.example .env
nano .env            # заповніть; збереження: Ctrl+O, Enter, вихід: Ctrl+X
docker compose up -d --build
docker compose logs -f        # журнал усіх ботів; вихід — Ctrl+C
```

Перевірка: відкрийте в браузері `https://mybots.duckdns.org` — має бути «Боти працюють.».

## Щоденне обслуговування

| Що | Команда (у теці `bots`) |
|---|---|
| Змінили `.env` | `docker compose up -d` |
| Оновили код (нові файли) | `docker compose up -d --build` |
| Подивитися журнал ШІ-бота / головного | `docker compose logs -f ai` / `docker compose logs -f hub` |
| Перезапустити все | `docker compose restart` |
| Зупинити все | `docker compose down` (дані лишаються) |

Оновлювати код можна в будь-який момент і скільки завгодно разів. Бот недоступний 10–60 секунд, а повідомлення, що прийшли за цей час, Telegram доставить після запуску. Розмови, нотатки, завдання й налаштування зберігаються в окремих томах Docker (`ai-data`, `hub-data`) і при оновленні не губляться.

Головне правило: **не запускайте той самий бот (той самий токен) у двох місцях одночасно**, наприклад на сервері й на комп'ютері. Вони конфліктуватимуть, і частина повідомлень загубиться.

## Інші безкоштовні варіанти

- **Свій комп'ютер або Raspberry Pi вдома**, що увімкнений цілодобово, + безкоштовний **Cloudflare Tunnel** замість Caddy і DuckDNS (не треба відкривати порти на роутері).
- **Google Cloud e2-micro** (Always Free, лише регіони США): 1 ГБ пам'яті — вистачить для одного-двох ботів.
