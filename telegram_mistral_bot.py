import os
import logging
import requests
import time
import random
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from system_prompt import SYSTEM_PROMPT
from collections import defaultdict, deque
import asyncio
from follow_up_prompts import FOLLOW_UP_PROMPTS, REMINDER_INTERVALS, get_next_valid_time
from datetime import datetime
import json
from dialog_states import DialogState, DialogTracker

load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

# Токен и идентификатор чата второго бота для уведомлений
SECOND_BOT_TOKEN = "8058814107:AAHV5JK_sz8RAhpObvxdokahYLNmMsvdIzQ"
SECOND_BOT_CHAT_ID = "6608507997"
SECOND_BOT_WEBHOOK_URL = f"https://api.telegram.org/bot{SECOND_BOT_TOKEN}/sendMessage"

# Словарь для хранения истории чатов и очередей сообщений
chat_histories = {}
message_queues = defaultdict(deque)
first_messages = set()  # Добавляем множество для отслеживания первых сообщений
last_response_time = {}
MESSAGE_COLLECTION_DELAY = 2  # Секунды ожидания следующего сообщения

# Добавляем в начало файла после существующих словарей
pending_reminders = {}
last_user_message = {}

# Обновляем шаблон сообщения, экранируем специальные символы
SYSTEM_MESSAGE_TEMPLATE = """
🔔 *Новая заявка на пробное занятие*

👤 Имя: `{name}`
📱 Телефон: `{phone}`
📝 Краткое описание: `{summarize}`
❓ Вопросы/потребности: `{quest}`
⏰ Предп��читаемое время: `{selected_time}`
📅 Дата создания: `{created_at}`
"""

# Добавляем после существующих словарей
user_names = {}
dialog_tracker = DialogTracker()

# Добавляем сл��варь для хранения контекста диалога
dialog_context = defaultdict(dict)

# Убираем импорт ReactionTypeEmoji и обновляем словарь реакций
DIALOG_REACTIONS = {
    DialogState.GOT_GOAL: "👍",
    DialogState.GOT_CLASS: "📚", 
    DialogState.GOT_TOPICS: "💡",
    DialogState.GOT_TIME: "✅"
}

