import requests

# Данные для отправки
bot_token = "8058814107:AAHV5JK_sz8RAhpObvxdokahYLNmMsvdIzQ"
chat_id = "6608507997"
message = "🔔 Тестовое сообщение через консоль"

# URL для отправки сообщения
url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

# Параметры запроса
params = {
    "chat_id": chat_id,
    "text": message
}

# Отправка запроса
response = requests.post(url, params=params)

# Проверка результата
if response.status_code == 200:
    print("Сообщение успешно отправлено!")
else:
    print(f"Ошибка отправки: {response.status_code}")
    print(response.text)