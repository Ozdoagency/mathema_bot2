import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import faiss
from transformers import AutoTokenizer, AutoModel
import torch
import numpy as np
from datetime import datetime
import re
import json


# Настройка логирования
logging.basicConfig(
   level=logging.INFO,
   format="%(asctime)s - %(levelname)s - %(message)s",
   handlers=[
       logging.FileHandler("bot.log", encoding="utf-8"),
       logging.StreamHandler()
   ],
)
logger = logging.getLogger(__name__)


# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SECOND_BOT_CHAT_ID = os.getenv("SECOND_BOT_CHAT_ID")


# Проверка наличия необходимых переменных окружения
if not TOKEN:
   logger.error("TELEGRAM_TOKEN не установлен в .env файле.")
   exit(1)
if not MISTRAL_API_KEY:
   logger.error("MISTRAL_API_KEY не установлен в .env файле.")
   exit(1)
if not SECOND_BOT_CHAT_ID:
   logger.error("SECOND_BOT_CHAT_ID не установлен в .env файле.")
   exit(1)


# Инициализация модели эмбеддингов
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
logger.info(f"Загрузка модели '{MODEL_NAME}' на устройстве {device}.")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModel.from_pretrained(MODEL_NAME).to(device)
logger.info("Модель успешно загружена.")


# Хранилища данных
chat_histories = {}
user_data = {}


# Директория и файлы промптов
PROMPT_DIR = "Prompts"
PROMPT_FILES = {
   "fixed_start": "promt_system.txt",  # Указываем правильный файл с промптом
}


# Функция загрузки промптов из файлов
def load_prompt(file_name):
   logger.info(f"Загрузка файла промпта: {file_name}")
   path = os.path.join(PROMPT_DIR, file_name)
   try:
       with open(path, "r", encoding="utf-8") as file:
           content = file.read()
           logger.info(f"Промпт '{file_name}' успешно загружен.")
           return content
   except FileNotFoundError:
       logger.error(f"Файл промпта не найден: {path}")
       return ""


# Функция создания эмбеддингов для текста
def embed_texts(texts):
   inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt").to(device)
   with torch.no_grad():
       outputs = model(**inputs)
   return outputs.last_hidden_state[:, 0, :].cpu().numpy()


# Функция подготовки FAISS индекса на основе фиксированного промпта
def prepare_faiss_index():
   fixed_prompt = load_prompt(PROMPT_FILES["fixed_start"])
   if not fixed_prompt:
       logger.error("Фиксированный промпт не загружен. Проверьте файл.")
       return None, ""
   embeddings = embed_texts([fixed_prompt])
   index = faiss.IndexFlatL2(embeddings.shape[1])
   index.add(embeddings)
   logger.info("FAISS индекс успешно создан и добавлен на основе фиксированного промпта.")
   return index, fixed_prompt


faiss_index, fixed_prompt = prepare_faiss_index()


# Функция отправки уведомления во второй бот
async def send_notification_to_second_bot(data):
   message = (
       f"🔔 Новая заявка на пробное занятие\n\n"
       f"👤 Имя: {data.get('name', 'Не указано')}\n"
       f"📱 Телефон: {data.get('phone', 'Не указано')}\n"
       f"🎯 Цель: {data.get('goal', 'Не указано')}\n"
       f"🏫 Класс: {data.get('class', 'Не указано')}\n"
       f"💡 Сложные темы: {data.get('topics', 'Не указано')}\n"
       f"📝 Итог: {data.get('summarize', 'Не указано')}\n"
       f"⏰ Предпочитаемое время: {data.get('selected_time', 'Не указано')}\n"
       f"📅 Дата создания: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
   )
   url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
   payload = {"chat_id": SECOND_BOT_CHAT_ID, "text": message}
   try:
       response = requests.post(url, data=payload)
       if response.status_code != 200:
           logger.error(f"Не удалось отправить уведомление: {response.text}")
       else:
           logger.info("Уведомление успешно отправлено во второй бот.")
   except Exception as e:
       logger.error(f"Ошибка при отправке уведомления: {e}")


# Функция для парсинга структурированных данных из ответа AI
async def parse_system_text(ai_message, chat_id):
   """
   Извлекает JSON данные из специального тега в сообщении AI и обновляет user_data.
   """
   pattern = r'【systemTextByAi\{(.*?)\}】'
   match = re.search(pattern, ai_message, re.DOTALL)
   if match:
       json_str = match.group(1)
       # Очистка строки от нестандартных символов, если необходимо
       json_str = json_str.replace("%%", "").replace(" ", "")
       try:
           data = json.loads(json_str)
           user_data[chat_id].update(data)
           logger.info(f"Данные пользователя для chat_id {chat_id} обновлены: {data}")
       except json.JSONDecodeError as e:
           logger.error(f"Ошибка декодирования JSON: {e} в строке: {json_str}")
   else:
       logger.debug("Тег systemTextByAi не найден в сообщении AI.")


# Функция получения ответа от AI
async def get_ai_response(chat_id, message):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
        user_data[chat_id] = {
            "goal": "",
            "class": "",
            "topics": "",
            "selected_time": "",
            "name": "",
            "phone": "",
            "summarize": "",
            "used_dynamic": False
        }

    # Добавляем текущее сообщение в историю чата
    chat_histories[chat_id].append({"role": "user", "content": message})

    # Начинаем с фиксированного промпта
    combined_prompt = fixed_prompt

    # Динамический промпт больше не используется

    # Проверяем необходимость добавления динамического промпта
    if not user_data[chat_id].get("used_dynamic", False) and faiss_index is not None:
        query_embedding = embed_texts([message])
        D, I = faiss_index.search(query_embedding, 1)  # Закрываем скобку и добавляем параметр k

    # Формируем историю сообщений для запроса к AI
    messages = [{"role": "system", "content": combined_prompt}] + chat_histories[chat_id]

    # Пример запроса к AI (замените на реальный вызов API)
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "mistral-large-2411",
        "messages": messages,
        "max_tokens": 1000,
        "temperature": 0.7,
        "top_p": 0.7,
        "frequency_penalty": 0,
        "presence_penalty": 1
    }
    logger.info(f"Отправка запроса к AI: {payload}")
    response = requests.post(MISTRAL_API_URL, headers=headers, json=payload)
    logger.info(f"Ответ от AI: {response.status_code} - {response.text}")
    if response.status_code == 200:
        ai_response = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # Добавляем ответ AI в историю чата
        chat_histories[chat_id].append({"role": "assistant", "content": ai_response})
    else:
        ai_response = "Извините, произошла ошибка при получении ответа от AI."

    logger.info(f"Ответ AI для chat_id {chat_id}: {ai_response}")
    return ai_response

# Функция обработки команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Доброго дня! 👋 Мене звати Надія, я менеджер центру "Математик Онлайн". Ви залишали заявку на безкоштовну пробну тижневу програму. Рада бути вашим помічником! Щоб підібрати графік та викладача, дозвольте поставити кілька запитань. Яка основна мета занять для вашої дитини? Наприклад, заповнення прогалин у знаннях, підготовка до іспитів чи щось інше? 🎯')

# Функция обработки текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    message = update.message.text
    response = await get_ai_response(chat_id, message)
    if response:  # Проверка на пустой ответ
        await update.message.reply_text(response)
    else:
        logger.error("Ответ AI пустой. Сообщение не отправлено.")

# Основная функция запуска бота
def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен.")
    application.run_polling()

if __name__ == "__main__":
    main()
