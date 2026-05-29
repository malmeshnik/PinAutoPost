import requests

url = "http://127.0.0.1:8008/api/v1/publish/47ff8013-c933-464b-a196-c5ee769b3ea8/"

# Явно кажемо серверу, що ми хочемо отримати НАЗАД саме JSON
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

data = {
    "title": "Amazon Desk Setup Ideas",
    "description": "Check out these amazing Amazon desk setup ideas to create a productive and stylish workspace.",
    "image_url": "https://i.pinimg.com/736x/24/1d/94/241d9435d90279beadc1d47bafae85c7.jpg",
    "board_name": "Amazon Desk Setup Ideas"
}

# Передаємо headers у запит
response = requests.post(url, json=data, headers=headers)

print(f"Статус код: {response.status_code}")
print(response.text)