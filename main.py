import json
from curl_cffi import requests

url = "https://www.pinterest.com/resource/PinResource/create/"

# Створюємо чисту сесію з маскуванням під Chrome 120
session = requests.Session(impersonate="chrome120")

# 1. Завантажуємо куки прямо з оригінального файлу, без ручного копіювання рядків
try:
    with open("cookies.json", "r", encoding="utf-8") as f:
        cookies_json = json.load(f)
        # Якщо всередині повний об'єкт розширення, забираємо масив cookies
        if isinstance(cookies_json, dict) and "cookies" in cookies_json:
            cookies_json = cookies_json["cookies"]
except FileNotFoundError:
    print("❌ Помилка: Створи файл cookies.json у папці зі скриптом і закинь туди куки.")
    exit()

# Проставляємо куки в сесію
for cookie in cookies_json:
    # Захист від null-значень, які іноді дають розширення
    cookie_val = cookie.get('value', '')
    session.cookies.set(
        cookie['name'], 
        cookie_val, 
        domain=cookie.get('domain', '.pinterest.com'), 
        path=cookie.get('path', '/')
    )

# 2. Налаштовуємо чисті заголовки
headers = {
    "content-type": "application/x-www-form-urlencoded",
    "x-csrftoken": "9a3bc131081bdc14b13633e9b8fc1a52",
    "x-requested-with": "XMLHttpRequest",
    "referer": "https://www.pinterest.com/pin-builder/?tab=save_from_url",
    "origin": "https://www.pinterest.com",
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"
}

# 3. Дані для публікації
options = {
    "field_set_key": "create_success",
    "skip_pin_create_log": True,
    "board_id": "1130403643910990481",
    "description": "A clean and well-organized coding setup can boost focus",
    "title": "Minimal Coding Setup",
    "image_url": "https://i.pinimg.com/736x/5f/ac/56/5fac56ae183096ffdba8b2088adf7265.jpg",
    "method": "scraped",
    "scrape_metric": {
        "source": "www_url_scrape"
    },
    "user_mention_tags": []
}

payload = {
    "source_url": "/pin-builder/?tab=save_from_url",
    "data": json.dumps({"options": options, "context": {}}),
    "context": "{}"
}

print("Надсилаю швидкий POST-запит з оригінальними куками з файлу...")
response = session.post(url, headers=headers, data=payload)

print(f"Статус код: {response.status_code}")
print("Відповідь сервера:")
try:
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
except ValueError:
    print(response.text)