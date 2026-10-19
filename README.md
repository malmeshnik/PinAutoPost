# PinAutoPost

Pinterest automation backend using Django 5.x, DRF, PostgreSQL, Redis, Celery, and django-unfold for the admin UI.

## 🚀 Основні можливості

- **Multi-tenant архітектура** - кожен користувач бачить тільки свої Pinterest акаунти
- **Синхронний постинг пінів** з відповіддю Pinterest у тому самому API-запиті
- **Підтримка різних доменів Pinterest** (www.pinterest.com, ru.pinterest.com, etc.)
- **Фонове оновлення дошок** через Celery
- **Telegram сповіщення** про помилки та важливі події
- **Структуроване логування** без виведення cookies та proxy credentials
- **Webhook API** для інтеграції з зовнішніми системами

## 📋 Вимоги

- Python 3.10+
- PostgreSQL (або SQLite для розробки)
- Redis (для Celery)
- Proxy сервер для Pinterest API запитів

## 🛠️ Встановлення

1. Клонуйте репозиторій:
```bash
git clone <repository-url>
cd PinAutoPost
```

2. Створіть віртуальне середовище:
```bash
python3 -m venv venv
source venv/bin/activate
```

3. Встановіть залежності:
```bash
pip install -r requirements.txt
```

4. Налаштуйте змінні оточення:
```bash
cp .env.example .env
# Відредагуйте .env файл
```

5. Виконайте міграції:
```bash
python manage.py migrate
```

6. Створіть суперюзера:
```bash
python manage.py createsuperuser
```

7. Зберіть статичні файли:
```bash
python manage.py collectstatic
```

## 🔧 Конфігурація

### Змінні оточення (.env)

```env
# Django
DJANGO_SECRET_KEY=your-secret-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

# Telegram (для групи: додайте бота в групу, відправте повідомлення, отримайте chat_id через getUpdates)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_CHAT_ID=your_chat_id_or_group_id  # Для групи починається з мінуса, наприклад: -1001234567890

# Database
POSTGRES_DB=pinterest_db
POSTGRES_USER=pinterest_user
POSTGRES_PASSWORD=pinterest_pass
DB_HOST=localhost
DB_PORT=5432

# Redis/Celery
REDIS_URL=redis://localhost:6379/0
```

### Отримання Telegram Group Chat ID

1. Додайте бота в закриту групу
2. Відправте будь-яке повідомлення в групу
3. Отримайте оновлення: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
4. Знайдіть `chat.id` (починається з мінуса для груп)
5. Додайте цей ID в `.env` файл

## 🚀 Запуск

### Development режим

1. Запустіть Django сервер:
```bash
python manage.py runserver
```

2. Запустіть Celery worker (в окремому терміналі):
```bash
celery -A pinterest_poster worker -l info
```

3. Запустіть Celery beat для планувальника (в окремому терміналі):
```bash
celery -A pinterest_poster beat -l info
```

### Production (Docker)

```bash
docker-compose up -d
```

Compose waits until PostgreSQL is ready, applies pending migrations once, and then collects static files, and starts the web, Celery worker, beat, and health monitor services. If migration fails, dependent services are not started; inspect it with `docker-compose logs migrate`.

## 📚 API Документація

Повний довідник по всіх ендпоінтах (формати запитів і відповідей, коди помилок, що робити в кожному випадку) — у [docs/API.md](docs/API.md). Його можна давати LLM-агенту як інструкцію з роботи з сервісом.

### Авторизація API (токен)

Інтеграції (скрипти, розширення браузера, n8n/Make) авторизуються токеном:

```bash
curl -X POST https://your-host/api/v1/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "..."}'
# → {"token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"}
```

Далі в кожному запиті: `Authorization: Token <token>`. Повторний запит повертає той самий токен. Ендпоінт обмежений 10 запитами на хвилину. Токени також можна видати або відкликати в адмінці (розділ «Токены»). Акаунти, створені з токеном, належать користувачу цього токена.

### Додавання Pinterest акаунта

**POST** `/api/v1/accounts/` (заголовок `Authorization: Token <token>`)

