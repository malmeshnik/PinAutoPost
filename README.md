# PinAutoPost

Pinterest automation backend using Django 5.x, DRF, PostgreSQL, Redis, Celery, and django-unfold for the admin UI.

## 🚀 Основні можливості

- **Multi-tenant архітектура** - кожен користувач бачить тільки свої Pinterest акаунти
- **Автоматичний постинг пінів** через Celery background tasks
- **Підтримка різних доменів Pinterest** (www.pinterest.com, ru.pinterest.com, etc.)
- **Синхронний та асинхронний API** для постингу
- **Автоматичний retry** для нестабільних помилок (proxy timeout, API errors)
- **Telegram сповіщення** про помилки та важливі події
- **Детальне логування** всіх операцій
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

## 📚 API Документація

### Додавання Pinterest акаунта

**POST** `/api/v1/accounts/`

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

**Валідація при створенні:**
- Перевіряється доступність проксі
- Автоматично витягується базовий URL з cookies (www.pinterest.com, ru.pinterest.com, etc.)
- Завантажуються дошки для перевірки що акаунт валідний
- Якщо дошки не знайдено - акаунт деактивується з повідомленням про помилку

### Отримання дошок акаунта

**GET** `/api/v1/accounts/{id}/boards/`

**GET** `/api/v1/accounts/{id}/boards/?refresh=true` - примусово оновити дошки

**Відповідь при помилці:**
```json
{
  "error": "no_boards",
  "message": "Не вдалося отримати дошки для акаунту Account Name. Перевірте cookies та проксі."
}
```

### Постинг піна (Webhook API)

#### Асинхронний режим (за замовчуванням)

**POST** `/api/v1/publish/{webhook_token}/`

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

**Відповідь (202 Accepted):**
```json
{
  "status": "accepted",
  "task_id": "123",
  "status_url": "/api/v1/task/123/"
}
```

#### Синхронний режим (чекає на результат)

**POST** `/api/v1/publish/{webhook_token}/?wait=true&timeout=60`

**Відповідь при успіху (200 OK):**
```json
{
  "status": "success",
  "message": "Pin published successfully",
  "task_id": "123"
}
```

**Відповідь при помилці (400/401/502):**
```json
{
  "error": "auth_error",
  "message": "Pinterest cookies expired. Account deactivated.",
  "task_id": "123"
}
```

**Відповідь при timeout (202 Accepted):**
```json
{
  "status": "timeout",
  "message": "Task is still processing after 60s. Check status at /api/v1/task/123/",
  "task_id": "123"
}
```

### Перевірка статусу таски

**GET** `/api/v1/task/{task_id}/`

**Статуси:**
- `pending` - в черзі
- `processing` - виконується
- `success` - успішно
- `failed` - помилка (з деталями в error_json)

**Приклад відповіді:**
```json
{
  "status": "failed",
  "error": "proxy_error",
  "message": "Proxy connection timeout or IP blocked",
  "retry_count": 2
}
```

## 🔄 Автоматичний Retry

Система автоматично повторює спроби для тимчасових помилок:

- **Proxy timeout/connection errors** - 3 спроби з exponential backoff (60s, 120s, 240s)
- **Pinterest API errors** - 3 спроби з exponential backoff
- **Auth errors** - НЕ повторюються, акаунт деактивується відразу

## 📊 Типи помилок

| Error Type | HTTP Status | Retry | Опис |
|------------|-------------|-------|------|
| `auth_error` | 401 | ❌ | Cookies прострочені, акаунт деактивовано |
| `proxy_error` | 502 | ✅ | Проблема з проксі або timeout |
| `api_error` | 400 | ✅ | Помилка Pinterest API |
| `validation_error` | 400 | ❌ | Невалідні дані (board не знайдено) |
| `unknown_error` | 400 | ❌ | Невідома помилка |

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
4. **Синхронний API** - можливість отримати результат постингу відразу через `?wait=true`
5. **Автоматичний retry** - повтор спроб для тимчасових помилок
6. **Telegram група** - сповіщення тепер можна відправляти в закриту групу

### Покращення

- Покращена обробка помилок з стандартизованими типами
- Retry механізм з exponential backoff
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