# ...existing code...
import random
# ...existing code...
# Добавим словарь для хранения ID сообщений
last_message_ids = {}
# Добавим словарь для хранения отправленных реакций
sent_reactions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_histories[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    first_messages.add(chat_id)  # Отмечаем первое сообщение
    await update.message.reply_text('Доброго дня! 👋 Мене звати Надія, я менеджер центру "Математик Онлайн". Ви залишали заявку на безкоштовну пробну тижневу програму. Рада бути вашим помічником! Щоб підібрати графік та викладача, дозвольте поставити кілька запитань. Яка основна мета занять для вашої дитини? Наприклад, заповнення прогалин у знаннях, підготовка до іспитів чи щось інше? 🎯')

async def process_message(chat_id: int, message: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    try:
        current_state = dialog_tracker.get_state(chat_id)
        if chat_id not in chat_histories:
            chat_histories[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

        chat_histories[chat_id].append({"role": "user", "content": message})

        # Отправляем запрос в Mistral API
        response = requests.post(
            MISTRAL_API_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "mistral-large-latest",
                "messages": chat_histories[chat_id],
                "temperature": 0.6,
                "max_tokens": 1000,
            },
        )

        if response.status_code == 200:
            assistant_message = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"Ответ от Mistral: {assistant_message}")
            
            # Проверяем наличие JSON с временем в ответе
            if '{"selected_time":' in assistant_message:
                # Извлекаем время и добавляем системное сообщение
                time_start = assistant_message.find('{"selected_time":')
                time_end = assistant_message.find('}', time_start) + 1
                time_json = assistant_message[time_start:time_end]
                
                try:
                    time_data = json.loads(time_json)
                    selected_time = time_data.get('selected_time', '')
                    
                    # Формируем системное сообщение
                    system_part = f'【systemTextByAi{{"trigger": "NEWLEAD", "name": %% "{user_names[chat_id]["first_name"]}" %%, "phone": %% "Не указано" %%, "summarize": %% "{dialog_context[chat_id].get("goal", "Математика")}, {dialog_context[chat_id].get("class", "")} клас, {dialog_context[chat_id].get("selected_time", "")}" %%, "quest": %% "{dialog_context[chat_id].get("topics", "Не указано")}" %%, "selected_time": "{selected_time}"}}】'
                    
                    # Удаляем JSON из ответа и добавляем системное сообщение
                    assistant_message = assistant_message.replace(time_json, '') + system_part
                except json.JSONDecodeError:
                    logger.error(f"Ошибка парсинга JSON времени: {time_json}")
            
            # Проверяем и исправляем незакрытые системные сообщения
            if "【systemTextByAi" in assistant_message and "】" not in assistant_message:
                assistant_message += "】"
            
            # Проверяем и форматируем JSON в систем��ом сообщении
            if "【systemTextByAi" in assistant_message:
                start_idx = assistant_message.find("【systemTextByAi")
                end_idx = assistant_message.find("】", start_idx)
                if (start_idx != -1 and end_idx != -1):
                    try:
                        system_part = assistant_message[start_idx:end_idx+1]
                        # Убедимся что JSON правильно сформирован
                        json_part = system_part[len("【systemTextByAi"):].strip()
                        json.loads(json_part)  # Проверка валидности JSON
                    except json.JSONDecodeError:
                        # Если JSON невалиден, исправляем типичные ошибки
                        assistant_message = assistant_message[:start_idx] + assistant_message[start_idx:].replace("\n", "").strip()
                        if not assistant_message.endswith("】"):
                            assistant_message += "】"
            
            chat_histories[chat_id].append({"role": "assistant", "content": assistant_message})
            
            # Обновляем состояние на основе ответа
            if "【systemTextByAi" in assistant_message:
                dialog_tracker.update_state(chat_id, DialogState.COMPLETED)
                logger.info(f"Dialog completed for {chat_id}")
            elif "уточню у викладачів" in assistant_message.lower():
                dialog_tracker.update_state(chat_id, DialogState.GOT_TIME)
                logger.info(f"Got time for {chat_id}, moving to completion")
            elif "у якому класі" in assistant_message.lower():
                dialog_tracker.update_state(chat_id, DialogState.GOT_GOAL)
            elif "які теми складні" in assistant_message.lower():
                dialog_tracker.update_state(chat_id, DialogState.GOT_CLASS)
            elif "коли приблизно зручно" in assistant_message.lower():
                dialog_tracker.update_state(chat_id, DialogState.GOT_TOPICS)
            
            logger.info(f"Dialog state for chat {chat_id}: {dialog_tracker.get_state(chat_id)}")
            
            # Добавляем реакцию на последнее сообщение пользователя
            if chat_id in last_message_ids and current_state in DIALOG_REACTIONS:
                if chat_id not in sent_reactions:
                    try:
                        reaction = random.choice(list(DIALOG_REACTIONS.values()))
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=reaction,
                            reply_to_message_id=last_message_ids[chat_id]
                        )
                        sent_reactions[chat_id] = reaction
                        logger.info(f"Added reaction {reaction} to message {last_message_ids[chat_id]}")
                    except Exception as e:
                        logger.error(f"Failed to add reaction: {e}")
            
            return assistant_message
        else:
            logger.error(f"Ошибка API Mistral: {response.status_code} - {response.text}")
            return "Произошла ошибка. Попробуйте позже."
    except Exception as e:
        logger.error(f"Ошибка в process_message: {e}")
        return "Произошла ошибка. Попробуйте позже."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Сохраняем ID сообщения
    message_id = update.message.message_id
    
    # Проверяем состояние диалога
    current_state = dialog_tracker.get_state(chat_id)
    if current_state == DialogState.COMPLETED or current_state == DialogState.GOT_TIME:
        logger.info(f"Dialog for chat {chat_id} is completed or time received. No further responses from AI.")
        return
    
    # Сохраняем ID последнего сообщения
    last_message_ids[chat_id] = message_id
    
    # Сохраняем имя пользователя и добавляем логирование
    user = update.effective_user
    if user:
        first_name = user.first_name or "Не указано"
        username = user.username or "Не указано"
        user_names[chat_id] = {
            "first_name": first_name,
            "username": username
        }
        logger.info(f"Сохранено имя пользователя: {user_names[chat_id]}")
    
    current_time = time.time()
    message_text = update.message.text
    logger.info(f"Получено сообщение от {chat_id}: {message_text}")
    
    message_queues[chat_id].append(message_text)
    last_user_message[chat_id] = current_time
    
    # Уменьшаем задержку для коротких сообщений
    delay = 1 if len(message_text.split()) <= 2 else MESSAGE_COLLECTION_DELAY
    
    # Проверяем время с последнего ответа
    if chat_id in last_response_time and current_time - last_response_time[chat_id] < delay:
        logger.info(f"Collecting messages for chat {chat_id}, delay: {delay}s")
        return
    
    await asyncio.sleep(delay)
    
    if message_queues[chat_id]:
        combined_message = " ".join(message_queues[chat_id]).strip()
        if not combined_message:
            logger.info(f"Пустое сообщение для {chat_id}, пропуск обработки")
            return
        
        logger.info(f"Обработка сообщения для {chat_id}: {combined_message}")
        message_queues[chat_id].clear()
        
        # Отправляем объединенное сообщение
        response = await process_message(chat_id, combined_message, context)
        
        # Разделяем ответ на пользовательскую и системную части
        user_response = response
        system_message = None
        
        if "【systemTextByAi" in response:
            try:
                # Извлекаем системное сообщение более надежным способом
                start_idx = response.find("【systemTextByAi")
                end_idx = response.find("】", start_idx)
                if (start_idx != -1 and end_idx != -1):
                    user_response = response[:start_idx].strip()
                    system_message = response[start_idx + len("【systemTextByAi"):end_idx].strip()
                    
                    # Добавляем chat_id в системное сообщение если его нет
                    if "chat_id" not in system_message:
                        system_message = system_message.replace(
                            '"NEWLEAD"',
                            f'"NEWLEAD", "chat_id": "{chat_id}"'
                        )
                    logger.info(f"Извлечено системное сообщение: {system_message}")
                    
                    # Сначала отправляем системное сообщение
                    try:
                        await send_system_message(system_message, chat_id, context)
                        logger.info("Системное сообщение успешно отправлено")
                    except Exception as e:
                        logger.error(f"Ошибка при отправке системного сообщения: {e}")
            except Exception as e:
                logger.error(f"Ошибка при обработке системного сообщения: {e}")
        
        # Эффект печатания
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        time.sleep(random.uniform(3, 5))
        
        # Отправляем ответ пользователю
        if user_response:
            await context.bot.send_message(chat_id=chat_id, text=user_response)
        
        # Сохраняем время последнего ответа
        last_response_time[chat_id] = time.time()
        
        # Меняем логику отправки напоминания
        current_state = dialog_tracker.get_state(chat_id)
        if current_state != DialogState.GOT_TIME and current_state != DialogState.COMPLETED:
            logger.info(f"Scheduling reminders for chat {chat_id} in state {current_state}")
            await schedule_reminders(chat_id, context)
        else:
            # Отменяем существующие напоминания если они есть
            if chat_id in pending_reminders:
                logger.info(f"Cancelling reminders for chat {chat_id} - dialog completed or time received")
                for task in pending_reminders[chat_id]:
                    task.cancel()
                pending_reminders[chat_id] = []