`cookies` можна передати в будь-якому з форматів, вони нормалізуються автоматично:
- JSON-масив з розширення (Cookie-Editor, EditThisCookie) як є;
- об'єкт `{"url": "https://ru.pinterest.com", "cookies": [...]}` — `url` задає домен Pinterest для запитів;
- рядок заголовка `"csrftoken=...; _pinterest_sess=..."`;
- вміст Netscape `cookies.txt`.

Під час нормалізації відкидаються cookies інших сайтів, прострочені cookies та дублікати; зберігаються лише `name`, `value`, `domain`, `path` (і `expires`, якщо є). Обов'язкові `csrftoken` і `_pinterest_sess` — без них запит відхиляється з `400` ще до перевірки проксі.

```json
{
  "name": "My Pinterest Account",
  "cookies": {
    "url": "https://www.pinterest.com",
    "cookies": [
      {"name": "csrftoken", "value": "...", "domain": ".pinterest.com"},
      {"name": "_auth", "value": "1", "domain": ".pinterest.com"}
    ]
  },
  "proxy": "http://user:pass@proxy.example.com:8080"
}
```

`proxy` приймається у форматах `host:port`, `host:port:логін:пароль` (як видають провайдери), `логін:пароль:host:port`, `логін:пароль@host:port` або повним URL (`socks5://логін:пароль@host:port`). Необов'язкове поле `proxy_type` (`http`, `https`, `socks4`, `socks5`, `socks5h`) задає тип; без нього береться схема з URL, а якщо її немає — `http`. Проксі зберігається як повний URL, спецсимволи в логіні й паролі кодуються автоматично.

```json
{"name": "my_pinterest_user", "cookies": [...], "proxy": "203.0.113.10:8000:login:password", "proxy_type": "socks5"}
```

**Валідація при створенні:**
- Перевіряється доступність проксі
- URL з cookies має належати домену `pinterest.com` або його піддомену
- Завантажуються дошки для перевірки що акаунт валідний
- Якщо Pinterest відхилив cookies або дошок немає — акаунт **не зберігається** (`400` з `account.error`), тож запит можна безпечно повторити зі свіжими cookies

Успішна відповідь (`201`) містить `id`, `webhook_url` для публікації та список `boards`.

Cookies та proxy приймаються лише під час створення/оновлення акаунта і не повертаються в API-відповідях.

#### Якщо перевірка проксі не пройшла

API повертає повідомлення, яке вказує на тип проблеми:

- **Відхилено авторизацію / HTTP 407** — перевірте логін і пароль у провайдера. Якщо облікові дані містять `@`, `:`, `/`, `#` або `%`, URL-кодуйте ці символи (наприклад, `@` → `%40`).
- **Не знайдено hostname** — перевірте адресу проксі на помилки та звірте її з налаштуваннями провайдера.
- **Не вдалося підключитися** — перевірте порт, тип проксі (`http://` або `socks5://`) і стан сервера.
- **Timeout** — перевірте доступність сервера проксі та чи дозволяє провайдер з'єднання з IP сервера PinAutoPost.

Використовуйте повну адресу проксі з провайдера, включно зі схемою, портом і потрібними обліковими даними. Не надсилайте адресу з логіном/паролем у підтримку чи логи. Для SOCKS5-відмови повідомлення означає, що сервер проксі не прийняв авторизацію; зміна налаштувань Pinterest цього не виправить.

### Отримання дошок акаунта

**GET** `/api/v1/accounts/{id}/boards/`

**GET** `/api/v1/accounts/{id}/boards/?refresh=true` - примусово оновити дошки

**Відповідь при помилці:**
```json
{
  "error": "no_boards",
  "message": "Pinterest returned no boards for this account."
}
```

### Постинг піна (Webhook API)

#### Синхронна публікація

**POST** `/api/v1/publish/{webhook_token}/`

Рекомендовано передавати унікальний `Idempotency-Key` (до 255 символів) для кожного логічного запиту. Без заголовка запит приймається, але дедуплікації немає: кожен повтор створює нову публікацію. При повторі після мережевої помилки надсилайте той самий ключ і тіло запиту. Поля зі значенням `null`/`""` і пропущені поля вважаються однаковими; ключ з іншим тілом дасть `409 Conflict`.

