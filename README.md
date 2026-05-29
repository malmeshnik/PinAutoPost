# PinAutoPost

Pinterest automation backend using Django 5.x, DRF, PostgreSQL, Redis, Celery, and django-unfold for the admin UI.

## Локалізація (I18n)

Проєкт підтримує українську мову. Для управління перекладами використовуйте наступні команди:

### Оновлення файлів перекладів
Якщо ви додали нові рядки для перекладу (використовуючи `_()` або `gettext_lazy`), виконайте:
```bash
python manage.py makemessages -l uk
```
Після цього відредагуйте файл `locale/uk/LC_MESSAGES/django.po`.

### Компіляція перекладів
Щоб зміни в перекладах вступили в силу, їх потрібно скомпілювати:
```bash
python manage.py compilemessages
```

*Примітка: Для роботи цих команд у системі повинен бути встановлений пакет `gettext`.*