def format_system_message(data_str: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, dict]:
    """Форматирует системное сообщение для второго бота"""
    try:
        # Очищаем строку от лишних пробелов
        clean_data = data_str.strip()
        
        # Убираем символы %% и форматируем JSON более аккуратно
        replacements = {
            '%% "': '"',  # Начало переменной
            '" %%': '"',  # Конец переменной
            '"{name}"': f'"{user_names.get(chat_id, {}).get("first_name", "Не указано")}"',
            '"{phone}"': '"Не указано"',  # Или другое значение по умолчанию
            '"{summarize}"': '"Математика"',  # Или другое значение по умолчанию
            '"{quest}"': '"Алгебра"',  # Обновляем на основе контекста
            '"{selected_time}"': '"после 17:00"',  # Обновляем реальное время
            '\\"%selected_time%\\"': '"после 17:00"'  # Добавляем новый формат
        }
        
        # Применяем замены
        for old, new in replacements.items():
            clean_data = clean_data.replace(old, new)
        
        # Парсим JSON
        data = json.loads(clean_data)
        
        # Заполняем данные пользователя
        if chat_id and chat_id in user_names:
            user_info = user_names[chat_id]
            data['name'] = f"{user_info['first_name']} (@{user_info['username']})"
            data['phone'] = "Не указано"  # Или получаем из другого источника
            data['summarize'] = "Математика"  # Или получаем из контекста диалога
            data['quest'] = "Индивидуальные занятия"  # Или получаем из контекста диалога
        
        # Обновляем данные на основе собранного контекста
        if chat_id in dialog_context:
            context_data = dialog_context[chat_id]
            data['quest'] = context_data.get('topics', 'Не указано')  # Используем сохраненную тему
            data['summarize'] = f"Математика, {context_data.get('class', '')} клас"  # Добавляем класс
            if 'goal' in context_data:
                data['summarize'] += f", {context_data['goal']}"  # Добавляем цель занятий
            
            # Если время было сохранено в контексте, используем его
            if 'selected_time' in context_data:
                data['selected_time'] = context_data['selected_time']

        # Форматируем время
        data['created_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data['name'] = data['name'].replace("_", "\\_")  # Экранируем подчеркивания
        
        # Обновляем quest из истории диалога если возможно
        if chat_id in chat_histories:
            for msg in reversed(chat_histories[chat_id]):
                if msg["role"] == "user" and msg["content"] not in ["start", "got_goal", "got_class", "got_topics", "got_time"]:
                    data['quest'] = msg["content"].split("|||")[-1]
                    break
        
        # Формируем более развернутое описание для summarize
        if 'goal' in context:
            data['summarize'] = f"Математика для {context.get('class', '')} класу, мета: {context['goal']}, час: {context['selected_time']}"
        else:
            data['summarize'] = f"Математика для {context.get('class', '')} класу, час: {context['selected_time']}"
        
        # Форматируем сообщение
        formatted_message = SYSTEM_MESSAGE_TEMPLATE.format(**data)
        
        logger.debug(f"Подготовленные данные: {data}")
        return formatted_message, data
        
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON: {e}\нИсходные данные: {data_str}")
        return "Ошибка обработки данных заявки", {}
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при форматировании: {e}\нИсходные данные: {data_str}")
        return "Ошибка обработки данных заявки", {}

async def send_system_message(system_data: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        # Форматируем сообщение
        formatted_message, parsed_data = format_system_message(system_data, chat_id, context)
        
        logger.info(f"Подготовлено форматированное сообщение: {formatted_message}")
        logger.info(f"Parsed data: {parsed_data}")
        
        payload = {
            "chat_id": SECOND_BOT_CHAT_ID,
            "text": formatted_message,
            "parse_mode": "MarkdownV2"  # Используем более новую версию Markdown
        }
        
        response = requests.post(
            SECOND_BOT_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info("Системное сообщение успешно отправлено")
            # Сохраняем данные заявки (опционально)
            save_lead_data(parsed_data)
        else:
            logger.error(f"Ошибка отправки: {response.status_code} - {response.text}")
            
    except requests.exceptions.Timeout:
        logger.error("Таймаут при отправке системного сообщения")
    except Exception as e:
        logger.error(f"Ошибка при отправке системного сообщения: {str(e)}", exc_info=True)

def save_lead_data(data: dict) -> None:
    """Опциональная функция для сохранения данных заявки"""
    try:
        # Здесь можно добавить сохранение в базу данных или файл
        logger.info(f"Сохранены данные заявки: {data}")
    except Exception as e:
        logger.error(f"Ошибка сохранения данных заявки: {e}")

async def schedule_reminders(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Планирует напоминания для чата.
    Args:
        chat_id (int): ID чата
        context (ContextTypes.DEFAULT_TYPE): Контекст бота
    """
    last_user_message[chat_id] = time.time()
    
    # Отменяем существующие напоминания для этого чата
    if (chat_id in pending_reminders):
        for task in pending_reminders[chat_id]:
            task.cancel()
    
    pending_reminders[chat_id] = []
    
    # Планируем новые напоминания
    for timing, interval in REMINDER_INTERVALS.items():
        reminder_context = context  # Создаем локальную копию контекста
        task = asyncio.create_task(
            send_reminder(chat_id, timing, interval, reminder_context)
        )
        pending_reminders[chat_id].append(task)

async def send_reminder(chat_id: int, timing: str, delay: int, context) -> None:
    try:
        await asyncio.sleep(delay)
        
        current_time = datetime.now()
        valid_time = get_next_valid_time(current_time)
        
        if valid_time != current_time:
            delay = (valid_time - current_time).total_seconds()
            await asyncio.sleep(delay)
        
        user_name = user_names.get(chat_id, {}).get("full_name", "Клиент")
        prompt = FOLLOW_UP_PROMPTS[timing].format(user_name=user_name)
        await context.bot.send_message(chat_id=chat_id, text=prompt)
        logger.info(f"Sent {timing} reminder to {chat_id}")
        
    except asyncio.CancelledError:
        logger.info(f"Reminder {timing} for {chat_id} was cancelled")
    except Exception as e:
        logger.error(f"Error sending reminder: {str(e)}")

def main() -> None:
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