Повтор з тим самим ключем:
- після успіху або остаточної помилки повертає збережену відповідь без повторної публікації;
- після тимчасової помилки (`proxy_error`, `timeout_error`, `queue_error` — Pinterest пін не отримав) запускає публікацію знову;
- якщо процес, що публікував, впав і задача зависла в `PROCESSING` довше 2 хвилин, повертає `504 publish_unknown`.

```http
Idempotency-Key: campaign-2026-pin-001
```

```json
{
  "title": "Pin Title",
  "description": "Pin Description",
  "link": "https://example.com",
  "image_url": "https://example.com/image.jpg",
  "board_id": "123456789",
  "board_name": "Board Name"  // Альтернатива board_id
}
```

За замовчуванням endpoint синхронно чекає на відповідь Pinterest з загальним deadline 60 секунд. Тимчасові помилки до відправки піна (проксі, 5xx від Pinterest) повторюються автоматично до 3 спроб у межах цього deadline. Перед створенням піна перевіряється доступність проксі, актуальні cookies/авторизація та належність вибраної дошки Pinterest-акаунту; дошки оновлюються з Pinterest лише тоді, коли потрібної немає в локальному кеші. HTTP-статус, тіло та `Content-Type` відповіді Pinterest передаються клієнту без зміни тіла. Якщо запит на створення піна завершується неоднозначним мережевим тайм-аутом, API повертає `504 publish_unknown`: Pinterest міг прийняти пін. Повтор з тим самим ключем поверне ту саму відповідь; перш ніж публікувати знову з новим ключем, перевірте дошку.

#### Асинхронна публікація (сумісність зі старим API)

**POST** `/api/v1/publish/{webhook_token}/?wait=false` ставить задачу в чергу Celery і одразу повертає `202` з `task_id` і `status_url`. Статус: **GET** `/api/v1/publish/status/{status_token}/`. Старі маршрути `/api/v1/task/{id}/` і `/api/v1/publish/status/{id}/` з числовим id ще працюють, але повертають лише `status` і код помилки (id можна перебрати, тому подробиці там не показуються).

Заголовок `title` обмежений 100 символами відповідно до обмеження Pinterest; довші значення відхиляються з HTTP 400 до будь-якого мережевого запиту.

Для перегляду збереженого результату запиту відповідь також містить заголовки `X-PinAutoPost-Task-ID` і `X-PinAutoPost-Status-URL`. Другий містить приватний токен статусу цієї задачі.

## 🔄 Celery

HTTP API за замовчуванням виконує публікацію синхронно; з `?wait=false` її виконує Celery worker з тією самою логікою. Неоднозначні помилки (`publish_unknown`) ніколи не повторюються автоматично, щоб не створити дубль.

Щоденне оновлення дошок та ручна дія адмінки залишаються фоновими Celery-задачами.

## Моніторинг

На головній сторінці `/admin/` суперкористувачу доступні стан PostgreSQL, Redis, Celery worker і beat, а також останні невдалі публікації. Cookies та повні адреси проксі на сторінці не показуються.

Перевірки виконує окремий процес `python manage.py monitor_system_health` (сервіс `monitor` у docker-compose), а не Celery: так він помічає, що впав Redis або всі worker'и. Celery beat щохвилини записує heartbeat у Redis; якщо heartbeat старший за 3 хвилини, beat вважається недоступним. Дашборд не робить живих перевірок: він показує останній знімок стану від монітора і попереджає, якщо монітор не звітував понад 3 хвилини.

Telegram-сповіщення надходять при зміні стану сервісу, після трьох помилок проксі одного акаунта за 15 хвилин, при 20 або більше задачах у черзі, або коли монітор знаходить задачі, що зависли в `PROCESSING` (їх позначено як `publish_unknown`). Після відновлення надсилається окреме повідомлення. Однакова активна проблема повторно сповіщається не частіше ніж раз на 30 хвилин. Стан сповіщень зберігається в БД.

