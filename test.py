import requests

url = "http://127.0.0.1:8008/api/v1/publish/47ff8013-c933-464b-a196-c5ee769b3ea8/"

# Явно кажемо серверу, що ми хочемо отримати НАЗАД саме JSON
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

data = {
    "image_url": "https://i.pinimg.com/1200x/b1/8f/c5/b18fc557d0364e43acbdc9f32d9fd70a.jpg",
    "board_name": "Amazon Desk Setup Ideas"
}

# Передаємо headers у запит
response = requests.post(url, json=data, headers=headers)

print(f"Статус код: {response.status_code}")
print(response.text)