Моніторинг Telegram працює за наявності `TELEGRAM_BOT_TOKEN` і `TELEGRAM_ADMIN_CHAT_ID`. Якщо недоступний увесь Docker-хост, монітор теж зупиниться, тому для цього випадку варто налаштувати зовнішній uptime-monitor.

## 📊 Типи помилок

| Error Type | HTTP Status | Retry | Опис |
|------------|-------------|-------|------|
| `auth_error` | 401 | ❌ | Cookies прострочені, акаунт деактивовано |
| `proxy_error` | 502 | ❌ | Проксі недоступний на preflight або запит дошок не пройшов |
| `api_error` | Pinterest HTTP status | ❌ | Помилка Pinterest API |
| `validation_error` | 400 | ❌ | Невалідні дані (board не знайдено) |
| `unknown_error` | 500 | ❌ | Невідома помилка |
| `internal_error` | 500 | ❌ | Внутрішня непередбачена помилка |
| `publish_unknown` | 504 | ❌ | Тайм-аут під час створення; Pinterest міг прийняти пін |
| `idempotency_key_conflict` | 409 | ❌ | Той самий ключ передано з іншим запитом |

## 🔔 Telegram сповіщення

Система відправляє сповіщення в Telegram групу про:

- ❌ Помилки при створенні піна
- 🚫 Auth помилки (cookies прострочені)
- ⚠️ Не вдалося отримати дошки
- ⏱ Timeout помилки

## 🧪 Тестування

Запуск тестів:
```bash
python manage.py test pinterest_app.tests
```

Тести покривають:
- Витяг базового URL з cookies (www.pinterest.com, ru.pinterest.com)
- Створення сесії та витяг CSRF токена
- Multi-tenant доступ до акаунтів
- Webhook API (async/sync режими)
- Task status endpoint
- Валідація неактивних акаунтів

## 📝 Логування

Всі операції детально логуються з емодзі індикаторами:

- ✓ Успішні операції
- ✗ Помилки
- ⚠ Попередження
- 📌 Створення піна
- 🔄 Оновлення дошок
- 📡 API запити/відповіді
- ⏱ Timeout помилки

**Приклад логів:**
```
✓ Extracted base URL from cookies: ru.pinterest.com
✓ Session configured with 9 cookies, CSRF: ✓, Base URL: ru.pinterest.com
🔄 Refreshing boards for account My Account using base URL ru.pinterest.com...
📡 API response status: 200
✓ Boards refreshed for account My Account. Total boards: 5
```

## 🆕 Що нового (Червень 2026)

### Виправлені проблеми

1. **Підтримка різних доменів Pinterest** - автоматичний витяг базового URL з cookies (`ru.pinterest.com`, `www.pinterest.com`, etc.)
2. **Детальне логування** - повне логування всіх операцій з емодзі індикаторами
3. **Валідація акаунтів** - чіткі повідомлення про помилки при додаванні акаунта
4. **Синхронний API** - API повертає відповідь Pinterest у тому самому запиті
5. **Idempotency-Key** - повторний HTTP-запит не дублює вже виконану публікацію
6. **Telegram група** - сповіщення тепер можна відправляти в закриту групу

### Покращення

- Покращена обробка помилок з стандартизованими типами
- Deadline 60 секунд і preflight перевірка проксі, авторизації та дошки
- Детальні повідомлення про помилки в UI
- Валідація cookies при створенні акаунта
- Автоматична деактивація акаунтів при auth помилках

## 🔒 Безпека

- Використовуйте HTTPS в production
- Тримайте cookies в безпеці
- Використовуйте сильні паролі для proxy
- Регулярно оновлюйте залежності
- Не commitьте .env файл в git

## 📖 Локалізація (I18n)

Проєкт підтримує українську мову. Для управління перекладами:

### Оновлення файлів перекладів
```bash
python manage.py makemessages -l uk
```

### Компіляція перекладів
```bash
python manage.py compilemessages
```

*Примітка: Для роботи цих команд у системі повинен бути встановлений пакет `gettext`.*

## 🤝 Внесок

Pull requests вітаються! Для великих змін спочатку відкрийте issue для обговорення.

## 📄 Ліцензія

[MIT](LICENSE)
